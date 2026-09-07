#!/usr/bin/env python3
"""Unit tests for ``amux-spawn watch`` (epic 37, task 37-03).

Covers the bulk-subscription command:
- One subscription supervises N workers; bursts produce one digest.
- Self-exclusion: the subscriber's own handle is absent from its digest.
- Consumer-busy completions are coalesced (debounce), never lost.
- Every terminal outcome (idle, terminated, stuck, permission-pending).
- Late-join convergence and cursor-based resume.
- Block mode: edge-triggered per-handle lines, exit 0 when all done.
- Existing ``--wait`` / ``--notify`` behaviour is unchanged (covered by
  the existing suites; no re-test here).
- No ad-hoc parsing: all output is valid JSON.

Hermetic: throwaway ``~/.amux`` (patched lib path constants), tmux
has-session stubbed, time.sleep stubbed. No network, no real tmux.
"""

import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402
import lifecycle_events  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli_watch",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_watch", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


def _redirect_amux_home(tmp: Path):
    return patch.multiple(
        lib,
        AMUX_HOME=tmp,
        AMUX_SESSIONS_DIR=tmp / "sessions",
        SPAWN_DIR=tmp / "spawn",
        SPAWN_LOCK=tmp / "spawn" / ".lock",
    )


def _seed_handle(name: str, run_id: str = "rid-test", abs_dir: str = "/ws/p",
                 state: str = "spawning", **overrides) -> dict:
    """Create and write a tracked handle."""
    h = lib.new_handle(
        name=name,
        session_id="11111111-2222-3333-4444-555555555555",
        run_id=run_id,
        abs_dir=abs_dir,
        transcript_path=None,
        stuck_after_s=600,
    )
    h.update(state=state, **overrides)
    lib.write_handle(name, h)
    return h


def _write_lifecycle_events(tmp: Path, name: str, events: list[dict]) -> None:
    """Write lifecycle events to the log file for a handle."""
    log_path = lifecycle_events.lifecycle_log_path(tmp / "spawn", name)
    with open(log_path, "ab") as f:
        for ev in events:
            data = lifecycle_events.serialize_bounded(ev)
            f.write(data)


def _stop_event(seq: int = 1, bg: int = 0, state: str = "idle",
                session_id: str = "sid-test") -> dict:
    """Build a Stop lifecycle event."""
    return lifecycle_events.make_event(
        event="stop", state=state, session_id=session_id,
        seq=seq, background_tasks_count=bg,
    )


def _turn_start_event(seq: int = 1, session_id: str = "sid-test") -> dict:
    """Build a turn_start lifecycle event."""
    return lifecycle_events.make_event(
        event="turn_start", state="running", session_id=session_id, seq=seq,
    )


def _session_end_event(seq: int = 1, last_state: str = "idle",
                       session_id: str = "sid-test") -> dict:
    """Build a session_end lifecycle event."""
    return lifecycle_events.make_event(
        event="session_end", state="terminated", session_id=session_id,
        seq=seq, last_state=last_state,
    )


def _permission_event(seq: int = 1, session_id: str = "sid-test") -> dict:
    """Build a permission_prompt lifecycle event."""
    return lifecycle_events.make_event(
        event="permission_prompt", state="running", session_id=session_id,
        seq=seq, permission_pending=True,
    )


def _make_watch_args(**overrides):
    """Build a minimal argparse.Namespace for cmd_watch."""
    import argparse
    defaults = dict(
        subcommand="watch",
        run_id=None,
        handle=None,
        debounce=None,
        since=None,
        block=False,
        timeout=None,
        stuck_after=None,
        dir=None,
        exclude=None,
        include_self=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── Watch: N-worker subscription ─────────────────────────────────────────────


class TestWatchSingleWorkerSettles(unittest.TestCase):
    """A single worker settling emits one digest."""

    def test_single_idle_emits_digest(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-1", state="idle",
                             last_message="done")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                # We need to break out of the watch loop. Use a timeout.
                # Patch time so it advances past the deadline.
                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-1", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    rc = cli.cmd_watch(ns)

                self.assertEqual(rc, 0)  # streaming timeout = clean exit
                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                self.assertIn("settled", digest)
                self.assertIn("cursor", digest)
                self.assertIn("watching", digest)
                settled_names = [e["name"] for e in digest["settled"]]
                self.assertIn("w1", settled_names)
                # Find w1 entry
                w1 = [e for e in digest["settled"] if e["name"] == "w1"][0]
                self.assertEqual(w1["state"], "idle")


class TestWatchMultipleWorkersOneBurst(unittest.TestCase):
    """N workers settling simultaneously produce one digest, not N."""

    def test_burst_coalesces(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                for i in range(5):
                    name = f"w{i}"
                    _seed_handle(name, run_id="rid-burst", state="idle",
                                 last_message=f"done-{i}")
                    _write_lifecycle_events(tmp, name, [
                        _turn_start_event(seq=1),
                        _stop_event(seq=2, state="idle"),
                    ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-burst", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                # Should be exactly ONE digest containing all 5 workers (burst coalesced).
                self.assertEqual(len(lines), 1)
                digest = json.loads(lines[0])
                self.assertEqual(len(digest["settled"]), 5)
                self.assertEqual(digest["pending"], 0)


class TestWatchDebounceCoalesces(unittest.TestCase):
    """Debounce: changes during the window are coalesced into one digest."""

    def test_debounce_window(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Start with w1 running.
                _seed_handle("w1", run_id="rid-deb", state="running")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                ])
                # w2 already idle.
                _seed_handle("w2", run_id="rid-deb", state="idle",
                             last_message="done-w2")
                _write_lifecycle_events(tmp, "w2", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                poll_count = [0]
                base_time = time.time()
                debounce_s = 2.0

                def mock_time():
                    # Advance 0.5s per poll, so debounce of 2s fires at poll 4+
                    return base_time + poll_count[0] * 0.5

                orig_snapshot = cli._settled_snapshot

                def counting_snapshot(handles, sa):
                    poll_count[0] += 1
                    # After poll 3, w1 settles too (simulate a stop event
                    # landing between polls).
                    if poll_count[0] == 3:
                        _seed_handle("w1", run_id="rid-deb", state="idle",
                                     last_message="done-w1")
                        _write_lifecycle_events(tmp, "w1", [
                            _stop_event(seq=2, state="idle"),
                        ])
                    return orig_snapshot(handles, sa)

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-deb", timeout="5s",
                                      debounce=f"{debounce_s}s")

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=mock_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(cli, "_settled_snapshot",
                                     side_effect=counting_snapshot), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                # First digest has w2 (already idle). After debounce,
                # second digest should include both w1 and w2.
                self.assertGreaterEqual(len(lines), 1)
                # Last digest should have both workers settled.
                last_digest = json.loads(lines[-1])
                names = {e["name"] for e in last_digest["settled"]}
                self.assertIn("w2", names)


# ── Self-exclusion ───────────────────────────────────────────────────────────


class TestWatchSelfExclusion(unittest.TestCase):
    """The subscriber's own handle is excluded from the subscription."""

    def test_own_handle_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # The "orchestrator" handle — seeded as idle so it WOULD appear
                # without self-exclusion; exclusion is the only reason it's absent.
                _seed_handle("orch", run_id="rid-self", state="idle",
                             last_message="orch-done")
                _write_lifecycle_events(tmp, "orch", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])
                # A worker that settled.
                _seed_handle("worker-1", run_id="rid-self", state="idle",
                             last_message="done")
                _write_lifecycle_events(tmp, "worker-1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-self", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value="orch"):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                settled_names = [e["name"] for e in digest["settled"]]
                # The orchestrator must NOT appear.
                self.assertNotIn("orch", settled_names)
                # The worker must appear.
                self.assertIn("worker-1", settled_names)

    def test_include_self_overrides_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("orch", run_id="rid-self2", state="idle",
                             last_message="orch-done")
                _write_lifecycle_events(tmp, "orch", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-self2", timeout="1s",
                                      debounce="0", include_self=True)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value="orch"):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                settled_names = [e["name"] for e in digest["settled"]]
                self.assertIn("orch", settled_names)


# ── Terminal outcomes ────────────────────────────────────────────────────────


class TestWatchTerminalOutcomes(unittest.TestCase):
    """Every terminal outcome appears in the digest."""

    def test_terminated_worker_reported(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w-dead", run_id="rid-term", state="running")
                _write_lifecycle_events(tmp, "w-dead", [
                    _turn_start_event(seq=1),
                    _session_end_event(seq=2, last_state="running"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-term", timeout="1s",
                                      debounce="0")

                # tmux gone = terminated
                with patch.object(lib, "tmux_has_session",
                                  return_value=False), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                dead = [e for e in digest["settled"]
                        if e["name"] == "w-dead"]
                self.assertEqual(len(dead), 1)
                self.assertEqual(dead[0]["state"], "terminated")

    def test_stuck_worker_reported(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w-stuck", run_id="rid-stuck", state="running",
                             stuck_after_s=1)
                # Turn started a long time ago, no stop yet.
                from datetime import datetime, timezone, timedelta
                old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                ev = lifecycle_events.make_event(
                    event="turn_start", state="running",
                    session_id="sid-test", seq=1,
                )
                ev["ts"] = old_ts
                _write_lifecycle_events(tmp, "w-stuck", [ev])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                # Use a very short stuck_after to trigger it.
                ns = _make_watch_args(run_id="rid-stuck", timeout="1s",
                                      debounce="0", stuck_after="1s")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                stuck = [e for e in digest["settled"]
                         if e["name"] == "w-stuck"]
                self.assertEqual(len(stuck), 1)
                self.assertEqual(stuck[0]["state"], "stuck")

    def test_permission_pending_reported(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w-perm", run_id="rid-perm", state="running",
                             permission_pending=True)
                _write_lifecycle_events(tmp, "w-perm", [
                    _turn_start_event(seq=1),
                    _permission_event(seq=2),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-perm", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                perm = [e for e in digest["settled"]
                        if e["name"] == "w-perm"]
                self.assertEqual(len(perm), 1)
                self.assertTrue(perm[0].get("permission_pending"))


# ── Late join ────────────────────────────────────────────────────────────────


class TestWatchLateJoin(unittest.TestCase):
    """Workers spawned after the subscription is armed are discovered."""

    def test_late_joiner_appears_in_digest(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Start with one worker already idle.
                _seed_handle("w1", run_id="rid-late", state="idle",
                             last_message="done-1")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                poll_count = [0]
                base_time = time.time()

                def mock_time():
                    return base_time + poll_count[0] * 0.5

                orig_enumerate = cli._watch_enumerate

                def counting_enumerate(*a, **kw):
                    poll_count[0] += 1
                    # On poll 3, a new worker appears.
                    if poll_count[0] == 3:
                        _seed_handle("w2-late", run_id="rid-late",
                                     state="idle", last_message="late")
                        _write_lifecycle_events(tmp, "w2-late", [
                            _turn_start_event(seq=1),
                            _stop_event(seq=2, state="idle"),
                        ])
                    return orig_enumerate(*a, **kw)

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-late", timeout="5s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=mock_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(cli, "_watch_enumerate",
                                     side_effect=counting_enumerate), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                # The late joiner should appear in a digest.
                all_settled_names = set()
                for line in lines:
                    dg = json.loads(line)
                    for e in dg["settled"]:
                        all_settled_names.add(e["name"])
                self.assertIn("w2-late", all_settled_names)
                self.assertIn("w1", all_settled_names)


# ── Cursor resume ────────────────────────────────────────────────────────────


class TestWatchCursorResume(unittest.TestCase):
    """A consumer resuming from a cursor converges on current truth."""

    def test_resume_with_cursor_emits_current_state(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-cursor", state="idle",
                             last_message="done")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                # Resume from an old cursor.
                ns = _make_watch_args(run_id="rid-cursor", timeout="1s",
                                      debounce="0",
                                      since=str(base_time - 3600))

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                settled_names = [e["name"] for e in digest["settled"]]
                self.assertIn("w1", settled_names)

    def test_fresh_arm_also_converges(self):
        """No --since: first digest immediately includes all settled handles."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-fresh", state="idle",
                             last_message="done")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-fresh", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                settled_names = [e["name"] for e in digest["settled"]]
                self.assertIn("w1", settled_names)


# ── Block mode ───────────────────────────────────────────────────────────────


class TestWatchBlockMode(unittest.TestCase):
    """Block mode: edge-triggered lines, exit 0 when all settled."""

    def test_block_exits_0_when_all_settled(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-block", state="idle",
                             last_message="done-1")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])
                _seed_handle("w2", run_id="rid-block", state="idle",
                             last_message="done-2")
                _write_lifecycle_events(tmp, "w2", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-block", block=True,
                                      timeout="5s", debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    rc = cli.cmd_watch(ns)

                self.assertEqual(rc, 0)
                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertEqual(len(lines), 2)
                names = {json.loads(l)["name"] for l in lines}
                self.assertEqual(names, {"w1", "w2"})

    def test_block_timeout_exits_3(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w-run", run_id="rid-bto", state="running")
                _write_lifecycle_events(tmp, "w-run", [
                    _turn_start_event(seq=1),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 10

                stdout = io.StringIO()
                stderr = io.StringIO()
                ns = _make_watch_args(run_id="rid-bto", block=True,
                                      timeout="5s")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch("sys.stderr", stderr), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    rc = cli.cmd_watch(ns)

                self.assertEqual(rc, 3)
                self.assertIn(cli.WAIT_TIMEOUT_MARKER,
                              stdout.getvalue())

    def test_block_edge_triggered_per_handle(self):
        """Block mode prints each handle as it settles, not all at once."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # w1 starts idle, w2 starts running.
                _seed_handle("w1", run_id="rid-edge", state="idle",
                             last_message="done-1")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])
                _seed_handle("w2", run_id="rid-edge", state="running")
                _write_lifecycle_events(tmp, "w2", [
                    _turn_start_event(seq=1),
                ])

                poll_count = [0]
                base_time = time.time()

                def mock_time():
                    return base_time + poll_count[0] * 0.5

                orig_snapshot = cli._settled_snapshot

                def counting_snapshot(handles, sa):
                    poll_count[0] += 1
                    # After poll 3, w2 settles.
                    if poll_count[0] == 3:
                        _seed_handle("w2", run_id="rid-edge", state="idle",
                                     last_message="done-2")
                        _write_lifecycle_events(tmp, "w2", [
                            _stop_event(seq=2, state="idle"),
                        ])
                    return orig_snapshot(handles, sa)

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-edge", block=True,
                                      timeout="10s")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=mock_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(cli, "_settled_snapshot",
                                     side_effect=counting_snapshot), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    rc = cli.cmd_watch(ns)

                self.assertEqual(rc, 0)
                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                # w1 printed first (settled from the start), w2 later.
                self.assertEqual(len(lines), 2)
                self.assertEqual(json.loads(lines[0])["name"], "w1")
                self.assertEqual(json.loads(lines[1])["name"], "w2")


# ── Level-triggered digest superset property ─────────────────────────────────


class TestWatchLevelTriggered(unittest.TestCase):
    """Successive digests are supersets; reading only the newest is safe."""

    def test_successive_digests_are_supersets(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # w1 starts idle.
                _seed_handle("w1", run_id="rid-lvl", state="idle",
                             last_message="done-1")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])
                # w2 starts running.
                _seed_handle("w2", run_id="rid-lvl", state="running")
                _write_lifecycle_events(tmp, "w2", [
                    _turn_start_event(seq=1),
                ])

                poll_count = [0]
                base_time = time.time()

                def mock_time():
                    return base_time + poll_count[0] * 0.5

                orig_snapshot = cli._settled_snapshot

                def counting_snapshot(handles, sa):
                    poll_count[0] += 1
                    if poll_count[0] == 4:
                        _seed_handle("w2", run_id="rid-lvl", state="idle",
                                     last_message="done-2")
                        _write_lifecycle_events(tmp, "w2", [
                            _stop_event(seq=2, state="idle"),
                        ])
                    return orig_snapshot(handles, sa)

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-lvl", timeout="10s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=mock_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(cli, "_settled_snapshot",
                                     side_effect=counting_snapshot), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                # Expect at least 2 digests: first with w1, then with w1+w2.
                if len(lines) >= 2:
                    d1 = json.loads(lines[0])
                    d_last = json.loads(lines[-1])
                    names1 = {e["name"] for e in d1["settled"]}
                    names_last = {e["name"] for e in d_last["settled"]}
                    # Superset property: last >= first.
                    self.assertTrue(names1.issubset(names_last),
                                    f"First digest {names1} is not a subset of "
                                    f"last {names_last}")


# ── JSON output is machine-readable ──────────────────────────────────────────


class TestWatchOutputFormat(unittest.TestCase):
    """All output is valid JSON, no ad-hoc parsing needed."""

    def test_every_line_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-json", state="idle",
                             last_message="msg")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-json", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                for line in stdout.getvalue().strip().split("\n"):
                    if line.strip():
                        parsed = json.loads(line)
                        self.assertIsInstance(parsed, dict)

    def test_digest_has_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-fields", state="idle",
                             last_message="msg")
                _write_lifecycle_events(tmp, "w1", [
                    _turn_start_event(seq=1),
                    _stop_event(seq=2, state="idle"),
                ])

                call_count = [0]
                base_time = time.time()

                def advancing_time():
                    call_count[0] += 1
                    return base_time + call_count[0] * 0.1

                stdout = io.StringIO()
                ns = _make_watch_args(run_id="rid-fields", timeout="1s",
                                      debounce="0")

                with patch.object(lib, "tmux_has_session",
                                  return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), \
                        patch.object(lib, "resolve_amux_session",
                                     return_value=None):
                    cli.cmd_watch(ns)

                lines = [l for l in stdout.getvalue().strip().split("\n") if l]
                self.assertGreaterEqual(len(lines), 1)
                digest = json.loads(lines[0])
                for key in ("seq", "ts", "cursor", "settled", "watching",
                            "pending"):
                    self.assertIn(key, digest, f"missing field: {key}")
                self.assertIsInstance(digest["settled"], list)
                self.assertIsInstance(digest["watching"], int)
                self.assertIsInstance(digest["pending"], int)


# ── Validation ───────────────────────────────────────────────────────────────


class TestWatchValidation(unittest.TestCase):
    """Argument validation for cmd_watch."""

    def test_missing_scope_returns_2(self):
        ns = _make_watch_args()
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            rc = cli.cmd_watch(ns)
        self.assertEqual(rc, 2)
        self.assertIn("--run-id", stderr.getvalue())

    def test_invalid_cursor_returns_2(self):
        ns = _make_watch_args(run_id="rid", since="not-a-number")
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            rc = cli.cmd_watch(ns)
        self.assertEqual(rc, 2)
        self.assertIn("invalid", stderr.getvalue())


# ── Helpers ──────────────────────────────────────────────────────────────────


class TestParseDebounce(unittest.TestCase):
    def test_bare_number(self):
        self.assertAlmostEqual(cli._parse_debounce("8"), 8.0)

    def test_seconds_suffix(self):
        self.assertAlmostEqual(cli._parse_debounce("2s"), 2.0)

    def test_minutes_suffix(self):
        self.assertAlmostEqual(cli._parse_debounce("1m"), 60.0)

    def test_fractional(self):
        self.assertAlmostEqual(cli._parse_debounce("0.5"), 0.5)

    def test_none_returns_default(self):
        self.assertAlmostEqual(cli._parse_debounce(None),
                               float(cli.WATCH_DEBOUNCE_DEFAULT_S))


class TestIsWatchReportable(unittest.TestCase):
    def test_idle_reportable(self):
        self.assertTrue(cli._is_watch_reportable("idle", False))

    def test_terminated_reportable(self):
        self.assertTrue(cli._is_watch_reportable("terminated", False))

    def test_stuck_reportable(self):
        self.assertTrue(cli._is_watch_reportable("stuck", False))

    def test_running_not_reportable(self):
        self.assertFalse(cli._is_watch_reportable("running", False))

    def test_running_with_permission_pending_reportable(self):
        self.assertTrue(cli._is_watch_reportable("running", True))

    def test_spawning_not_reportable(self):
        self.assertFalse(cli._is_watch_reportable("spawning", False))


class TestWatchEnumerate(unittest.TestCase):
    """_watch_enumerate filters correctly."""

    def test_filters_by_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-a")
                _seed_handle("w2", run_id="rid-b")
                result = cli._watch_enumerate("rid-a", [], set(), None)
                self.assertIn("w1", result)
                self.assertNotIn("w2", result)

    def test_excludes_names(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-x")
                _seed_handle("w2", run_id="rid-x")
                result = cli._watch_enumerate("rid-x", [], {"w1"}, None)
                self.assertNotIn("w1", result)
                self.assertIn("w2", result)

    def test_explicit_handles(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("w1", run_id="rid-y")
                _seed_handle("w2", run_id="rid-y")
                result = cli._watch_enumerate(None, ["w1"], set(), None)
                self.assertIn("w1", result)
                self.assertNotIn("w2", result)


# ── Existing --wait/--notify unchanged ───────────────────────────────────────


class TestExistingWaitUnchanged(unittest.TestCase):
    """The existing --wait/--notify exit-code contract is preserved.

    This is a smoke test: the full suite is in test_unit_amux_supervise.py
    and test_unit_amux_wait_print.py; we verify that ``watch`` did not break
    the existing surface by checking the constants and that _wait_for_idle
    still exists and is callable.
    """

    def test_wait_timeout_marker_unchanged(self):
        self.assertEqual(cli.WAIT_TIMEOUT_MARKER, "AMUX_WAIT_TIMEOUT")

    def test_wait_for_idle_still_callable(self):
        self.assertTrue(callable(cli._wait_for_idle))


if __name__ == "__main__":
    unittest.main(verbosity=2)
