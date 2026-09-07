#!/usr/bin/env python3
"""Pure, bounded reducer over Claude lifecycle event logs (epic 37, task 37-02).

Sibling of ``codex_event_reducer.py``.  Reads the per-session
``<name>.lifecycle.jsonl`` artifact that ``lifecycle_events.py`` produces
and reduces it into normalised lifecycle facts that ``_derive_claude_status``
(``amux-spawn``) consumes to derive a Claude worker's state without reading,
stating, or parsing the worker's transcript.

**Purity contract (same as codex_event_reducer.py):**

- Standard library only — no subprocess, no network, no Telegram, no
  permission modules, no ``amux_spawn_lib``.
- Never reads or stats the worker's transcript.  The only file this module
  reads is the lifecycle log.
- Fail-soft throughout: a missing/unreadable/garbage log yields an empty
  reduction rather than an exception.

**State-hint derivation** (architecture §3, §6, state.md §9):

+-----------------------------------------------------+-------------------+
| Evidence in the log                                 | ``state_hint``    |
+=====================================================+===================+
| ``session_end`` seen                                | ``"terminated"``  |
+-----------------------------------------------------+-------------------+
| ``turn_start`` not yet closed by a ``stop``         | ``"running"``     |
+-----------------------------------------------------+-------------------+
| Last ``stop`` had ``background_tasks_count > 0``    | ``"running"``     |
+-----------------------------------------------------+-------------------+
| Most recent bg-draining event was a ``subagent_stop``| ``"running"``    |
| (final ``stop`` not yet fired)                      |                   |
+-----------------------------------------------------+-------------------+
| Last ``stop`` had ``background_tasks_count == 0``   | ``"idle"``        |
| AND was the most recent bg-draining event           |                   |
+-----------------------------------------------------+-------------------+
| No ``stop`` seen at all                             | ``None``          |
|                                                     | (caller uses      |
|                                                     | stored state)     |
+-----------------------------------------------------+-------------------+

The watchdog (``stale_activity``) is derived **at read time** by the caller
and is **never appended** to the log (state.md §12 / architecture §7 inv 12).

Reused-name handling: events from successive sessions under the same handle
name accumulate in one file.  Each record carries ``session_id``; the
current session's events start after the last ``session_end`` of an earlier
one.  The reducer processes all events in order (last-wins across sessions).
A consumer that needs per-session partitioning can filter on ``session_id``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ── Event type constants ─────────────────────────────────────────────────────
# Mirrored from lifecycle_events so this module stays pure stdlib — no project
# imports.

EVENT_TURN_START = "turn_start"
EVENT_STOP = "stop"
EVENT_SUBAGENT_STOP = "subagent_stop"
EVENT_PERMISSION_PROMPT = "permission_prompt"
EVENT_SESSION_END = "session_end"

KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_TURN_START,
    EVENT_STOP,
    EVENT_SUBAGENT_STOP,
    EVENT_PERMISSION_PROMPT,
    EVENT_SESSION_END,
})

#: Truncate individual lines to this before parsing (matches
#: ``lifecycle_events.MAX_RECORD_BYTES`` — records are bounded to ≤ 4000 B).
MAX_LINE_BYTES = 4096
#: Cap on the number of distinct unknown event types collected.
MAX_UNKNOWN_TYPES = 32

# Claude log suffix — kept here as a constant so callers can build the path
# without importing lifecycle_events.  MUST match CLAUDE_EVENT_SUFFIX there.
CLAUDE_EVENT_SUFFIX = ".lifecycle.jsonl"

# State hint values.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_TERMINATED = "terminated"


# ── Empty reduction ───────────────────────────────────────────────────────────

def empty_reduction() -> dict[str, Any]:
    """A reduction with no evidence (missing / refused / empty log).

    All boolean flags are ``False``, all counters ``0``, ``state_hint`` is
    ``None`` (the caller falls back to stored state).
    """
    return {
        "state_hint": None,           # None = no authoritative evidence yet
        "turn_open": False,           # True after turn_start until next stop
        "has_stop_event": False,      # At least one stop event seen
        "permission_pending": False,  # Snapshot from the most recent event
        "background_tasks_count": 0, # From the most recent stop/subagent_stop
        "last_stop_state": None,      # state field of the most recent stop
        "last_event_ts": None,        # ISO timestamp of the most recent event
        "terminated": False,          # session_end seen
        "events": 0,                  # Total valid events parsed
        "malformed_lines": 0,         # Lines that could not be parsed
        "truncated_tail": False,      # Last line was a partial live write
        "unknown_event_types": [],    # Event types not in KNOWN_EVENT_TYPES
    }


# ── Reduction logic ───────────────────────────────────────────────────────────

def reduce_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a list of parsed lifecycle event dicts into state facts.

    Pure and stateless: takes already-parsed dicts, never touches the
    filesystem.  Handles unknown event types, malformed dicts, and future
    event vocabulary without raising (forward-compatibility).

    **SubagentStop bg-drain semantics:** when a ``subagent_stop`` event
    reduces ``background_tasks_count`` to 0, the ``state_hint`` is still
    ``"running"`` — the final ``stop`` has not fired yet.  Only when a
    ``stop`` event is the most-recent bg-draining event does idle become
    reachable.  This preserves the invariant: ``"idle"`` is only reported
    when a ``stop`` (not a ``subagent_stop``) confirmed it.
    """
    out = empty_reduction()
    unknown: list[str] = []

    # "stop" = the last bg-draining event was a stop → idle is reachable.
    # "subagent_stop" = the last bg-draining event was a subagent_stop → not
    # yet settled; the final stop has not fired.
    last_bg_setter: str | None = None

    for ev in events:
        if not isinstance(ev, dict):
            out["malformed_lines"] += 1
            continue

        out["events"] += 1
        etype = ev.get("event")
        if not isinstance(etype, str):
            out["malformed_lines"] += 1
            continue

        # Every valid event advances the activity clock.
        ts = ev.get("ts")
        if isinstance(ts, str) and ts:
            out["last_event_ts"] = ts

        # permission_pending is carried as a snapshot on every event; last-wins.
        pp = ev.get("permission_pending")
        if isinstance(pp, bool):
            out["permission_pending"] = pp

        if etype == EVENT_TURN_START:
            out["turn_open"] = True
            # A new turn starting means any prior permission gate was resolved.
            out["permission_pending"] = False

        elif etype == EVENT_STOP:
            out["turn_open"] = False
            out["has_stop_event"] = True
            out["last_stop_state"] = ev.get("state")
            bg = ev.get("background_tasks_count", 0)
            if isinstance(bg, int):
                out["background_tasks_count"] = bg
            out["permission_pending"] = False
            last_bg_setter = "stop"

        elif etype == EVENT_SUBAGENT_STOP:
            bg = ev.get("background_tasks_count", 0)
            if isinstance(bg, int):
                out["background_tasks_count"] = bg
            if not (last_bg_setter == "stop" and out.get("last_stop_state") == STATE_IDLE):
                last_bg_setter = "subagent_stop"

        elif etype == EVENT_PERMISSION_PROMPT:
            out["permission_pending"] = True

        elif etype == EVENT_SESSION_END:
            out["terminated"] = True
            out["turn_open"] = False

        else:
            if etype not in unknown and len(unknown) < MAX_UNKNOWN_TYPES:
                unknown.append(etype)

    out["unknown_event_types"] = unknown

    # ── State hint ────────────────────────────────────────────────────────────
    if out["terminated"]:
        out["state_hint"] = STATE_TERMINATED
    elif out["turn_open"]:
        # A turn_start was recorded with no closing stop: turn is open.
        out["state_hint"] = STATE_RUNNING
    elif out["has_stop_event"]:
        if out["background_tasks_count"] > 0:
            # Background work still outstanding after the last stop.
            out["state_hint"] = STATE_RUNNING
        elif last_bg_setter == "subagent_stop":
            # A subagent just completed; main agent is responding.
            # The final stop has not fired — not yet idle.
            out["state_hint"] = STATE_RUNNING
        else:
            # stop confirmed empty background_tasks → settled idle.
            out["state_hint"] = STATE_IDLE
    else:
        # No stop event in the log: caller falls back to stored state.
        out["state_hint"] = None

    return out


# ── File reading ──────────────────────────────────────────────────────────────

def reduce_log(spawn_dir: Path, name: str) -> dict[str, Any]:
    """Read and reduce the Claude lifecycle log for handle *name*.

    Returns :func:`empty_reduction` on any error (missing file, unreadable,
    permission denied, etc.) so callers stay fail-open.

    Handles a partial last line written during a live ``O_APPEND`` without
    crashing: the final line is marked ``truncated_tail = True`` if it cannot
    be parsed, rather than dropped silently.
    """
    log_path = spawn_dir / f"{name}{CLAUDE_EVENT_SUFFIX}"
    try:
        with open(log_path, "rb") as f:
            raw = f.read()
    except OSError:
        return empty_reduction()

    if not raw:
        return empty_reduction()

    lines = raw.splitlines()
    last_index = len(lines) - 1
    events: list[dict[str, Any]] = []
    extra_malformed = 0
    truncated_tail = False

    for index, raw_line in enumerate(lines):
        line = raw_line[:MAX_LINE_BYTES].strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                events.append(ev)
            else:
                extra_malformed += 1
        except (json.JSONDecodeError, ValueError):
            if index == last_index:
                # Partial write during a live O_APPEND — tolerate.
                truncated_tail = True
            else:
                extra_malformed += 1

    out = reduce_events(events)
    out["malformed_lines"] += extra_malformed
    out["truncated_tail"] = truncated_tail
    return out
