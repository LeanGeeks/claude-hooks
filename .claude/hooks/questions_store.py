#!/usr/bin/env python3
"""Queue-file engine for epic 23 — asynchronous agent↔human questions.

The **only** module in the system that reads or writes a workspace question
file (state.md invariant 5).  Both the questions MCP server (23-04) and the
answer listener (23-05) import it, so the on-disk format cannot drift between
the writer and the answerer.

Pure library: no relay, no MCP, no subprocess, no network.  Anchor resolution
is filesystem-only — it reads ``.git`` metadata directly rather than shelling
out to ``git``, so it can neither mutate git state nor block on a subprocess.

The parse contract (architecture.md §3.2), which this module implements
exactly:

1. **Entry** — a heading at the configured level (default ``##``) whose text
   *begins* with a well-formed id token.  Never "contains the id somewhere":
   ``## Q-388 — …after Q-253`` is Q-388's entry and never Q-253's.
2. **Id** — default ``Q-<digits>`` with an optional short alpha suffix
   (``Q-200-ds``); the whole token shape is configurable.  Allocation is
   ``max + 1`` across the queue set **plus** ``answered/``, never first-free.
3. **Status** — a bracketed token on the entry heading whose *first word* is in
   the configured status set; free text after that word is preserved verbatim
   on rewrite (``[resolved 2026-08-13]`` is a ``resolved`` and keeps its date).
4. **Boundary** — an entry ends at the next heading of the same-or-higher
   level, else at EOF.
5. **Ambiguity refuses** — two entry headings for one id return ``conflict``,
   zero return ``not_found``.  Both are normal return values, never exceptions,
   and the store never guesses which of two candidates a human meant.
6. **Non-conforming headings are ignored, never repaired** — a heading that
   does not begin with a well-formed id is not an entry and is left exactly as
   it was found.

Write discipline (architecture.md §3.3): every mutation takes ``flock`` on
``<dir>/.lock``, reads, modifies in memory, writes a temp file in the same
directory and ``os.replace``s it into place, then releases.  Never a partial
write, never truncate-then-write.

Answer escaping (architecture.md §3.4, state.md invariant 11): answer text is
human free-form from Telegram and lands in a file agents later read *as
instructions*.  It is indented, fenced and per-line guarded so that **no
possible answer text changes how any later parse of that file resolves**.

Public API
----------
load_questions_config  — parse ``[questions]`` out of a workspace roles.toml
resolve_anchor         — the architecture §3.1 ladder; pure, no side effects
open_store             — config + anchor → QuestionsStore, or None when inert
QuestionsStore         — allocate/compose/append, mark_dispatched, apply_answer
escape_answer_text     — the invariant-11 escaper, exposed for tests and tools
parse_entries          — the contract's entry parser, exposed for diagnostics
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, NamedTuple, Sequence

import roles_config

# ── Defaults (brd §5 — the reference workspace's §5.2 shapes) ─────────────────

DEFAULT_DIR = "docs/questions"
DEFAULT_ANCHOR = "repo"
DEFAULT_NUDGE = "4h,1d,3d,7d*"
DEFAULT_ESCALATE_AFTER = "24h"
DEFAULT_MAX_OPEN = 20
DEFAULT_MIN_INTERVAL_S = 30.0

# ``[questions.format]`` defaults
DEFAULT_ID = "Q-{n:03d}"
DEFAULT_STATUS: tuple[str, ...] = ("open", "resolved")
DEFAULT_HEADING = "## {id} — {title}  [{status}]{tags}"
DEFAULT_ANSWER_TEMPLATE = (
    "**Answered by:** {answered_by} · {answered_at} · relay #{message_id}\n"
    "\n"
    "{answer}"
)
# Phase 0: a non-queue file shares the reference directory, so the default glob
# is narrower than ``*.md``.  The queue set is the union of this glob with the
# files named in ``[questions.queue]`` (or the flat single file), so a
# greenfield ``questions.md`` is covered without matching the glob.
DEFAULT_GLOB = "for-*.md"
DEFAULT_FILE = "questions.md"

#: Sub-directory scanned for ids in addition to the queue set (rule 2) and used
#: by 23-05's sidecar fallback.
ANSWERED_DIRNAME = "answered"

LOCK_FILENAME = ".lock"

# ── Errors and results ────────────────────────────────────────────────────────


class QuestionsStoreError(Exception):
    """A queue file could not be read or written.

    Raised instead of letting an ``OSError`` / ``UnicodeDecodeError`` escape:
    a corrupt or unreadable file degrades to *this* clear, documented error
    carrying the path and the reason, never to a raw exception from the I/O
    layer (task 23-03 §6).  ``not_found`` and ``conflict`` are **not** errors —
    they are ordinary return values of :class:`ApplyResult`.
    """


class ApplyResult(str, Enum):
    """Outcome of a locate-and-mutate operation (architecture §3.5)."""

    APPLIED = "applied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuestionsFormat:
    """The overridable half of the parse contract (``[questions.format]``)."""

    id: str = DEFAULT_ID
    status: tuple[str, ...] = DEFAULT_STATUS
    heading: str = DEFAULT_HEADING
    answer_template: str = DEFAULT_ANSWER_TEMPLATE
    glob: str = DEFAULT_GLOB

    # ── derived ──
    @property
    def open_status(self) -> str:
        """The token a freshly composed entry carries (``status[0]``)."""
        return self.status[0]

    @property
    def resolved_status(self) -> str:
        """The token ``apply_answer`` flips to (``status[1]``, else ``status[0]``).

        Elements beyond the second are recognised vocabulary only: declaring
        them stops a workspace having to rewrite 33 headings (Phase 0), but
        they are never *written* by this module.
        """
        return self.status[1] if len(self.status) > 1 else self.status[0]

    @property
    def status_set(self) -> frozenset[str]:
        return frozenset(s.lower() for s in self.status)

    @property
    def level(self) -> int:
        """Heading level of an entry, taken from the ``heading`` template."""
        return _heading_level(self.heading)


@dataclass(frozen=True)
class QuestionsConfig:
    """A workspace's resolved ``[questions]`` section."""

    dir: str = DEFAULT_DIR
    anchor: str = DEFAULT_ANCHOR
    path: str = ""
    nudge: str = DEFAULT_NUDGE
    escalate_after: float | None = 24 * 3600.0
    queue: dict[str, str] = field(default_factory=dict)
    file: str = DEFAULT_FILE
    max_open: int = DEFAULT_MAX_OPEN
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    fmt: QuestionsFormat = field(default_factory=QuestionsFormat)
    workspace_id: str = ""
    default_role: str | None = None
    roles_path: Path | None = None
    errors: tuple[str, ...] = ()


def _parse_duration(value: object) -> float | None:
    """``roles_config.parse_duration`` plus a ``<n>d`` day suffix.

    The epic's own defaults are written in days (``escalate_after = "24h"`` but
    ladders like ``7d``), and ``roles_config.parse_duration`` — a shared module
    this task does not own — accepts only ``s``/``m``/``h``.  Days are handled
    here rather than by widening the shared parser.
    """
    if isinstance(value, str):
        s = value.strip().lower()
        if s.endswith("d") and len(s) > 1:
            try:
                days = float(s[:-1])
            except ValueError:
                raise ValueError(f"Invalid duration: {value!r}")
            return None if days == 0.0 else days * 86400.0
    return roles_config.parse_duration(value)


def _heading_level(template: str) -> int:
    """Number of leading ``#`` in a heading template (default 2)."""
    m = re.match(r"^(#{1,6})(?!#)", template)
    return len(m.group(1)) if m else 2


def load_questions_config(
    workspace_dir: str,
    *,
    roles_path: Path | None = None,
) -> QuestionsConfig | None:
    """Load ``[questions]`` from the workspace's ``.claude/roles.toml``.

    Returns ``None`` when the store must be **inert** (state.md invariant 9):
    no roles.toml, an unparseable one, or no ``[questions]`` section.  An inert
    workspace has no store, so every entry point is a clean no-op — nothing is
    read, nothing is created, nothing raises.

    Recoverable problems (an unknown ``anchor``, a bad duration, a malformed
    ``id`` template) are recorded in ``errors`` and the default is used; they
    never make the section inert.
    """
    path = roles_path if roles_path is not None else roles_config.find_roles_file(workspace_dir)
    if path is None:
        return None

    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception:
        return None

    section = raw.get("questions")
    if not isinstance(section, dict):
        return None

    errors: list[str] = []

    def _str(key: str, default: str) -> str:
        value = section.get(key, default)
        if not isinstance(value, str):
            errors.append(f"[questions].{key}: expected a string, ignored")
            return default
        return value

    dir_value = _str("dir", DEFAULT_DIR)

    anchor = _str("anchor", DEFAULT_ANCHOR)
    if anchor not in ("repo", "worktree", "path"):
        errors.append(f"[questions].anchor: unknown value {anchor!r}, using {DEFAULT_ANCHOR!r}")
        anchor = DEFAULT_ANCHOR

    anchor_path = _str("path", "")
    if anchor == "path" and not anchor_path:
        errors.append('[questions].path is required when anchor = "path"; falling back to "repo"')
        anchor = DEFAULT_ANCHOR

    nudge = _str("nudge", DEFAULT_NUDGE)

    raw_escalate = section.get("escalate_after", DEFAULT_ESCALATE_AFTER)
    try:
        escalate_after = _parse_duration(raw_escalate)
    except ValueError:
        errors.append(f"[questions].escalate_after: invalid duration {raw_escalate!r}, using default")
        escalate_after = _parse_duration(DEFAULT_ESCALATE_AFTER)

    queue: dict[str, str] = {}
    raw_queue = section.get("queue", {})
    if isinstance(raw_queue, dict):
        for role_id, filename in raw_queue.items():
            if isinstance(filename, str) and filename:
                queue[role_id] = filename
            else:
                errors.append(f"[questions.queue].{role_id}: expected a filename, ignored")
    elif raw_queue:
        errors.append("[questions.queue]: expected a table, ignored")

    flat_file = _str("file", DEFAULT_FILE)

    def _number(key: str, default: float) -> float:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"[questions].{key}: expected a number, using {default}")
            return float(default)
        return float(value)

    max_open = int(_number("max_open", DEFAULT_MAX_OPEN))
    min_interval_s = _number("min_interval_s", DEFAULT_MIN_INTERVAL_S)

    fmt = _load_format(section.get("format"), errors)

    # workspace_id: the role catalog owns it when there is one (it survives a
    # rename, architecture §3.1).  A greenfield workspace has no roles at all —
    # ``load_catalog`` returns None for a roles.toml with no ``default`` — so
    # fall back to the raw key, then to the workspace directory name.
    catalog = roles_config.load_catalog(workspace_dir, path=path)
    if catalog is not None:
        workspace_id = catalog.workspace_id
        default_role: str | None = catalog.default_role
    else:
        raw_ws = raw.get("workspace_id")
        workspace_id = raw_ws if isinstance(raw_ws, str) and raw_ws else path.parent.parent.name
        default_role = None

    return QuestionsConfig(
        dir=dir_value,
        anchor=anchor,
        path=anchor_path,
        nudge=nudge,
        escalate_after=escalate_after,
        queue=queue,
        file=flat_file,
        max_open=max_open,
        min_interval_s=min_interval_s,
        fmt=fmt,
        workspace_id=workspace_id,
        default_role=default_role,
        roles_path=path,
        errors=tuple(errors),
    )


def _load_format(raw: object, errors: list[str]) -> QuestionsFormat:
    """Build a :class:`QuestionsFormat` from ``[questions.format]``."""
    if raw is None:
        return QuestionsFormat()
    if not isinstance(raw, dict):
        errors.append("[questions.format]: expected a table, ignored")
        return QuestionsFormat()

    id_template = raw.get("id", DEFAULT_ID)
    if not isinstance(id_template, str) or _parse_id_template(id_template) is None:
        errors.append(f"[questions.format].id: invalid template {id_template!r}, using {DEFAULT_ID!r}")
        id_template = DEFAULT_ID

    raw_status = raw.get("status", list(DEFAULT_STATUS))
    if isinstance(raw_status, list):
        status = tuple(s for s in raw_status if isinstance(s, str) and s.strip())
    else:
        status = ()
    if not status:
        errors.append("[questions.format].status: expected a non-empty list of tokens, using the default")
        status = DEFAULT_STATUS

    heading = raw.get("heading", DEFAULT_HEADING)
    if not isinstance(heading, str) or not re.match(r"^#{1,6}(?!#)\s", heading):
        errors.append(f"[questions.format].heading: invalid template {heading!r}, using the default")
        heading = DEFAULT_HEADING

    if "{status}" in heading and "[{status}]" not in heading:
        errors.append(
            "[questions.format].heading: the status placeholder must be bracketed "
            "([{status}]) — contract rule 3 only parses a bracketed token, so an "
            "entry written with this heading will never have its status flipped"
        )

    answer_template = raw.get("answer_template", DEFAULT_ANSWER_TEMPLATE)
    if not isinstance(answer_template, str) or "{answer}" not in answer_template:
        errors.append(
            "[questions.format].answer_template: must be a string containing {answer}, using the default"
        )
        answer_template = DEFAULT_ANSWER_TEMPLATE

    glob = raw.get("glob", DEFAULT_GLOB)
    if not isinstance(glob, str) or not glob:
        errors.append(f"[questions.format].glob: invalid pattern {glob!r}, using {DEFAULT_GLOB!r}")
        glob = DEFAULT_GLOB

    return QuestionsFormat(
        id=id_template,
        status=status,
        heading=heading,
        answer_template=answer_template,
        glob=glob,
    )


# ── Id tokens (contract rule 2) ───────────────────────────────────────────────


class _IdTemplate(NamedTuple):
    prefix: str
    suffix: str
    width: int


#: ``Q-{n}`` / ``Q-{n:03d}`` / ``{n}-question`` …
_ID_TEMPLATE_RE = re.compile(r"^(?P<prefix>[^{}]*)\{n(?::0(?P<width>\d+)d?)?\}(?P<suffix>[^{}]*)$")

#: The optional short alpha suffix of rule 2 (``Q-200-ds``).  Deliberately
#: short: a long alpha run after the number is prose, not an id variant, and a
#: heading it appears on is simply not an entry (rule 6).
_ID_VARIANT = r"(?:-(?P<var>[A-Za-z]{1,4}))?"

#: Nothing word-ish may follow an id token, else it is not a well-formed id.
_ID_TAIL = r"(?![A-Za-z0-9_-])"


def _parse_id_template(template: str) -> _IdTemplate | None:
    m = _ID_TEMPLATE_RE.match(template)
    if m is None:
        return None
    width = int(m.group("width")) if m.group("width") else 0
    return _IdTemplate(prefix=m.group("prefix"), suffix=m.group("suffix"), width=width)


def _id_body(spec: _IdTemplate) -> str:
    return re.escape(spec.prefix) + r"(?P<num>\d+)" + re.escape(spec.suffix) + _ID_VARIANT


def _id_start_re(fmt: QuestionsFormat) -> re.Pattern[str]:
    """Matches a well-formed id **at the start** of a string (rule 1)."""
    spec = _parse_id_template(fmt.id) or _parse_id_template(DEFAULT_ID)
    assert spec is not None
    return re.compile("^" + _id_body(spec) + _ID_TAIL)


def _id_scan_re(fmt: QuestionsFormat) -> re.Pattern[str]:
    """Matches a well-formed id **anywhere**, used only for id allocation."""
    spec = _parse_id_template(fmt.id) or _parse_id_template(DEFAULT_ID)
    assert spec is not None
    return re.compile(r"(?<![A-Za-z0-9_])" + _id_body(spec) + _ID_TAIL)


def format_id(fmt: QuestionsFormat, number: int, variant: str = "") -> str:
    """Render *number* as an id token in this workspace's shape."""
    spec = _parse_id_template(fmt.id) or _parse_id_template(DEFAULT_ID)
    assert spec is not None
    digits = str(int(number)).rjust(spec.width, "0") if spec.width else str(int(number))
    token = f"{spec.prefix}{digits}{spec.suffix}"
    return f"{token}-{variant}" if variant else token


def parse_id(fmt: QuestionsFormat, token: str) -> tuple[int, str] | None:
    """Parse an id token into ``(number, variant)``; ``None`` if malformed.

    The identity of an id is ``(number, variant)`` and not its spelling, so
    ``Q-42`` and ``Q-042`` are the same entry — which is what keeps
    ``max + 1`` allocation and a caller-supplied qid in agreement.
    """
    m = _id_start_re(fmt).match(token.strip())
    if m is None or m.end() != len(token.strip()):
        return None
    return int(m.group("num")), (m.group("var") or "").lower()


# ── Entry parsing (contract rules 1, 3, 4, 6) ─────────────────────────────────


@dataclass(frozen=True)
class StatusToken:
    """A parsed status bracket: the word plus its span on the heading line."""

    word: str          # as written, e.g. "SUPERSEDED"
    start: int         # index of the word on the heading line
    end: int           # exclusive


@dataclass(frozen=True)
class Entry:
    """One parsed queue entry."""

    qid: str                     # the id token exactly as written on the heading
    number: int
    variant: str                 # "" or e.g. "ds"
    status: str | None           # lowercased status word, None when absent
    status_token: StatusToken | None
    heading: str                 # the heading line, verbatim
    start: int                   # line index of the heading
    end: int                     # exclusive line index of the boundary (rule 4)
    path: Path | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.number, self.variant)


def _boundary_re(level: int) -> re.Pattern[str]:
    """Any heading of the same-or-higher level — the rule-4 boundary."""
    return re.compile(rf"^ {{0,3}}#{{1,{level}}}(?!#)(?:\s|$)")


def _entry_heading_re(level: int) -> re.Pattern[str]:
    """A heading at exactly the configured level."""
    return re.compile(rf"^ {{0,3}}#{{{level}}}(?!#)[ \t]+")


def parse_status(line: str, fmt: QuestionsFormat) -> StatusToken | None:
    """Find the status bracket on a heading line (contract rule 3).

    The status is *not* necessarily the first bracket — the reference workspace
    has headings whose first bracket is ``[D1]`` or ``[role="tablist"]``.  The
    first bracket whose **first word** is in the configured set wins; matching
    is case-insensitive so ``[SUPERSEDED by Q-269]`` is a declared
    ``superseded``, and everything after that first word is free text that is
    preserved verbatim on rewrite.
    """
    status_set = fmt.status_set
    for m in re.finditer(r"\[([^\]\n]*)\]", line):
        inner = m.group(1)
        wm = re.match(r"\s*([A-Za-z0-9_]+)", inner)
        if wm is None:
            continue
        if wm.group(1).lower() in status_set:
            base = m.start(1)
            return StatusToken(word=wm.group(1), start=base + wm.start(1), end=base + wm.end(1))
    return None


def parse_entries(
    lines: Sequence[str],
    fmt: QuestionsFormat,
    path: Path | None = None,
) -> list[Entry]:
    """Parse every entry in *lines* (a file split on ``\\n``).

    Rule 1: only a heading at the configured level whose text *begins* with a
    well-formed id is an entry.  Rule 6: everything else — a ``## Q-NNN``
    schema example, a ``## ✅ ANSWER …`` summary, a ``### Q-450 item 4``
    sub-heading — is not an entry and is never touched.
    """
    level = fmt.level
    heading_re = _entry_heading_re(level)
    boundary_re = _boundary_re(level)
    id_re = _id_start_re(fmt)

    starts: list[tuple[int, re.Match[str], re.Match[str]]] = []
    for index, line in enumerate(lines):
        hm = heading_re.match(line)
        if hm is None:
            continue
        im = id_re.match(line[hm.end():])
        if im is None:
            continue  # rule 6: not an entry, left exactly as found
        starts.append((index, hm, im))

    entries: list[Entry] = []
    for index, hm, im in starts:
        end = len(lines)
        for j in range(index + 1, len(lines)):
            if boundary_re.match(lines[j]):
                end = j
                break
        status_token = parse_status(lines[index], fmt)
        entries.append(
            Entry(
                qid=im.group(0),
                number=int(im.group("num")),
                variant=(im.group("var") or "").lower(),
                status=status_token.word.lower() if status_token else None,
                status_token=status_token,
                heading=lines[index],
                start=index,
                end=end,
                path=path,
            )
        )
    return entries


# ── Answer escaping (state.md invariant 11) ───────────────────────────────────

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Line-leading shapes that could alter a later parse if a reader ever stripped
#: the indentation this module adds.  Guarding them is the "per-line guard" of
#: architecture §3.4; the 4-space indent is the "indented block".
_GUARD_PREFIXES = ("#", "<!--", "**", "```", "~~~")

_GUARD_RULE_RE = re.compile(r"^(?:-{3,}|={3,}|_{3,})\s*$")

#: Matches a bracketed status token at the very start of a (stripped) line, e.g.
#: ``[open]``, ``[resolved 2026-08-13]``, ``[OPEN]``.  Rule 3 only reads *bracketed*
#: tokens, so only the bracketed shape can ever be misread by the parser; an
#: unbracketed bare word (``Open the file…``, ``Resolved: proceed``) is structurally
#: inert on an indented answer line and must not receive a spurious backslash.
_STATUS_BRACKET_RE = re.compile(r"^\[([A-Za-z0-9_]+)")

ANSWER_MARKER_RE = re.compile(r"^ {0,3}<!--\s*answer:(\d+)\s*-->\s*$")


def _answer_marker(message_id: int | str) -> str:
    return f"<!-- answer:{int(message_id)} -->"


def _needs_guard(content: str, status_set: frozenset[str]) -> bool:
    if any(content.startswith(prefix) for prefix in _GUARD_PREFIXES):
        return True
    if _GUARD_RULE_RE.match(content):
        return True
    m = _STATUS_BRACKET_RE.match(content)
    if m is not None and m.group(1).lower() in status_set:
        return True
    return False


def _sanitise_inline(value: str) -> str:
    """Collapse a caller-supplied field to a single control-character-free line."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ")
    text = _CONTROL_RE.sub("", text)
    return text.strip()


def _sanitise_heading_field(value: str) -> str:
    """Inline-sanitise *and* neutralise brackets.

    Brackets are frame syntax on a heading line (the status token lives in
    one), so a title or tag carrying ``[`` / ``]`` could change which bracket a
    later parse reads as the status.  The frame is this module's to own
    (brd D3), so it renders them as parentheses.
    """
    return _sanitise_inline(value).replace("[", "(").replace("]", ")")


def escape_answer_text(text: str, fmt: QuestionsFormat | None = None) -> list[str]:
    """Render human answer text as lines that cannot forge structure.

    Three layers, all required (architecture §3.4):

    1. **Indent** — every content line is indented four spaces, so no produced
       line begins with ``#``, with a status bracket, or with an
       ``**Answered by:**``-shaped field, even after a leading-whitespace strip
       of up to the three spaces markdown allows before a heading.
    2. **Guard** — a line whose content would still start with a structural
       token is backslash-escaped, so a reader that fully ``lstrip``s each line
       still cannot see a heading, a fence, a comment marker or a status word.
    3. **Fence** — the block is wrapped in a backtick fence one longer than the
       longest backtick run in the text, so the answer can neither close the
       fence nor open a new one.

    Returns the block as a list of lines (no trailing newline).
    """
    fmt = fmt or QuestionsFormat()
    status_set = fmt.status_set

    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    normalised = normalised.replace("\t", "    ")
    normalised = _CONTROL_RE.sub("", normalised)

    longest_run = 0
    for run in re.findall(r"`+", normalised):
        longest_run = max(longest_run, len(run))
    fence = "`" * max(3, longest_run + 1)

    body: list[str] = []
    for raw_line in normalised.split("\n"):
        stripped = raw_line.lstrip()
        if not stripped:
            body.append("")
            continue
        indent = raw_line[: len(raw_line) - len(stripped)]
        if _needs_guard(stripped, status_set):
            stripped = "\\" + stripped
        body.append("    " + indent + stripped)

    # A trailing empty line inside the fence is noise, not information.
    while body and body[-1] == "":
        body.pop()

    return [f"{fence}text", *body, fence]


def render_answer_block(
    fmt: QuestionsFormat,
    *,
    qid: str,
    answer_text: str,
    answered_by: str,
    message_id: int,
    answered_at: str,
) -> list[str]:
    """Render the full answer block, marker line first.

    The ``<!-- answer:N -->`` marker is emitted by this module regardless of
    ``answer_template``, because idempotency (invariant 3) must not depend on a
    workspace's chosen wording.  Answer text can never forge it: the escaper
    guards a line starting with ``<!--`` and the marker is only ever recognised
    at line start.
    """
    answer_lines = escape_answer_text(answer_text, fmt)
    sentinel = "\x00ANSWER\x00"
    try:
        rendered = fmt.answer_template.format(
            answer=sentinel,
            answered_by=_sanitise_inline(answered_by),
            answered_at=_sanitise_inline(answered_at),
            message_id=int(message_id),
            qid=_sanitise_inline(qid),
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise QuestionsStoreError(f"answer_template is not renderable: {exc}") from exc

    out: list[str] = [_answer_marker(message_id)]
    for line in rendered.split("\n"):
        if sentinel not in line:
            out.append(line)
            continue
        before, after = line.split(sentinel, 1)
        # The fence must start at column 0 whatever the template does with it.
        if before.strip():
            out.append(before.rstrip())
        out.extend(answer_lines)
        if after.strip():
            out.append(after.lstrip())
    return out


# ── Anchor resolution (architecture §3.1) ─────────────────────────────────────


class AnchorResolution(NamedTuple):
    """``(root, workspace_id)`` plus the mode that actually produced the root."""

    root: Path
    workspace_id: str
    mode: str


def _read_gitdir_file(path: Path) -> Path | None:
    """Parse a ``.git`` *file* (``gitdir: …``) into an absolute path."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            target = line[len("gitdir:"):].strip()
            if not target:
                return None
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = (path.parent / candidate)
            return candidate
    return None


def _find_git(start: Path) -> tuple[Path, Path] | None:
    """Nearest ``(git_dir, worktree_root)`` at or above *start*, else None."""
    current = start
    while True:
        dot_git = current / ".git"
        if dot_git.is_dir():
            return dot_git, current
        if dot_git.is_file():
            git_dir = _read_gitdir_file(dot_git)
            if git_dir is not None:
                return git_dir, current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _git_common_dir(git_dir: Path) -> Path:
    """Resolve a worktree's git dir to the repository's common dir."""
    commondir = git_dir / "commondir"
    try:
        text = commondir.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return git_dir
    if not text:
        return git_dir
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = git_dir / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate


_GIT_SECTION_RE = re.compile(r'^\[([A-Za-z0-9_-]+)(?:\s+"[^"]*")?\]')


def _is_bare(common_dir: Path) -> bool:
    """True when *common_dir* belongs to a repository with no working tree.

    Only ``bare`` entries inside the ``[core]`` section (including
    ``[core "sub"]`` subsections) are considered.  A ``bare = true`` key
    under any other section (e.g. ``[branch "main"]``) is ignored.  When
    multiple ``bare`` lines appear inside ``[core]``, the *last* one wins,
    matching git's own last-value-wins behaviour within a section.
    """
    if common_dir.name != ".git":
        return True
    config = common_dir / "config"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    in_core = False
    result = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("["):
            sm = _GIT_SECTION_RE.match(stripped)
            in_core = bool(sm and sm.group(1).lower() == "core")
            continue
        if not in_core:
            continue
        m = re.match(r"^bare\s*=\s*(\S+)", stripped)
        if m is not None:
            result = m.group(1).lower() in ("true", "yes", "1", "on")
    return result


def _nearest_marker_root(start: Path) -> Path:
    """Nearest ancestor carrying ``.claude`` (the non-git fall-back), else cwd."""
    current = start
    while True:
        if (current / ".claude").exists():
            return current
        parent = current.parent
        if parent == current:
            return start
        current = parent


def resolve_anchor(start_dir: str | os.PathLike[str], config: QuestionsConfig) -> AnchorResolution:
    """Resolve the queue's root directory (architecture §3.1).

    The ladder::

        anchor = "repo"      → the repository's common git dir → its parent,
                               i.e. the primary worktree root; if that root has
                               no working tree (bare), fall through to the
                               cwd's own worktree root
        anchor = "worktree"  → the cwd's worktree root
        anchor = "path"      → the configured absolute path (``~`` expanded)
        not a git repo       → nearest ancestor holding ``.claude``, else cwd

    **Pure and side-effect free.**  It creates no directory, runs no
    subprocess and touches no git state — the ``.git`` metadata it needs
    (``gitdir:`` pointer, ``commondir``, ``core.bare``) is read directly.
    """
    start = Path(start_dir).expanduser()
    try:
        start = start.resolve()
    except OSError:
        start = Path(os.path.abspath(str(start)))

    workspace_id = config.workspace_id or start.name

    if config.anchor == "path":
        target = Path(config.path).expanduser()
        if target.is_absolute():
            return AnchorResolution(target, workspace_id, "path")
        # A relative ``path`` is a config error; load_questions_config already
        # recorded it. Degrade to the repo ladder rather than writing somewhere
        # surprising relative to the process cwd.

    found = _find_git(start)
    if found is None:
        return AnchorResolution(_nearest_marker_root(start), workspace_id, "non-git")

    git_dir, worktree_root = found

    if config.anchor == "worktree":
        return AnchorResolution(worktree_root, workspace_id, "worktree")

    common = _git_common_dir(git_dir)
    if _is_bare(common):
        # A bare repository has no primary worktree to anchor to.
        return AnchorResolution(worktree_root, workspace_id, "repo→worktree")

    primary = common.parent
    if not primary.is_dir():
        return AnchorResolution(worktree_root, workspace_id, "repo→worktree")
    return AnchorResolution(primary, workspace_id, "repo")


# ── The store ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewEntry:
    """What ``create_entry`` allocated and wrote."""

    qid: str
    path: Path
    rel_path: str
    workspace_id: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, text: str) -> None:
    """tmp + ``os.replace`` in the same directory (architecture §3.3).

    A reader — a human's editor, a concurrent ``apply_answer``, ``git`` — sees
    either the old file or the new one, never a partial one, and a writer that
    dies before the replace leaves the original untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Durability of the rename itself.
    try:
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


class QuestionsStore:
    """Read/write access to one workspace's question queue.

    Constructed against an already-resolved *root* so the listener can
    re-resolve the anchor at apply time (invariant 7) and hand the result in.
    """

    def __init__(self, config: QuestionsConfig, root: Path) -> None:
        self.config = config
        self.root = Path(root)

    # ── layout ──

    @property
    def fmt(self) -> QuestionsFormat:
        return self.config.fmt

    @property
    def workspace_id(self) -> str:
        return self.config.workspace_id

    @property
    def queue_dir(self) -> Path:
        directory = Path(self.config.dir).expanduser()
        return directory if directory.is_absolute() else self.root / directory

    @property
    def answered_dir(self) -> Path:
        return self.queue_dir / ANSWERED_DIRNAME

    def rel_path(self, path: Path) -> str:
        """The path of *path* relative to the anchor root (index-friendly)."""
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)

    def queue_file_for_role(self, role: str | None) -> Path | None:
        """The queue file a role writes to, or ``None`` when it is chat-only.

        A role absent from ``[questions.queue]`` has no queue (brd §5).  A
        workspace that declares no queue map at all degenerates to a single
        flat file, which is the greenfield adoption test.
        """
        if not self.config.queue:
            return self.queue_dir / self.config.file
        if role is None:
            role = self.config.default_role
        if role is None or role not in self.config.queue:
            return None
        return self.queue_dir / self.config.queue[role]

    def queue_files(self) -> list[Path]:
        """Every file that is part of this workspace's queue set."""
        found: dict[Path, None] = {}
        if self.config.queue:
            for filename in self.config.queue.values():
                found[self.queue_dir / filename] = None
        else:
            found[self.queue_dir / self.config.file] = None
        try:
            for path in sorted(self.queue_dir.glob(self.fmt.glob)):
                if path.is_file():
                    found[path] = None
        except OSError:
            pass
        return list(found)

    def scan_files(self) -> list[Path]:
        """The queue set **plus** ``answered/`` — the id-allocation scope.

        Scanning ``answered/`` is what stops an archived entry's id being
        reissued (contract rule 2).
        """
        files = list(self.queue_files())
        try:
            for path in sorted(self.answered_dir.glob("*.md")):
                if path.is_file():
                    files.append(path)
        except OSError:
            pass
        return files

    # ── reading ──

    def _read_lines(self, path: Path) -> list[str] | None:
        """Read *path* split on ``\\n``; ``None`` when it does not exist.

        ``"\\n".join(read_lines(p)) == p.read_text()`` exactly, so a rewrite
        preserves every byte this module did not deliberately change.
        """
        try:
            # newline="" disables universal-newline translation: a CRLF file must
            # round-trip byte-for-byte, not be silently rewritten to LF.
            with open(path, "r", encoding="utf-8", newline="") as fh:
                text = fh.read()
        except FileNotFoundError:
            return None
        except IsADirectoryError as exc:
            raise QuestionsStoreError(f"{path} is a directory, not a queue file") from exc
        except UnicodeDecodeError as exc:
            raise QuestionsStoreError(f"{path} is not valid UTF-8: {exc}") from exc
        except OSError as exc:
            raise QuestionsStoreError(f"{path} could not be read: {exc}") from exc
        return text.split("\n")

    def entries_in(self, path: Path) -> list[Entry]:
        """Every entry in *path* (empty when the file does not exist)."""
        lines = self._read_lines(path)
        if lines is None:
            return []
        return parse_entries(lines, self.fmt, path)

    def all_entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for path in self.queue_files():
            entries.extend(self.entries_in(path))
        return entries

    def count_open(self, role: str | None = None) -> int:
        """Open entries — the durable counter behind ``max_open`` (brd D12)."""
        open_token = self.fmt.open_status.lower()
        if role is not None:
            target = self.queue_file_for_role(role)
            paths = [target] if target is not None else []
        else:
            paths = self.queue_files()
        return sum(
            1
            for path in paths
            for entry in self.entries_in(path)
            if entry.status == open_token
        )

    def next_id(self) -> str:
        """The id ``create_entry`` would allocate next (``max + 1``).

        Read without the lock — diagnostics only.  The allocation that matters
        happens inside :meth:`create_entry`'s critical section.
        """
        return format_id(self.fmt, self._max_number() + 1)

    def _max_number(self) -> int:
        """High-water mark across the queue set and ``answered/``.

        Ids are collected from **heading lines at any level**, plus the names
        of ``answered/`` sidecar files.  Two deliberate choices:

        * *any* level, and *anywhere* in the heading, so a ``### Q-450 item 4``
          sub-heading or a ``## Q-388 — …after Q-253`` cross-reference still
          raises the mark.  Allocating high is always safe; reissuing is not.
        * headings only, never body text — the escaper guarantees no answer
          text can produce a line starting with ``#``, so a hostile answer
          cannot inflate the counter.
        """
        scan_re = _id_scan_re(self.fmt)
        any_heading_re = re.compile(r"^ {0,3}#{1,6}(?!#)(?:\s|$)")
        highest = 0
        for path in self.scan_files():
            lines = self._read_lines(path)
            if lines is None:
                continue
            for line in lines:
                if not any_heading_re.match(line):
                    continue
                for m in scan_re.finditer(line):
                    highest = max(highest, int(m.group("num")))
            m = scan_re.match(path.stem)
            if m is not None and m.end() == len(path.stem):
                highest = max(highest, int(m.group("num")))
        return highest

    # ── locking ──

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive ``flock`` on ``<dir>/.lock`` for the whole critical section.

        Every mutation — and the whole of [scan → allocate → compose → append →
        fsync] — happens inside this, which is what makes id allocation safe
        under parallelism (brd §2.7).
        """
        directory = self.queue_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QuestionsStoreError(f"{directory} could not be created: {exc}") from exc
        lock_path = directory / LOCK_FILENAME
        try:
            handle = open(lock_path, "a+")
        except OSError as exc:
            raise QuestionsStoreError(f"{lock_path} could not be opened: {exc}") from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    # ── composition (invariant 6: frame only) ──

    def compose_entry(
        self,
        *,
        qid: str,
        title: str,
        body: str = "",
        options: Sequence[str] = (),
        tags: Sequence[str] = (),
        routed_to: str | None = None,
    ) -> str:
        """Render one entry: frame plus the caller's ``body`` verbatim.

        The frame is heading, id, status token, tags, rendered options and the
        routing line — nothing else (brd D3, invariant 6).  A future field
        belongs in ``body``, which is passed through untouched.
        """
        clean_tags = [_sanitise_heading_field(tag) for tag in tags]
        rendered_tags = "".join(f"  [{tag}]" for tag in clean_tags if tag)
        try:
            heading = self.fmt.heading.format(
                id=qid,
                title=_sanitise_heading_field(title),
                status=self.fmt.open_status,
                tags=rendered_tags,
            )
        except (KeyError, IndexError, ValueError) as exc:
            raise QuestionsStoreError(f"heading template is not renderable: {exc}") from exc

        lines: list[str] = [heading, ""]

        body_lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while body_lines and body_lines[-1].strip() == "":
            body_lines.pop()
        while body_lines and body_lines[0].strip() == "":
            body_lines.pop(0)
        if body_lines:
            lines.extend(body_lines)
            lines.append("")

        clean_options = [_sanitise_inline(option) for option in options]
        clean_options = [option for option in clean_options if option]
        if clean_options:
            lines.append("**Options:**")
            for index, option in enumerate(clean_options, start=1):
                lines.append(f"{index}. {option}")
            lines.append("")

        alias = _sanitise_inline(routed_to or "").lstrip("@")
        if alias:
            lines.append(f"**Routed to:** @{alias}")
            lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    # ── mutations ──

    def create_entry(
        self,
        *,
        title: str,
        body: str = "",
        role: str | None = None,
        options: Sequence[str] = (),
        tags: Sequence[str] = (),
        routed_to: str | None = None,
        path: Path | None = None,
    ) -> NewEntry:
        """Allocate an id, compose the entry and append it — all under the lock.

        The critical section spans [scan → allocate → compose → append → fsync]
        so ``max + 1`` cannot be observed twice by two writers.
        """
        target = path if path is not None else self.queue_file_for_role(role)
        if target is None:
            raise QuestionsStoreError(
                f"role {role!r} has no queue file in [questions.queue]; it is chat-only"
            )

        with self.lock():
            qid = format_id(self.fmt, self._max_number() + 1)
            entry_text = self.compose_entry(
                qid=qid,
                title=title,
                body=body,
                options=options,
                tags=tags,
                routed_to=routed_to,
            )

            existing = self._read_lines(target)
            kept: list[str] = list(existing) if existing is not None else []
            suffix = _eol_suffix(kept)
            while kept and kept[-1].strip() == "":
                kept.pop()
            if kept:
                kept.append("" + suffix)  # exactly one blank line between entries
            kept.extend(line + suffix for line in entry_text.split("\n"))
            kept.append("")               # the file's trailing newline
            _atomic_write(target, "\n".join(kept))

        return NewEntry(
            qid=qid,
            path=target,
            rel_path=self.rel_path(target),
            workspace_id=self.workspace_id,
        )

    def mark_dispatched(
        self,
        qid: str,
        message_id: int,
        *,
        rel_path: str | None = None,
        role: str | None = None,
        dispatched_at: str | None = None,
    ) -> ApplyResult:
        """Record the Telegram message id on the entry (brd §6.1 step 5).

        Idempotent: a second call replaces the existing ``**Dispatched:**``
        line rather than adding another.
        """
        stamp = dispatched_at or _utc_now()
        marker = f"**Dispatched:** {stamp} · relay #{int(message_id)}"

        with self.lock():
            located = self._locate(qid, rel_path=rel_path, role=role)
            if located.result is not ApplyResult.APPLIED:
                return located.result
            assert located.path is not None and located.lines is not None and located.entry is not None

            lines = list(located.lines)
            entry = located.entry
            for index in range(entry.start + 1, entry.end):
                if lines[index].lstrip().startswith("**Dispatched:**"):
                    if lines[index] == marker:
                        return ApplyResult.APPLIED
                    lines[index] = marker + ("\r" if lines[index].endswith("\r") else "")
                    _atomic_write(located.path, "\n".join(lines))
                    return ApplyResult.APPLIED

            lines = _insert_before_boundary(lines, entry, [marker])
            _atomic_write(located.path, "\n".join(lines))
            return ApplyResult.APPLIED

    def apply_answer(
        self,
        qid: str,
        answer_text: str,
        answered_by: str,
        message_id: int,
        *,
        rel_path: str | None = None,
        role: str | None = None,
        answered_at: str | None = None,
    ) -> ApplyResult:
        """Write a human's answer into the entry (architecture §3.5).

        Flips the status token in place — preserving any free text after it —
        and inserts the rendered ``answer_template`` before the entry's
        boundary.  Moves nothing and deletes nothing: archival is a workspace
        action (brd §9).

        Idempotent on *message_id*: an answer block already citing it makes the
        call a no-op success (invariant 3).  ``not_found`` and ``conflict`` are
        normal outcomes routed to ``pending`` by the caller, never exceptions.
        """
        stamp = answered_at or _utc_now()

        with self.lock():
            located = self._locate(qid, rel_path=rel_path, role=role, message_id=message_id)
            if located.result is not ApplyResult.APPLIED:
                return located.result
            if located.entry is None:
                # Located by an existing marker: already applied.
                return ApplyResult.APPLIED
            assert located.path is not None and located.lines is not None

            entry = located.entry
            lines = list(located.lines)

            if entry.status_token is not None:
                token = entry.status_token
                heading = lines[entry.start]
                lines[entry.start] = (
                    heading[: token.start] + self.fmt.resolved_status + heading[token.end:]
                )
            # No status bracket → nothing to flip.  Rule 6: a non-conforming
            # heading is never repaired, so no token is invented here.

            block = render_answer_block(
                self.fmt,
                qid=qid,
                answer_text=answer_text,
                answered_by=answered_by,
                message_id=int(message_id),
                answered_at=stamp,
            )
            lines = _insert_before_boundary(lines, entry, block)
            _atomic_write(located.path, "\n".join(lines))
            return ApplyResult.APPLIED

    # ── locating ──

    def _target_paths(self, rel_path: str | None, role: str | None) -> list[Path]:
        if rel_path:
            candidate = Path(rel_path).expanduser()
            return [candidate if candidate.is_absolute() else self.root / candidate]
        if role is not None:
            target = self.queue_file_for_role(role)
            return [target] if target is not None else []
        return self.queue_files()

    def _locate(
        self,
        qid: str,
        *,
        rel_path: str | None = None,
        role: str | None = None,
        message_id: int | None = None,
    ) -> _Located:
        """Find the single entry for *qid* (contract rule 5).

        Zero candidates → ``not_found``; two or more → ``conflict``, in the
        target file or across the queue set.  The store never picks.
        """
        key = parse_id(self.fmt, qid)
        if key is None:
            return _Located(ApplyResult.NOT_FOUND, None, None, None)

        matches: list[tuple[Path, list[str], Entry]] = []
        for path in self._target_paths(rel_path, role):
            lines = self._read_lines(path)
            if lines is None:
                continue
            if message_id is not None and _has_answer_marker(lines, message_id):
                # Already applied — idempotent success even if the file has
                # since grown an ambiguity (invariant 3 outranks rule 5 here:
                # the work is provably done, so there is nothing to guess).
                return _Located(ApplyResult.APPLIED, path, lines, None)
            for entry in parse_entries(lines, self.fmt, path):
                if entry.key == key:
                    matches.append((path, lines, entry))

        if len(matches) == 1:
            path, lines, entry = matches[0]
            return _Located(ApplyResult.APPLIED, path, lines, entry)
        if len(matches) > 1:
            return _Located(ApplyResult.CONFLICT, None, None, None)
        return _Located(ApplyResult.NOT_FOUND, None, None, None)


@dataclass(frozen=True)
class _Located:
    result: ApplyResult
    path: Path | None
    lines: list[str] | None
    entry: Entry | None


def _eol_suffix(lines: Sequence[str]) -> str:
    """``"\r"`` when the file is CRLF-dominant, else ``""``.

    Lines are kept exactly as read (a CRLF file's lines still carry their
    trailing ``\r``), so only *newly inserted* lines need the terminator
    appended for the rewrite to stay consistent with the file it edits.
    """
    terminated = max(len(lines) - 1, 0)
    if not terminated:
        return ""
    crlf = sum(1 for line in lines if line.endswith("\r"))
    return "\r" if crlf * 2 > terminated else ""


def _has_answer_marker(lines: Sequence[str], message_id: int) -> bool:
    """True when some line *is* the answer marker for *message_id*.

    Anchored at line start on purpose: an answer whose text happens to contain
    ``<!-- answer:99 -->`` mid-line cannot make a later, different answer look
    already-applied.

    **Precondition:** ``message_id`` must be unique per qid *within the file*
    being searched.  If two different qids in the same file shared a
    ``message_id``, applying the second answer would incorrectly be reported as
    already done.  This precondition is guaranteed by the relay architecture:
    one relay message carries exactly one qid, so no two entries in the same
    queue file can share a ``message_id``.  23-05 additionally narrows the
    search to a single ``rel_path`` from the index, further reducing the
    (already-impossible) window.
    """
    wanted = str(int(message_id))
    for line in lines:
        m = ANSWER_MARKER_RE.match(line)
        if m is not None and m.group(1) == wanted:
            return True
    return False


def _insert_before_boundary(
    lines: Sequence[str],
    entry: Entry,
    block: Sequence[str],
) -> list[str]:
    """Insert *block* at the end of *entry*, before its boundary (rule 4).

    Trailing blank lines that separate this entry from the next are kept where
    they are, so the diff contains only the inserted lines.
    """
    result = list(lines)
    suffix = _eol_suffix(result)
    index = entry.end
    while index > entry.start + 1 and result[index - 1].strip() == "":
        index -= 1
    inserted = [line + suffix for line in ["", *block]]
    return result[:index] + inserted + result[index:]


# ── Entry points ──────────────────────────────────────────────────────────────


def open_store(
    workspace_dir: str | os.PathLike[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    roles_path: Path | None = None,
) -> QuestionsStore | None:
    """Load config, resolve the anchor and return a store — or ``None``.

    ``None`` means the workspace declares no ``[questions]`` section, and is
    the whole of state.md invariant 9: with no store there is no entry point to
    call, so nothing is read, nothing is created and nothing raises.  Callers
    check for ``None`` once instead of guarding every operation.
    """
    config = load_questions_config(str(workspace_dir), roles_path=roles_path)
    if config is None:
        return None
    anchor = resolve_anchor(cwd if cwd is not None else workspace_dir, config)
    return QuestionsStore(config, anchor.root)


def store_for_root(config: QuestionsConfig, root: str | os.PathLike[str]) -> QuestionsStore:
    """Build a store against an already-resolved root.

    The listener re-resolves the anchor at apply time from the index's
    ``workspace_id`` + anchor mode (invariant 7) and hands the result in here.
    """
    return QuestionsStore(config, Path(root))
