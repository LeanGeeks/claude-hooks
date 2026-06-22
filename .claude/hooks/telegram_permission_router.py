#!/usr/bin/env python3
"""
Telegram Permission Router - Relay-backed transport for permission hooks.

This module replaces the legacy direct-Bot-API transport with calls into the
central relay server (``relay-server/``). The server owns the bot token and
delivers callback answers via webhook; this client just speaks HTTP to it.

Key differences vs the legacy router:

* No bot token or chat_id on disk — only a relay URL + installation token in
  ``~/.config/claude-tg-relay/config.toml``.
* No ``getUpdates`` polling. ``wait_for_relay_answer`` long-polls
  ``RelayClient.wait_for_answer``; the relay's webhook handler is the single
  source of truth for callback/reply attribution across all devices.
* The ``telegram_daemon.py`` background process is gone (deleted).

Helper signatures are preserved where call sites already exist:
``send_permission_message``, ``send_question_message``, ``send_freetext_followup``
return an integer message id (the relay's, not Telegram's — but the state-store
field is named ``telegram_message_id`` for historical reasons and we reuse it
unchanged).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from permission_state_store import (
    PermissionRequest,
    set_telegram_message_id,
    debug_log,
)

# Make the ``relay_server`` package importable regardless of how this hook is
# launched. Resolution order:
#   1. Already importable — pip-installed, or via a user-site ``.pth`` written by
#      install-claude-config.sh. Nothing to do.
#   2. ``CLAUDE_RELAY_SERVER_PATH`` env var pointing at the ``relay-server`` dir.
#   3. Walk up from this file looking for a ``relay-server/relay_server`` package
#      (covers running hooks straight from a repo checkout, at any depth).
#
# The previous logic hard-coded ``parent.parent.parent / "relay-server"``, which
# assumed hooks live at ``<repo>/.claude/hooks/``. Once copied to
# ``~/.claude/hooks/`` it resolved to the nonexistent ``~/relay-server`` and the
# import silently failed, disabling the relay.
def _ensure_relay_server_on_path() -> None:
    try:
        if importlib.util.find_spec("relay_server") is not None:
            return
    except (ImportError, ValueError):
        pass

    candidates: List[Path] = []
    env_path = os.environ.get("CLAUDE_RELAY_SERVER_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    here = Path(__file__).resolve()
    candidates.extend(parent / "relay-server" for parent in here.parents)

    for cand in candidates:
        if (cand / "relay_server" / "__init__.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return


_ensure_relay_server_on_path()

try:
    from relay_server.client import (  # type: ignore[import-not-found]
        Answer,
        MessageHandle,
        NotBoundError,
        RelayClient,
        RelayError,
    )
except Exception:  # noqa: BLE001
    RelayClient = None  # type: ignore[assignment]
    Answer = None  # type: ignore[assignment]
    MessageHandle = None  # type: ignore[assignment]
    NotBoundError = Exception  # type: ignore[assignment,misc]
    RelayError = Exception  # type: ignore[assignment,misc]


# Configuration / runtime state
RELAY_CONFIG_FILE = Path.home() / ".config" / "claude-tg-relay" / "config.toml"
ERROR_LOG_FILE = Path.home() / ".claude" / "permission_telegram_errors.log"

TELEGRAM_ENABLED = False
RELAY_CONFIG_SOURCE = "unknown"

# Module-level singleton RelayClient. Created lazily on first send so unit
# tests can patch ``RelayClient`` before any HTTP call goes out.
_relay_client: Optional["RelayClient"] = None  # type: ignore[type-arg]

# Default TTL for relay messages (seconds). The hook still tracks its own
# longer-running TTL in the state store; this is just how long the relay row
# stays "open" before the server marks it expired.
RELAY_MESSAGE_TTL = 43200


def error_log(message: str) -> None:
    """Always write Telegram permission integration errors to disk."""
    try:
        ERROR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG_FILE, "a") as f:
            timestamp = datetime.now(timezone.utc).isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except Exception:  # noqa: BLE001
        pass


def load_telegram_config() -> None:
    """Discover relay credentials and set ``TELEGRAM_ENABLED`` accordingly.

    Name kept for call-site compatibility with the legacy router.
    """
    global TELEGRAM_ENABLED, RELAY_CONFIG_SOURCE, _relay_client

    if RelayClient is None:
        TELEGRAM_ENABLED = False
        RELAY_CONFIG_SOURCE = "unavailable"
        error_log("relay_server package not importable; relay disabled")
        return

    if not RELAY_CONFIG_FILE.exists():
        TELEGRAM_ENABLED = False
        RELAY_CONFIG_SOURCE = "missing"
        error_log(
            f"Relay config missing: {RELAY_CONFIG_FILE}. "
            "Run `relay-client config init` and `relay-client bind`."
        )
        return

    try:
        _relay_client = RelayClient.from_config(RELAY_CONFIG_FILE)
        TELEGRAM_ENABLED = True
        RELAY_CONFIG_SOURCE = str(RELAY_CONFIG_FILE)
    except Exception as e:  # noqa: BLE001
        TELEGRAM_ENABLED = False
        RELAY_CONFIG_SOURCE = "error"
        error_log(f"Failed loading relay config: {e}")

    debug_log(f"Relay enabled: {TELEGRAM_ENABLED} (source={RELAY_CONFIG_SOURCE})")


def _client() -> "RelayClient":
    """Return the active relay client; raises if disabled."""
    if not TELEGRAM_ENABLED or _relay_client is None:
        raise RelayError("relay client not initialised")
    return _relay_client


# Sentinel button ``value`` for the multi-select Submit button. The relay
# recognises it (vs. the ``qa<N>`` option values) and finalises the message's
# accumulated selection instead of toggling another option. Must stay in sync
# with ``_QUESTION_SUBMIT_VALUE`` in relay_server/app.py.
QUESTION_SUBMIT_VALUE = "qa_submit"


# ---- Option labels that signal "let me type a custom answer" ---------------

FREE_TEXT_TRIGGER_LABELS = frozenset({
    "let me type it", "other", "custom", "type it",
    "write it", "free text", "type your answer",
    "enter text", "specify", "something else",
})


def is_free_text_trigger(label: str) -> bool:
    """True if an option label signals the user wants to type a custom answer."""
    if not label:
        return False
    normalized = label.lower().strip().rstrip(".")
    return normalized in FREE_TEXT_TRIGGER_LABELS or label.strip().endswith("...")


# ---- Message senders -------------------------------------------------------

# Action labels are encoded as the button ``value`` so the relay's webhook
# handler ships them back verbatim in the answer payload. The hook then maps
# value -> action.
_PERMISSION_ACTIONS: list[list[dict[str, str]]] = [
    [
        {"label": "Allow", "value": "allow"},
        {"label": "Deny", "value": "deny"},
    ],
    [
        {"label": "Stop", "value": "stop"},
        {"label": "Whitelist", "value": "whitelist"},
    ],
]


def _format_command_summary(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Format a summary of the tool/command for display in Telegram.

    Up to 10 lines and 2 KB of text. Lines are never truncated mid-line except
    the last included one, which may end with ``...`` to signal truncation.
    """
    MAX_LINES = 10
    MAX_BYTES = 2048

    if tool_name == "Bash":
        raw = tool_input.get("command", "")
    elif tool_name in ("Read", "Write", "Edit"):
        file_path = tool_input.get("file_path", tool_input.get("path", ""))
        raw = f"{tool_name}({file_path})"
    elif tool_name == "WebFetch":
        raw = f"WebFetch({tool_input.get('url', '')})"
    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            parts = []
            for q in questions:
                text = q.get("question", "")
                opts = q.get("options", [])
                if opts:
                    labels = ", ".join(
                        o.get("label", str(o)) for o in opts
                    )
                    parts.append(f"{text}\n  Options: {labels}")
                else:
                    parts.append(text)
            raw = "\n\n".join(parts)
        else:
            raw = f"AskUserQuestion: {json.dumps(tool_input)}"
    else:
        raw = f"{tool_name}: {json.dumps(tool_input)}"

    lines = raw.splitlines()
    selected = lines[:MAX_LINES]

    result_lines: list[str] = []
    budget = MAX_BYTES
    for i, line in enumerate(selected):
        encoded = line.encode("utf-8")
        is_last_selected = i == len(selected) - 1
        if len(encoded) <= budget:
            result_lines.append(line)
            budget -= len(encoded) + 1
        else:
            truncated = encoded[: max(0, budget - 3)].decode(
                "utf-8", errors="ignore"
            )
            result_lines.append(truncated + "...")
            break

        if budget <= 0:
            if not is_last_selected or len(lines) > MAX_LINES:
                result_lines[-1] += "..."
            break

    if (
        len(lines) > MAX_LINES
        and result_lines
        and not result_lines[-1].endswith("...")
    ):
        result_lines[-1] += "..."

    return "\n".join(result_lines)


def _unallowlisted_bash_parts(request: PermissionRequest) -> tuple[list[str], list[str]]:
    """Return ``(denied, unknown)`` sub-commands for a Bash permission request.

    PreToolUse names the non-allowlisted sub-commands in its
    ``permissionDecisionReason`` so the terminal prompt can show them, but
    Claude Code does NOT forward that reason to the PermissionRequest hook, so
    the Telegram message has no access to it. We re-derive the same breakdown
    here by running the very same validator against the command (workspace dir =
    ``request.cwd``, exactly as PreToolUse did), guaranteeing the annotation
    matches what the terminal shows.

    Best-effort only: returns ``([], [])`` for non-Bash tools, an empty command,
    or any failure — computing this annotation must never block sending the
    prompt.
    """
    if request.tool_name != "Bash":
        return [], []
    command = (request.tool_input or {}).get("command", "")
    if not command.strip():
        return [], []
    try:
        from pretool_hook import BashPermissionValidator
        from settings_loader import SettingsLoader
        from bash_command_parser import BashCommandParser

        validator = BashPermissionValidator(
            SettingsLoader(request.cwd), BashCommandParser(), workspace_dir=request.cwd
        )
        result = validator.validate_bash_command(command)
    except Exception as e:  # noqa: BLE001
        debug_log(f"Could not compute non-allowlisted parts: {e}")
        return [], []

    seen: set[str] = set()
    denied: list[str] = []
    unknown: list[str] = []
    for r in result.get("validation_results", []):
        cmd = r.get("command", "")
        if not cmd or cmd in seen:
            continue
        seen.add(cmd)
        if r.get("denied"):
            denied.append(cmd)
        elif not r.get("allowed"):
            unknown.append(cmd)
    return denied, unknown


def _send_relay(
    *,
    text: str,
    keyboard: list[list[dict[str, str]]] | None,
    kind: str,
    reply_required: bool,
    request_id: str,
    group_id: Optional[str] = None,
    group_total: Optional[int] = None,
    multi_select: bool = False,
) -> Optional[int]:
    """Common send helper. Returns the relay message_id or None on failure."""
    if not TELEGRAM_ENABLED:
        return None
    try:
        handle = _client().send_message(
            text=text,
            keyboard=keyboard,
            kind=kind,
            reply_required=reply_required,
            ttl_sec=RELAY_MESSAGE_TTL,
            idempotency_key=f"req:{request_id}:send",
            group_id=group_id,
            group_total=group_total,
            multi_select=multi_select,
        )
    except NotBoundError as e:
        error_log(
            f"Relay returned not_bound; run `relay-client bind` to re-attach: {e}"
        )
        return None
    except RelayError as e:
        error_log(f"Relay send failed: {e}")
        return None
    return handle.message_id


def send_permission_message(
    request: PermissionRequest,
    workspace_name: str,
    session_name: Optional[str] = None,
) -> Optional[int]:
    """Send a permission-request prompt with allow/deny/stop/whitelist buttons.

    Returns the relay message id (stored as ``telegram_message_id`` on the
    state-store row for legacy reasons).
    """
    import html as _html

    # Everything interpolated into the HTML body must be escaped: workspace /
    # session names and the command summary can all contain <, >, & (commands
    # routinely do — e.g. `2>&1`), which would otherwise corrupt Telegram's HTML
    # parse and either garble or drop the message.
    title = f"<b>{_html.escape(workspace_name, quote=False)}</b>"
    if session_name:
        title += f" <i>{_html.escape(session_name, quote=False)}</i>"

    summary = _format_command_summary(request.tool_name, request.tool_input)

    lines = [
        title,
        "",
        f"<b>Permission Request</b> <code>{request.request_id}</code>",
        "",
        "<pre>",
        _html.escape(summary, quote=False),
        "</pre>",
    ]

    # Re-derive and surface which sub-commands tripped the prompt (PreToolUse's
    # reason isn't forwarded here — see _unallowlisted_bash_parts). Escaped
    # because command fragments routinely contain <, >, & (e.g. `2>&1`).
    denied, unknown = _unallowlisted_bash_parts(request)
    if denied:
        lines.append("")
        lines.append("🚫 <b>Matches a denied pattern:</b>")
        lines += [f"<code>{_html.escape(c, quote=False)}</code>" for c in denied]
    if unknown:
        lines.append("")
        lines.append("⚠️ <b>Not in allowlist:</b>")
        lines += [f"<code>{_html.escape(c, quote=False)}</code>" for c in unknown]

    lines += ["", "Approve this command?"]
    text = "\n".join(lines)

    message_id = _send_relay(
        text=text,
        keyboard=_PERMISSION_ACTIONS,
        kind="permission",
        reply_required=True,
        request_id=request.request_id,
    )
    if message_id is not None:
        set_telegram_message_id(request.request_id, message_id)
    return message_id


def send_question_message(
    request: PermissionRequest,
    workspace_name: str,
    index: int,
    total: int,
    group_id: Optional[str] = None,
) -> Optional[int]:
    """Send a single AskUserQuestion prompt; buttons map to option indices.

    The button ``value`` is ``qa<N>`` (matches the legacy callback_data prefix)
    so the answer-handling code can distinguish question answers from
    permission actions when both routes share a state row.

    When ``group_id`` is set, the relay treats all ``total`` sibling messages as
    one re-answerable group: taps highlight the choice and stay live until every
    question is answered, then the whole group's keyboards are stripped at once.
    """
    ti = request.tool_input or {}
    question_text = ti.get("question", "")
    header = ti.get("header", "")
    options = ti.get("options", [])
    multi_select = ti.get("multiSelect", False)

    import html as _html

    def esc(s: str) -> str:
        return _html.escape(s or "", quote=False)

    lines: list[str] = []
    if index == 0 and total > 1:
        lines.append("━" * 23)
    if total > 1:
        lines.append(
            f"<b>[{index + 1}/{total}] Question</b> — {esc(workspace_name)}"
        )
    else:
        lines.append(f"<b>Question</b> — {esc(workspace_name)}")
    lines.append("")
    if header:
        lines += [f"<b><i>{esc(header)}</i></b>", ""]
    lines.append(esc(question_text))

    keyboard: list[list[dict[str, str]]] | None
    if options:
        # Telegram inline buttons are narrow and truncate (never wrap) their
        # text, so we render the *full* option label + description in the
        # message body as a numbered list, where text wraps freely. Each button
        # then only needs to carry its number (and as much of the label as
        # fits) — the body is the source of truth for what each number means.
        if multi_select:
            lines += [
                "",
                "<i>Tap numbers to toggle (select as many as you like), "
                "then tap ✓ Submit. Or reply with text:</i>",
                "",
            ]
        else:
            lines += ["", "<i>Tap a number below, or reply with text:</i>", ""]

        rows: list[list[dict[str, str]]] = []
        for i, opt in enumerate(options):
            if isinstance(opt, dict):
                label = opt.get("label", str(opt))
                description = opt.get("description", "")
            else:
                label = str(opt)
                description = ""
            num = i + 1
            # Blank line between options for readability. Telegram HTML has no
            # font-size control, so the contrast is bold label vs italic
            # description (the user can't make text literally smaller).
            if i > 0:
                lines.append("")
            lines.append(f"<b>{num}. {esc(label)}</b>")
            if description:
                lines.append(f"    <i>{esc(description)}</i>")
            # One button per row for maximum width; the button shows the number
            # plus the leading part of the label (Telegram trims the overflow).
            rows.append([{"label": f"{num}. {label}", "value": f"qa{i}"}])
        if multi_select:
            # Dedicated Submit button the relay recognises by its sentinel value.
            # Taps on the option buttons above only toggle the selection; this is
            # what finalises the message's answer.
            rows.append([{"label": "✓ Submit", "value": QUESTION_SUBMIT_VALUE}])
        keyboard = rows
    else:
        lines += ["", "<i>Reply to this message with your answer.</i>"]
        keyboard = None

    if total > 1 and index == total - 1:
        lines += ["", "━" * 23]

    message_id = _send_relay(
        text="\n".join(lines),
        keyboard=keyboard,
        kind="question",
        reply_required=True,
        request_id=request.request_id,
        group_id=group_id,
        group_total=total if group_id is not None else None,
        multi_select=bool(multi_select),
    )
    if message_id is not None:
        set_telegram_message_id(request.request_id, message_id)
    return message_id


def send_idle_notification(
    text: str, dedupe_key: str, *, reply_required: bool = False
) -> Optional[int]:
    """Send an idle notification carrying the agent's last message.

    Used by ``notification_hook.py``. ``kind="notification"`` keeps it out of the
    permission/question flows; ``dedupe_key`` seeds the idempotency key so a
    re-fired idle prompt for the same state won't double-post.

    ``reply_required`` controls answerability:

    * ``False`` (default) — fire-and-forget. No force-reply prompt; the message
      is delivered and forgotten. Used for non-amux sessions where a reply can't
      be injected anyway.
    * ``True`` — the relay attaches a Telegram force-reply prompt so the user can
      reply in-thread, and the message stays answerable (answerability is
      per-message, not gated by kind). ``notification_hook`` uses this for
      amux-hosted sessions and hands the returned id to ``reply_injector.py``.

    Returns the relay message id, or None if the relay is disabled / send fails.
    """
    return _send_relay(
        text=text,
        keyboard=None,
        kind="notification",
        reply_required=reply_required,
        request_id=dedupe_key,
    )


def send_freetext_followup(request_id: str, workspace_name: str) -> Optional[int]:
    """Send a follow-up free-text prompt and re-route the request to it."""
    text = f"<b>{workspace_name}</b>\n\n<i>Please type your answer:</i>"
    message_id = _send_relay(
        text=text,
        keyboard=None,
        kind="question",
        reply_required=True,
        request_id=f"{request_id}:followup",
    )
    if message_id is not None:
        set_telegram_message_id(request_id, message_id)
    return message_id


# ---- Message lifecycle helpers --------------------------------------------


def remove_inline_buttons(message_id: int) -> bool:
    """Cancel the relay message (server strips keyboard + marks cancelled).

    Returns True on success. Kept under the legacy name so ``posttool_hook``
    and the AskUserQuestion fan-out don't need to know about the swap.
    """
    if not TELEGRAM_ENABLED or message_id is None:
        return False
    try:
        _client().cancel_message(int(message_id))
        return True
    except RelayError as e:
        error_log(f"Relay cancel_message failed: {e}")
        return False


def set_message_reaction(message_id: int, emoji: str) -> bool:
    """No-op shim — the relay does not expose reactions.

    The legacy daemon used reactions to mark a request as resolved in the chat
    UI. The relay strips the keyboard via cancel/expire which is the more
    important affordance; reactions are deferred.
    """
    return True


def edit_message_text(
    message_id: int,
    text: str,
    inline_buttons: Optional[List] = None,
) -> bool:
    """Update a relay message's text. Keyboard edits are not supported."""
    if not TELEGRAM_ENABLED or message_id is None:
        return False
    try:
        _client().edit_message(int(message_id), text=text)
        return True
    except RelayError as e:
        error_log(f"Relay edit_message failed: {e}")
        return False


def delete_message(message_id: int) -> bool:
    """Delete a relay message via the server."""
    if not TELEGRAM_ENABLED or message_id is None:
        return False
    try:
        _client().delete_message(int(message_id))
        return True
    except RelayError as e:
        error_log(f"Relay delete_message failed: {e}")
        return False


# ---- Whitelist / settings helpers (unchanged from legacy router) -----------


def generate_whitelist_pattern(request: PermissionRequest) -> str:
    """Pick the best Bash permission pattern for whitelisting this request."""
    if request.permission_suggestions:
        return request.permission_suggestions[0]

    if request.tool_name == "Bash":
        command = request.tool_input.get("command", "")
        parts = command.split()
        base_cmd = None
        for part in parts:
            if "=" in part and not part.startswith("-"):
                continue
            if part == "sudo":
                continue
            base_cmd = part
            break
        if base_cmd:
            return f"Bash({base_cmd}:*)"

    return f"{request.tool_name}"


def update_settings_local_json(workspace_dir: str, permission_pattern: str) -> bool:
    """Add a permission pattern to ``.claude/settings.local.json`` atomically."""
    settings_path = Path(workspace_dir) / ".claude" / "settings.local.json"

    settings: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            with open(settings_path, "r") as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            debug_log(f"Error parsing settings.local.json: {e}")
            backup_path = settings_path.with_suffix(".json.backup")
            settings_path.rename(backup_path)
            settings = {}
        except Exception as e:  # noqa: BLE001
            debug_log(f"Error reading settings.local.json: {e}")
            settings = {}

    if "permissions" not in settings:
        settings["permissions"] = {}
    if "allow" not in settings["permissions"]:
        settings["permissions"]["allow"] = []

    allow_list = settings["permissions"]["allow"]
    if permission_pattern in allow_list:
        debug_log(f"Permission pattern already exists: {permission_pattern}")
        return True

    allow_list.append(permission_pattern)
    debug_log(f"Added permission pattern: {permission_pattern}")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_suffix(".json.tmp")
    try:
        with open(temp_path, "w") as f:
            json.dump(settings, f, indent=2)
        temp_path.rename(settings_path)
        return True
    except Exception as e:  # noqa: BLE001
        debug_log(f"Error writing settings.local.json: {e}")
        if temp_path.exists():
            temp_path.unlink()
        return False


def process_whitelist_update(
    request: PermissionRequest, decision: Dict[str, Any]
) -> bool:
    """Apply a whitelist decision to ``settings.local.json``.

    Mirrors the legacy semantics: string patterns are written locally; rule
    objects (e.g. MCP addRules) are left to Claude Code to persist via
    ``updatedPermissions``.
    """
    updated_perms = decision.get("updatedPermissions", [])
    if isinstance(updated_perms, dict):
        updated_perms = updated_perms.get("add", [])

    if not updated_perms:
        debug_log("No permission patterns to add")
        return True

    success = True
    for pattern in updated_perms:
        if isinstance(pattern, str):
            if not update_settings_local_json(request.cwd, pattern):
                success = False
        else:
            debug_log(f"Skipping rule object (handled by Claude Code): {pattern}")
    return success


# ---- Answer routing --------------------------------------------------------


def relay_answer_to_decision(
    request: PermissionRequest, answer: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Translate a relay answer payload into a hook decision dict.

    Permission answers (allow/deny/stop/whitelist) arrive as button taps with
    ``value`` set by ``_PERMISSION_ACTIONS``. AskUserQuestion answers arrive
    either as a button tap (``value`` = ``qa<N>``) or a free-text reply.
    """
    via = answer.get("via")
    if via == "button_multi":
        # Multi-select: ``option_idxs`` are indices into the question's options.
        # Re-derive the clean labels (the relay only stores button labels) and
        # join with ", " — the format AskUserQuestion expects for multi-select.
        options = (request.tool_input or {}).get("options", [])
        labels: list[str] = []
        for idx in answer.get("option_idxs", []):
            if isinstance(idx, int) and 0 <= idx < len(options):
                opt = options[idx]
                labels.append(
                    opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                )
        return {"action": "reply", "reply_text": ", ".join(labels)}
    if via == "button":
        value = answer.get("value")
        if value in ("allow", "deny", "stop"):
            return {"action": value}
        if value == "whitelist":
            perms = list(request.permission_suggestions)
            # Add session-scoped duplicates so the rule takes effect immediately
            # without waiting for the settings file to be re-read.
            session_perms = [
                dict(p, destination="session")
                for p in perms
                if isinstance(p, dict) and p.get("destination") != "session"
            ]
            return {"action": "whitelist", "updatedPermissions": perms + session_perms}
        if isinstance(value, str) and value.startswith("qa") and value[2:].isdigit():
            options = (request.tool_input or {}).get("options", [])
            idx = int(value[2:])
            if 0 <= idx < len(options):
                opt = options[idx]
                label = (
                    opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                )
            else:
                label = answer.get("label", value)
            return {"action": "reply", "reply_text": label}
        # Unknown button value — surface label as a reply for visibility.
        label = answer.get("label", "")
        return {"action": "reply", "reply_text": label}

    # Free-text reply (force_reply or fallback).
    text = answer.get("text", "")
    return {"action": "reply", "reply_text": text}


def wait_for_relay_answer(
    message_id: int,
    timeout: float = 300.0,
    long_poll_chunk: int = 5,
) -> Optional[Dict[str, Any]]:
    """Long-poll the relay for an answer to ``message_id``.

    Returns the raw answer dict on success, ``None`` on overall timeout, or a
    sentinel ``{"_state": state}`` dict when the message terminated without a
    user answer (expired or cancelled — the caller decides what to do).
    """
    if not TELEGRAM_ENABLED:
        return None
    try:
        result = _client().wait_for_answer(
            int(message_id), timeout=timeout, long_poll_chunk=long_poll_chunk
        )
    except RelayError as e:
        error_log(f"wait_for_answer failed: {e}")
        return None
    if result is None:
        return None
    if result.state == "answered":
        return result.answer or {}
    return {"_state": result.state}
