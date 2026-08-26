# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3"]
# ///
"""Permissions MCP server — read and decide Claude Code permission requests.

Epic 22 tasks 22-03 and 22-04. Six tools:

* ``list_permission_requests`` — the current (or historical) request rows,
  each with a live "may an agent decide this?" verdict;
* ``get_permission_request`` — one row in full, with the patterns that matched;
* ``permission_history`` — the daily reviewer's feed;
* ``decide_permission_request`` — write an allow/deny/stop decision for a
  request another session (or a subagent) is parked on;
* ``allowlist_add`` — promote a pattern into versioned settings: the caller's
  own checkout directly, any other workspace via the proposal queue;
* ``report_parser_issue`` — file a validator/parser defect for the reviewer.

All logic lives in ``permissions_mcp_lib``; this file only registers tools, so
the behaviour is testable with the repo's plain-``python3`` suite while the
``mcp`` dependency stays inside uv's environment.

Caller identity is resolved **once at startup** from the server process env
(the context-mcp precedent). Without ``CLAUDE_CODE_SESSION_ID`` the read tools
still work and ``decide`` refuses every call: a guard that cannot identify its
caller cannot enforce the self-decision rule.
"""

import json
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer

import permissions_mcp_lib as lib

mcp = MCPServer("permissions", version="1.0.0")

# Resolved once, at startup, from this process's environment.
CALLER = lib.resolve_caller_identity()


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@mcp.tool()
def list_permission_requests(state: str = "pending", workspace: Optional[str] = None) -> str:
    """List Claude Code permission requests from the shared state store.

    Every workspace and worktree on this machine writes to one user-scoped
    store, so this sees requests from other sessions too — that is the point:
    an orchestrator can watch what its workers are blocked on.

    Args:
        state: "pending" (default), "all", or a comma-separated list of store
            states (pending, allow, deny, stop, whitelist, reply, expired,
            resolved_terminal).
        workspace: Optional path filter; keeps rows whose cwd is that directory
            or below it.

    Each row carries a `decidable` verdict with a reason, so you do not need to
    call decide to learn that a request is human-only. AskUserQuestion rows are
    listed with kind "question" and are never decidable — questions are answered
    by humans. Rows also carry `caller_guard`, which says whether *you* may
    decide that particular row.

    Note: the verdict is computed from the row's own workspace settings, which
    are cached for up to 60 s in this long-lived process — a classification can
    lag a settings edit by that much.
    """
    return _dumps(lib.list_permission_requests(state=state, workspace=workspace, identity=CALLER))


@mcp.tool()
def get_permission_request(request_id: str) -> str:
    """Get one permission request in full, with its live tier classification.

    Returns the whole stored row (tool input, permission suggestions, timestamps,
    resolution fields) plus a classification naming the exact allow/deny/ask
    patterns that matched in that row's workspace right now.

    Note: classification may lag a settings edit by up to 60 s (settings are
    cached at class level in this long-lived process).
    """
    return _dumps(lib.get_permission_request(request_id, identity=CALLER))


@mcp.tool()
def permission_history(
    days: float = 1, workspace: Optional[str] = None, include_auto_denied: bool = True
) -> str:
    """Review recent permission activity: resolved requests + prompt causes.

    Joins two feeds over the same window: terminal rows from the permission
    state store (who decided what, how, and when) and entries from
    bash_manual_confirm.log (every command the validator did NOT auto-approve,
    with its per-sub-command validation results — the "why did this prompt"
    signal).

    Args:
        days: Window size in days, counting back from now (default 1).
        workspace: Optional path filter; keeps rows whose cwd/workspace is that
            directory or below it.
        include_auto_denied: When False, drops what nobody actually decided —
            requests resolved by the TTL timeout, and commands the validator
            hard-denied before any prompt existed.
    """
    return _dumps(
        lib.permission_history(
            days=days, workspace=workspace, include_auto_denied=include_auto_denied
        )
    )


@mcp.tool()
def decide_permission_request(request_id: str, action: str, reason: str) -> str:
    """Decide a pending permission request: allow, deny, or stop.

    This RECORDS a decision; it does not run anything. On success the waiting
    hook picks the decision up within ~30 s and the blocked tool proceeds (or
    does not). If the waiting process is already gone, nothing runs and the row
    simply reads as decided.

    Args:
        request_id: The request to decide (from list_permission_requests).
        action: "allow", "deny", or "stop" (stop also interrupts the session).
        reason: Required. Why this decision — it goes into the audit trail. An
            unexplained approval is not auditable.

    Two guards refuse calls:

    * You may not decide your own session's main-agent request. A parent MAY
      decide a subagent's request (a row that carries an agent_id), and any row
      from another session.
    * Human-only requests are refused: anything matching an `ask` pattern,
      anything whose redirects write outside the workspace/tmp, anything that
      now matches a deny pattern, and every AskUserQuestion row.

    The tier is re-computed at call time against the ROW's workspace settings,
    which are cached for up to 60 s in this long-lived process — a classification
    can lag a settings edit by that much.
    """
    return _dumps(
        lib.decide_permission_request(
            request_id=request_id, action=action, reason=reason, identity=CALLER
        )
    )


@mcp.tool()
def allowlist_add(
    pattern: str,
    rationale: str,
    scope: str = "workspace",
    target_workspace: Optional[str] = None,
    evidence_request_ids: Optional[List[str]] = None,
) -> str:
    """Promote a permission pattern into git-versioned settings.

    Two destinations, one rule: a pattern for the project you are running in is
    written straight into that checkout's `.claude/settings.json`; a pattern for
    any OTHER project is filed as a proposal in that project's queue, for the
    scheduled reviewer that lives there. No agent ever edits, commits or pushes
    in a checkout it is not running in.

    Every worktree of a repo counts as the same project (the queue is keyed by
    the main checkout), so a proposal filed from a feature-branch worktree is
    applied centrally rather than on the branch.

    Args:
        pattern: A Claude Code permission pattern — `Bash(git log:*)`,
            `Bash(pwd)`, `WebFetch(domain:example.com)`, or a bare tool name.
            Free text is refused.
        rationale: Required. Why widening this is safe — the reviewer applying
            the proposal, and the git history afterwards, both need it.
        scope: "workspace" (default) for the target project's own settings, or
            "user" for the claude-hooks repo's settings, which the installer
            merges into every workspace on this machine.
        target_workspace: Any path inside the project you are proposing for.
            Defaults to your own workspace. Ignored when scope is "user".
        evidence_request_ids: Optional request ids from
            list_permission_requests / permission_history that show why the
            prompt keeps firing.

    Refused before anything is written: a pattern that is not a permission
    pattern, and a pattern already covered by a deny entry in the target's
    settings (deny beats allow, so the entry would be a confusing no-op — the
    refusal names the deny entry).

    Note: the deny check reads merged settings that are cached for up to 60 s in
    this long-lived process, so it can lag a settings edit by that much. A
    scope="user" result always says what is and is not live yet.
    """
    return _dumps(
        lib.allowlist_add(
            pattern=pattern,
            scope=scope,
            target_workspace=target_workspace,
            rationale=rationale,
            evidence_request_ids=evidence_request_ids,
            identity=CALLER,
        )
    )


@mcp.tool()
def report_parser_issue(
    command: str, observed: str, expected: str, notes: str = ""
) -> str:
    """Report a permission-parser or validator defect to the claude-hooks reviewer.

    Use this when a command was denied, prompted or auto-approved in a way that
    looks wrong — a compound the parser split badly, a wrapper it failed to peel,
    a deny pattern that over-matched. It files a queue entry for the claude-hooks
    reviewer; it does not change any settings.

    This always queues, even when you are working in the claude-hooks checkout
    itself: parser issues are the reviewer's input and they all arrive the same
    way.

    Args:
        command: The raw command, exactly as it was run.
        observed: The decision and reason you actually got.
        expected: What you believe the right decision is, and why.
        notes: Optional extra context (session, what you were doing).
    """
    return _dumps(
        lib.report_parser_issue(
            command=command,
            observed=observed,
            expected=expected,
            notes=notes,
            identity=CALLER,
        )
    )


if __name__ == "__main__":
    mcp.run()
