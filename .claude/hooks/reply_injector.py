#!/usr/bin/env python3
"""
Reply Injector - Inject a Telegram reply into an amux-hosted session.

Spawned **detached** by ``notification_hook.py`` when an amux-hosted session
goes idle and its force-reply notification is sent. This process long-polls the
relay for the answer to that one notification message; on a free-text reply it
clears the session's prompt (Ctrl-U/Ctrl-K line-kills) and types the reply via
``amux send``, which auto-submits it as the next user turn.

Design (see tasks/09_reply_from_telegram.md):
- One injector per idle notification; injects exactly once, then exits.
- Non-blocking from the hook's perspective (the hook spawns and returns).
- Fail-open: any error logs and exits 0 — a broken reply path must never disrupt
  the session.

Usage (spawned automatically; not wired as a hook event):
    reply_injector.py --message-id <relay_message_id> --amux <session_name>
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Make sibling hook modules importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import telegram_permission_router as telegram_router
from telegram_permission_router import load_telegram_config, wait_for_relay_answer

# Wait up to the relay message TTL — the same budget a permission request gets.
# On answer we inject and exit; on expiry/cancel/timeout we exit without acting.
INJECT_TTL = 43200
LONG_POLL_CHUNK = 25

# Line-kills used to clear a half-typed local draft before injecting the reply:
# Ctrl-U kills from the cursor UP to the input start (climbing across newlines),
# Ctrl-K kills from the cursor DOWN to the end. Sent together (one send-keys call)
# they clear a multiline draft in both directions from wherever the cursor sits,
# with no char limit and — crucially — no arrow keys (Up/Down recall input history
# and would make the draft vanish). ~2 keystrokes clear one line, so N of each
# clears roughly N/2 lines per direction; 10 each handles ~5 lines up + ~5 down,
# far beyond any realistic idle half-typed prompt.
CLEAR_LINE_KILLS = 10

DEBUG_LOG = Path.home() / ".claude" / "reply_injector_debug.log"


def debug_log(message: str) -> None:
    """Log if CLAUDE_HOOK_DEBUG=1; the injector is detached so this is the only
    visibility into what it did."""
    if os.environ.get("CLAUDE_HOOK_DEBUG", "0") == "1":
        try:
            with open(DEBUG_LOG, "a") as f:
                f.write(f"[{datetime.now().isoformat()}] {message}\n")
        except Exception:
            pass


def sanitize_reply(text: str) -> str:
    """Flatten a reply for ``amux send``.

    ``amux send <name> <text>`` runs ``tmux send-keys "<text>" Enter`` — a
    newline mid-text would submit early, so we collapse all line breaks into
    spaces for v1 (multi-line replies become a single line).
    """
    return " ".join(text.splitlines()).strip()


def amux_send(amux_name: str, *args: str) -> bool:
    """Run ``amux send <name> <args...>``. Returns True on success."""
    try:
        result = subprocess.run(
            ["amux", "send", amux_name, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        debug_log("amux not found on PATH; cannot inject")
        return False
    except Exception as e:  # noqa: BLE001
        debug_log(f"amux send failed: {e}")
        return False
    if result.returncode != 0:
        debug_log(f"amux send rc={result.returncode}: {result.stderr.strip()}")
        return False
    return True


def inject_reply(amux_name: str, text: str) -> None:
    """Clear any half-typed local draft, then type + submit the reply.

    Clearing uses ``Ctrl-U`` (kill to input start, climbing up across newlines) +
    ``Ctrl-K`` (kill to input end, climbing down) — deliberately NOT Escape or
    arrow keys. ``Escape Escape`` is Claude Code's **Rewind** shortcut: on an
    empty prompt it opens the rewind menu, so the reply types *into that menu* and
    the trailing Enter acts on Rewind instead of submitting — the reply silently
    vanishes; a lone ``Esc`` also aborts a running turn. ``End`` + Backspace only
    deletes backward, so lines *below* the cursor survive, and reaching them would
    need ``Down``, which recalls input history and makes the draft vanish. The
    Ctrl-U/Ctrl-K pair clears in *both* directions from wherever the cursor sits,
    with no char limit and no history risk. On an empty idle box these are
    harmless no-ops, and neither opens Rewind. (All verified against Claude Code
    v2.1.169.) Sent as one ``send-keys`` call — a single fast round-trip.

    Known v1 limitation: ~2 keystrokes clear one line, so a draft taller than
    ~``CLEAR_LINE_KILLS``/2 lines above or below the cursor is only partly cleared.
    """
    kill_keys = ["C-u"] * CLEAR_LINE_KILLS + ["C-k"] * CLEAR_LINE_KILLS
    amux_send(amux_name, "--keys", *kill_keys)
    if amux_send(amux_name, text):
        debug_log(f"Injected into amux:{amux_name}: {text[:80]!r}")
    else:
        debug_log(f"Injection into amux:{amux_name} failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject a Telegram reply via amux send")
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--amux", required=True, help="amux session name")
    args = parser.parse_args()

    try:
        load_telegram_config()
        if not telegram_router.TELEGRAM_ENABLED:
            debug_log("Relay disabled; injector exiting")
            return

        answer = wait_for_relay_answer(
            args.message_id, timeout=INJECT_TTL, long_poll_chunk=LONG_POLL_CHUNK
        )
        if answer is None:
            debug_log(f"No answer within TTL for message {args.message_id}")
            return
        if "_state" in answer:
            # expired / cancelled — nothing to inject.
            debug_log(f"Message {args.message_id} terminal w/o answer: {answer['_state']}")
            return

        # Free-text reply arrives as {"text": ..., "via": ...}; tolerate a button
        # label as a fallback shape even though idle notifications have no buttons.
        raw = answer.get("text") or answer.get("label") or ""
        text = sanitize_reply(raw)
        if not text:
            debug_log(f"Empty reply for message {args.message_id}; skipping")
            return

        inject_reply(args.amux, text)

    except Exception as e:  # noqa: BLE001 — fail open, never disrupt the session.
        debug_log(f"ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
