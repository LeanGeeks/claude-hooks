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
import sys
import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import telegram_permission_router as telegram_router

# Import the new modules
from permission_state_store import (
    PermissionRequest,
    RequestState,
    create_request,
    get_request,
    update_request_state,
    cleanup_expired_requests,
    RESOLUTION_SOURCE_TELEGRAM,
    RESOLUTION_SOURCE_TERMINAL,
    RESOLUTION_SOURCE_TIMEOUT,
)
from telegram_permission_router import (
    load_telegram_config,
    send_permission_message,
    send_question_message,
    process_whitelist_update,
    wait_for_relay_answer,
    relay_answer_to_decision,
    remove_inline_buttons,
    set_message_reaction,
)

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

    elif action == 'whitelist':
        # Process whitelist update
        success = process_whitelist_update(request, decision)
        if success:
            debug_log(f"Whitelist update successful for {request.request_id}")
        else:
            debug_log(f"Whitelist update failed for {request.request_id}")

        # Build whitelist with updated permissions
        updated_perms = decision.get('updatedPermissions', {})
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PermissionRequest',
                'decision': {
                    'behavior': 'allow',
                    'updatedPermissions': updated_perms
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
                    'reason': f"User reply: {reply_text}"
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
    "whitelist": RequestState.WHITELIST,
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
                'reason': reason,
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


def handle_ask_user_question(
    session_id: str,
    cwd: str,
    tool_input: Dict[str, Any],
    workspace_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Handle AskUserQuestion: send each question to Telegram, race the terminal,
    build {question: answer} on success.

    Returns a hook output dict (with `updatedInput.answers`) on Telegram win,
    or None to fall through to terminal (which handles the native UI).
    """
    questions = tool_input.get('questions', [])
    if not questions:
        return None

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
    children = []  # list of (PermissionRequest, question_dict, message_id)
    for i, q in enumerate(questions):
        child = create_request(
            session_id=session_id,
            cwd=cwd,
            tool_name='AskUserQuestion',
            tool_input={
                'question': q.get('question', ''),
                'header': q.get('header', ''),
                'options': q.get('options', []),
                'multiSelect': q.get('multiSelect', False),
            },
            permission_suggestions=[],
            ttl_seconds=REQUEST_TTL,
        )
        msg_id = send_question_message(child, workspace_name, i, len(questions), group_id)
        if not msg_id:
            error_log(f"Failed to send AskUserQuestion message for child {child.request_id}; falling back to terminal")
            return None
        children.append((child, q, msg_id))

    debug_log(f"Sent {len(children)} question messages; polling for answers")

    # Race the relay long-poll for each child against the local state store's
    # ``resolved_terminal`` signal. Process children in order so the UI feels
    # sequential; the relay still attributes button taps to the right message.
    answers: Dict[str, str] = {}
    deadline = time.time() + REQUEST_TTL
    for child, q, child_msg_id in children:
        if time.time() >= deadline:
            return None
        # Tight inner loop: short relay long-poll chunks interleaved with a
        # terminal-resolution check on the local state store.
        resolved = False
        while time.time() < deadline and not resolved:
            # The PostToolUse hook only flips the *most recent* pending child to
            # resolved_terminal (find_pending_request_by_tool_session returns one
            # row), but that signals the whole AskUserQuestion was answered in the
            # terminal. So detect the terminal state on ANY child — not just the
            # one we happen to be polling — otherwise a loop parked on an earlier
            # child never fires and that child's keyboard is left live.
            if any(
                (st := get_request(c.request_id))
                and st.state == RequestState.RESOLVED_TERMINAL.value
                for c, _cq, _cm in children
            ):
                debug_log("AskUserQuestion resolved via terminal; revoking all messages")
                from permission_state_store import resolve_via_terminal
                for sib, _sq, sib_msg in children:
                    sib_state = get_request(sib.request_id)
                    if sib_state and sib_state.state == RequestState.PENDING.value:
                        resolve_via_terminal(sib.request_id)
                    # Strip the keyboard on every sibling (idempotent for the one
                    # the posttool hook already revoked).
                    try:
                        remove_inline_buttons(sib_msg)
                        set_message_reaction(sib_msg, '✅')
                    except Exception as e:
                        debug_log(f"Failed to revoke message {sib_msg}: {e}")
                return None
            chunk = min(25, max(1, int(deadline - time.time())))
            answer = wait_for_relay_answer(
                child_msg_id, timeout=chunk, long_poll_chunk=chunk
            )
            if answer is None:
                continue
            if "_state" in answer:
                # The relay marked this message expired/cancelled — fall back.
                debug_log(
                    f"Question {child.request_id} relay state={answer['_state']}; falling back"
                )
                return None
            decision = relay_answer_to_decision(child, answer)
            if not decision or decision.get('action') != 'reply':
                debug_log(f"Unexpected decision shape for question: {decision}")
                return None
            answers[q.get('question', '')] = decision.get('reply_text', '')
            # Mark this child terminal so the PostToolUse hook's pending-request
            # sweep won't later try to cancel its (already group-finalized)
            # relay message.
            update_request_state(
                child.request_id,
                RequestState.REPLY,
                reply_text=decision.get('reply_text', ''),
                resolution_source=RESOLUTION_SOURCE_TELEGRAM,
            )
            resolved = True

        if not resolved:
            return None

    # All answered via Telegram — build updatedInput preserving original question structure.
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

        debug_log(f"Session: {session_id}")
        debug_log(f"CWD: {cwd}")
        debug_log(f"Tool: {tool_name}")
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
        )
        debug_log(f"Created request: {request.request_id}")

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
