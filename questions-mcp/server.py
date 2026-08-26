# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3"]
# ///
"""Questions MCP server — async agent↔human question and notification tools.

Epic 23 task 23-04.  Two tools:

* ``ask``    — write a question entry into the workspace queue file, send it
               to Telegram as a ``kind=question`` message (``never_expires``),
               and return the allocated id immediately.
* ``notify`` — send an async statement; with ``ack=True`` it carries an
               ``[ Acknowledge ]`` button so the nudge ladder has something to
               chase (brd D7); with ``ack=False`` it is fire-and-forget.

All logic lives in ``questions_mcp_lib``; this file only registers tools, so
the behaviour is testable with the repo's plain-``python3`` suite while the
``mcp`` dependency stays inside uv's environment.

Identity and config are resolved **per call** (not at startup): the server is
long-lived and may serve sessions in different workspaces.
``CLAUDE_PROJECT_DIR``/``PWD`` is read per invocation with a short TTL cache.
"""

import json
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer

import questions_mcp_lib as lib

mcp = MCPServer("questions", version="1.0.0")


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


@mcp.tool()
def ask(
    title: str,
    body: str = "",
    options: Optional[List[str]] = None,
    role: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> str:
    """Ask a human an async question — write to the queue, send to Telegram.

    Use this instead of AskUserQuestion when you have other work to do while
    waiting.  The question is written to a durable queue file in the workspace
    and sent to the human's Telegram chat.  Return immediately; the human may
    answer hours or days later.

    **When to use ask vs AskUserQuestion**

    * Use ``ask`` when the answer is not needed to continue — you have other
      tasks, or you are about to halt/exit.
    * Use ``AskUserQuestion`` when the answer IS the next step and you are
      prepared to wait up to 60 s.

    **What goes in body vs options**

    * ``body`` — the full question text: context, alternatives, links.  This
      is passed through verbatim to both the queue file and Telegram.  The
      tool owns only the frame (heading, id, status, routing line, buttons).
    * ``options`` — short answer choices.  Each becomes one Telegram button
      and a numbered line in the message body.  Single-select only: do not
      use multi_select semantics (the relay ignores them for ungrouped async
      messages).  Omit for free-text questions.

    **The returned id**

    The returned ``id`` (e.g. ``Q-042``) is the stable reference.  Cite it
    in your halt/exit message so the workspace's workflow predicate can
    re-route work to the question once it is answered.  Example verdict line::

        HALT: waiting on design decision for phase-0.2/brd (Q-042)

    Args:
        title: Short question title (≤ 80 chars recommended).
        body: The question body.  Opaque passthrough — you own its content.
        options: Optional list of answer choices (single-select, v1).
        role: Role alias to route to.  None → workspace default role.
            Unresolvable roles (no token on this machine) are refused before
            any write — no dangling entry is created.
        tags: Optional list of tags appended to the heading in square brackets.

    Returns a JSON string with:
        id            — allocated question id (Q-NNN)
        file          — queue-file path relative to the workspace root
        dispatched    — true when the relay call succeeded
        message_id    — relay message id (null when dispatched is false)
        pending_answers — count of unapplied answers in the local index
        error         — present only when something went wrong
    """
    return _dumps(lib.ask(
        title=title,
        body=body,
        options=options,
        role=role,
        tags=tags,
    ))


@mcp.tool()
def notify(
    text: str,
    role: Optional[str] = None,
    ack: bool = True,
) -> str:
    """Send an async notification to a human role.

    Unlike ``ask``, this does not write a queue-file entry.  Use it for
    status updates or alerts that do not need a structured answer.

    With ``ack=True`` (the default) the message carries an
    ``[ Acknowledge ]`` button so the nudge ladder has something to chase —
    it is sent as ``kind=question`` and gets the ``#unanswered`` tag and
    reminders until the human taps Acknowledge.  The listener finalises the
    Telegram message on receipt without touching any file.

    With ``ack=False`` the message is sent as ``kind=notification`` and is
    never nudged.  Idle-session notifications are byte-identical to today
    (invariant 9 / brd D7).

    Args:
        text: The notification text (plain prose, HTML-escaped before send).
        role: Role alias.  None → workspace default role.
        ack: True (default) → nudgeable question with Acknowledge button.
             False → fire-and-forget notification, no record kept.

    Returns a JSON string with:
        dispatched  — true when the relay call succeeded
        message_id  — relay message id (null when dispatched is false)
        error       — present only when something went wrong
    """
    return _dumps(lib.notify(text=text, role=role, ack=ack))


if __name__ == "__main__":
    mcp.run()
