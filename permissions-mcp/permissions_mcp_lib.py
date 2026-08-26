#!/usr/bin/env python3
"""Logic behind the ``permissions`` MCP server (epic 22, task 22-03).

Everything that does not need the ``mcp`` package lives here so the behaviour
can be unit-tested with the repo's plain-``python3`` suite while ``server.py``
stays a thin tool-registration shell (it is a ``uv`` single-file script and its
dependency is only installed inside uv's environment).

Three things this module is responsible for:

* **Reading** current and historical permission requests — always through
  ``permission_state_store`` (epic 22 invariant 6 applies to readers as well as
  writers), never by parsing ``permission_requests.jsonl`` here.
* **Classifying** a row into the D3 tier with the *same* ``BashPermissionValidator``
  the PreToolUse hook runs, re-run against the **row's** ``cwd`` settings
  (invariant 2 / brd H5). ``classify`` is the single implementation; ``list``'s
  ``decidable`` field and ``decide``'s tier check are its two callers.
* **Deciding** a row, behind the row-shaped D5 guard, by writing the exact
  decision dict the 22-02 wait loop adopts.
* **Writing allowlist patterns** (task 22-04) — the caller's own checkout
  directly, every other project through the proposal queue, with the project
  identity coming from ``project_key.resolve_project_key`` and nothing else
  (invariant 7).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Hook imports
#
# The server runs from the checkout (the installer registers ``server.py`` in
# place, like context-mcp), so importing the hook modules from the repo keeps
# the classifier in lockstep with the repo's validator. The *stores* on disk are
# shared with the installed hooks — same JSONL format by construction, because
# it is the same module reading and writing them.
#
# ``CLAUDE_HOOKS_REPO`` is exported by the installer's MCP registration; it wins
# when present so a relocated checkout still resolves, with the path relative to
# this file as the fallback.
# ---------------------------------------------------------------------------

def _resolve_hooks_dir() -> Path:
    env_repo = os.environ.get("CLAUDE_HOOKS_REPO")
    if env_repo:
        candidate = Path(env_repo).expanduser() / ".claude" / "hooks"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / ".claude" / "hooks"


HOOKS_DIR = _resolve_hooks_dir()
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from bash_command_parser import BashCommandParser  # noqa: E402
from permission_state_store import (  # noqa: E402
    AuditEntry,
    PermissionRequest,
    RESOLUTION_SOURCE_AGENT,
    RequestState,
    TERMINAL_STATES,
    append_agent_decision_reason,
    get_all_pending_requests,
    get_request,
    get_requests,
    update_request_state,
)
from project_key import (  # noqa: E402
    resolve_project_key,
    resolve_project_root,
    resolve_workspace_root,
)
from pretool_hook import MANUAL_CONFIRM_LOG, BashPermissionValidator  # noqa: E402
from settings_loader import SettingsLoader  # noqa: E402
from settings_writer import VERSIONED_SETTINGS, add_permission_pattern  # noqa: E402

# Sibling module (same directory as this file, which ``server.py`` puts on
# sys.path): the proposal queue. It never resolves a project key itself — the
# keys it is handed come from ``resolve_project_key`` above (invariant 7).
import permission_queue  # noqa: E402


# Tools whose ``tool_input['command']`` is a shell command the validator can
# classify. Mirrors the PreToolUse hook's own gate (``pretool_hook.main``).
COMMAND_BEARING_TOOLS = ("Bash", "Monitor")

# Questions are answered by humans, never decided by an agent (brd §3.2).
QUESTION_TOOL = "AskUserQuestion"

# 22-01's tier vocabulary is read off a **fresh** validator run, never off the
# row's stored reason, and structurally wherever possible: the deny and ask tiers
# come from that run's ``decision`` plus its per-sub-command
# ``matched_deny_patterns`` / ``matched_ask_patterns``, not from prose. The one
# branch with no structured marker is the redirect escape (the validator reports
# it as an ``ask`` with no matched patterns), so that single branch is told apart
# by its reason prefix — still off the fresh run, still the one classifier.
REDIRECT_REASON_PREFIX = "Redirects output outside the workspace"

# The action vocabulary ``decide`` accepts, and the terminal state each writes.
# Deliberately not ``reply``/``whitelist``: those have their own semantics and
# their own writer paths, and 22-02's wait loop only adopts these three.
ACTION_TO_STATE = {
    "allow": RequestState.ALLOW,
    "deny": RequestState.DENY,
    "stop": RequestState.STOP,
}

# H3: the tool records a decision, it does not run anything. If the waiting hook
# process is gone the row simply reads as decided and no tool ever runs.
DECISION_RECORDED_STATUS = (
    "decision recorded; the waiting hook applies it within ~30 s. "
    "This tool does not run the tool call — if the waiting hook is gone "
    "(session killed, machine slept) nothing runs and the row just reads as decided."
)


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerIdentity:
    """Who is calling, resolved once from the server process env at startup.

    ``session_id`` is ``None`` when the server was not started by a Claude Code
    session. That is the fail-closed case: read tools keep working, ``decide``
    refuses every call, because a guard that cannot identify its caller cannot
    enforce invariant 3.
    """

    session_id: Optional[str]
    project_dir: str
    actor_agent: Optional[str]

    @property
    def can_decide(self) -> bool:
        return bool(self.session_id)


def resolve_caller_identity(env: Optional[Dict[str, str]] = None) -> CallerIdentity:
    """Compose the caller identity from the process environment.

    Follows the context-mcp precedent (``context-mcp/server.py:47-48``):
    ``CLAUDE_CODE_SESSION_ID`` plus ``CLAUDE_PROJECT_DIR``/``PWD``.
    """
    environ = os.environ if env is None else env
    session_id = (environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None
    project_dir = (
        environ.get("CLAUDE_PROJECT_DIR")
        or environ.get("PWD")
        or os.getcwd()
    )
    actor_agent = None
    if session_id:
        actor_agent = f"{session_id[:12]} @ {os.path.basename(project_dir.rstrip('/')) or project_dir}"
    return CallerIdentity(
        session_id=session_id, project_dir=project_dir, actor_agent=actor_agent
    )


# ---------------------------------------------------------------------------
# Classification (§4) — ONE implementation, two callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """The D3 tier verdict for a single row."""

    decidable: bool
    tier_reason: str
    matched_patterns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decidable": self.decidable,
            "tier_reason": self.tier_reason,
            "matched_patterns": list(self.matched_patterns),
        }


def _build_validator(cwd: str) -> BashPermissionValidator:
    """The one classifier, bound to the **row's** workspace settings.

    H4: ``SettingsLoader`` caches merged settings for 60 s at class level. In a
    per-invocation hook that is invisible; in this long-lived server a
    classification can lag a settings edit by up to a minute.
    """
    workspace_dir = cwd or os.getcwd()
    return BashPermissionValidator(
        SettingsLoader(workspace_dir), BashCommandParser(), workspace_dir
    )


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _tool_level_ask_matches(
    ask_patterns: Iterable[str], tool_name: str, arguments_are_matchable: bool
) -> List[str]:
    """Ask patterns that target a whole tool rather than a command.

    ``BashPermissionValidator._matches_pattern`` only understands ``Bash(...)``
    forms matched against a command string, so two forms are outside its reach
    and are resolved here by reading the pattern's *head* — pattern namespacing,
    not a second command matcher:

    * the bare ``Tool`` form (``"Bash"``, ``"WebFetch"``) always names the whole
      tool;
    * the parenthesised ``Tool(...)`` form counts only when this row's arguments
      are **not** matchable (a non-command tool). We cannot evaluate, say,
      ``WebFetch(domain:example.com)`` — Claude Code does that natively — so the
      safe direction is to treat the row as human-only whenever an ask entry
      targets its tool at all. For Bash/Monitor rows the parenthesised forms are
      the validator's job and are deliberately left to it.
    """
    matches: List[str] = []
    for pattern in ask_patterns:
        if pattern == tool_name:
            matches.append(pattern)
        elif not arguments_are_matchable and pattern.startswith(tool_name + "("):
            matches.append(pattern)
    return _dedupe(matches)


def classify(row: PermissionRequest) -> Classification:
    """Classify a row into the D3 tier: may an agent decide it?

    Called by ``list_permission_requests`` (for its ``decidable`` field) and by
    ``decide_permission_request`` (for its tier check) — one implementation, two
    callers (brd H5). The verdict is produced by re-running the imported
    ``BashPermissionValidator`` against the row's ``cwd`` settings; no matching
    logic is reimplemented here.

    Note this is the *tier* only. The D5 caller guard is separate (``guard_row``)
    because it depends on who is asking, not on what the row contains.
    """
    tool_name = row.tool_name or ""
    if tool_name == QUESTION_TOOL:
        return Classification(
            False,
            "questions are answered by humans; see brd §3.2",
            [],
        )

    tool_input = row.tool_input if isinstance(row.tool_input, dict) else {}
    command = tool_input.get("command") if tool_name in COMMAND_BEARING_TOOLS else None

    try:
        validator = _build_validator(row.cwd)
    except Exception as exc:  # noqa: BLE001 — a classifier that cannot run is human-only.
        return Classification(
            False,
            f"human-only: could not load the row's workspace settings to classify it ({exc})",
            [],
        )

    whole_tool_asks = _tool_level_ask_matches(
        validator.ask_patterns, tool_name, arguments_are_matchable=bool(command)
    )
    if whole_tool_asks:
        return Classification(
            False,
            "human-only: an ask pattern targets the whole tool "
            f"({', '.join(whole_tool_asks)})",
            whole_tool_asks,
        )

    if command is None:
        # Non-command tool with no ask entry naming it: agent-decidable
        # (state.md default 3 — tightening means adding ask entries, not code).
        return Classification(
            True,
            f"no ask pattern matches the tool name {tool_name!r} — agent-decidable",
            [],
        )

    return _classify_command(validator, command)


def _classify_command(
    validator: BashPermissionValidator, command: str
) -> Classification:
    """Tier a shell command by re-running the validator on it, right now."""
    try:
        fresh = validator.validate_bash_command(command)
    except Exception as exc:  # noqa: BLE001 — an unclassifiable command is human-only.
        return Classification(
            False, f"human-only: the validator could not classify this command ({exc})", []
        )

    decision = fresh.get("decision")
    reason = fresh.get("reason", "")
    results = fresh.get("validation_results", []) or []

    def _patterns(key: str) -> List[str]:
        return _dedupe(
            pattern
            for result in results
            for pattern in (result.get(key) or [])
        )

    if decision == "deny":
        denied = _patterns("matched_deny_patterns")
        return Classification(
            False,
            "human-only: now matches a deny pattern — the request predates a "
            "settings change; let it expire or have a human act. " + reason,
            denied,
        )

    if decision == "ask":
        asked = _patterns("matched_ask_patterns")
        if asked:
            return Classification(
                False,
                "human-only: " + reason + f" (ask patterns: {', '.join(asked)})",
                asked,
            )
        if reason.startswith(REDIRECT_REASON_PREFIX):
            return Classification(False, "human-only: " + reason, [])
        return Classification(True, "agent-decidable: " + reason, [])

    if decision == "allow":
        return Classification(
            True,
            "agent-decidable: every sub-command is allowlisted in the row's "
            "workspace now (the request predates a settings change)",
            _patterns("matched_allow_patterns"),
        )

    return Classification(
        False,
        f"human-only: unexpected validator decision {decision!r} — {reason}",
        [],
    )


# ---------------------------------------------------------------------------
# The D5 guard (invariant 3) — row-shaped, not session-shaped
# ---------------------------------------------------------------------------


def guard_row(row: PermissionRequest, identity: CallerIdentity) -> Optional[str]:
    """Return a refusal string, or ``None`` when the caller may decide this row.

    Exactly one shape is refused: the caller's **own** session's **main-agent**
    row (``agent_id is None``). Same-session rows that carry an ``agent_id`` are
    a parent deciding its subagent's request — the primary use case (brd D5).
    Rows from other sessions are decidable; cross-session collusion is not
    blockable structurally, it is the trust the global allowlist grants.
    """
    if not identity.can_decide:
        return (
            "refused: this MCP server has no CLAUDE_CODE_SESSION_ID, so it cannot "
            "identify its caller and cannot enforce the self-decision guard. "
            "Start it from a Claude Code session to decide requests (read tools "
            "keep working)."
        )
    if row.session_id == identity.session_id and row.agent_id is None:
        return (
            "refused: you may not decide your own session's main-agent request "
            f"(session {row.session_id}, agent_id null). A parent may decide a "
            "subagent's row (one that carries an agent_id), and any row from "
            "another session."
        )
    return None


# ---------------------------------------------------------------------------
# Workspace filter
# ---------------------------------------------------------------------------


def _normalize_workspace(workspace: str) -> str:
    return os.path.abspath(os.path.expanduser(workspace)).rstrip("/") or "/"


def _matches_workspace(cwd: Optional[str], workspace: Optional[str]) -> bool:
    """Prefix-match a row's ``cwd`` against a workspace filter.

    22-04 introduces ``resolve_project_key`` (one key per repo, every worktree
    included); until it lands this is a plain path-prefix match, which already
    covers "this workspace and anything under it".
    """
    if not workspace:
        return True
    if not cwd:
        return False
    target = _normalize_workspace(workspace)
    candidate = _normalize_workspace(cwd)
    return candidate == target or candidate.startswith(target + "/")


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------


def _row_command(row: PermissionRequest) -> Optional[str]:
    tool_input = row.tool_input if isinstance(row.tool_input, dict) else {}
    if (row.tool_name or "") in COMMAND_BEARING_TOOLS:
        return tool_input.get("command")
    return None


def summarize_row(
    row: PermissionRequest, identity: Optional[CallerIdentity] = None
) -> Dict[str, Any]:
    """Compact JSON view of a row, with its live tier verdict.

    The ``decidable`` field is computed here so an orchestrator never has to
    call ``decide`` just to learn that a request is human-only.
    """
    verdict = classify(row)
    summary: Dict[str, Any] = {
        "request_id": row.request_id,
        "session_id": row.session_id,
        "agent_id": row.agent_id,
        "cwd": row.cwd,
        "tool_name": row.tool_name,
        "kind": "question" if (row.tool_name or "") == QUESTION_TOOL else "permission",
        "state": row.state,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "decidable": verdict.decidable,
        "decidable_reason": verdict.tier_reason,
        "matched_patterns": verdict.matched_patterns,
    }
    command = _row_command(row)
    if command is not None:
        summary["command"] = command
    if row.state != RequestState.PENDING.value:
        summary["resolution_source"] = row.resolution_source
        summary["resolved_at"] = row.resolved_at
        summary["actor_agent"] = row.actor_agent
        summary["actor_user_id"] = row.actor_user_id
        summary["decision"] = row.decision
    if identity is not None:
        refusal = guard_row(row, identity)
        summary["caller_guard"] = "ok" if refusal is None else refusal
    return summary


def _describe_resolver(row: PermissionRequest) -> str:
    """Who resolved a row, and how — for the non-pending refusal."""
    source = row.resolution_source or "unknown source"
    if row.actor_agent:
        who = f"agent {row.actor_agent}"
    elif row.actor_user_id is not None:
        who = f"Telegram user {row.actor_user_id}"
    else:
        who = "no recorded actor"
    when = row.resolved_at or row.updated_at
    return f"state {row.state!r} via {source} ({who}) at {when}"


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def _parse_state_filter(state: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Translate the ``state`` argument into store state values.

    Returns ``(states, error)``. ``states is None`` with no error means "every
    state" (the ``all`` keyword).
    """
    raw = (state or "pending").strip()
    if raw.lower() == "all":
        return None, None
    wanted = [part.strip() for part in raw.split(",") if part.strip()]
    valid = {member.value for member in RequestState}
    unknown = [part for part in wanted if part not in valid]
    if unknown:
        return None, (
            f"unknown state(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(valid))}, or 'all'."
        )
    return wanted, None


def list_permission_requests(
    state: str = "pending",
    workspace: Optional[str] = None,
    identity: Optional[CallerIdentity] = None,
) -> Dict[str, Any]:
    """List permission requests from the state store, with tier verdicts."""
    states, error = _parse_state_filter(state)
    if error:
        return {"error": error}

    if states == [RequestState.PENDING.value]:
        # The default path: skips rows whose TTL has lapsed (and marks them).
        rows = get_all_pending_requests()
    else:
        rows = get_requests(states=states)

    rows = [row for row in rows if _matches_workspace(row.cwd, workspace)]
    rows.sort(key=lambda r: r.created_at or "", reverse=True)
    return {
        "state_filter": state,
        "workspace_filter": workspace,
        "count": len(rows),
        "requests": [summarize_row(row, identity) for row in rows],
    }


def get_permission_request(
    request_id: str, identity: Optional[CallerIdentity] = None
) -> Dict[str, Any]:
    """The full row plus its live classification, patterns spelled out."""
    row = get_request(request_id)
    if row is None:
        return {
            "error": f"no request {request_id!r} in the state store "
            "(unknown id, or a pending row whose TTL has lapsed)"
        }
    result: Dict[str, Any] = {"request": row.to_dict(), "classification": classify(row).to_dict()}
    if identity is not None:
        refusal = guard_row(row, identity)
        result["caller_guard"] = "ok" if refusal is None else refusal
    return result


def _manual_confirm_log_path() -> Path:
    """Where ``log_manual_confirmation`` writes.

    The default is the hook module's own constant, so the two can never drift;
    the env override exists so tests (and any sandboxed run) can read a scratch
    log instead of the developer's real one.
    """
    override = os.environ.get("CLAUDE_MANUAL_CONFIRM_LOG")
    return Path(override) if override else Path(MANUAL_CONFIRM_LOG)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_manual_confirm_log(
    cutoff: datetime, workspace: Optional[str], include_auto_denied: bool
) -> List[Dict[str, Any]]:
    """Timestamp-ranged entries from ``bash_manual_confirm.log``.

    This is the "why did this prompt" feed: every command the validator did not
    auto-approve, with its per-sub-command validation results.
    """
    path = _manual_confirm_log_path()
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = _parse_ts(entry.get("timestamp"))
                if timestamp is None or timestamp < cutoff:
                    continue
                if not _matches_workspace(entry.get("workspace"), workspace):
                    continue
                if not include_auto_denied and entry.get("decision") == "deny":
                    continue
                entries.append(entry)
    except OSError:
        # Fail open on a read error: an unreadable log must not sink the whole
        # history call, the store rows are still worth returning.
        return entries
    entries.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return entries


def permission_history(
    days: float = 1,
    workspace: Optional[str] = None,
    include_auto_denied: bool = True,
) -> Dict[str, Any]:
    """The daily reviewer's feed: terminal store rows joined with the
    manual-confirmation log over the same window.

    ``include_auto_denied=False`` drops the two classes nobody actually decided:
    store rows resolved by ``timeout`` (the TTL auto-deny / expiry) and
    ``bash_manual_confirm.log`` entries the validator hard-denied (D1) — the
    ones that never became a prompt at all.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=float(days))
    terminal_states = [state.value for state in TERMINAL_STATES]
    rows = get_requests(states=terminal_states, since=cutoff.isoformat())
    rows = [row for row in rows if _matches_workspace(row.cwd, workspace)]
    if not include_auto_denied:
        rows = [
            row
            for row in rows
            if row.resolution_source != "timeout"
        ]
    rows.sort(key=lambda r: (r.resolved_at or r.updated_at or ""), reverse=True)

    confirm_entries = _read_manual_confirm_log(cutoff, workspace, include_auto_denied)

    return {
        "since": cutoff.isoformat(),
        "days": days,
        "workspace_filter": workspace,
        "include_auto_denied": include_auto_denied,
        "requests": [
            {
                "request_id": row.request_id,
                "session_id": row.session_id,
                "agent_id": row.agent_id,
                "cwd": row.cwd,
                "tool_name": row.tool_name,
                "command": _row_command(row),
                "state": row.state,
                "decision": row.decision,
                "resolution_source": row.resolution_source,
                "actor_agent": row.actor_agent,
                "actor_user_id": row.actor_user_id,
                "created_at": row.created_at,
                "resolved_at": row.resolved_at,
            }
            for row in rows
        ],
        "request_count": len(rows),
        "confirm_log": confirm_entries,
        "confirm_log_count": len(confirm_entries),
    }


# ---------------------------------------------------------------------------
# Decide tool
# ---------------------------------------------------------------------------


def _refusal(message: str, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "refused": True, "reason": message}
    result.update(extra)
    return result


def _record_decision_reason(
    row: PermissionRequest,
    action: str,
    reason: str,
    verdict: Classification,
    identity: CallerIdentity,
) -> None:
    """Put the caller's justification into the audit trail.

    ``update_request_state`` writes its own audit entry (state transition,
    decision dict, ``actor_agent``) but has no channel for a free-text reason,
    and the decision dict itself must stay exactly ``{"action": ...}`` — that is
    the shape 22-02's wait loop adopts. So the justification goes in as its own
    ``agent_decision`` entry via the store's public ``append_agent_decision_reason``
    wrapper (invariant 6: no bespoke JSONL writers, no reaching into private store
    internals). An unexplained approval is not auditable.
    """
    append_agent_decision_reason(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            request_id=row.request_id,
            action="agent_decision",
            actor_user_id=None,
            previous_state=RequestState.PENDING.value,
            new_state=ACTION_TO_STATE[action].value,
            details={
                "decision": {"action": action},
                "reason": reason,
                "resolution_source": RESOLUTION_SOURCE_AGENT,
                "tier": verdict.tier_reason,
                "matched_patterns": verdict.matched_patterns,
                "caller_session_id": identity.session_id,
            },
            actor_agent=identity.actor_agent,
        )
    )


def decide_permission_request(
    request_id: str,
    action: str,
    reason: str,
    identity: Optional[CallerIdentity] = None,
) -> Dict[str, Any]:
    """Decide a pending permission request on behalf of an agent."""
    caller = identity if identity is not None else resolve_caller_identity()

    if not caller.can_decide:
        return _refusal(
            "refused: this MCP server has no CLAUDE_CODE_SESSION_ID, so it cannot "
            "identify its caller and cannot enforce the self-decision guard "
            "(epic 22 invariant 3). Read tools still work; decisions do not.",
            request_id=request_id,
        )

    normalized_action = (action or "").strip().lower()
    if normalized_action not in ACTION_TO_STATE:
        return _refusal(
            f"refused: action must be one of {', '.join(sorted(ACTION_TO_STATE))} "
            f"(got {action!r}).",
            request_id=request_id,
        )

    if not (reason or "").strip():
        return _refusal(
            "refused: a reason is required — an unexplained approval is not "
            "auditable. Say why this request should be allowed/denied/stopped.",
            request_id=request_id,
        )

    row = get_request(request_id)
    if row is None:
        return _refusal(
            f"refused: no request {request_id!r} in the state store (unknown id, "
            "or a pending row whose TTL has lapsed and is now expired).",
            request_id=request_id,
        )

    if row.state != RequestState.PENDING.value:
        return _refusal(
            f"refused: request {request_id} is no longer pending — "
            + _describe_resolver(row),
            request_id=request_id,
            state=row.state,
            resolution_source=row.resolution_source,
            actor_agent=row.actor_agent,
            actor_user_id=row.actor_user_id,
        )

    guard_refusal = guard_row(row, caller)
    if guard_refusal is not None:
        return _refusal(guard_refusal, request_id=request_id)

    verdict = classify(row)
    if not verdict.decidable:
        return _refusal(
            f"refused: {verdict.tier_reason}",
            request_id=request_id,
            matched_patterns=verdict.matched_patterns,
            tier="human-only",
        )

    new_state = ACTION_TO_STATE[normalized_action]
    updated = update_request_state(
        request_id,
        new_state,
        decision={"action": normalized_action},
        actor_agent=caller.actor_agent,
        resolution_source=RESOLUTION_SOURCE_AGENT,
    )
    if updated is None:
        current = get_request(request_id)
        detail = _describe_resolver(current) if current else "the row is gone or expired"
        return _refusal(
            f"refused: lost the race — request {request_id} was resolved by "
            f"someone else between the check and the write ({detail}). "
            "Nothing was written.",
            request_id=request_id,
        )

    _record_decision_reason(updated, normalized_action, reason.strip(), verdict, caller)

    return {
        "ok": True,
        "request_id": request_id,
        "action": normalized_action,
        "state": updated.state,
        "decision": updated.decision,
        "resolution_source": updated.resolution_source,
        "actor_agent": updated.actor_agent,
        "reason": reason.strip(),
        "tier": verdict.tier_reason,
        "status": DECISION_RECORDED_STATUS,
    }


# ---------------------------------------------------------------------------
# Allowlist writing + the proposal queue (task 22-04)
#
# Two destinations, one rule (brd D6 / invariant 4): a pattern for the checkout
# this agent is running in is written straight into that checkout's versioned
# ``.claude/settings.json``; a pattern for **any other** project is filed as a
# queue entry for the agent that lives there. Nothing in this section opens a
# file inside a foreign checkout — not to edit, not to commit, not to push.
# ---------------------------------------------------------------------------


# Pattern shapes Claude Code understands: a bare tool name (``Bash``,
# ``WebFetch``, ``mcp__permissions__*``) or the parenthesised form
# (``Bash(git log:*)``, ``WebFetch(domain:example.com)``). Free text is refused —
# a sentence in the allow list is silently inert, which is worse than an error.
_BARE_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*\*?$")
_PAREN_TOOL_RE = re.compile(r"^(?P<tool>[A-Za-z][A-Za-z0-9_.-]*)\((?P<inner>.+)\)$", re.DOTALL)

# Every user-scope answer carries this (brd H6 / invariant 8): the repo's
# settings file is the user-scoped allowlist, but it only reaches other
# workspaces through the installer's merge.
H6_NOTE = (
    "H6: user-scope patterns live in the claude-hooks repo's .claude/settings.json. "
    "That is live in the claude-hooks workspace immediately, but it does NOT reach "
    "any other workspace until install-claude-config.sh re-runs its merge into "
    "~/.claude/settings.json (the daily reviewer runs it when it applies user-scope "
    "changes)."
)

QUEUED_STATUS = (
    "proposal queued for {key}; the workspace's scheduled reviewer applies it "
    "(nothing is written in that checkout by this call — brd D6)"
)

SCOPES = ("workspace", "user")


@dataclass(frozen=True)
class ParsedPattern:
    """A permission pattern split into tool and (optional) argument part."""

    raw: str
    tool: str
    inner: Optional[str]

    @property
    def is_bash(self) -> bool:
        return self.tool == "Bash"

    def representative_command(self) -> Optional[str]:
        """The shortest command this Bash pattern would grant.

        ``Bash(git log:*)`` → ``git log``; ``Bash(pwd)`` → ``pwd``. Used to ask
        the validator whether a deny entry already covers the whole proposal.
        """
        if not self.is_bash or self.inner is None:
            return None
        if self.inner.endswith(":*"):
            return self.inner[:-2]
        return self.inner


def parse_pattern(pattern: str) -> Optional[ParsedPattern]:
    """Parse a permission pattern, or return ``None`` if it is not one."""
    text = (pattern or "").strip()
    if not text or "\n" in text:
        return None
    paren = _PAREN_TOOL_RE.match(text)
    if paren:
        inner = paren.group("inner").strip()
        if not inner:
            return None
        return ParsedPattern(raw=text, tool=paren.group("tool"), inner=inner)
    if _BARE_TOOL_RE.match(text):
        return ParsedPattern(raw=text, tool=text, inner=None)
    return None


def _deny_subsumes(proposal: ParsedPattern, deny: str, validator: BashPermissionValidator) -> bool:
    """Would ``deny`` override everything ``proposal`` grants?

    Only full coverage counts. A deny that carves a slice out of a broader
    proposal (deny ``Bash(git push:*)`` vs proposal ``Bash(git:*)``) is normal
    layering, not a collision — the proposal still widens the rest.
    """
    deny_text = (deny or "").strip()
    if not deny_text:
        return False
    if deny_text == proposal.raw:
        return True

    parsed_deny = parse_pattern(deny_text)
    if parsed_deny is None:
        return False

    if parsed_deny.inner is None:
        # A bare-tool deny (or a wildcard one like ``mcp__permissions__*``)
        # covers the whole tool, hence the whole proposal.
        return fnmatch.fnmatchcase(proposal.tool, parsed_deny.tool) or fnmatch.fnmatchcase(
            proposal.raw, parsed_deny.raw
        )

    if not fnmatch.fnmatchcase(proposal.tool, parsed_deny.tool):
        return False
    if proposal.inner is None:
        # ``Bash`` proposed wholesale against a narrower ``Bash(...)`` deny:
        # layering, not a no-op.
        return False

    if proposal.is_bash:
        command = proposal.representative_command()
        if not command:
            return False
        # The validator's own pattern matcher, asked the same question its deny
        # loop asks — no second implementation of ``Bash(...)`` matching
        # (invariant 2). Deliberately the matcher and not
        # ``validate_bash_command``: the full validation path short-circuits on
        # shapes like a workspace-local ``rm`` before it ever consults the deny
        # list, and a collision missed that way would be written as a silent
        # no-op allow entry.
        try:
            return bool(validator._matches_pattern(command, deny_text))
        except Exception:  # noqa: BLE001 — an unmatchable representative is not a collision.
            return False

    # Non-Bash tools: Claude Code matches their argument syntax natively, so the
    # only claim made here is the textual glob one — enough for the exact and
    # ``Tool(prefix:*)`` cases that occur in practice.
    return fnmatch.fnmatchcase(proposal.inner, parsed_deny.inner)


def find_deny_collision(
    pattern: ParsedPattern, workspace_dir: str
) -> Tuple[Optional[str], List[str]]:
    """The deny entry that already covers ``pattern`` in ``workspace_dir``, if any.

    Reads the **target's** merged settings (global + workspace + local) through
    the same ``SettingsLoader`` the validator uses, and asks the same
    ``BashPermissionValidator`` whether a deny pattern matches — no second
    matcher (invariant 2's spirit; H4's 60 s settings cache applies).

    Returns ``(colliding_deny_entry_or_None, all_deny_entries)``.
    """
    validator = _build_validator(workspace_dir)
    deny_patterns = list(validator.denied_patterns)
    for deny in deny_patterns:
        if _deny_subsumes(pattern, deny, validator):
            return deny, deny_patterns
    return None, deny_patterns


def claude_hooks_repo() -> str:
    """The claude-hooks checkout — the user-scoped allowlist's home.

    ``CLAUDE_HOOKS_REPO`` comes from the installer's MCP registration
    (22-03 §5); the checkout this file lives in is the fallback, which is the
    same directory whenever the server runs from its own repo.
    """
    env_repo = os.environ.get("CLAUDE_HOOKS_REPO")
    if env_repo:
        return str(Path(env_repo).expanduser())
    return str(HOOKS_DIR.parent.parent)


def _queue_source(identity: CallerIdentity) -> Dict[str, Optional[str]]:
    return {
        "session_id": identity.session_id,
        "actor_agent": identity.actor_agent,
        "cwd": identity.project_dir,
    }


def allowlist_add(
    pattern: str,
    scope: str = "workspace",
    target_workspace: Optional[str] = None,
    rationale: str = "",
    evidence_request_ids: Optional[List[str]] = None,
    identity: Optional[CallerIdentity] = None,
) -> Dict[str, Any]:
    """Promote a permission pattern into versioned settings, or queue it.

    Own project key → a direct write of the caller's own
    ``.claude/settings.json``. Any other key → an ``allowlist_proposal`` entry
    in that project's queue. Both refusals (shape, deny collision) happen before
    anything touches disk.
    """
    caller = identity if identity is not None else resolve_caller_identity()

    parsed = parse_pattern(pattern)
    if parsed is None:
        return _refusal(
            f"refused: {pattern!r} is not a permission pattern. Use a tool name "
            "(\"Bash\", \"WebFetch\", \"mcp__permissions__*\") or the parenthesised "
            "form (\"Bash(git log:*)\", \"Bash(pwd)\", \"WebFetch(domain:example.com)\"). "
            "Free text in an allow list never matches anything.",
            pattern=pattern,
        )

    normalized_scope = (scope or "workspace").strip().lower()
    if normalized_scope not in SCOPES:
        return _refusal(
            f"refused: scope must be one of {', '.join(SCOPES)} (got {scope!r}). "
            "\"workspace\" targets a project's own settings; \"user\" targets the "
            "claude-hooks repo, which the installer merges into every workspace.",
            pattern=parsed.raw,
        )

    if not (rationale or "").strip():
        return _refusal(
            "refused: a rationale is required — the reviewer applying this "
            "proposal (and the git history afterwards) needs to know why the "
            "pattern is safe to widen.",
            pattern=parsed.raw,
        )

    notes: List[str] = []
    if normalized_scope == "user":
        target_dir = claude_hooks_repo()
        if target_workspace:
            notes.append(
                f"target_workspace {target_workspace!r} ignored: scope=\"user\" always "
                f"targets the claude-hooks checkout ({target_dir})."
            )
    else:
        target_dir = target_workspace or caller.project_dir

    target_key = resolve_project_key(target_dir)
    own_root = resolve_workspace_root(caller.project_dir)
    own_key = resolve_project_key(caller.project_dir)
    is_own_project = target_key == own_key

    # The deny check reads the settings that would actually govern the pattern:
    # our own checkout when we are about to write it there, and the target's
    # **main checkout** otherwise — that is where the reviewer applies a queued
    # proposal (D7), so its deny list is the one that decides whether the entry
    # would ever take effect.
    settings_dir = own_root if is_own_project else resolve_project_root(target_dir)
    try:
        collision, deny_patterns = find_deny_collision(parsed, settings_dir)
    except Exception as exc:  # noqa: BLE001 — unreadable settings must not become a silent write.
        return _refusal(
            f"refused: could not read the target's merged settings to check for a "
            f"deny collision ({exc}). Nothing was written or queued.",
            pattern=parsed.raw,
            target_project_key=target_key,
        )
    if collision is not None:
        return _refusal(
            f"refused: {parsed.raw!r} collides with the deny entry {collision!r} "
            f"already in effect for {settings_dir}. Claude Code evaluates "
            "deny → ask → allow, so the allow entry would never take effect — a "
            "confusing no-op. Remove or narrow the deny entry first, or propose a "
            "pattern the deny does not cover.",
            pattern=parsed.raw,
            deny_entry=collision,
            target_project_key=target_key,
            deny_patterns_checked=len(deny_patterns),
        )

    if normalized_scope == "user":
        notes.append(H6_NOTE)

    if is_own_project:
        result = add_permission_pattern(
            own_root,
            parsed.raw,
            settings_filename=VERSIONED_SETTINGS,
            list_name="allow",
        )
        if not result.ok:
            return _refusal(
                f"refused: could not write {result.path or own_root} ({result.error}). "
                "Nothing was changed.",
                pattern=parsed.raw,
                target_project_key=target_key,
            )
        notes.append(
            f"{result.path} is git-versioned and this call only edited the working "
            "tree — commit it for the change to survive a clean checkout and reach "
            "other worktrees."
        )
        if target_workspace and normalized_scope == "workspace":
            named_root = resolve_workspace_root(target_workspace)
            if named_root != own_root:
                notes.append(
                    f"the workspace you named ({target_workspace}) shares this "
                    f"project key but is a different checkout; the write went to "
                    f"your own checkout ({own_root}) — an agent never edits a "
                    "checkout it is not running in (brd D6)."
                )
        return {
            "ok": True,
            "action": "wrote_settings",
            "pattern": parsed.raw,
            "scope": normalized_scope,
            "target_project_key": target_key,
            "path": result.path,
            "added": result.added,
            "status": (
                f"pattern written to {result.path}"
                if result.added
                else f"pattern was already present in {result.path} (no change)"
            ),
            "notes": notes,
        }

    entry = permission_queue.build_allowlist_proposal(
        pattern=parsed.raw,
        scope=normalized_scope,
        rationale=rationale.strip(),
        evidence_request_ids=evidence_request_ids,
        **_queue_source(caller),
    )
    try:
        path = permission_queue.enqueue(target_key, entry)
    except OSError as exc:
        return _refusal(
            f"refused: could not write the queue entry for {target_key} ({exc}).",
            pattern=parsed.raw,
            target_project_key=target_key,
        )
    return {
        "ok": True,
        "action": "queued",
        "pattern": parsed.raw,
        "scope": normalized_scope,
        "target_project_key": target_key,
        "target_workspace": target_dir,
        "path": str(path),
        "entry": entry,
        "status": QUEUED_STATUS.format(key=target_key),
        "notes": notes,
    }


def report_parser_issue(
    command: str,
    observed: str,
    expected: str,
    notes: str = "",
    identity: Optional[CallerIdentity] = None,
) -> Dict[str, Any]:
    """File a parser/validator issue for the claude-hooks reviewer.

    Always an enqueue, even when filed from the claude-hooks checkout itself:
    parser issues are the reviewer's input, and one uniform path is worth more
    than saving a hop (task 22-04 §4).
    """
    caller = identity if identity is not None else resolve_caller_identity()

    missing = [
        name
        for name, value in (
            ("command", command),
            ("observed", observed),
            ("expected", expected),
        )
        if not (value or "").strip()
    ]
    if missing:
        return _refusal(
            f"refused: {', '.join(missing)} required — a parser issue without the "
            "command, what the validator did, and what it should have done is not "
            "actionable.",
        )

    repo = claude_hooks_repo()
    target_key = resolve_project_key(repo)
    entry = permission_queue.build_parser_issue(
        command=command.strip(),
        observed=observed.strip(),
        expected=expected.strip(),
        notes=(notes or "").strip(),
        **_queue_source(caller),
    )
    try:
        path = permission_queue.enqueue(target_key, entry)
    except OSError as exc:
        return _refusal(f"refused: could not write the queue entry for {target_key} ({exc}).")

    return {
        "ok": True,
        "action": "queued",
        "type": permission_queue.TYPE_PARSER_ISSUE,
        "target_project_key": target_key,
        "target_workspace": repo,
        "path": str(path),
        "entry": entry,
        "status": QUEUED_STATUS.format(key=target_key),
        "notes": [
            "parser issues always queue — including from the claude-hooks "
            "workspace itself — so the reviewer sees every one of them in the "
            "same place.",
        ],
    }
