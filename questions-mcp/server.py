# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3", "httpx>=0.26"]
# ///
"""Questions MCP server — async agent↔human question and notification tools.

Epic 23 task 23-04, extended by epic 46.  Four tools:

* ``ask``      — write a question entry into the workspace queue file, send it
                 to Telegram as a ``kind=question`` message (``never_expires``),
                 and return the allocated id immediately.
* ``notify``   — send an async statement; with ``ack=True`` it carries an
                 ``[ Acknowledge ]`` button so the nudge ladder has something to
                 chase (brd D7); with ``ack=False`` it is fire-and-forget.
* ``withdraw`` — close a question that has become moot: the entry is marked
                 withdrawn, the Telegram message loses its buttons.
* ``claim``    — reserve the next id(s) for a question filed by hand.

All logic lives in ``questions_mcp_lib``; this file only registers tools, so
the behaviour is testable with the repo's plain-``python3`` suite while the
``mcp`` dependency stays inside uv's environment.

Identity and config are resolved **per call** (not at startup): the server is
long-lived and may serve sessions in different workspaces.
``CLAUDE_PROJECT_DIR``/``PWD`` is read per invocation with a short TTL cache.

``httpx`` is declared above because this server — unlike ``context-mcp`` and
``permissions-mcp`` — talks to the relay, through ``relay_server.client``.
``mcp`` 2.x depends on ``httpx2`` and no longer brings ``httpx`` with it, so
without the explicit entry the relay client fails to import inside uv's
environment and every ``ask``/``notify`` is refused (task 45).
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
    id: Optional[str] = None,
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
        id: Use this id instead of allocating the next one.  Only an id you
            reserved with ``claim`` is accepted.  Normally omit it.

    **If the question becomes moot**, call ``withdraw`` with the returned id —
    do not edit the heading by hand: that leaves the Telegram buttons live and
    the reminders running.

    Returns a JSON string with:
        id            — allocated question id (Q-NNN)
        file          — queue-file path relative to the workspace root
        dispatched    — true when the relay call succeeded
        message_id    — relay message id (null when dispatched is false)
        pending_answers — count of unapplied answers in the local index
        sync          — present only when the workspace has git sync on: what
                        was committed and pushed.  ``sync.pushed`` false means
                        the id is NOT yet claimed for other checkouts; the
                        ``reason`` says why, and it is retried automatically.
                        ``sync.dispatch_marker`` appears only if the second
                        commit (the Dispatched line) fared differently.
        warnings      — present only when the send succeeded in a degraded form
        error         — present only when something went wrong
    """
    return _dumps(lib.ask(
        title=title,
        body=body,
        options=options,
        role=role,
        tags=tags,
        id=id,
    ))


@mcp.tool()
def withdraw(
    reason: str,
    id: Optional[str] = None,
    message_id: Optional[int] = None,
) -> str:
    """Withdraw a question that no longer needs an answer.

    Use it when a question you (or an earlier session) asked has become moot —
    the change went ahead, the blocker disappeared, the question was wrong.
    It marks the queue entry withdrawn with your reason, replaces the Telegram
    message with "Withdrawn — <reason>", and removes its buttons, reminders and
    escalation.  (A copy that was *already* escalated to another chat loses its
    buttons too only if the relay is recent enough; otherwise a tap there is
    simply dropped.)

    Only an **open** question can be withdrawn.  If the human has already
    answered, the call is refused and the answer stands.  If an answer arrives
    after the withdrawal, it is recorded under the entry as a late answer and
    the status stays withdrawn — nothing a human said is discarded.

    Calling it again on a withdrawn question is harmless and retries the
    Telegram side, which is what to do when ``relay_cancelled`` came back false.

    Args:
        reason: Why the question is moot.  Required; the human sees it.
        id: The question id (``Q-042``).
        message_id: Instead of ``id``: the relay message id of an
            acknowledge-notification (``notify(ack=True)``) to cancel.

    Returns a JSON string with:
        id              — the question id
        withdrawn       — true when the entry is withdrawn after this call
        status          — the status word now on the entry
        file            — queue-file path relative to the workspace root
        message_id      — relay message id, when the question was dispatched
        relay_cancelled — true when the Telegram message lost its buttons
        sync            — present only when the workspace has git sync on
        note            — present when there is something to know that is not
                          an error ("already withdrawn", "never dispatched")
        error           — present only when something went wrong
    """
    return _dumps(lib.withdraw(reason=reason, id=id, message_id=message_id))


@mcp.tool()
def claim(count: int = 1, note: str = "") -> str:
    """Reserve the next question id(s) without asking anything.

    For a question you are going to write into a queue file by hand, or need to
    cite before it exists.  ``ask`` allocates its own id — you do not need
    ``claim`` first.  Never pick an id yourself by reading the files: an id on a
    branch that has not landed, or on another machine, is invisible from here,
    and that is how two questions end up with one id.

    The reservation is a line in the workspace's claims ledger.  With git sync
    on it is committed and pushed, which is what makes it binding for every
    other checkout; read ``claimed_on_remote`` to know whether that happened.

    To *ask* with a claimed id later, pass it as ``ask(id=…)``.

    Args:
        count: How many consecutive ids to reserve (1–50).
        note: What the id is for (a branch, a task) — written into the ledger.

    Returns a JSON string with:
        ids               — the reserved ids
        file              — the ledger, relative to the workspace root
        claimed_on_remote — true when the reservation reached the upstream
        sync              — present only when the workspace has git sync on
        note              — present when the reservation is local only, with why
        error             — present only when something went wrong
    """
    return _dumps(lib.claim(count=count, note=note))


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
