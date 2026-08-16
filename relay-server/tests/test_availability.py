"""Tests for relay_server.availability — the active-time engine (epic 19-01)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from relay_server.availability import (
    Window,
    advance_active,
    format_windows,
    is_active,
    next_active_start,
    parse_tz,
    parse_windows,
)

UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def berlin(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN)


# ---------------------------------------------------------------------------
# parse_tz
# ---------------------------------------------------------------------------

class TestParseTz:
    def test_valid(self):
        assert parse_tz("Europe/Berlin") == "Europe/Berlin"
        assert parse_tz("UTC") == "UTC"
        assert parse_tz("America/New_York") == "America/New_York"

    def test_invalid(self):
        assert parse_tz("Bad/Zone") is None
        assert parse_tz("25:00") is None
        assert parse_tz("") is None


# ---------------------------------------------------------------------------
# parse_windows / format_windows round-trip
# ---------------------------------------------------------------------------

class TestParseWindows:
    def test_simple(self):
        ws = parse_windows("mon 09:00-17:00")
        assert ws == [Window(0, 9 * 60, 17 * 60)]

    def test_daily(self):
        ws = parse_windows("mon-sun 09:00-19:00")
        assert len(ws) == 7
        for i, w in enumerate(ws):
            assert w.weekday == i
            assert w.start_minute == 9 * 60
            assert w.end_minute == 19 * 60

    def test_multi_clause(self):
        ws = parse_windows("mon-fri 09:00-19:00, sat 11:00-15:00")
        assert len(ws) == 6
        weekdays = [w.weekday for w in ws]
        assert 0 in weekdays and 4 in weekdays and 5 in weekdays

    def test_wrapping_day_range(self):
        # fri-mon: fri=4, sat=5, sun=6, mon=0
        ws = parse_windows("fri-mon 09:00-17:00")
        weekdays = {w.weekday for w in ws}
        assert weekdays == {0, 4, 5, 6}

    def test_midnight_crossing_splits(self):
        ws = parse_windows("mon 22:00-02:00")
        assert len(ws) == 2
        # First: mon 22:00-24:00
        assert Window(0, 22 * 60, 24 * 60) in ws
        # Second: tue 00:00-02:00
        assert Window(1, 0, 2 * 60) in ws

    def test_empty_string_returns_none(self):
        assert parse_windows("") is None
        assert parse_windows("   ") is None

    def test_bad_tz_hour(self):
        with pytest.raises(ValueError):
            parse_windows("mon 25:00-17:00")

    def test_inverted_times_raises(self):
        # start == end is invalid
        with pytest.raises(ValueError):
            parse_windows("mon 09:00-09:00")

    def test_unknown_day_raises(self):
        with pytest.raises(ValueError):
            parse_windows("xyz 09:00-17:00")


class TestFormatWindows:
    def test_round_trip_simple(self):
        spec = "mon 09:00-17:00"
        ws = parse_windows(spec)
        assert ws is not None
        assert format_windows(ws) == spec

    def test_round_trip_multi(self):
        spec = "mon-fri 09:00-19:00, sat 11:00-15:00"
        ws = parse_windows(spec)
        assert ws is not None
        result = format_windows(ws)
        # Re-parse and compare windows (order may differ in string but semantics same)
        ws2 = parse_windows(result)
        assert set(ws) == set(ws2)

    def test_round_trip_daily(self):
        ws = parse_windows("mon-sun 00:00-23:59")
        assert ws is not None
        out = format_windows(ws)
        ws2 = parse_windows(out)
        assert set(ws) == set(ws2)

    def test_round_trip_midnight_crossing(self):
        ws = parse_windows("mon 22:00-02:00")
        assert ws is not None
        out = format_windows(ws)
        ws2 = parse_windows(out)
        assert ws2 is not None
        assert set(ws) == set(ws2)

    def test_round_trip_wrapping_range(self):
        ws = parse_windows("fri-mon 09:00-17:00")
        assert ws is not None
        out = format_windows(ws)
        ws2 = parse_windows(out)
        assert ws2 is not None
        assert set(ws) == set(ws2)

    def test_round_trip_sat_only(self):
        spec = "sat 11:00-15:00"
        ws = parse_windows(spec)
        assert ws is not None
        assert format_windows(ws) == spec

    def test_round_trip_single_day_explicit(self):
        spec = "wed 08:30-16:45"
        ws = parse_windows(spec)
        assert ws is not None
        assert format_windows(ws) == spec


# ---------------------------------------------------------------------------
# advance_active — behaviour table from the task spec
# ---------------------------------------------------------------------------

# Daily 09:00–19:00 windows for all 7 days.
_DAILY_09_19 = parse_windows("mon-sun 09:00-19:00")
assert _DAILY_09_19 is not None

# Mon–Fri only, 09:00–19:00.
_WEEKDAY_09_19 = parse_windows("mon-fri 09:00-19:00")
assert _WEEKDAY_09_19 is not None

TZ_UTC = "UTC"


class TestAdvanceActiveTable:
    """Behaviour table from task 19-01."""

    def test_inside_window_30m(self):
        # now=09:00 Mon, window 09:00–19:00 daily, delta=30m → 09:30 Mon
        now = berlin(2024, 1, 1, 9, 0)  # Monday (weekday 0)
        assert now.weekday() == 0
        result = advance_active(now, timedelta(minutes=30), "Europe/Berlin", _DAILY_09_19)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        assert result_local.hour == 9
        assert result_local.minute == 30
        assert result_local.date() == now.date()

    def test_spans_window_end_30m(self):
        # now=18:50 Mon, window 09:00–19:00 daily, delta=30m → 09:20 Tue
        now = berlin(2024, 1, 1, 18, 50)
        result = advance_active(now, timedelta(minutes=30), "Europe/Berlin", _DAILY_09_19)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        assert result_local.hour == 9
        assert result_local.minute == 20
        # Should be the next day (Tue).
        expected_date = now.date() + timedelta(days=1)
        assert result_local.date() == expected_date

    def test_before_window_30m(self):
        # now=02:00 Tue, window 09:00–19:00 daily, delta=30m → 09:30 Tue
        now = berlin(2024, 1, 2, 2, 0)  # Tuesday
        result = advance_active(now, timedelta(minutes=30), "Europe/Berlin", _DAILY_09_19)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        assert result_local.hour == 9
        assert result_local.minute == 30
        assert result_local.date() == now.date()

    def test_weekend_crosses_to_monday(self):
        # now=18:00 Fri, mon-fri only, delta=4h → 12:00 Mon
        # Friday (weekday 4) 18:00, 4h active time needed.
        # 18:00–19:00 = 1h on Friday. Need 3h more.
        # Mon 09:00 + 3h = 12:00 Mon.
        now = berlin(2024, 1, 5, 18, 0)  # Friday 2024-01-05
        assert now.weekday() == 4
        result = advance_active(now, timedelta(hours=4), "Europe/Berlin", _WEEKDAY_09_19)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        assert result_local.hour == 12
        assert result_local.minute == 0
        # Should be Monday (weekday 0).
        assert result_local.weekday() == 0

    def test_always_available(self):
        # windows=None → now + delta
        now = berlin(2024, 1, 1, 3, 0)
        delta = timedelta(hours=2, minutes=15)
        result = advance_active(now, delta, "Europe/Berlin", None)
        assert result is not None
        assert abs((result - (now + delta)).total_seconds()) < 1

    def test_never_active_returns_none(self):
        # windows=[] → None (no windows, never active)
        now = berlin(2024, 1, 1, 9, 0)
        result = advance_active(now, timedelta(minutes=30), "Europe/Berlin", [])
        assert result is None

    def test_sat_11_11_is_zero_length_never_active(self):
        # "sat 11:00-11:00" parses with an error (zero-length), but
        # a user might produce an empty list some other way.  The zero-length
        # parse case: it should raise ValueError.
        with pytest.raises(ValueError):
            parse_windows("sat 11:00-11:00")


# ---------------------------------------------------------------------------
# Never-active terminates within 14 days
# ---------------------------------------------------------------------------

class TestNeverActive:
    def test_empty_windows_returns_none_not_loop(self):
        now = berlin(2024, 6, 1, 12, 0)
        assert advance_active(now, timedelta(minutes=1), "Europe/Berlin", []) is None

    def test_next_active_start_empty_returns_none(self):
        now = berlin(2024, 6, 1, 12, 0)
        assert next_active_start(now, "Europe/Berlin", []) is None


# ---------------------------------------------------------------------------
# Always-available property: advance_active(now, d, None) == now + d
# ---------------------------------------------------------------------------

class TestAlwaysAvailable:
    def test_various_deltas(self):
        nows = [
            berlin(2024, 1, 1, 0, 0),
            berlin(2024, 6, 15, 13, 30),
            berlin(2024, 12, 31, 23, 59),
        ]
        deltas = [
            timedelta(0),
            timedelta(minutes=15),
            timedelta(hours=3),
            timedelta(days=1),
        ]
        for now in nows:
            for delta in deltas:
                result = advance_active(now, delta, None, None)
                assert result is not None
                expected = now + delta
                assert abs((result - expected).total_seconds()) < 1, (
                    f"now={now}, delta={delta}: got {result}, expected {expected}"
                )

    def test_with_explicit_tz_none_windows_none(self):
        now = utc(2024, 3, 15, 10, 0)
        delta = timedelta(hours=2)
        result = advance_active(now, delta, None, None)
        assert result == now + delta


# ---------------------------------------------------------------------------
# DST: spring-forward (Europe/Berlin: last Sunday March → clocks move from
#       02:00 local → 03:00, so 02:00–03:00 does not exist)
# ---------------------------------------------------------------------------

class TestDSTSpringForward:
    """Europe/Berlin: spring-forward on the last Sunday of March.
    In 2024 that is 2024-03-31 at 02:00 local (clock jumps to 03:00).
    """

    def test_window_spanning_gap_loses_hour(self):
        # Window: mon-sun 01:00-05:00 Europe/Berlin
        # On spring-forward day (2024-03-31, Sunday), 02:00-03:00 doesn't exist.
        # Active time: 01:00→02:00 (1h) then the clock jumps so 03:00 appears.
        # The window "resumes" at 03:00 (already past in wall time).
        # So from 01:00 to 05:00 on that day there are only 3h of real time
        # (01:00-02:00 = 1h, then gap, then 03:00-05:00 = 2h) → 3h total active.
        windows = parse_windows("mon-sun 01:00-05:00")
        assert windows is not None
        # Start just before spring-forward.
        now = berlin(2024, 3, 31, 1, 0)  # 01:00 local on spring-forward Sunday
        # 4 nominal hours in the window, but only 3 wall-clock hours of active time.
        # advance_active with delta=3h should land at or before 05:00 local.
        result = advance_active(now, timedelta(hours=3), "Europe/Berlin", windows)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        # Should be 05:00 on the same day (consumed all active time in window).
        assert result_local.date() == now.date()
        assert result_local.hour == 5
        assert result_local.minute == 0

    def test_advance_crosses_spring_forward_midnight(self):
        # Now = 18:00 Sat (day before spring-forward), window mon-sun 09:00-19:00.
        # Delta = 12h of active time.
        # Sat: 18:00-19:00 = 1h. Need 11h more.
        # Sun 2024-03-31 09:00-19:00: due to spring-forward (02:00→03:00),
        # that window still has 10 wall-clock hours (spring-forward happens at 02:00,
        # outside our 09:00-19:00 window, so the window is unaffected).
        # Actually the spring-forward is at 02:00, before our 09:00 window opens,
        # so the window is a full 10 hours.
        windows = parse_windows("mon-sun 09:00-19:00")
        assert windows is not None
        now = berlin(2024, 3, 30, 18, 0)  # Sat 18:00
        result = advance_active(now, timedelta(hours=12), "Europe/Berlin", windows)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        # 1h Sat + 10h Sun = 11h. Need 12h → 1h into Mon = Mon 10:00.
        # Wait: Sat 18:00-19:00 = 1h. Sun 09:00-19:00 = 10h. Total = 11h.
        # Need 12h. Mon 09:00 + 1h = Mon 10:00.
        assert result_local.weekday() == 0  # Monday
        assert result_local.hour == 10
        assert result_local.minute == 0


# ---------------------------------------------------------------------------
# DST: fall-back (Europe/Berlin: last Sunday October → clocks move from
#       03:00 local → 02:00, so 02:00–03:00 occurs twice)
# ---------------------------------------------------------------------------

class TestDSTFallBack:
    """Europe/Berlin: fall-back on 2024-10-27 at 03:00 local (reverts to 02:00)."""

    def test_window_during_fallback_does_not_double_count(self):
        # Window: mon-sun 01:00-04:00 Europe/Berlin
        # On fall-back day (2024-10-27, Sunday), 02:00-03:00 occurs twice.
        # Active window 01:00-04:00 spans 4 UTC hours (fall-back adds 1h, not
        # subtracts), so the implementation correctly credits 4h, not 3h.
        windows = parse_windows("mon-sun 01:00-04:00")
        assert windows is not None
        now = berlin(2024, 10, 27, 1, 0)  # 01:00 local on fall-back Sunday
        # The window is 01:00-04:00. In UTC that is:
        # 01:00 CEST = 23:00 UTC (Sat). 04:00 CET = 03:00 UTC (Sun).
        # UTC duration: 03:00 - 23:00 = 4h (because of the extra hour).
        # The window in wall-clock seconds is 4h (fall-back adds 1h).
        # advance_active(delta=4h) should reach 04:00 local.
        result = advance_active(now, timedelta(hours=4), "Europe/Berlin", windows)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        assert result_local.date() == now.date()
        assert result_local.hour == 4
        assert result_local.minute == 0

    def test_advance_across_fallback_boundary(self):
        # Start before the window, delta = small, crosses the fall-back gap.
        # Window: mon-sun 09:00-19:00 — well outside the fall-back hour, so unaffected.
        windows = parse_windows("mon-sun 09:00-19:00")
        assert windows is not None
        now = berlin(2024, 10, 27, 18, 50)  # 18:50 on fall-back Sunday
        # 10 minutes left in window. delta = 30m. Spills 20m into the next window (Mon).
        result = advance_active(now, timedelta(minutes=30), "Europe/Berlin", windows)
        assert result is not None
        result_local = result.astimezone(BERLIN)
        assert result_local.weekday() == 0  # Monday
        assert result_local.hour == 9
        assert result_local.minute == 20


# ---------------------------------------------------------------------------
# is_active
# ---------------------------------------------------------------------------

class TestIsActive:
    def test_inside(self):
        windows = parse_windows("mon-sun 09:00-19:00")
        assert is_active(berlin(2024, 1, 1, 12, 0), "Europe/Berlin", windows)

    def test_outside(self):
        windows = parse_windows("mon-sun 09:00-19:00")
        assert not is_active(berlin(2024, 1, 1, 22, 0), "Europe/Berlin", windows)

    def test_always_available(self):
        assert is_active(berlin(2024, 1, 1, 3, 0), "Europe/Berlin", None)

    def test_never_active(self):
        assert not is_active(berlin(2024, 1, 1, 12, 0), "Europe/Berlin", [])
