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

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from permission_state_store import (
    PermissionRequest,
    set_telegram_message_id,
    debug_log,
)

# Make the relay_server package importable when running hooks from the global
# hooks dir (no editable install). The repo path is the fallback when the
# package is not pip-installed.
_REPO_RELAY = Path(__file__).resolve().parent.parent.parent / "relay-server"
if _REPO_RELAY.exists() and str(_REPO_RELAY) not in sys.path:
    sys.path.insert(0, str(_REPO_RELAY))

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
RELAY_MESSAGE_TTL = 3600


def error_log(message: str) -> None:
    """Always write Telegram permission integration errors to disk."""
    try:
        ERROR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG_FILE, "a") as f:
            timestamp = datetime.now().isoformat()
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


def _send_relay(
    *,
    text: str,
    keyboard: list[list[dict[str, str]]] | None,
    kind: str,
    reply_required: bool,
    request_id: str,
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
    title = f"<b>{workspace_name}</b>"
    if session_name:
        title += f" <i>{session_name}</i>"

    summary = _format_command_summary(request.tool_name, request.tool_input)

    text = "\n".join([
        title,
        "",
        f"<b>Permission Request</b> <code>{request.request_id}</code>",
        "",
        "<pre>",
        summary,
        "</pre>",
        "",
        "Approve this command?",
    ])

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
) -> Optional[int]:
    """Send a single AskUserQuestion prompt; buttons map to option indices.

    The button ``value`` is ``qa<N>`` (matches the legacy callback_data prefix)
    so the answer-handling code can distinguish question answers from
    permission actions when both routes share a state row.
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
    if options:
        lines += ["", "<i>Tap an option, or reply with text:</i>"]
        if multi_select:
            lines.append(
                "<i>(multi-select not supported via Telegram — first answer wins)</i>"
            )
    else:
        lines += ["", "<i>Reply to this message with your answer.</i>"]
    if total > 1 and index == total - 1:
        lines += ["", "━" * 23]

    keyboard: list[list[dict[str, str]]] | None
    if options:
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for i, opt in enumerate(options):
            label = (
                opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
            )
            row.append({"label": label, "value": f"qa{i}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        keyboard = rows
    else:
        keyboard = None

    message_id = _send_relay(
        text="\n".join(lines),
        keyboard=keyboard,
        kind="question",
        reply_required=True,
        request_id=request.request_id,
    )
    if message_id is not None:
        set_telegram_message_id(request.request_id, message_id)
    return message_id


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
