# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3"]
# ///
"""Permissions MCP server — read and decide Claude Code permission requests.

Epic 22 task 22-03. Four tools:

* ``list_permission_requests`` — the current (or historical) request rows,
  each with a live "may an agent decide this?" verdict;
* ``get_permission_request`` — one row in full, with the patterns that matched;
* ``permission_history`` — the daily reviewer's feed;
* ``decide_permission_request`` — write an allow/deny/stop decision for a
  request another session (or a subagent) is parked on.

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
from typing import Optional

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


if __name__ == "__main__":
    mcp.run()
