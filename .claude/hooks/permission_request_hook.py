#!/usr/bin/env python3
"""
PermissionRequest Hook - Telegram-Gated Permission Approval

This hook is triggered when PreToolUse returns `ask` for a command.
It sends a Telegram message via the central relay server and long-polls for
the user's answer (button tap or text reply).

Transport: the relay server (``relay-server/``) owns the bot token and routes
callbacks via webhook. This hook talks to it over HTTP via ``RelayClient``;
there is no per-device ``getUpdates`` poller anymore.

The posttool_hook.py handles:
- Detecting when terminal prompt is used instead
- Cancelling the relay message (strips buttons) when resolved via terminal

This hook races the relay long-poll against the local state store for either
source (Telegram answer or terminal resolution).

Action mappings:
- allow     -> behavior: "allow"
- deny      -> behavior: "deny"
- stop      -> behavior: "deny" + interrupt: true
- whitelist -> behavior: "allow" + updatedPermissions
- reply     -> behavior: "deny" + message (text reply from user)
- resolved_terminal -> exit without decision (terminal handles it)
"""

import json
import queue
import re
import sys
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List, Tuple

import telegram_permission_router as telegram_router

# Import the new modules
from permission_state_store import (
    PermissionRequest,
    RequestState,
    create_request,
    get_request,
    resolve_via_terminal,
    update_request_state,
    cleanup_expired_requests,
    RESOLUTION_SOURCE_TELEGRAM,
    RESOLUTION_SOURCE_TERMINAL,
    RESOLUTION_SOURCE_TIMEOUT,
)
from telegram_permission_router import (
    load_telegram_config,
    finalize_message,
    send_permission_message,
    send_question_message,
    wait_for_relay_answer,
    relay_answer_to_decision,
    remove_inline_buttons,
)
import session_yolo_store

# ── Per-question record for AskUserQuestion groups ────────────────────────────
# A small dataclass (not a tuple) so 15-04/15-05 can add fields without
# rewriting every unpack site in the wait phase.

@dataclass
class _ChildRecord:
    """One question's relay state, held for the wait phase and future PATCHes."""
    child: "PermissionRequest"   # state-store row
    question: Dict[str, Any]     # original question dict from tool_input
    message_id: int              # relay message id
    body: str                    # rendered body text (needed by 15-04/15-05 to PATCH)


# Debug logging
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = Path.home() / ".claude" / "permission_request_debug.log"
ERROR_LOG = Path.home() / ".claude" / "permission_telegram_errors.log"

# Configuration
WAIT_BEFORE_TELEGRAM = 0  # seconds to wait before sending Telegram message
REQUEST_TTL = 43200  # 12 hour TTL for pending requests
MAX_WAIT_FOR_RESPONSE = REQUEST_TTL  # Backwards-compatible name for tests/importers
POLL_INTERVAL = 0.5  # seconds between state store polls
# Coarser interval for the relay-less wait, which can run for the full TTL.
# Noticing a terminal resolution a few seconds late is harmless; re-reading the
# (growing) state file twice a second for 12h is not.
STATE_STORE_POLL_INTERVAL = 10  # seconds

# ── AskUserQuestion wait-phase tuning (15-04) ────────────────────────────────
# Relay long-poll chunk cap, unchanged from the sequential loop. The result
# queue makes wake-up latency independent of chunk size, so shorter chunks would
# only add request volume.
LONG_POLL_CHUNK = 25  # seconds
# Main-loop tick. Bounds how late a terminal resolution is noticed; a child
# answering wakes the loop immediately through the queue.
WAIT_TICK_SECONDS = 2.0
# Floor on one child-thread poll cycle. A relay that fails instantly (or a test
# double) would otherwise spin a thread hot for the full 12h TTL.
CHILD_POLL_FLOOR_SECONDS = 1.0
# Shortest queue wait the loop will use when it is about to escalate (15-05).
# The tick shrinks towards the escalation deadline so the second group goes out
# on time instead of up to ``WAIT_TICK_SECONDS`` late; the floor keeps a
# rounding error from turning into a hot spin.
ESCALATION_TICK_FLOOR = 0.05
# How long the wait loop keeps re-reading the rows for a terminal resolution
# before it accepts that a group really is dead, and how often it looks. Only
# ever paid on the way out of the loop, never per tick.
#
# The poll interval is deliberately not tighter: ``get_request`` takes the state
# file's lock and scans it whole, once per child, so a busy interval here both
# burns IO and contends with the PostToolUse write this loop is waiting for.
# Six passes is already generous insurance against an ordering that should not
# be possible in the first place (see ``_terminal_win``).
TERMINAL_GRACE_SECONDS = 1.5
TERMINAL_GRACE_POLL_SECONDS = 0.25

# Literal ``answers[q]`` values that mean "the user picked nothing" — the real
# content then lives in ``annotations[q]["notes"]``. Matched exactly, never as a
# "any parenthesised value" heuristic: ``(none of these)`` is a legitimate
# free-text answer and must survive untouched.
TERMINAL_ANSWER_PLACEHOLDERS = frozenset({"(notes only)", "(no option selected)"})

# Shown for a question we could not match an answer to (parse failed, no
# ``terminal_answers`` recorded, or Claude Code's askUserQuestionTimeout
# auto-continued with only a subset of the questions answered).
TERMINAL_ANSWER_FALLBACK_TEXT = "Answered in the terminal"


def debug_log(message: str):
    """Log debug message if debug mode is enabled."""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now(timezone.utc).isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            print(f"Debug log error: {e}", file=sys.stderr)


def error_log(message: str):
    """Always log permission-request Telegram errors for troubleshooting."""
    try:
        Path(ERROR_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, 'a') as f:
            timestamp = datetime.now(timezone.utc).isoformat()
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def get_session_name(session_id: str, cwd: str) -> Optional[str]:
    """
    Get session name (slug) from Claude's session storage.

    Args:
        session_id: UUID of the current session
        cwd: Current working directory

    Returns:
        Session slug/name if found, None otherwise
    """
    if not session_id:
        return None

    claude_dir = Path.home() / ".claude"

    # Sanitize cwd to match Claude's project directory naming
    normalized_path = cwd.lstrip("/")
    project_dir_name = "-" + normalized_path.replace("/", "-")

    # Path to session file
    session_file = claude_dir / "projects" / project_dir_name / f"{session_id}.jsonl"

    debug_log(f"Looking for session file: {session_file}")

    if not session_file.exists():
        debug_log(f"Session file not found: {session_file}")
        return None

    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if 'slug' in entry and entry['slug']:
                        debug_log(f"Found session slug: {entry['slug']}")
                        return entry['slug']
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        debug_log(f"Error reading session file: {e}")

    return None


def get_workspace_name(cwd: str) -> str:
    """Extract workspace name (last segment of cwd)."""
    return Path(cwd).name


def build_output_decision(decision: Optional[Dict[str, Any]], request: PermissionRequest) -> Optional[Dict[str, Any]]:
    """
    Build the hook output payload based on the decision.

    Args:
        decision: Decision dict with 'action' and optionally 'updatedPermissions' or 'reply_text'
        request: The original permission request

    Returns:
        Hook output dict, or None for fallback to terminal
    """
    if not decision:
        # No decision - fall back to terminal prompt
        return None

    action = decision.get('action')

    if action == 'allow':
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'allow'
                }
            }
        }

    elif action == 'deny':
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'deny'
                }
            }
        }

    elif action == 'stop':
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'deny',
                    'interrupt': True
                }
            }
        }

    elif action == 'yolo':
        # Allow this request AND flag the session so every subsequent request
        # auto-allows without prompting (consulted at the top of main()). The
        # flag is keyed by session_id, which lives on the request — the relay
        # has no session concept, so this can only happen here in the hook.
        if request is not None:
            session_yolo_store.enable(request.session_id)
            debug_log(f"YOLO enabled for session {request.session_id}")
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'allow'
                }
            }
        }

    elif action == 'reply':
        # Text reply from user - map to deny with message
        reply_text = decision.get('reply_text', '')
        debug_log(f"Processing reply action: {reply_text[:50]}...")

        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'deny',
                    'message': f"User reply: {reply_text}",
                }
            }
        }

    elif action == 'answer':
        # AskUserQuestion answers — inject `answers` into tool input so the
        # tool short-circuits without prompting the user.
        updated_input = decision.get('updatedInput', {})
        debug_log(f"Processing answer action with {len(updated_input.get('answers', {}))} answer(s)")
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'allow',
                    'updatedInput': updated_input,
                }
            }
        }

    elif action == 'deny_mixed_roles':
        # AskUserQuestion addressed two or more distinct human roles in one call.
        # Bail immediately — nothing was sent, nothing was stored.
        reason = decision.get('reason', '')
        debug_log(f"Mixed-role rejection: {reason[:120]}")
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'deny',
                    'message': reason,
                }
            }
        }

    # Unknown action - fall back to terminal
    debug_log(f"Unknown action: {action}")
    return None


def wait_for_response(
    request_id: str,
    message_id: Optional[int],
    ttl_seconds: int = REQUEST_TTL,
) -> Optional[Dict[str, Any]]:
    """Race the relay long-poll against the local state store.

    Two competing wakeups need to be reconciled:

    * The relay sees a button tap / free-text reply (via webhook on the
      server). We pick this up by long-polling ``wait_for_relay_answer`` in
      short chunks.
    * The user resolves the request through the terminal prompt. The
      PostToolUse hook writes ``resolved_terminal`` to the local state store;
      between long-poll chunks we check for that and bail out.

    Returns a decision dict for Telegram answers, ``None`` if the request was
    resolved via terminal, the message expired/cancelled, or the overall TTL
    elapsed.
    """
    if message_id is None:
        # No relay handle — only the state store can resolve this request.
        # This path is exercised when Telegram is disabled or send failed.
        return _wait_state_store_only(request_id, ttl_seconds)

    request = get_request(request_id)
    deadline = time.time() + ttl_seconds
    while time.time() < deadline:
        current = get_request(request_id)
        if current and current.state == RequestState.RESOLVED_TERMINAL.value:
            debug_log(f"Request {request_id} resolved via terminal, cancelling relay msg")
            remove_inline_buttons(message_id)
            return None
        chunk = min(25, max(1, int(deadline - time.time())))
        answer = wait_for_relay_answer(message_id, timeout=chunk, long_poll_chunk=chunk)
        if answer is None:
            continue
        if "_state" in answer:
            debug_log(f"Relay message terminal w/o user answer: {answer['_state']}")
            return None
        # We have a real user answer. Translate into a decision.
        if request is None:
            request = get_request(request_id)
        decision = relay_answer_to_decision(request, answer) if request else None
        if decision is None:
            debug_log("Failed to translate relay answer to decision")
            return None
        debug_log(f"Request {request_id} resolved via relay: {decision}")
        # Strip the inline keyboard the instant the user taps a button, mirroring
        # the terminal-resolution branch above. Without this, a Telegram-approved
        # permission keeps its live buttons until PostToolUse fires *after the
        # tool finishes* — for a long-running tool that looks like the buttons
        # never disappear.
        remove_inline_buttons(message_id)
        # Record the Telegram resolution so the PostToolUse sweep skips this
        # request and won't re-cancel an already-stripped message.
        _mark_relay_resolved(request_id, decision)
        return decision

    debug_log(f"Request {request_id} polling timed out after {ttl_seconds}s")
    return None


# Map a relay decision's action onto the state-store terminal state so the
# PostToolUse hook's pending-request sweep (which only matches PENDING) leaves a
# Telegram-resolved permission alone.
_ACTION_TO_STATE = {
    "allow": RequestState.ALLOW,
    "deny": RequestState.DENY,
    "stop": RequestState.STOP,
    "yolo": RequestState.ALLOW,  # YOLO resolves this request as a plain allow
    "reply": RequestState.REPLY,
}


def _mark_relay_resolved(request_id: str, decision: Dict[str, Any]) -> None:
    """Flip a Telegram-resolved permission request to its terminal state.

    Best-effort: a failure here only means PostToolUse may redundantly re-cancel
    the (already stripped) relay message, which is harmless.
    """
    state = _ACTION_TO_STATE.get(decision.get("action"))
    if state is None:
        return
    try:
        update_request_state(
            request_id,
            state,
            decision=decision,
            reply_text=decision.get("reply_text"),
            resolution_source=RESOLUTION_SOURCE_TELEGRAM,
        )
    except Exception as e:  # noqa: BLE001 — never disrupt the decision path.
        debug_log(f"Failed to mark request {request_id} relay-resolved: {e}")


def _format_non_whitelisted(request: PermissionRequest) -> str:
    """One-line summary of the command parts that tripped the gate, for the
    auto-deny note. Best-effort; empty string for non-Bash / none / any failure."""
    try:
        from telegram_permission_router import _unallowlisted_bash_parts
        denied, unknown = _unallowlisted_bash_parts(request)
    except Exception as e:  # noqa: BLE001
        debug_log(f"Could not compute non-whitelisted parts for note: {e}")
        return ""
    parts = []
    if denied:
        parts.append("matches a denied pattern: " + ", ".join(denied))
    if unknown:
        parts.append("not in allowlist: " + ", ".join(unknown))
    return "; ".join(parts)


def _auto_deny_output(
    request: PermissionRequest, delivery_failed: bool
) -> Dict[str, Any]:
    """Build the PermissionRequest auto-deny payload used when nobody answered
    within the TTL. Carries a note the agent can act on (the command may work on
    retry, plus which parts needed approval). Fail-safe: deny, never allow.

    Two variants depending on what we actually know — a *delivery failure* (we
    could not even reach the operator) vs. *delivered but unanswered*."""
    hours = REQUEST_TTL // 3600
    if delivery_failed:
        reason = (
            "Auto-denied: this command required operator approval, but the approval "
            "request could not be delivered to the operator (Telegram relay "
            f"unreachable or not bound) and no terminal approval arrived within {hours}h. "
            "The session was unblocked instead of waiting indefinitely. The same "
            "command may succeed if retried once the relay is reachable again."
        )
    else:
        reason = (
            "Auto-denied: this command required operator approval, but no response "
            f"was received within {hours}h. Re-run the command to request approval again."
        )
    parts = _format_non_whitelisted(request)
    if parts:
        reason += f" Parts that required approval — {parts}."
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PermissionRequest',
            'decision': {
                'behavior': 'deny',
                'message': reason,
            }
        }
    }


def _record_auto_deny(request_id: str) -> None:
    """Mark the row terminal (deny/timeout) so it stops reading as ``pending``
    for stuck-detection. Best-effort, idempotent vs already-terminal rows."""
    try:
        update_request_state(
            request_id,
            RequestState.DENY,
            resolution_source=RESOLUTION_SOURCE_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 — never disrupt the hook's exit path.
        debug_log(f"Failed to record auto-deny for {request_id}: {e}")


def _wait_state_store_only(
    request_id: str, ttl_seconds: int
) -> Optional[Dict[str, Any]]:
    """State-store-only wait used when there is no relay message (Telegram send
    failed or is disabled). Races only the terminal: we poll for a terminal
    resolution written by PostToolUse. Returns the decision if the terminal
    produced one, else None (terminal-resolved or TTL elapsed — the caller
    distinguishes by re-reading the row).

    Polls coarsely (``STATE_STORE_POLL_INTERVAL``): this can run for the full
    12h TTL, and a few seconds of latency in *noticing* a terminal resolution is
    harmless (Claude Code has already proceeded) — far better than re-reading a
    growing state file twice a second for hours.
    """
    start_time = time.time()
    while time.time() - start_time < ttl_seconds:
        request = get_request(request_id)
        if request:
            if request.state == RequestState.RESOLVED_TERMINAL.value:
                return None
            if request.decision and request.state in [
                RequestState.ALLOW.value,
                RequestState.DENY.value,
                RequestState.STOP.value,
                RequestState.WHITELIST.value,
                RequestState.REPLY.value,
            ]:
                return request.decision
        time.sleep(STATE_STORE_POLL_INTERVAL)
    return None


def parse_terminal_answers(
    stored: Optional[str], questions: List[str]
) -> Dict[str, str]:
    """Turn the row's ``terminal_answers`` into ``{question text: answer text}``.

    Three tiers, in order. Never raises: ``None``, ``""``, invalid JSON and
    unrelated prose all return ``{}``, and any question left unmatched is simply
    absent from the result (the caller renders the generic fallback text).

    1. **Structured** — the real path. ``json.loads`` the reduced payload and
       read ``answers[q]`` by exact question text. When the answer is a
       placeholder (empty, ``(notes only)``, ``(no option selected)``) and
       ``notes[q]`` carries content, the notes are rendered instead; a
       placeholder with no notes is dropped, because patching
       ``✅ (notes only)`` into someone's chat is worse than useless.
    2. **Prose** — defensive only, for a shape change that stops the payload
       being JSON. Exact question-text matching against ``"Q"="A"`` pairs.
       Deliberately *no* positional zip: on real data the pair count disagrees
       with the question count precisely when the parse is already wrong, so
       zipping would mis-assign answers rather than degrade cleanly.
    3. Nothing matched → ``{}``.
    """
    if not stored:
        return {}

    try:
        data = json.loads(stored)
    except Exception:  # noqa: BLE001 — not JSON is an expected input here
        data = None

    if isinstance(data, dict):
        answers = data.get("answers")
        notes = data.get("notes")
        if not isinstance(answers, dict):
            answers = {}
        if not isinstance(notes, dict):
            notes = {}
        parsed: Dict[str, str] = {}
        for question in questions:
            if question not in answers:
                # ``answers`` can be partial — askUserQuestionTimeout
                # auto-continues with whatever was selected so far.
                continue
            raw = answers[question]
            value = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
            if not value.strip() or value in TERMINAL_ANSWER_PLACEHOLDERS:
                note = notes.get(question)
                if isinstance(note, str) and note.strip():
                    parsed[question] = note
                continue
            parsed[question] = value
        return parsed

    pairs = dict(re.findall(r'"([^"]*)"="([^"]*)"', stored))
    return {q: pairs[q] for q in questions if pairs.get(q)}


def _humanize_duration(seconds: float) -> str:
    """Render an escalation delay the way it was written in config.

    ``parse_duration`` hands back seconds, so the ``"15m"`` an operator typed
    arrives here as ``900.0``. The escalation banner is read by a human, so it
    has to say ``15m`` — not ``900s`` and certainly not ``900.0``.

    Sub-second values (only realistic in tests) keep a fractional ``s``.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    total = int(round(value))
    if total <= 0:
        return f"{value:g}s"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return "".join(parts)


def _child_wait_loop(
    group_key: str,
    message_id: int,
    token: Optional[str],
    deadline: float,
    results: "queue.Queue",
    stop: threading.Event,
) -> None:
    """Thread body: long-poll one relay message until it answers or the TTL.

    Pushes ``(group_key, message_id, result)`` where ``result`` is the answer
    dict, a ``{"_state": ...}`` sentinel, or ``None`` on a chunk timeout. Does
    nothing else — no state-store writes happen off the main thread.

    Swallows every exception and pushes a sentinel rather than dying silently:
    a thread that raises would leave the main loop waiting for a result that
    never arrives, until the 12h TTL.
    """
    try:
        while not stop.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            chunk = min(LONG_POLL_CHUNK, max(1, int(remaining)))
            started = time.time()
            result = wait_for_relay_answer(
                message_id, timeout=chunk, long_poll_chunk=chunk, token=token
            )
            if result is not None:
                # Answer or {"_state": ...}: either way this child is done.
                results.put((group_key, message_id, result))
                return
            results.put((group_key, message_id, None))
            elapsed = time.time() - started
            if elapsed < CHILD_POLL_FLOOR_SECONDS:
                stop.wait(CHILD_POLL_FLOOR_SECONDS - elapsed)
    except BaseException as e:  # noqa: BLE001 — must never hang the main loop
        try:
            error_log(
                f"AskUserQuestion wait thread for message {message_id} failed: "
                f"{type(e).__name__}: {e}"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            results.put((group_key, message_id, {"_state": "thread_error"}))
        except Exception:  # noqa: BLE001
            pass


def _any_child_resolved_in_terminal(groups: Dict[str, List["_ChildRecord"]]) -> bool:
    """True when *any* child row reads ``resolved_terminal``.

    The PostToolUse hook only flips the most recent pending child
    (``find_pending_request_by_tool_session`` returns a single row), but that
    signals the whole AskUserQuestion was answered at the keyboard. Detecting it
    on any child is what stops a loop parked elsewhere leaving keyboards live.
    """
    for records in groups.values():
        for rec in records:
            row = get_request(rec.child.request_id)
            if row and row.state == RequestState.RESOLVED_TERMINAL.value:
                return True
    return False


def _terminal_win(
    groups: Dict[str, List["_ChildRecord"]],
    group_tokens: Dict[str, Optional[str]],
    *,
    grace_seconds: float = 0.0,
) -> bool:
    """Detect a terminal resolution and finalize every message if there is one.

    Returns True when the terminal won (the caller returns ``None``, letting the
    native UI stand). Must be consulted at **every** exit from the wait loop, not
    only at the top of a tick — see ``grace_seconds``.

    ``grace_seconds`` re-polls the rows for a short while before answering False.
    The PostToolUse hook writes ``resolved_terminal`` *before* it cancels the
    relay message, so by the time a cancel is observable the row is already
    written and no grace is needed. The grace exists because the cost is
    asymmetric: a wrongly-negative answer silently leaves a chat unpatched (the
    G3 failure of 15-07), while a needless second of latency on a genuinely dead
    group costs nothing — the native UI is live throughout either way.
    """
    deadline = time.time() + max(0.0, grace_seconds)
    while True:
        if _any_child_resolved_in_terminal(groups):
            debug_log("AskUserQuestion resolved via terminal; finalizing messages")
            _finalize_on_terminal_win(groups, group_tokens)
            return True
        if time.time() >= deadline:
            return False
        time.sleep(TERMINAL_GRACE_POLL_SECONDS)


def _finalize_on_terminal_win(
    groups: Dict[str, List["_ChildRecord"]],
    group_tokens: Dict[str, Optional[str]],
) -> None:
    """The terminal answered first: show every open message what was decided.

    Runs on the main thread only. Patches each message with ``✅ <answer>`` and
    strips its keyboard (``finalize_message`` PATCHes then cancels, in that
    order), then sweeps still-pending sibling rows to ``resolved_terminal``.

    Every rung of the ladder leaves the keyboard stripped: a question we cannot
    match an answer to gets the generic text, and a failed PATCH still cancels.
    """
    # Read every row once — the sweep below reuses these.
    rows = {}
    for records in groups.values():
        for rec in records:
            rows[rec.child.request_id] = get_request(rec.child.request_id)

    # PostToolUse only ever flips one row, and not necessarily the child that
    # detected the terminal state — so scan them all and take the first value.
    stored = None
    for records in groups.values():
        for rec in records:
            row = rows.get(rec.child.request_id)
            value = getattr(row, "terminal_answers", None) if row else None
            if value:
                stored = value
                break
        if stored:
            break

    question_texts = [
        (rec.question or {}).get("question", "")
        for records in groups.values()
        for rec in records
    ]
    answers = parse_terminal_answers(stored, question_texts)

    for group_key, records in groups.items():
        token = group_tokens.get(group_key)
        for rec in records:
            text = answers.get((rec.question or {}).get("question", "")) \
                or TERMINAL_ANSWER_FALLBACK_TEXT
            try:
                finalize_message(
                    rec.message_id, rec.body, text, prefix="✅ ", token=token
                )
            except Exception as e:  # noqa: BLE001 — a dead chat must not block
                debug_log(f"Failed to finalize message {rec.message_id}: {e}")

    # Sweep the siblings PostToolUse did not touch. No ``terminal_answers`` is
    # passed, so this cannot blank out the value recorded a moment earlier.
    for records in groups.values():
        for rec in records:
            row = rows.get(rec.child.request_id)
            if row and row.state == RequestState.PENDING.value:
                try:
                    resolve_via_terminal(rec.child.request_id)
                except Exception as e:  # noqa: BLE001
                    debug_log(
                        f"Failed to mark {rec.child.request_id} terminal-resolved: {e}"
                    )


def _finalize_losing_groups(
    groups: Dict[str, List["_ChildRecord"]],
    group_tokens: Dict[str, Optional[str]],
    winning_key: str,
    answers: Dict[str, str],
) -> None:
    """One group answered in full; show the other one what was decided.

    The winner finalizes itself server-side (``app.py:1203`` strips the whole
    group's keyboards once it completes), so only the loser needs client-side
    cleanup: each of its messages is PATCHed with the winning answer and then
    cancelled, including any child that had already been answered — a partially
    answered losing group must not keep a live keyboard.

    Both groups carry the same questions in the same order, so they are paired
    **by index**. The answer patched into the chat is the *unannotated* one: the
    ``(answered by …)`` suffix of brd §5.6 is addressed to the agent, and in the
    chat it would be redundant and confusing.

    Every row of the losing group is then marked ``REPLY`` so ``posttool_hook``'s
    pending-request sweep skips it, just like the winner's rows (which the wait
    loop marked as their answers arrived).
    """
    winner_texts = [
        (rec.question or {}).get("question", "")
        for rec in groups.get(winning_key, [])
    ]

    for group_key, records in groups.items():
        if group_key == winning_key:
            continue
        token = group_tokens.get(group_key)
        for idx, rec in enumerate(records):
            text = answers.get(winner_texts[idx], "") if idx < len(winner_texts) else ""
            try:
                finalize_message(rec.message_id, rec.body, text, prefix="✅ ", token=token)
            except Exception as e:  # noqa: BLE001 — a dead chat must not block
                debug_log(f"Failed to finalize losing message {rec.message_id}: {e}")
            try:
                update_request_state(
                    rec.child.request_id,
                    RequestState.REPLY,
                    reply_text=text,
                    resolution_source=RESOLUTION_SOURCE_TELEGRAM,
                )
            except Exception as e:  # noqa: BLE001
                debug_log(
                    f"Failed to mark losing row {rec.child.request_id} terminal: {e}"
                )


def _wait_for_group_answers(
    groups: Dict[str, List["_ChildRecord"]],
    group_tokens: Dict[str, Optional[str]],
    deadline: float,
    *,
    escalate_at: Optional[float] = None,
    send_escalation: Optional[
        Callable[[], Optional[Tuple[str, List["_ChildRecord"], Optional[str]]]]
    ] = None,
) -> Optional[Tuple[str, Dict[str, str]]]:
    """Race every open question group; the first to answer in full wins.

    One daemon thread per open child message long-polls the relay and pushes its
    result onto a queue. **The main thread owns everything else**: terminal
    detection, every state-store write, the escalation send, and the decision.

    Returns ``(group_key, {question: answer})`` for the winning group, or
    ``None`` when the terminal answered first, no group can be answered any
    more, or the TTL elapsed. ``None`` always means "native terminal UI stands".

    ``escalate_at`` (15-05) is an absolute deadline; when it is reached with the
    call still open, ``send_escalation`` is invoked **once** on this thread. It
    returns ``(group_key, records, token)`` for a freshly sent duplicate group,
    or ``None`` when the send failed — in which case the role group simply stays
    live and nothing is retried (brd §5.1). A group registered this way is added
    to ``groups`` / ``group_tokens`` **in place**, so the caller can finalize
    whichever group loses the race once this returns.

    Daemon threads are never joined: when the hook exits, threads still parked
    in a long-poll die with the process. ``stop`` only spares a doomed thread
    one more relay round trip.
    """
    results: "queue.Queue" = queue.Queue()
    stop = threading.Event()

    # message_id -> (group_key, record). Message ids are relay-global, so this
    # stays unambiguous with the escalation group live alongside the role one.
    index: Dict[int, Tuple[str, "_ChildRecord"]] = {}
    for group_key, records in groups.items():
        for rec in records:
            index[rec.message_id] = (group_key, rec)

    answers: Dict[str, Dict[str, str]] = {gk: {} for gk in groups}
    answered_ids: Dict[str, set] = {gk: set() for gk in groups}
    unanswerable: Dict[str, set] = {gk: set() for gk in groups}

    def _start_child(group_key: str, rec: "_ChildRecord") -> None:
        threading.Thread(
            target=_child_wait_loop,
            args=(
                group_key,
                rec.message_id,
                group_tokens.get(group_key),
                deadline,
                results,
                stop,
            ),
            name=f"askq-wait-{rec.message_id}",
            daemon=True,
        ).start()

    try:
        for group_key, records in groups.items():
            for rec in records:
                _start_child(group_key, rec)

        while time.time() < deadline:
            # 1. The terminal beats every group, always checked first.
            if _terminal_win(groups, group_tokens):
                return None

            # 2. The escalation deadline (15-05). Checked after terminal
            #    detection and before the drain, so an already-resolved call
            #    never sends a second group. Fires at most once: a failed send
            #    is logged and dropped, never retried — the role group is still
            #    the real destination and the operator can answer at the
            #    keyboard.
            if escalate_at is not None and time.time() >= escalate_at:
                escalate_at = None
                added = None
                if send_escalation is not None:
                    try:
                        added = send_escalation()
                    except Exception as e:  # noqa: BLE001 — never kill the wait
                        error_log(
                            f"Escalation send raised {type(e).__name__}: {e}; "
                            "leaving the role group live"
                        )
                if added is not None:
                    esc_key, esc_records, esc_token = added
                    groups[esc_key] = esc_records
                    group_tokens[esc_key] = esc_token
                    answers[esc_key] = {}
                    answered_ids[esc_key] = set()
                    unanswerable[esc_key] = set()
                    for rec in esc_records:
                        index[rec.message_id] = (esc_key, rec)
                        _start_child(esc_key, rec)
                    debug_log(
                        f"Escalation group {esc_key} sent "
                        f"({len(esc_records)} message(s)); both groups now live"
                    )

            # 3. Drain what the threads produced. ``get`` returns immediately
            #    while the queue is non-empty, so a group finalizing server-side
            #    (which wakes all of its children at once) drains across
            #    consecutive ticks without any of them waiting. The tick shrinks
            #    towards a pending escalation deadline — driving it off the queue
            #    rather than a sleep is what keeps that deadline from drifting by
            #    a whole long-poll chunk.
            tick = WAIT_TICK_SECONDS
            if escalate_at is not None:
                tick = min(
                    tick, max(ESCALATION_TICK_FLOOR, escalate_at - time.time())
                )
            try:
                _, message_id, result = results.get(timeout=tick)
            except queue.Empty:
                continue

            entry = index.get(message_id)
            if entry is None:
                continue
            # Use group_key from the index so the two cannot diverge if a
            # message id were ever reused across groups or a stale queue item
            # arrived after a group's re-registration.
            group_key, rec = entry

            if result is None:
                continue  # chunk timeout — that thread is still polling
            if isinstance(result, dict) and "_state" in result:
                debug_log(
                    f"Question {rec.child.request_id} relay state={result['_state']}"
                )
                unanswerable[group_key].add(message_id)
            else:
                decision = relay_answer_to_decision(rec.child, result)
                if not decision or decision.get("action") != "reply":
                    debug_log(f"Unexpected decision shape for question: {decision}")
                    unanswerable[group_key].add(message_id)
                else:
                    reply_text = decision.get("reply_text", "")
                    answers[group_key][(rec.question or {}).get("question", "")] = reply_text
                    answered_ids[group_key].add(message_id)
                    # Mark this child terminal so the PostToolUse hook's
                    # pending-request sweep won't later try to cancel its
                    # (already group-finalized) relay message.
                    update_request_state(
                        rec.child.request_id,
                        RequestState.REPLY,
                        reply_text=reply_text,
                        resolution_source=RESOLUTION_SOURCE_TELEGRAM,
                    )

            # 4. A group is won when every one of its children has answered.
            if (
                not unanswerable[group_key]
                and len(answered_ids[group_key]) == len(groups[group_key])
            ):
                return group_key, answers[group_key]

            # 5. A group with an expired/cancelled child can never finalize
            #    server-side (``_finalize_group_if_complete`` needs every
            #    member), so it is dead. **Per group** (15-05): one group dying
            #    is that group losing the race, not the call failing — the other
            #    may still win. Only when every registered group is dead does
            #    the call fall back to the terminal, exactly as the sequential
            #    loop did when there was only ever one group.
            if all(unanswerable[gk] for gk in groups):
                # ...**unless** the terminal is what killed them. PostToolUse
                # resolves the row and then cancels the relay message, so a
                # terminal win reaches this loop as a `cancelled` child — i.e.
                # disguised as group death. Checking here (not only at the top
                # of the next tick, which this `return` would never reach) is
                # what makes brd §5.5 hold: without it the loop falls back
                # silently and every role chat keeps its unpatched body, which
                # is exactly how G3 failed on 2026-08-06.
                if _terminal_win(
                    groups, group_tokens, grace_seconds=TERMINAL_GRACE_SECONDS
                ):
                    return None
                debug_log("Every question group is unanswerable; falling back")
                return None

        # The TTL is the other way out, and a terminal resolution can land in
        # the same tick that crosses it.
        if _terminal_win(groups, group_tokens):
            return None
        debug_log("AskUserQuestion wait phase reached the TTL deadline")
        return None
    finally:
        stop.set()


def handle_ask_user_question(
    session_id: str,
    cwd: str,
    tool_input: Dict[str, Any],
    workspace_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Handle AskUserQuestion: resolve role aliases, send each question to Telegram,
    race the terminal, build {question: answer} on success.

    Returns a hook output dict (with `updatedInput.answers`) on Telegram win,
    or None to fall through to terminal (which handles the native UI).
    """
    questions = tool_input.get('questions', [])
    if not questions:
        return None

    # ── Role resolution (epic 15 — lazy import so a missing/broken
    # roles_config degrades silently to the pre-roles path, not an exception) ──
    # Matching the deferred-import pattern already used in this function below.
    catalog = None
    _bindings = None
    _rc = None  # roles_config module reference (needed for retry path)
    try:
        import roles_config as _rc
        catalog = _rc.load_catalog(cwd)
        _bindings = _rc.load_bindings()
        # Log config errors once per call — never into the Telegram message body.
        if catalog is not None:
            for _err in catalog.errors:
                error_log(f"roles.toml: {_err}")
        if _bindings is not None:
            for _err in _bindings.errors:
                error_log(f"relay config: {_err}")
    except Exception as _e:  # noqa: BLE001
        error_log(f"roles_config load failed: {_e}; using legacy mode")
        catalog = None
        _bindings = None
        _rc = None

    # Per-question resolution: (original_q, alias|None, cleaned_header, Destination|None)
    # catalog is None  → legacy mode → destination=None for every question.
    # catalog is set   → destination is a Destination dataclass for every question.
    _per_q = []
    if catalog is not None and _rc is not None:
        for q in questions:
            header = q.get('header', '')
            alias, cleaned = _rc.parse_header_alias(header)
            dest = _rc.resolve_destination(catalog, _bindings, alias)
            _per_q.append((q, alias, cleaned, dest))

        # Mixed-role check: all questions in one call must resolve to the same
        # role_id.  Two unknown aliases both falling to the default are ONE role
        # and must pass (brd §5.3).  Skipped entirely in legacy mode.
        _role_ids = {entry[3].role_id for entry in _per_q}
        if len(_role_ids) > 1:
            # Collect one (alias, title) per distinct role_id (first occurrence).
            _seen: Dict[str, Any] = {}
            for _q, alias, _c, dest in _per_q:
                if dest.role_id not in _seen:
                    _seen[dest.role_id] = (alias, dest.title)
            _parts = []
            for _rid, (alias, title) in _seen.items():
                _tag = f"@{alias}" if alias else f"@{_rid}"
                _parts.append(f"{_tag} -> {title}")
            return {
                "action": "deny_mixed_roles",
                "reason": (
                    f"This AskUserQuestion call addressed {len(_role_ids)} different human roles "
                    f"({', '.join(_parts)}). "
                    "Each call must target exactly one role, because a question group cannot "
                    "span two Telegram chats. Split this into one AskUserQuestion call per role."
                ),
            }
    else:
        # Legacy mode: no alias parsing, no routing, destination=None for all.
        for q in questions:
            _per_q.append((q, None, q.get('header', ''), None))

    # Single destination for the whole call (None = legacy mode).
    destination = _per_q[0][3]

    # destination.token is None → roles are configured but this role is
    # unreachable on this machine (no binding, broken chain).  Distinct from
    # legacy mode (destination is None).  Return None → terminal only.
    if destination is not None and destination.token is None:
        error_log(
            f"Role '{destination.role_id}' is unreachable (no binding or broken chain); "
            "falling back to terminal"
        )
        return None

    # ── Send the question group ───────────────────────────────────────────────
    # token/role_title/notes for this attempt (may be updated on retry).
    token = destination.token if destination is not None else None
    role_title = destination.title if destination is not None else None
    role_id_for_store = destination.role_id if destination is not None else None
    notes = destination.notes if destination is not None else ()
    # Escalation delay for this attempt (15-05). ``resolve_destination`` already
    # returns None whenever there is nobody to escalate *to* — no policy
    # configured, the role IS the default, the role's binding resolves to the
    # default token anyway (same human), or the alias fell back to the default.
    # The send-failure retry below zeroes it for the same reason.
    escalate_after = destination.escalate_after if destination is not None else None

    # One child request per question. Each gets its own relay message and
    # state-store entry; relay button values ``qa<N>`` (or a free-text reply)
    # are mapped to the originating child via ``relay_answer_to_decision``.
    #
    # A shared ``group_id`` ties the sibling relay messages into one
    # re-answerable group: the relay keeps every question's buttons live (taps
    # just highlight the choice) until all are answered, then strips them all at
    # once. The hook still polls children in order — each only goes terminal
    # when the whole group finalizes, so the sequential loop below stays valid.
    group_id = uuid.uuid4().hex
    children: list  # list of _ChildRecord

    def _try_send_group(
        grp_id: str,
        _token: Optional[str],
        _role_title: Optional[str],
        _role_id: Optional[str],
        _notes: tuple,
        _banner: Optional[str] = None,
    ) -> Optional[list]:
        """Send all questions as one group; return list of _ChildRecord or None.

        On partial failure (one message fails): cancels already-sent siblings
        through ``_token`` and marks their rows terminal so posttool_hook's
        sweep leaves them alone.  Returns None on any failure.

        ``_banner`` (15-05) goes on **every** message of the group, not only the
        first: whichever one the operator opens has to explain itself.
        """
        records: list = []
        for i, (q, _alias, cleaned_header, _dest) in enumerate(_per_q):
            # Cleaned header (alias stripped) goes to Telegram; the raw header
            # stays in tool_input so updatedInput's native UI chip is untouched.
            _h = cleaned_header if destination is not None else q.get('header', '')
            _child = create_request(
                session_id=session_id,
                cwd=cwd,
                tool_name='AskUserQuestion',
                tool_input={
                    'question': q.get('question', ''),
                    'header': _h,
                    'options': q.get('options', []),
                    'multiSelect': q.get('multiSelect', False),
                },
                permission_suggestions=[],
                ttl_seconds=REQUEST_TTL,
                role=_role_id,
            )
            _body = telegram_router.render_question_body(
                _child, workspace_name, i, len(questions),
                role_title=_role_title, notes=_notes, banner=_banner,
            )
            _mid = send_question_message(
                _child, workspace_name, i, len(questions), grp_id,
                token=_token, role_title=_role_title, notes=_notes,
                banner=_banner,
            )
            if _mid is None:
                # Cancel already-sent siblings and mark their rows terminal.
                for rec in records:
                    try:
                        remove_inline_buttons(rec.message_id, token=_token)
                    except Exception as _ce:
                        debug_log(f"Failed to cancel sibling {rec.message_id}: {_ce}")
                    try:
                        update_request_state(
                            rec.child.request_id,
                            RequestState.DENY,
                            resolution_source=RESOLUTION_SOURCE_TIMEOUT,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return None
            records.append(_ChildRecord(child=_child, question=q, message_id=_mid, body=_body))
        return records

    children = _try_send_group(group_id, token, role_title, role_id_for_store, notes)

    # ── Send-failure fallback ─────────────────────────────────────────────────
    if children is None:
        # Was already targeting the default (or legacy mode)? Give up → terminal.
        if destination is None or destination.is_default:
            error_log(
                "Failed to send AskUserQuestion to default destination; "
                "falling back to terminal"
            )
            return None
        # Retry once against the default destination with a runtime-failure note.
        # The retry uses fresh request_ids and a fresh group_id so the relay's
        # idempotency key (req:{request_id}:send) cannot 422 on the changed body.
        _default_dest = _rc.resolve_destination(catalog, _bindings, None)
        if _default_dest.token is None:
            error_log(
                "Default destination unreachable after send failure; terminal only"
            )
            return None
        _retry_note = (
            f"Intended for {destination.title} — "
            "the relay could not deliver to them (not bound or send failed)."
        )
        token = _default_dest.token
        role_title = _default_dest.title
        role_id_for_store = _default_dest.role_id
        # The retry carries only the delivery-failure note; static notes from
        # the original resolution are irrelevant (the default is now the target).
        notes = (_retry_note,)
        # The default destination IS the target now, so there is nobody left to
        # escalate to (brd §5.4).
        escalate_after = None
        group_id = uuid.uuid4().hex
        children = _try_send_group(
            group_id, token, role_title, role_id_for_store, notes
        )
        if children is None:
            error_log(
                "Retry send to default also failed; falling back to terminal"
            )
            return None

    debug_log(f"Sent {len(children)} question messages; waiting for answers")

    # ── Escalation deadline (15-05) ───────────────────────────────────────────
    # Waiting is unbounded by default (brd §5.4, decision 5): with no
    # ``escalate_after`` configured nothing below changes observable behaviour.
    _start = time.time()
    _wait_deadline = _start + REQUEST_TTL
    _escalation_deadline: Optional[float] = None
    if escalate_after is not None and catalog is not None and _rc is not None:
        if escalate_after < REQUEST_TTL:
            _escalation_deadline = _start + escalate_after
        else:
            # A delay longer than the window we wait in would never fire; say so
            # rather than pretending escalation is armed.
            error_log(
                f"escalate_after ({escalate_after:g}s) exceeds the request TTL "
                f"({REQUEST_TTL}s); escalation suppressed for this call"
            )

    # What the escalation group needs to know once it has been sent: its
    # group_id (to recognise it as the winner) and the default role's title (the
    # attribution appended to the answers, brd §5.6).
    _escalation: Dict[str, Any] = {"group_id": None, "title": None}

    def _send_escalation_group() -> Optional[Tuple[str, list, Optional[str]]]:
        """Send a duplicate of this call to the **default role**.

        Not the router's implicit default client: the default role can carry its
        own explicit binding (brd §3.3), so ``resolve_destination(..., None)`` is
        the authority on both the token and the title.

        Returns ``(group_id, records, token)``, or ``None`` when the escalation
        could not be sent — the caller then leaves the role group live and does
        not retry.
        """
        try:
            _dest = _rc.resolve_destination(catalog, _bindings, None)
        except Exception as e:  # noqa: BLE001
            error_log(f"Could not resolve the escalation destination: {e}")
            return None
        if _dest.token is None:
            error_log(
                "Escalation destination (default role) is unreachable from this "
                "machine; leaving the role group live"
            )
            return None

        # The alias is the one the agent actually wrote (``@design`` if that is
        # what it used), paired with the role's title.
        _alias_tag = _per_q[0][1] or destination.role_id
        _banner = (
            f"@{_alias_tag} ({destination.title}) hasn't answered in "
            f"{_humanize_duration(escalate_after)} — you can decide instead."
        )
        # A new group_id and fresh request_ids: distinct ids give distinct
        # idempotency keys (``req:{request_id}:send``) and keep
        # ``set_telegram_message_id`` writing to the right row.
        _esc_group_id = uuid.uuid4().hex
        # ``role_title`` is the *default* role's — this copy really is addressed
        # to the operator. Reroute notes belong to the role group, not here.
        _records = _try_send_group(
            _esc_group_id, _dest.token, _dest.title, _dest.role_id, (), _banner
        )
        if _records is None:
            error_log(
                "Escalation send failed; the role group stays live and the "
                "escalation is not retried"
            )
            return None
        _escalation["group_id"] = _esc_group_id
        _escalation["title"] = _dest.title
        return _esc_group_id, _records, _dest.token

    # ── Wait phase ────────────────────────────────────────────────────────────
    # The role group starts alone; the escalation duplicate is registered into
    # these two dicts by the wait loop if and when its deadline is reached.
    groups: Dict[str, list] = {group_id: children}
    group_tokens: Dict[str, Optional[str]] = {group_id: token}

    won = _wait_for_group_answers(
        groups,
        group_tokens,
        _wait_deadline,
        escalate_at=_escalation_deadline,
        send_escalation=_send_escalation_group,
    )
    if won is None:
        # Terminal win, every group dead, or the TTL elapsed — the native UI is
        # live throughout, so there is never an auto-deny here.
        return None

    winning_key, answers = won

    # The loser (if any) gets the winning answers patched in and its keyboards
    # stripped; the winner finalized itself server-side.
    _finalize_losing_groups(groups, group_tokens, winning_key, answers)

    # brd §5.6: an escalated call answered by the default role rather than the
    # tagged one is attributed back to the agent, because the answer value is the
    # only channel it has. The chat gets the answer unannotated (above).
    if _escalation["group_id"] is not None and winning_key == _escalation["group_id"]:
        _suffix = f" (answered by {_escalation['title']})"
        answers = {q: f"{a}{_suffix}" for q, a in answers.items()}

    # All answered via Telegram — build updatedInput preserving the original
    # question structure exactly.  updatedInput is built from the *original*
    # tool_input so the raw ``@ux Layout`` header chips are untouched in the
    # terminal; the cleaned headers live only on the child rows sent to Telegram.
    return {
        'action': 'answer',
        'updatedInput': {
            **tool_input,
            'answers': answers,
        },
    }


def get_wait_before_telegram(tool_name: str) -> int:
    """
    Return pre-send delay before posting Telegram request.

    Send immediately for all tools to maximize response window.
    """
    return WAIT_BEFORE_TELEGRAM


def main():
    """Main hook entry point."""
    try:
        # Load Telegram configuration
        load_telegram_config()

        # Cleanup expired requests periodically
        cleanup_expired_requests()
        session_yolo_store.prune()

        # Read hook input from stdin
        raw_input = sys.stdin.read()
        debug_log(f"=== PermissionRequest Hook called ===")
        debug_log(f"Raw input: {raw_input[:500]}...")

        input_data = json.loads(raw_input)
        debug_log(f"Parsed input: {json.dumps(input_data, indent=2)}")

        # Extract key fields
        session_id = input_data.get('session_id', '')
        cwd = input_data.get('cwd', os.getcwd())
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})
        permission_suggestions = input_data.get('permission_suggestions', [])
        agent_id = input_data.get('agent_id') or None

        debug_log(f"Session: {session_id}")
        debug_log(f"CWD: {cwd}")
        debug_log(f"Tool: {tool_name}")
        debug_log(f"Agent ID: {agent_id}")
        debug_log(f"Tool input: {json.dumps(tool_input)[:200]}")
        debug_log(f"Permission suggestions: {permission_suggestions}")

        # If Telegram is not enabled, fall back to terminal immediately
        if not telegram_router.TELEGRAM_ENABLED:
            debug_log("Telegram not configured, falling back to terminal prompt")
            error_log(
                "Telegram disabled; skipping permission-request message. "
                "Check ~/.config/claude-tg-relay/config.toml exists and server_url is reachable. "
                "Run `relay-client config init --server-url URL --token TOKEN` then `relay-client bind`."
            )
            sys.exit(0)

        # AskUserQuestion takes a different shape (questions[] instead of a single
        # tool input) and resolves with `updatedInput.answers` instead of allow/deny.
        if tool_name == 'AskUserQuestion':
            workspace_name = get_workspace_name(cwd)
            decision = handle_ask_user_question(session_id, cwd, tool_input, workspace_name)
            output = build_output_decision(decision, request=None)  # type: ignore[arg-type]
            if output:
                debug_log(f"AskUserQuestion returning: {json.dumps(output)[:200]}")
                print(json.dumps(output), flush=True)
            else:
                debug_log("AskUserQuestion: no Telegram answer; native UI will handle")
            sys.exit(0)

        # Create request in state store
        request = create_request(
            session_id=session_id,
            cwd=cwd,
            tool_name=tool_name,
            tool_input=tool_input,
            permission_suggestions=permission_suggestions,
            ttl_seconds=REQUEST_TTL,
            agent_id=agent_id,
        )
        debug_log(f"Created request: {request.request_id}")

        # YOLO mode: the operator previously tapped YOLO for this session, so
        # auto-allow without sending a Telegram message or prompting. Recorded as
        # a normal ALLOW (resolution source telegram, since it stems from the
        # earlier button tap) for the audit trail.
        if session_yolo_store.is_enabled(session_id):
            debug_log(f"Session {session_id} in YOLO mode; auto-allowing {request.request_id}")
            update_request_state(
                request.request_id,
                RequestState.ALLOW,
                decision={"action": "yolo"},
                resolution_source=RESOLUTION_SOURCE_TELEGRAM,
            )
            print(json.dumps({
                'hookSpecificOutput': {
                    'hookEventName': 'PermissionRequest',
                    'decision': {'behavior': 'allow'},
                }
            }), flush=True)
            sys.exit(0)

        # Get session/workspace info for message
        workspace_name = get_workspace_name(cwd)
        session_name = get_session_name(session_id, cwd)

        wait_before = get_wait_before_telegram(tool_name)
        debug_log(f"Waiting {wait_before}s before sending Telegram message...")

        # Wait before sending Telegram message
        if wait_before > 0:
            time.sleep(wait_before)

        # Send Telegram message using the router.
        message_id = send_permission_message(
            request=request,
            workspace_name=workspace_name,
            session_name=session_name,
        )

        # A failed send is NOT a reason to bail: the native terminal prompt is
        # shown concurrently regardless, so an attended operator can still
        # approve at the CLI. We keep racing the terminal for the full TTL and,
        # only if nobody answers, auto-deny with a note (see below). ``None``
        # message_id routes wait_for_response through the state-store-only race.
        delivery_failed = message_id is None
        if delivery_failed:
            debug_log("Telegram send failed; racing the terminal up to the TTL before auto-deny")
            error_log(
                f"Failed to send Telegram permission message for request {request.request_id}; "
                f"racing the terminal for up to {REQUEST_TTL // 3600}h, will auto-deny if unanswered"
            )
        else:
            debug_log(f"Telegram message sent with ID: {message_id}")

        # Race relay long-poll (if delivered) against terminal resolution.
        decision = wait_for_response(
            request.request_id,
            message_id=message_id,
            ttl_seconds=REQUEST_TTL,
        )

        output = build_output_decision(decision, request)
        if output:
            debug_log(f"Returning decision: {json.dumps(output)}")
            print(json.dumps(output), flush=True)
            sys.exit(0)

        # No answer from Telegram. If the terminal resolved it, we're done —
        # Claude Code already acted, nothing to emit.
        current = get_request(request.request_id)
        if current and current.state == RequestState.RESOLVED_TERMINAL.value:
            debug_log("Resolved via terminal; exiting without a decision")
            sys.exit(0)

        # Genuinely unanswered within the TTL — delivered-but-ignored (e.g. an
        # unattended session over a 12h window) or never delivered. Fail safe:
        # auto-deny with an explanatory note so the agent isn't blocked forever
        # and an unattended session can move on. AskUserQuestion never reaches
        # here (it returns earlier and keeps the native UI indefinitely).
        debug_log(
            f"No response within TTL (delivery_failed={delivery_failed}); auto-denying"
        )
        _record_auto_deny(request.request_id)
        output = _auto_deny_output(request, delivery_failed)
        print(json.dumps(output), flush=True)
        sys.exit(0)

    except Exception as e:
        # On error, fall back to terminal (fail open)
        debug_log(f"ERROR: {type(e).__name__}: {str(e)}")
        debug_log(f"Traceback:\n{traceback.format_exc()}")
        error_log(f"Hook exception {type(e).__name__}: {e}")
        error_log(traceback.format_exc())
        # Exit 0 with no output means use default Claude behavior
        sys.exit(0)


if __name__ == '__main__':
    main()
