#!/usr/bin/env python3
"""Producer hook for tracked ``amux-spawn`` sessions (epic 10, task 10-02).

This is the state-machine *producer*: it keeps each tracked session's handle
(``~/.amux/spawn/<name>.json``) current from Claude Code lifecycle events, so the
read side (10-03) can report ``running | idle | stuck | terminated``. Producer is
``Stop``-based; the activity clock is the transcript mtime (no per-tool
heartbeat). It writes only the fields defined in architecture s6.0.

One executable handles all four events, dispatched by ``--event``:

- ``Stop`` (authoritative): ``last_message <- last_assistant_message``;
  ``background_tasks <- payload``; ``state = idle`` iff ``background_tasks == []``
  else ``running``; clear ``permission_pending``; capture the real
  ``transcript_path`` from the payload; snapshot ``mtime_at_stop`` = the transcript
  file's current mtime at processing time; bump ``updated_at``.
- ``SubagentStop``: freshness only — refresh ``background_tasks`` + ``mtime_at_stop``
  (and ``transcript_path`` if the payload carries one) but NEVER set ``state: idle``
  (a subagent finishing is not the main turn ending; leave ``idle`` to ``Stop``).
- ``Notification`` (matcher ``permission_prompt``): set ``permission_pending=true``
  (reason-context for 10-03). The next ``Stop`` clears it. ``idle_prompt`` is
  informational only and is ignored here — the existing notification_hook owns the
  Telegram idle notification.
- ``SessionEnd`` (optional): mark ``state=terminated`` + ``reason``, preserving
  ``last_state`` (the last-known running/idle).

Every event is **handle-gated**: it no-ops fast for sessions without a handle
(plain/human sessions, other repos' sessions). Resolution uses
``amux_spawn_lib.resolve_amux_session`` (the current pane's ``amux-<name>`` tmux
session name) and then checks the handle file exists.

Fail-OPEN: any error exits 0 so the session is never disrupted; but a handle is
never silently corrupted (writes are atomic tmp+rename via the shared lib, and we
read-modify-write the existing handle so unrelated fields are preserved).

Registered user-global by ``install-claude-config.sh`` so it fires in any
workspace (D-Hooks / spike Q7).
"""

import json
import os
import sys
from pathlib import Path

# Make the shared lib importable both from the repo checkout and from the flat
# install layout (~/.claude/hooks/), mirroring posttool_hook.py.
sys.path.insert(0, str(Path(__file__).parent))

import amux_spawn_lib as lib

CLAUDE_DIR = Path.home() / ".claude"

# Events this producer understands. Anything else is ignored (fail-open no-op).
KNOWN_EVENTS = {"Stop", "SubagentStop", "Notification", "SessionEnd"}


def debug_log(message: str) -> None:
    """Log a debug line iff CLAUDE_HOOK_DEBUG=1 (mirrors the other hooks)."""
    if os.environ.get("CLAUDE_HOOK_DEBUG", "0") != "1":
        return
    try:
        with open(CLAUDE_DIR / "spawn_producer_debug.log", "a") as f:
            f.write(f"[DEBUG] {message}\n")
    except Exception:
        pass


def _resolve_tracked_handle() -> tuple[str, dict] | None:
    """Resolve this session's amux name + existing handle, or None.

    Handle-gate: returns None (so the caller no-ops) for any session that is not
    a tracked amux session — plain/human amux sessions (no handle), bare
    ``claude`` in tmux, non-tmux sessions, and other repos' sessions (their handle
    lives under a different name). The current pane's ``amux-<name>`` is the source
    of truth, exactly like notification_hook.resolve_amux_session.
    """
    amux_name = lib.resolve_amux_session()
    if not amux_name:
        debug_log("Not an amux session; no-op")
        return None
    handle = lib.read_handle(amux_name)
    if handle is None:
        debug_log(f"No handle for amux:{amux_name}; plain/untracked session, no-op")
        return None
    return amux_name, handle


def _payload_background_tasks(payload: dict) -> list:
    """Extract ``background_tasks`` from a Stop/SubagentStop payload (list or [])."""
    tasks = payload.get("background_tasks")
    return tasks if isinstance(tasks, list) else []


def _payload_transcript_path(payload: dict) -> str | None:
    """The real transcript path from the payload, if present (preferred over computed)."""
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        return tp
    return None


def _current_mtime(transcript_path: str | None) -> float | None:
    """Transcript file's current mtime (epoch float) at processing time, or None."""
    return lib.transcript_mtime(transcript_path)


def handle_stop(name: str, handle: dict, payload: dict) -> None:
    """Authoritative producer step for a ``Stop`` (turn-done).

    Sets last_message, background_tasks, state (idle iff bg empty else running),
    clears permission_pending, captures the real transcript_path, snapshots
    mtime_at_stop, and bumps updated_at. Read-modify-write so unrelated s6.0
    fields (name/session_id/run_id/dir/stuck_after_s/created_at) are preserved.
    """
    background_tasks = _payload_background_tasks(payload)
    # Prefer the real transcript path from the payload; fall back to whatever the
    # handle already has (10-01 wrote the deterministic best-guess at spawn).
    transcript_path = _payload_transcript_path(payload) or handle.get("transcript_path")

    last_message = payload.get("last_assistant_message")
    if isinstance(last_message, str):
        handle["last_message"] = last_message
    elif last_message is not None:
        # Defensive: payload should carry a string, but never crash on a surprise.
        handle["last_message"] = str(last_message)

    handle["background_tasks"] = background_tasks
    handle["state"] = "idle" if not background_tasks else "running"
    handle["permission_pending"] = False
    if transcript_path:
        handle["transcript_path"] = transcript_path
    handle["mtime_at_stop"] = _current_mtime(transcript_path)
    handle["updated_at"] = lib.iso_now()

    lib.write_handle(name, handle)
    debug_log(
        f"Stop: amux:{name} state={handle['state']} "
        f"bg={len(background_tasks)} mtime_at_stop={handle['mtime_at_stop']}"
    )


def handle_subagent_stop(name: str, handle: dict, payload: dict) -> None:
    """Freshness-only producer step for a ``SubagentStop``.

    Refresh background_tasks + mtime_at_stop (and transcript_path if carried) so a
    long fan-out's handle stays live, but NEVER set ``state: idle`` — a subagent
    finishing is not the main turn ending; ``idle`` is owned by ``Stop``.
    """
    background_tasks = _payload_background_tasks(payload)
    transcript_path = _payload_transcript_path(payload) or handle.get("transcript_path")

    handle["background_tasks"] = background_tasks
    if transcript_path:
        handle["transcript_path"] = transcript_path
    handle["mtime_at_stop"] = _current_mtime(transcript_path)
    handle["updated_at"] = lib.iso_now()

    lib.write_handle(name, handle)
    debug_log(
        f"SubagentStop: amux:{name} bg={len(background_tasks)} "
        f"mtime_at_stop={handle['mtime_at_stop']} (state left {handle.get('state')!r})"
    )


def handle_notification(name: str, handle: dict, payload: dict) -> None:
    """Set the permission-pending marker for a ``permission_prompt`` Notification.

    Only ``permission_prompt`` is meaningful here (reason-context for 10-03). The
    next ``Stop`` clears it. ``idle_prompt`` is informational only — it is NOT the
    idle signal and we do NOT duplicate the existing Telegram idle notification.
    """
    notification_type = payload.get("notification_type", "")
    if notification_type != "permission_prompt":
        debug_log(f"Notification {notification_type!r} ignored (not permission_prompt)")
        return

    handle["permission_pending"] = True
    handle["updated_at"] = lib.iso_now()
    lib.write_handle(name, handle)
    debug_log(f"Notification permission_prompt: amux:{name} permission_pending=true")


def handle_session_end(name: str, handle: dict, payload: dict) -> None:
    """Mark ``terminated`` promptly, preserving the last-known running/idle state.

    ``last_state`` records what the session was doing before exit (e.g.
    crashed-mid-run vs clean), so 10-03 can report ``terminated`` with context.
    The architecture s6.0 schema has no dedicated termination-reason field, so the
    SessionEnd ``reason`` is intentionally not persisted (we never invent fields
    outside the schema); ``terminated`` is itself the authoritative read-side flag,
    backstopped by ``tmux has-session`` at status time.
    """
    prior_state = handle.get("state")
    if prior_state in ("running", "idle"):
        handle["last_state"] = prior_state
    handle["state"] = "terminated"
    handle["updated_at"] = lib.iso_now()
    lib.write_handle(name, handle)
    reason = payload.get("reason")
    debug_log(
        f"SessionEnd: amux:{name} terminated "
        f"last_state={handle.get('last_state')!r} reason={reason!r}"
    )


_DISPATCH = {
    "Stop": handle_stop,
    "SubagentStop": handle_subagent_stop,
    "Notification": handle_notification,
    "SessionEnd": handle_session_end,
}


def _parse_event_arg(argv: list[str]) -> str | None:
    """Read the ``--event <name>`` CLI arg (which hook fired this process)."""
    for i, tok in enumerate(argv):
        if tok == "--event" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--event="):
            return tok.split("=", 1)[1]
    return None


def main() -> None:
    """Entry point. Fail-OPEN: always exit 0; never disrupt the session."""
    try:
        event = _parse_event_arg(sys.argv[1:])
        if event not in KNOWN_EVENTS:
            debug_log(f"Unknown/absent --event {event!r}; no-op")
            sys.exit(0)

        raw_input = sys.stdin.read()
        debug_log(f"=== spawn_producer ({event}) ===")
        debug_log(f"Raw input: {raw_input[:500]}...")
        try:
            payload = json.loads(raw_input) if raw_input.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        # Handle-gate FIRST and FAST: untracked sessions (plain/human, other
        # repos, bare claude, non-tmux) no-op before any handle write.
        resolved = _resolve_tracked_handle()
        if resolved is None:
            sys.exit(0)
        name, handle = resolved

        _DISPATCH[event](name, handle, payload)
        sys.exit(0)

    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — fail-open: never disrupt the session
        debug_log(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
