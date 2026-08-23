#!/usr/bin/env python3
"""Pure, bounded reducer over a ``codex exec --json`` event artifact (task 20-01).

Epic 20 turns ``amux-spawn`` into a provider-neutral tracked-worker surface. The
Claude side derives state from Claude Code hooks plus the transcript mtime; the
Codex side has no hooks at all, so its lifecycle has to be *reduced* from three
files amux's launch wrapper produces per tracked session:

===========================  ===============================================
``<activity_path>``          the ``codex exec --json`` event stream (JSONL)
``<activity_path>.rc``       the process exit status (``printf '%s' $?``)
``<result_path>``            the ``--output-last-message`` final message
===========================  ===============================================

**Purity.** This module imports nothing but the standard library. It never
launches a process, never invokes ``amux``/``tmux``, and never imports the
Telegram or permission modules (epic-20 architecture s9 "Codex reducers do not
import Telegram or permission modules"). Everything here is a read of the three
paths above.

**It never reads Codex's internal transcripts.** ``~/.codex/sessions/`` (the
rollout files) is an unstable internal format and is explicitly out of contract
(BRD s5, invariant 5). :func:`reduce_events` refuses any path under
``~/.codex`` and reports ``refused_path=True`` instead of reading it.

**Boundedness.** Artifacts grow without limit (a long tool-using turn, or many
appended resume segments). Nothing here loads a whole artifact into memory: a
file up to :data:`MAX_EVENT_BYTES` is streamed line by line, and a larger one is
sampled head + tail (:data:`HEAD_SAMPLE_BYTES` / :data:`TAIL_SAMPLE_BYTES`) —
the head carries ``thread.started`` and the tail carries the authoritative turn
outcome, which are the only two facts that need the file's extremes. Individual
lines are truncated at :data:`MAX_LINE_BYTES` before parsing.

**Priority rule (epic-20 architecture s5, backed by the amux 01-01 spike on
codex-cli 0.149.0).** Completion evidence is authoritative and the exit code is
NOT:

- a valid ``turn.completed`` reduces to ``idle`` *regardless of exit code* —
  including the SIGKILL-orphan case where exit ``137`` and ``turn.completed``
  are both present, because the forked child finished the turn after the parent
  died;
- an explicit ``turn.failed`` reduces to ``terminated`` *regardless of exit
  code*;
- the exit code is consulted only when there is neither: a SIGTERMed run exits
  **0** having emitted only ``thread.started``/``turn.started``, so exit 0
  without ``turn.completed`` is ``terminated``, and the code travels as reason
  context only;
- an **absent** ``.rc`` file means amux's wrapper never reached its ``printf``
  (the session was killed), which is terminal evidence — but only when
  ``process_gone=True``; a caller that has not confirmed the session gone
  should re-check after the orphan window (see :func:`reduce_events` docstring);
- the ``--output-last-message`` file can exist with no ``turn.completed`` (the
  SIGKILL capture proves it), so its existence is never completion evidence.

Fail-soft throughout, matching ``amux_spawn_lib``: a missing/unreadable/garbage
artifact yields a reduction with no evidence rather than an exception, so the
callers stay fail-open.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ── Bounds ───────────────────────────────────────────────────────────────────

#: Stream the whole artifact only up to this size (8 MiB). Beyond it the file is
#: sampled head+tail instead — see the module docstring.
MAX_EVENT_BYTES = 8 * 1024 * 1024
#: Bytes read from the start of an oversized artifact (carries ``thread.started``).
HEAD_SAMPLE_BYTES = 64 * 1024
#: Bytes read from the end of an oversized artifact (carries the turn outcome).
TAIL_SAMPLE_BYTES = 1024 * 1024
#: A single event line longer than this is truncated before parsing (a
#: ``command_execution`` item embeds its whole aggregated output).
MAX_LINE_BYTES = 512 * 1024
#: Bound on :func:`read_result_text` (architecture s6: ``last`` is size-limited).
MAX_RESULT_BYTES = 256 * 1024
#: An exit-status ``.rc`` file is a handful of bytes; anything larger is junk.
MAX_RC_BYTES = 64
#: Cap on how many distinct unknown event/item type names are reported.
MAX_UNKNOWN_TYPES = 32

# ── Event vocabulary (amux 01-01 spike, codex-cli 0.149.0) ───────────────────

EVENT_THREAD_STARTED = "thread.started"
EVENT_TURN_STARTED = "turn.started"
EVENT_TURN_COMPLETED = "turn.completed"
EVENT_TURN_FAILED = "turn.failed"
EVENT_ITEM_STARTED = "item.started"
EVENT_ITEM_COMPLETED = "item.completed"
EVENT_ERROR = "error"

#: Every top-level ``type`` this reducer understands. Anything else is retained
#: in the raw artifact, counted, and otherwise ignored (forward compatibility).
KNOWN_EVENT_TYPES = frozenset({
    EVENT_THREAD_STARTED,
    EVENT_TURN_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_ITEM_STARTED,
    EVENT_ITEM_COMPLETED,
    EVENT_ERROR,
})

#: Item ``type`` values inside ``item.started`` / ``item.completed``.
ITEM_AGENT_MESSAGE = "agent_message"
ITEM_COMMAND_EXECUTION = "command_execution"
ITEM_ERROR = "error"
KNOWN_ITEM_TYPES = frozenset({
    ITEM_AGENT_MESSAGE,
    ITEM_COMMAND_EXECUTION,
    ITEM_ERROR,
})

# Normalized outcomes.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_INCOMPLETE = "incomplete"

# State hints handed to the derivation code (epic-20 architecture s5). ``None``
# means "no authoritative evidence yet" — the caller decides running/stuck from
# liveness and activity age.
STATE_IDLE = "idle"
STATE_TERMINATED = "terminated"


# ── Path guards ──────────────────────────────────────────────────────────────

def codex_internal_dir() -> Path:
    """The Codex internal state dir this reducer must never read from."""
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def is_codex_internal_path(path: str | os.PathLike[str] | None) -> bool:
    """True iff *path* points inside Codex's internal state dir.

    Guards invariant 5: lifecycle comes from the event artifact and the explicit
    final-message artifact, never from ``~/.codex/sessions/`` rollout files.
    Uses ``os.path.realpath`` to resolve symlinks before comparing, so a symlink
    outside ``CODEX_HOME`` pointing into it is correctly rejected.
    ``os.path.realpath`` on a nonexistent path normalizes lexically (does not
    raise), so the guard remains safe to call on absent-but-legitimate paths.
    """
    if not path:
        return False
    try:
        target = os.path.realpath(os.path.expanduser(str(path)))
        internal = os.path.realpath(str(codex_internal_dir()))
    except (OSError, ValueError):
        return False
    return target == internal or target.startswith(internal + os.sep)


# ── Small readers ────────────────────────────────────────────────────────────

def rc_path_for(activity_path: str | os.PathLike[str]) -> str:
    """The sibling exit-status file amux's wrapper writes: ``<activity>.rc``."""
    return f"{os.fspath(activity_path)}.rc"


def read_exit_code(activity_path: str | os.PathLike[str] | None) -> int | None:
    """Read ``<activity_path>.rc`` and return the exit code, or ``None``.

    ``None`` covers both "no ``.rc`` file" (the wrapper never finished — the
    session was killed) and an unparsable one. Callers distinguish the cases
    with ``exit_code_present`` in the reduction. ``printf '%s' $?`` writes no
    trailing newline, but the value is stripped anyway.
    """
    if not activity_path:
        return None
    path = rc_path_for(activity_path)
    if is_codex_internal_path(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_RC_BYTES)
    except OSError:
        return None
    try:
        return int(raw.decode("utf-8", "replace").strip())
    except ValueError:
        return None


def read_result_text(
    result_path: str | os.PathLike[str] | None,
    max_bytes: int = MAX_RESULT_BYTES,
) -> str | None:
    """Bounded read of the ``--output-last-message`` artifact, or ``None``.

    Returns ``None`` when there is no path, the file is missing/unreadable, or
    it is empty. The file is written WITHOUT a trailing newline; we strip
    trailing whitespace anyway so a future codex version adding one is a no-op.

    NOTE: a non-``None`` return is **not** completion evidence (see the module
    docstring) — only a ``turn.completed`` event is.
    """
    if not result_path or is_codex_internal_path(result_path):
        return None
    try:
        with open(result_path, "rb") as f:
            raw = f.read(max(0, int(max_bytes)))
    except OSError:
        return None
    text = raw.decode("utf-8", "replace").rstrip("\n")
    return text or None


def artifact_mtime(path: str | os.PathLike[str] | None) -> float | None:
    """The artifact's mtime (epoch float), or ``None`` if absent/unreadable.

    The 01-01 spike confirmed ``codex exec --json`` line-flushes even to a
    regular file, so this is a sound activity signal.
    """
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ── Line sampling ────────────────────────────────────────────────────────────

def _iter_sampled_lines(path: str, max_bytes: int) -> tuple[list[bytes], bool]:
    """Return ``(lines, sampled)`` for *path* under a byte budget.

    ``sampled`` is True when the artifact exceeded *max_bytes* and only its head
    and tail were read; the tail chunk's first (necessarily partial) line is
    dropped so it is never mistaken for a truncated live write.
    """
    with open(path, "rb") as f:
        try:
            size = os.fstat(f.fileno()).st_size
        except OSError:
            size = 0
        if size <= max_bytes:
            return f.read().splitlines(), False

        head = f.read(HEAD_SAMPLE_BYTES)
        f.seek(max(HEAD_SAMPLE_BYTES, size - TAIL_SAMPLE_BYTES))
        tail = f.read(TAIL_SAMPLE_BYTES)

    head_lines = head.splitlines()
    # The head chunk's own last line is cut mid-object by the byte budget, not by
    # a live writer: drop it.
    if head_lines:
        head_lines = head_lines[:-1]
    tail_lines = tail.splitlines()
    if tail_lines:
        tail_lines = tail_lines[1:]
    return head_lines + tail_lines, True


# ── The reducer ──────────────────────────────────────────────────────────────

def empty_reduction(
    activity_path: str | None = None,
    result_path: str | None = None,
) -> dict[str, Any]:
    """A reduction carrying no evidence at all (missing/refused artifact)."""
    return {
        "activity_path": activity_path,
        "result_path": result_path,
        "activity_mtime": None,
        "thread_id": None,
        "thread_ids": [],
        "thread_started": False,
        "turn_started": False,
        "turn_completed": False,
        "turn_failed": False,
        "segments": 0,
        "events": 0,
        "malformed_lines": 0,
        "truncated_tail": False,
        "sampled": False,
        "unknown_event_types": [],
        "unknown_item_types": [],
        "last_agent_message": None,
        "exit_code": None,
        "exit_code_present": False,
        "result_available": False,
        "result_text": None,
        "outcome": OUTCOME_INCOMPLETE,
        "state_hint": None,
        "failure": None,
        "refused_path": False,
    }


def reduce_events(
    activity_path: str | os.PathLike[str] | None,
    *,
    result_path: str | os.PathLike[str] | None = None,
    exit_code: int | None = None,
    exit_code_present: bool | None = None,
    process_gone: bool = False,
    max_bytes: int = MAX_EVENT_BYTES,
    read_result: bool = False,
) -> dict[str, Any]:
    """Reduce a Codex event artifact into normalized lifecycle facts.

    Parameters
    ----------
    activity_path:
        The ``codex exec --json`` event JSONL written by amux's launch wrapper.
    result_path:
        The ``--output-last-message`` artifact. Only its existence (and, with
        *read_result*, its bounded text) is used.
    exit_code / exit_code_present:
        Override the sibling ``.rc`` read — used by tests and by callers that
        already read it. When both are omitted the ``.rc`` file is consulted.
    process_gone:
        Whether the caller has observed the tmux session/process to be gone.
        Only affects the ``incomplete`` branch: with no completion evidence and
        a dead process the run is ``terminated``. NOTE the 01-01 evidence that
        a SIGKILLed parent leaves an orphan which can append ``turn.completed``
        up to ~10 s later, so a caller must re-run the reducer rather than latch
        the first "gone" observation.
    read_result:
        Also load the (bounded) final-message text into ``result_text``.

    Returns a plain dict (JSON-serializable) — see :func:`empty_reduction` for
    the full key set. The two keys state derivation consumes are ``outcome``
    (``completed`` / ``failed`` / ``incomplete``) and ``state_hint``
    (``idle`` / ``terminated`` / ``None`` = no authoritative evidence yet).
    """
    path = os.fspath(activity_path) if activity_path else None
    res_path = os.fspath(result_path) if result_path else None
    out = empty_reduction(path, res_path)

    if path and is_codex_internal_path(path):
        # Never parse Codex's internal rollout files (invariant 5).
        out["refused_path"] = True
        path = None
    if res_path and is_codex_internal_path(res_path):
        out["refused_path"] = True
        res_path = None
        out["result_path"] = None

    # ── exit status ──
    if exit_code_present is None:
        rc = read_exit_code(path) if path else None
        out["exit_code"] = rc
        out["exit_code_present"] = rc is not None
    else:
        out["exit_code"] = exit_code
        out["exit_code_present"] = bool(exit_code_present)

    # ── final-message artifact ──
    if res_path:
        try:
            out["result_available"] = os.path.getsize(res_path) > 0
        except OSError:
            out["result_available"] = False
        if read_result:
            out["result_text"] = read_result_text(res_path)

    # ── events ──
    lines: list[bytes] = []
    if path:
        out["activity_mtime"] = artifact_mtime(path)
        try:
            lines, out["sampled"] = _iter_sampled_lines(path, max_bytes)
        except OSError:
            lines = []

    unknown_events: list[str] = []
    unknown_items: list[str] = []
    last_index = len(lines) - 1
    # The last authoritative turn boundary seen **within the newest segment**,
    # so appended resume segments are resolved last-wins per segment (see
    # ``outcome`` below and EVENT_THREAD_STARTED in the loop).
    last_turn_event: str | None = None
    failure_message: str | None = None

    for index, raw_line in enumerate(lines):
        line = raw_line[:MAX_LINE_BYTES].strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A partial final line is the documented live-write case: tolerate
            # it and mark the reduction. An earlier bad line is just malformed.
            if index == last_index:
                out["truncated_tail"] = True
            else:
                out["malformed_lines"] += 1
            continue
        if not isinstance(event, dict):
            out["malformed_lines"] += 1
            continue

        out["events"] += 1
        etype = event.get("type")
        if not isinstance(etype, str):
            out["malformed_lines"] += 1
            continue

        if etype == EVENT_THREAD_STARTED:
            out["thread_started"] = True
            out["segments"] += 1
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid and tid not in out["thread_ids"]:
                out["thread_ids"].append(tid)
            # A new segment began: any turn outcome already seen belongs to an
            # OLDER attempt and must stop governing (amux appends resume
            # segments to the same log; architecture §5 — "the next attempt
            # temporarily returns to spawning/running"). Until this segment
            # emits its own turn terminal event there is no authoritative
            # outcome, exactly like a first segment mid-turn. Without this
            # reset a resumed worker mid-turn false-read idle off the previous
            # attempt's turn.completed once the new segment's first event
            # landed (epic-20 sign-off finding F1).
            last_turn_event = None
            failure_message = None
        elif etype == EVENT_TURN_STARTED:
            out["turn_started"] = True
        elif etype == EVENT_TURN_COMPLETED:
            out["turn_completed"] = True
            last_turn_event = EVENT_TURN_COMPLETED
        elif etype == EVENT_TURN_FAILED:
            out["turn_failed"] = True
            last_turn_event = EVENT_TURN_FAILED
            failure_message = _error_message(event.get("error")) or failure_message
        elif etype == EVENT_ERROR:
            # A bare error event precedes turn.failed; keep its text as a
            # fallback failure reason but do NOT treat it as a turn outcome.
            failure_message = failure_message or _error_message(event)
        elif etype in (EVENT_ITEM_STARTED, EVENT_ITEM_COMPLETED):
            _reduce_item(event.get("item"), out, unknown_items)
        else:
            if etype not in unknown_events and len(unknown_events) < MAX_UNKNOWN_TYPES:
                unknown_events.append(etype)

    out["unknown_event_types"] = unknown_events
    out["unknown_item_types"] = unknown_items
    # The FIRST thread.started is the authoritative thread identity: a resume
    # re-emits the same id, and a name-miss resume emits a different one (which
    # is exactly the mismatch the launcher must fail closed on).
    out["thread_id"] = out["thread_ids"][0] if out["thread_ids"] else None

    # ── outcome (architecture s5 priority rule) ──
    #
    # Completion evidence is authoritative; the exit code is never consulted
    # here. ``last_turn_event`` is last-wins *within the newest segment* (a
    # ``thread.started`` resets it, see the loop), so an appended resume
    # segment governs: a single segment can only carry one of the two events,
    # so this reduces exactly to s5 for the non-resume case, while a successful
    # resume after a failed attempt is not permanently poisoned by the old
    # failure (s7: "a failed resume preserves the previous result and
    # evidence") and an IN-FLIGHT resumed segment inherits no verdict at all
    # from the older one.
    if last_turn_event == EVENT_TURN_FAILED:
        out["outcome"] = OUTCOME_FAILED
        out["state_hint"] = STATE_TERMINATED
        out["failure"] = {
            "reason": "turn_failed",
            "message": failure_message,
            "exit_code": out["exit_code"],
        }
    elif last_turn_event == EVENT_TURN_COMPLETED:
        out["outcome"] = OUTCOME_COMPLETED
        out["state_hint"] = STATE_IDLE
        out["failure"] = None
    else:
        out["outcome"] = OUTCOME_INCOMPLETE
        # No completion evidence and no explicit failure: only NOW does the exit
        # code matter, and only as reason context. An absent .rc means amux's
        # wrapper never finished (killed session) — terminal, not "running".
        finished = out["exit_code_present"] or process_gone
        if finished:
            out["state_hint"] = STATE_TERMINATED
            out["failure"] = {
                "reason": _incomplete_reason(out),
                "message": failure_message,
                "exit_code": out["exit_code"],
            }
        else:
            # Still live (or unobserved): the caller derives running/stuck from
            # liveness plus ``activity_mtime``.
            out["state_hint"] = None
            out["failure"] = None

    return out


def _incomplete_reason(out: dict[str, Any]) -> str:
    """Normalized reason for a run that ended with no turn outcome."""
    if not out["exit_code_present"]:
        # No .rc file: the launch wrapper never reached its printf.
        return "no_exit_status" if out["thread_started"] else "no_evidence"
    if out["exit_code"] == 0:
        # The SIGTERM case: exit 0 with no completion event.
        return "no_completion_event"
    return "nonzero_exit"


def _error_message(payload: Any) -> str | None:
    """Best-effort human-readable message out of an error-shaped payload.

    ``turn.failed`` carries ``{"error": {"message": "<json string>"}}`` where the
    message is itself a JSON error document; a bare ``error`` event carries the
    same string at the top level. We unwrap one level of nested JSON when it is
    present so the normalized failure is a sentence, not a blob — architecture
    s3 ("normalized summary, not raw terminal scrape").
    """
    if isinstance(payload, str):
        message = payload
    elif isinstance(payload, dict):
        raw = payload.get("message")
        message = raw if isinstance(raw, str) else None
    else:
        message = None
    if not message:
        return None
    stripped = message.strip()
    if stripped.startswith("{"):
        try:
            nested = json.loads(stripped)
        except ValueError:
            return message
        if isinstance(nested, dict):
            inner = nested.get("error")
            if isinstance(inner, dict) and isinstance(inner.get("message"), str):
                return inner["message"]
            if isinstance(nested.get("message"), str):
                return nested["message"]
    return message


def _reduce_item(item: Any, out: dict[str, Any], unknown_items: list[str]) -> None:
    """Fold one ``item.started`` / ``item.completed`` payload into *out*.

    ``item.completed`` ids restart at ``item_0`` in EVERY process, so they are
    unique per resume segment only and are deliberately not used as keys here.
    """
    if not isinstance(item, dict):
        return
    itype = item.get("type")
    if not isinstance(itype, str):
        return
    if itype == ITEM_AGENT_MESSAGE:
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            out["last_agent_message"] = text
    elif itype not in KNOWN_ITEM_TYPES:
        if itype not in unknown_items and len(unknown_items) < MAX_UNKNOWN_TYPES:
            unknown_items.append(itype)


def reduce_handle(
    handle: dict[str, Any],
    *,
    process_gone: bool = False,
    read_result: bool = False,
) -> dict[str, Any]:
    """Reduce the Codex artifacts referenced by a tracked-session *handle*.

    Convenience wrapper: pulls ``activity_path`` / ``result_path`` out of the
    epic-20 handle schema. Kept import-free of ``amux_spawn_lib`` so the reducer
    stays a leaf module (the handle is just a dict).
    """
    if not isinstance(handle, dict):
        return empty_reduction()
    activity = handle.get("activity_path") or handle.get("transcript_path")
    return reduce_events(
        activity if isinstance(activity, str) else None,
        result_path=handle.get("result_path"),
        process_gone=process_gone,
        read_result=read_result,
    )
