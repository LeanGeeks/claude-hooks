"""Pure availability-time arithmetic for epic 19.

No DB, no network, no clock reads.  ``now`` is always a parameter.
Importable with no relay state (e.g. by epic 15).

A ``Window`` is ``(weekday, start_minute, end_minute)`` in **local** time.
``weekday`` is 0=Monday … 6=Sunday (``datetime.weekday()`` convention).
``start_minute`` and ``end_minute`` are minutes since midnight; a window is
non-wrapping (``start_minute < end_minute``).  Midnight-crossing specs are
split into two non-wrapping windows at parse time.

All timezone work uses ``zoneinfo`` so DST is handled correctly: we never
store a fixed UTC offset.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta
from typing import NamedTuple

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

__all__ = [
    "Window",
    "parse_tz",
    "near_tz_matches",
    "parse_windows",
    "format_windows",
    "parse_duration",
    "parse_nudge_schedule",
    "format_nudge_schedule",
    "describe_active_status",
    "is_active",
    "next_active_start",
    "advance_active",
]

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_NAMES = {v: k for k, v in _DAYS.items()}

# Maximum search horizon for advance_active / next_active_start (14 days).
_MAX_SEARCH_DAYS = 14


class Window(NamedTuple):
    """A non-wrapping weekly availability window in local time."""

    weekday: int    # 0=Mon … 6=Sun
    start_minute: int  # minutes since midnight
    end_minute: int    # minutes since midnight; strictly > start_minute


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_tz(name: str) -> str | None:
    """Return *name* if it is a valid IANA timezone; ``None`` otherwise."""
    if not name or not name.strip():
        return None
    if name in available_timezones():
        return name
    # zoneinfo.available_timezones() may not include aliases on all platforms;
    # try constructing the zone as a secondary check.
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


def near_tz_matches(name: str, n: int = 3) -> list[str]:
    """Return up to *n* timezone names that resemble *name*.

    Uses difflib for fuzzy matching, then falls back to a case-insensitive
    substring scan so that "Berlin" still finds "Europe/Berlin".
    """
    candidates = sorted(available_timezones())
    matches: list[str] = list(
        difflib.get_close_matches(name, candidates, n=n, cutoff=0.4)
    )
    if len(matches) < n:
        lower = name.lower()
        for c in candidates:
            if lower in c.lower() and c not in matches:
                matches.append(c)
                if len(matches) >= n:
                    break
    return matches[:n]


def _parse_minute(s: str) -> int:
    """Parse ``HH:MM`` into minutes since midnight, or raise ``ValueError``.

    ``24:00`` is accepted as a special end-of-day marker (1440 minutes).
    It is only produced by ``format_windows`` for midnight-split windows and
    is not a valid *start* time — the caller enforces that.
    """
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        raise ValueError(f"bad time format: {s!r}")
    h, mn = int(m.group(1)), int(m.group(2))
    if h == 24 and mn == 0:
        return 24 * 60  # end-of-day sentinel; only valid as window end
    if h > 23 or mn > 59:
        raise ValueError(f"time out of range: {s!r}")
    return h * 60 + mn


def _expand_day_range(spec: str) -> list[int]:
    """Return list of weekday ints for a day spec like ``mon``, ``mon-fri``, ``fri-mon``."""
    spec = spec.strip().lower()
    if "-" in spec:
        parts = spec.split("-", 1)
        if len(parts) != 2:
            raise ValueError(f"bad day range: {spec!r}")
        start_name, end_name = parts[0].strip(), parts[1].strip()
        if start_name not in _DAYS:
            raise ValueError(f"unknown day: {start_name!r}")
        if end_name not in _DAYS:
            raise ValueError(f"unknown day: {end_name!r}")
        s, e = _DAYS[start_name], _DAYS[end_name]
        if s <= e:
            return list(range(s, e + 1))
        # Wrapping range (e.g. fri-mon): fri=4 sat=5 sun=6 mon=0
        return list(range(s, 7)) + list(range(0, e + 1))
    else:
        if spec not in _DAYS:
            raise ValueError(f"unknown day: {spec!r}")
        return [_DAYS[spec]]


def _parse_clause(clause: str) -> list[Window]:
    """Parse one clause like ``mon-fri 09:00-19:00`` into ``Window`` objects.

    Midnight-crossing windows (``22:00-02:00``) are split into two.
    """
    clause = clause.strip()
    if not clause:
        raise ValueError("empty clause")
    # Split on whitespace; expect exactly two tokens: days and time range.
    parts = clause.split()
    if len(parts) != 2:
        raise ValueError(f"expected '<days> <HH:MM>-<HH:MM>', got: {clause!r}")
    day_spec, time_spec = parts
    time_parts = time_spec.split("-", 1)
    if len(time_parts) != 2:
        raise ValueError(f"bad time range: {time_spec!r}")
    start_min = _parse_minute(time_parts[0])
    end_min = _parse_minute(time_parts[1])
    if start_min >= 24 * 60:
        raise ValueError(f"start time out of range: {time_parts[0]!r}")
    if start_min == end_min:
        raise ValueError(
            f"inverted or zero-length window: {time_spec!r}"
        )
    days = _expand_day_range(day_spec)
    windows: list[Window] = []
    for day in days:
        if start_min < end_min:
            windows.append(Window(day, start_min, end_min))
        else:
            # Midnight-crossing: split at 00:00.
            # First segment: [start_min, 1440) on this day.
            windows.append(Window(day, start_min, 24 * 60))
            # Second segment: [0, end_min) on the next day.
            next_day = (day + 1) % 7
            windows.append(Window(next_day, 0, end_min))
    return windows


def parse_windows(spec: str) -> list[Window] | None:
    """Parse a window spec string into a list of ``Window`` objects.

    Returns ``None`` on any parse error; the error reason is attached as
    ``parse_windows.error`` on ``None`` — but callers that want the reason
    should catch the ``ValueError`` from ``_parse_clause`` directly.

    Actually: this function returns ``None`` for empty/blank input and raises
    ``ValueError`` with a structured reason for other errors, so 19-02 can
    relay the reason to the user.
    """
    if not spec or not spec.strip():
        return None
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        return None
    windows: list[Window] = []
    for clause in clauses:
        windows.extend(_parse_clause(clause))
    return windows


def format_windows(windows: list[Window]) -> str:
    """Serialise *windows* to canonical string form.

    Emits one clause per window.  Consecutive days with identical time ranges
    are collapsed to a non-wrapping range (``mon-fri``) when the days form a
    contiguous, non-wrapping sequence.  Wrapping and non-contiguous groups are
    each emitted as individual single-day clauses to guarantee round-trip
    parseability.
    """
    if not windows:
        return ""

    def _time_str(start_min: int, end_min: int) -> str:
        return (
            f"{start_min // 60:02d}:{start_min % 60:02d}"
            f"-{end_min // 60:02d}:{end_min % 60:02d}"
        )

    # Group windows that share identical (start_minute, end_minute) AND form a
    # contiguous non-wrapping weekday sequence.
    # Strategy: build output clause by clause; try to merge the next window into
    # the last clause if they share times and are adjacent weekdays.
    parts: list[str] = []
    # Each entry: (days: list[int], start_min, end_min) — days is contiguous, sorted.
    groups: list[tuple[list[int], int, int]] = []

    for w in windows:
        merged = False
        for g in groups:
            g_days, g_start, g_end = g
            if g_start == w.start_minute and g_end == w.end_minute:
                # Only merge if the new day extends the contiguous range by one.
                last_day = g_days[-1]
                first_day = g_days[0]
                if w.weekday == (last_day + 1) % 7 and (last_day + 1) < 7:
                    # Contiguous non-wrapping extension.
                    g_days.append(w.weekday)
                    merged = True
                    break
        if not merged:
            groups.append(([w.weekday], w.start_minute, w.end_minute))

    for days, start_min, end_min in groups:
        ts = _time_str(start_min, end_min)
        if len(days) == 1:
            parts.append(f"{_DAY_NAMES[days[0]]} {ts}")
        else:
            parts.append(f"{_DAY_NAMES[days[0]]}-{_DAY_NAMES[days[-1]]} {ts}")

    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Active-time queries
# ---------------------------------------------------------------------------

def _local_dt(dt: datetime, tz: ZoneInfo) -> datetime:
    """Convert *dt* (aware UTC or already local) to a local-time-aware datetime."""
    return dt.astimezone(tz)


def _window_bounds_utc(
    date_local: datetime, w: Window, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return UTC-aware (start, end) for window *w* on the local date of *date_local*.

    ``date_local`` must be a datetime in *tz* (or any timezone — only its
    .date() is used).
    """
    local_date = date_local.date()
    start_h, start_m = w.start_minute // 60, w.start_minute % 60
    end_h, end_m = w.end_minute // 60, w.end_minute % 60
    # Handle end_minute == 1440 (midnight of next day).
    if w.end_minute == 24 * 60:
        next_date = local_date + timedelta(days=1)
        end_naive = datetime(next_date.year, next_date.month, next_date.day, 0, 0, 0)
    else:
        end_naive = datetime(local_date.year, local_date.month, local_date.day, end_h, end_m, 0)
    start_naive = datetime(local_date.year, local_date.month, local_date.day, start_h, start_m, 0)
    # Use fold=0 for start (first occurrence in ambiguous hour), fold=0 for end.
    start_aware = start_naive.replace(tzinfo=tz)
    end_aware = end_naive.replace(tzinfo=tz)
    return start_aware.astimezone(ZoneInfo("UTC")), end_aware.astimezone(ZoneInfo("UTC"))


def is_active(now: datetime, tz: str | None, windows: list[Window] | None) -> bool:
    """Return ``True`` if *now* falls inside any availability window.

    With ``windows is None`` the person is always available.  With an empty
    list they are never available.
    """
    if windows is None:
        return True
    if not windows:
        return False
    if tz is None:
        tz_zone = ZoneInfo("UTC")
    else:
        tz_zone = ZoneInfo(tz)
    now_local = _local_dt(now, tz_zone)
    local_weekday = now_local.weekday()
    for w in windows:
        if w.weekday != local_weekday:
            continue
        start_utc, end_utc = _window_bounds_utc(now_local, w, tz_zone)
        now_utc = now.astimezone(ZoneInfo("UTC"))
        if start_utc <= now_utc < end_utc:
            return True
    return False


def next_active_start(
    now: datetime, tz: str | None, windows: list[Window] | None
) -> datetime | None:
    """Return the next moment (>= *now*) when an active window opens.

    Returns *now* if currently active.  Returns ``None`` if windows are empty
    (never active) or if no window starts within 14 days.

    With ``windows is None`` (always available), returns *now*.
    """
    if windows is None:
        return now
    if not windows:
        return None
    if tz is None:
        tz_zone = ZoneInfo("UTC")
    else:
        tz_zone = ZoneInfo(tz)
    now_utc = now.astimezone(ZoneInfo("UTC"))
    now_local = _local_dt(now, tz_zone)

    deadline = now_utc + timedelta(days=_MAX_SEARCH_DAYS)
    # Iterate day by day for up to 14 days.
    for day_offset in range(_MAX_SEARCH_DAYS + 1):
        check_local = now_local + timedelta(days=day_offset)
        local_weekday = check_local.weekday()
        for w in windows:
            if w.weekday != local_weekday:
                continue
            start_utc, end_utc = _window_bounds_utc(check_local, w, tz_zone)
            if end_utc <= now_utc:
                # Window already ended.
                continue
            if start_utc > deadline:
                return None
            # Window is relevant; return max(start_utc, now_utc).
            return max(start_utc, now_utc)
    return None


def advance_active(
    now: datetime,
    delta: timedelta,
    tz: str | None,
    windows: list[Window] | None,
) -> datetime | None:
    """Return the wall-clock instant at which *delta* of active time elapses.

    ``delta`` must be non-negative.  Returns ``None`` if no solution is found
    within 14 days (i.e. the window config is effectively never-active).

    With ``windows is None`` (always available), returns ``now + delta``.
    """
    if windows is None:
        return now + delta
    if not windows:
        return None
    if tz is None:
        tz_zone = ZoneInfo("UTC")
    else:
        tz_zone = ZoneInfo(tz)

    remaining = delta
    cursor_utc = now.astimezone(ZoneInfo("UTC"))
    deadline = cursor_utc + timedelta(days=_MAX_SEARCH_DAYS)

    # Short-circuit: if delta is zero, just find next active start.
    if remaining == timedelta(0):
        result = next_active_start(now, tz, windows)
        return result

    # Iterate: find the next active window from cursor, consume as much of
    # remaining as possible, advance cursor.
    while remaining > timedelta(0):
        if cursor_utc >= deadline:
            return None
        # Find the next active window start from cursor.
        cursor_aware = cursor_utc.astimezone(tz_zone)
        # We need to find which windows are open at cursor_utc or open next.
        # Check current moment first.
        cursor_local = _local_dt(cursor_utc, tz_zone)
        local_weekday = cursor_local.weekday()

        # Find the active window segment that contains cursor_utc or starts soonest.
        best_start: datetime | None = None
        best_end: datetime | None = None

        for day_offset in range(_MAX_SEARCH_DAYS + 1):
            check_local = cursor_local + timedelta(days=day_offset)
            local_weekday = check_local.weekday()
            for w in windows:
                if w.weekday != local_weekday:
                    continue
                start_utc, end_utc = _window_bounds_utc(check_local, w, tz_zone)
                if end_utc <= cursor_utc:
                    continue  # Already passed.
                # start_utc might be before cursor_utc (we're inside window).
                effective_start = max(start_utc, cursor_utc)
                if best_start is None or effective_start < best_start:
                    best_start = effective_start
                    best_end = end_utc
            if best_start is not None:
                break

        if best_start is None or best_end is None:
            return None  # Never active within horizon.

        if best_start >= deadline:
            return None

        # Clamp best_end to deadline.
        best_end = min(best_end, deadline)

        window_duration = best_end - best_start
        if window_duration <= timedelta(0):
            # Degenerate window; advance cursor past it and retry.
            cursor_utc = best_end + timedelta(seconds=1)
            continue

        if window_duration >= remaining:
            # This window contains the answer.
            return best_start + remaining
        else:
            # Consume the entire window and continue.
            remaining -= window_duration
            cursor_utc = best_end

    # remaining reached zero — shouldn't happen (handled above), but guard.
    return cursor_utc


# ---------------------------------------------------------------------------
# Nudge schedule parsing (epic 19-02)
# ---------------------------------------------------------------------------

# Matches forms like "15m", "3h", "2h30m".  Both groups are optional but at
# least one must be present (enforced by the caller).
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")


def parse_duration(s: str) -> timedelta | None:
    """Parse a duration like ``15m``, ``3h``, or ``2h30m`` into a timedelta.

    Returns ``None`` if the string is empty, zero-length, or does not match
    the expected form.
    """
    s = s.strip()
    if not s:
        return None
    m = _DURATION_RE.fullmatch(s)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    if hours == 0 and minutes == 0:
        return None  # Zero-length is not a valid nudge interval.
    return timedelta(hours=hours, minutes=minutes)


def parse_nudge_schedule(spec: str, max_entries: int) -> list[timedelta]:
    """Parse a nudge schedule like ``15m,45m,3h`` into a list of timedeltas.

    Raises ``ValueError`` if any entry is unparseable or the list is longer
    than *max_entries* (reject rather than truncate silently).
    """
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise ValueError("empty nudge schedule")
    if len(parts) > max_entries:
        raise ValueError(
            f"nudge schedule has {len(parts)} entries; server cap is {max_entries}"
        )
    result: list[timedelta] = []
    for p in parts:
        td = parse_duration(p)
        if td is None:
            raise ValueError(
                f"bad duration {p!r}; accepted forms: 15m, 3h, 2h30m"
            )
        result.append(td)
    return result


def format_nudge_schedule(schedule: list[timedelta]) -> str:
    """Serialise a nudge schedule back to canonical string form (``15m,45m,3h``)."""
    parts: list[str] = []
    for td in schedule:
        total_minutes = int(td.total_seconds() // 60)
        h, m = divmod(total_minutes, 60)
        if h and m:
            parts.append(f"{h}h{m}m")
        elif h:
            parts.append(f"{h}h")
        else:
            parts.append(f"{m}m")
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Human-readable active-status description (epic 19-02)
# ---------------------------------------------------------------------------


def _current_window_end(
    now_utc: datetime, tz_zone: ZoneInfo, windows: list[Window], now_local: datetime
) -> datetime | None:
    """Return the UTC end of the window that currently contains *now_utc*.

    Returns ``None`` if *now_utc* is not inside any window (caller should check
    :func:`is_active` first).
    """
    local_weekday = now_local.weekday()
    best_end: datetime | None = None
    for w in windows:
        if w.weekday != local_weekday:
            continue
        start_utc, end_utc = _window_bounds_utc(now_local, w, tz_zone)
        if start_utc <= now_utc < end_utc:
            # Prefer the earliest-ending window if two overlap.
            if best_end is None or end_utc < best_end:
                best_end = end_utc
    return best_end


def describe_active_status(
    now: datetime, tz: str | None, windows: list[Window] | None
) -> str:
    """Return a human-readable string describing current availability.

    Examples:
    - ``"always available"``
    - ``"active now, until 19:00"``
    - ``"active now, until Fri 00:00"``
    - ``"inactive — next window Mon 09:00"``
    - ``"inactive — next window today 14:00"``
    - ``"inactive — next window tomorrow 09:00"``
    - ``"never active"``
    """
    if windows is None:
        return "always available"

    tz_zone = ZoneInfo(tz) if tz else ZoneInfo("UTC")
    now_local = _local_dt(now, tz_zone)
    now_utc = now.astimezone(ZoneInfo("UTC"))

    if is_active(now, tz, windows):
        end_utc = _current_window_end(now_utc, tz_zone, windows, now_local)
        if end_utc is None:
            return "active now"
        end_local = end_utc.astimezone(tz_zone)
        if end_local.date() == now_local.date():
            return f"active now, until {end_local.strftime('%H:%M')}"
        return f"active now, until {end_local.strftime('%a %H:%M')}"

    nxt = next_active_start(now, tz, windows)
    if nxt is None:
        return "never active"
    nxt_local = nxt.astimezone(tz_zone)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    if nxt_local.date() == today:
        return f"inactive — next window today {nxt_local.strftime('%H:%M')}"
    if nxt_local.date() == tomorrow:
        return f"inactive — next window tomorrow {nxt_local.strftime('%H:%M')}"
    return f"inactive — next window {nxt_local.strftime('%a %H:%M')}"
