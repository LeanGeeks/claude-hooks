"""Message-body rendering — the one place the ``#unanswered`` tag exists.

Two rules, and everything here follows from them (epic 19, brd §4.2):

1. **``payload_json["text"]`` is the canonical body and is always untagged.**
   Every writer keeps it current — including ``PATCH /v1/messages/{id}``, which
   persists the client's text rather than only forwarding it to Telegram.
2. **One ``render_body``, called by every send and every edit.** No call site
   decides tagging for itself, so the tag cannot double under a retry, cannot
   survive a cancel, and cannot be authored by a client.

A message carries the trailing tag iff it is ``open``, its ``kind`` is *not*
``notification``, and it actually awaits a human (``reply_required``, or a
keyboard, or a ``group_id``) — see :func:`awaits_human`.

This lives in its own module rather than in ``app.py`` because ``reaper.py``
renders too, and ``app.py`` already imports ``reaper`` (importing back would be
a cycle). ``app.py`` re-exports these names, so call sites read the same.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

# The tag itself. Plain text on its own trailing line — Telegram does not
# linkify a hashtag inside <pre>/<code>, and the relay sends HTML parse mode
# with code blocks in the body (brd §2.3). Kept as one constant so brd §2.4's
# fallback (a more distinctive tag, e.g. "#unanswered_cc") stays a one-line
# change.
TAG = "#unanswered"
TAG_LINE = "\n\n" + TAG

# Telegram's hard cap on a message body. Bodies already approach it (long diffs,
# code blocks), so appending the tag has to be truncation-aware (brd §2.6).
TELEGRAM_TEXT_LIMIT = 4096

# How far back from the ideal cut point we are willing to look for a boundary
# that is safe to truncate at, and how many candidates we are willing to test.
_TRIM_WINDOW = 1024
_TRIM_MAX_CANDIDATES = 256

_TAG_ESC = re.escape(TAG)
# The shape render_body produces: one or more newlines, then a line holding
# nothing but the tag. Consumes the leading newlines so stripping is exact.
_TAG_LINE_RE = re.compile(r"(?:[ \t]*\r?\n)+[ \t]*" + _TAG_ESC + r"[ \t]*(?=\r?\n|\Z)")
# The same, but as the very first line of the body.
_TAG_HEAD_RE = re.compile(r"\A[ \t]*" + _TAG_ESC + r"[ \t]*(?:\r?\n)?")
# Any remaining standalone occurrence (a client that typed the tag mid-prose).
# The lookbehind keeps "##unanswered"/"x#unanswered" intact, the lookahead keeps
# a longer tag such as "#unanswered_cc" intact.
_TAG_TOKEN_RE = re.compile(r"[ \t]*(?<![\w#])" + _TAG_ESC + r"(?!\w)")

# HTML-ish tag scanner used by the truncation guard.
_HTML_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)[^>]*>")
# Longest entity we bother to protect ("&thetasym;" and friends are shorter).
_MAX_ENTITY_LEN = 12


def payload_for(row: Any) -> dict[str, Any]:
    """Decode a message row's ``payload_json`` into a dict.

    Returns ``{}`` for a missing or unreadable payload — every caller reads
    optional keys off it, so a corrupt row degrades to "no keyboard, no group"
    rather than exploding inside a webhook handler.
    """
    try:
        data = json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def strip_tag(text: str) -> str:
    """Remove the relay's tag from a body, wherever it sits.

    Makes rendering idempotent: a retried PATCH, a client echoing the text we
    rendered back at us, or a client that typed the tag itself all converge on
    the same canonical, untagged body.
    """
    if not text:
        return text or ""
    if TAG not in text:
        return text
    out = _TAG_LINE_RE.sub("", text)
    out = _TAG_HEAD_RE.sub("", out)
    return _TAG_TOKEN_RE.sub("", out)


def awaits_human(payload: Mapping[str, Any], state: str) -> bool:
    """True when this message is an agent blocked on a person.

    ``state == 'open'`` **and** ``kind != 'notification'`` **and** (a free-text
    answer is required, or there are buttons, or it belongs to a question group).

    The ``kind`` clause is load-bearing, not redundant (brd §4.1, invariant 7):
    ``notification_hook`` sends idle-session messages with ``reply_required=True``
    whenever reply-from-Telegram is on, so a predicate keyed on
    ``reply_required`` alone would tag every idle session — and, once the nudge
    engine lands, ping about it at 09:00. ``#unanswered`` means *an agent is
    blocked on you*, not *a session finished*. Do not simplify this.
    """
    if state != "open":
        return False
    if payload.get("kind") == "notification":
        return False
    return bool(
        payload.get("reply_required")
        or payload.get("keyboard")
        or payload.get("group_id")
    )


def render_body(payload: Mapping[str, Any], state: str) -> str:
    """Render the body Telegram should display for ``payload`` in ``state``.

    Takes a payload dict and a state rather than a row: the create path has no
    usable row yet (it inserts ``telegram_message_id = 0`` and sends *before*
    the id is known), and terminal paths want to pass the state they are
    transitioning *to* rather than re-read a row that still says ``open``.
    """
    body = strip_tag(str(payload.get("text") or ""))
    if not awaits_human(payload, state):
        return body
    return _with_tag(body)


def render_body_row(row: Any) -> str:
    """``render_body`` for a message row — read the row *after* any state flip."""
    return render_body(payload_for(row), row["state"])


# ---- Length guard (brd §2.6) ----------------------------------------------


def _with_tag(body: str) -> str:
    """Append the tag if it fits, trimming the body only where that is safe.

    Order of preference: tag appended whole → body trimmed at a boundary that
    cannot split an HTML entity or land inside a tag, then tagged → no tag at
    all. Sending a body Telegram rejects is never an option, and neither is
    emitting HTML that would fail to parse.
    """
    if len(body) + len(TAG_LINE) <= TELEGRAM_TEXT_LIMIT:
        return body + TAG_LINE
    cut = _safe_cut(body, TELEGRAM_TEXT_LIMIT - len(TAG_LINE))
    if cut is None:
        return body
    return body[:cut].rstrip() + TAG_LINE


def _safe_cut(body: str, limit: int) -> int | None:
    """Largest index ``<= limit`` where ``body`` may be cut, or None.

    Prefers a line break, then any whitespace, and only accepts a cut whose
    prefix is still well-formed HTML (see :func:`_html_safe_prefix`).
    """
    if limit <= 0:
        return None
    floor = max(0, limit - _TRIM_WINDOW)
    for boundary in ("\n", " \t\n"):
        checked = 0
        for idx in range(min(limit, len(body)), floor, -1):
            if body[idx - 1] not in boundary:
                continue
            checked += 1
            if checked > _TRIM_MAX_CANDIDATES:
                break
            if _html_safe_prefix(body[:idx]):
                return idx
    return None


def _html_safe_prefix(prefix: str) -> bool:
    """True if ``prefix`` can stand alone as HTML in a Telegram message.

    Rejects a cut that lands inside a tag (``<b`` with no ``>``), inside an
    entity (``&am``), or between an opening tag and its close (``<pre>`` with no
    ``</pre>``) — all three make Telegram reject the whole edit with
    "can't parse entities", which would be a worse outcome than no tag.
    """
    if prefix.rfind("<") > prefix.rfind(">"):
        return False
    amp = prefix.rfind("&")
    if amp != -1 and len(prefix) - amp <= _MAX_ENTITY_LEN and ";" not in prefix[amp:]:
        return False
    stack: list[str] = []
    for match in _HTML_TAG_RE.finditer(prefix):
        closing, name = match.group(1), match.group(2).lower()
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack
