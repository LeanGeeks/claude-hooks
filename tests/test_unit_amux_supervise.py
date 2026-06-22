#!/usr/bin/env python3
"""Unit tests for the epic-10 supervise subcommand (task 10-04).

Covers ``spawn --wait`` / ``spawn --notify`` / ``--timeout`` against a
throwaway ``~/.amux`` (redirected via the shared lib's path constants), with
``tmux has-session`` patched and ``time.sleep`` / ``time.time`` mocked so tests
are fast and deterministic. No network, no real tmux, no real ~/.amux / ~/.claude.

Mirrors the task's Testing bullets:
- --wait on a quick prompt -> returns correct last_message, exits 0.
- --wait on a prompt that leaves background child at the first Stop -> does NOT
  return at the early non-empty-background_tasks Stop; returns only after the
  handle drains to true idle (simulate: first poll running + non-empty bg, later
  poll idle + empty bg).
- false-idle guard: a freshly-created session still in spawning / no
  mtime_at_stop must NOT satisfy --wait (it keeps waiting), even if stored_state
  transitions happen.
- --timeout 5 (simulated) on a never-idle session -> exit 3, AMUX_WAIT_TIMEOUT
  on stdout, no hang.
- --notify is a strict synonym of --wait (same outcome).
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli",
        importlib.machinery.SourceFileLoader("amux_spawn_cli", str(_BIN)),
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


def _seed_handle(name: str, abs_dir: str, transcript_path: str, **overrides) -> dict:
    h = lib.new_handle(
        name=name,
        session_id="11111111-2222-3333-4444-555555555555",
        run_id="rid",
        abs_dir=abs_dir,
        transcript_path=transcript_path,
        stuck_after_s=600,
    )
    h.update(overrides)
    lib.write_handle(name, h)
    return h


def _write_transcript(path: Path, lines: list, mtime: float | None = None) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return os.path.getmtime(path)


def _user_turn(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


# ── _wait_for_idle unit tests ─────────────────────────────────────────────────


class TestWaitForIdleQuickPrompt(unittest.TestCase):
    """--wait on a quick prompt returns the correct last_message after idle."""

    def test_quick_idle_returns_last_message(self):
        """A session that is already idle (after having been running) should
        return last_message on the first poll that sees idle."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_user_turn("go"), _assistant_text("done")])
                # Seed handle in idle state (simulates the Stop hook having fired).
                _seed_handle("p", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m,
                             background_tasks=[], last_message="the answer")

                # Poll sequence: first poll sees running (seen_non_idle becomes True),
                # second poll sees idle -> return.
                # We simulate this by having _derive_status return running then idle.
                call_count = [0]
                original_derive = cli._derive_status

                def fake_derive(handle, override):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        # First poll: pretend session is still running.
                        r = original_derive(handle, override)
                        r = dict(r)
                        r["state"] = "running"
                        return r
                    # Second poll: true idle.
                    return original_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=None)

                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "the answer")
                self.assertEqual(call_count[0], 2)

    def test_quick_idle_no_last_message_returns_empty_string(self):
        """When last_message is None on idle, payload is empty string (not None)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m,
                             background_tasks=[], last_message=None)

                call_count = [0]
                original_derive = cli._derive_status

                def fake_derive(handle, override):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        r = dict(original_derive(handle, override))
                        r["state"] = "running"
                        return r
                    return original_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=None)

                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "")


class TestWaitForIdleBackgroundDrain(unittest.TestCase):
    """--wait does NOT return at the first Stop when background_tasks is non-empty.

    It keeps waiting until the handle drains to true idle (empty background_tasks).
    Simulates: first poll shows running + non-empty bg, later poll shows idle.
    """

    def test_wait_does_not_return_on_non_empty_background(self):
        """Simulate a session where:
        - Poll 1: state=running, no_stop_yet=False (Stop fired), non-empty bg tasks.
        - Poll 2: still running + non-empty bg (child still running).
        - Poll 3: state=idle, bg=[] (child completed, draining Stop fired).
        Must NOT return at polls 1 or 2; must return at poll 3.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                now = time.time()
                m = _write_transcript(tpath, [_assistant_text("kicked off bg child")],
                                      mtime=now - 5)

                # Start with running state + non-empty background_tasks.
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 30"}]
                _seed_handle("p", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m,
                             background_tasks=bg, last_message="kicked off bg child")

                call_count = [0]

                def fake_derive(handle, override):
                    call_count[0] += 1
                    if call_count[0] <= 2:
                        # Polls 1+2: running with live background task.
                        return {
                            "name": "p", "state": "running",
                            "stored_state": "running", "active": True,
                            "stuck_after_s": 600, "activity_age_s": 5.0,
                            "signals": {
                                "live_background_tasks": True,
                                "open_turn": False,
                                "no_stop_yet": False,
                            },
                            "reason_context": {},
                        }
                    # Poll 3: true idle — bg task drained (draining Stop fired).
                    # Mutate the handle dict in-place so _wait_for_idle sees the
                    # updated last_message when it reads handle.get("last_message").
                    handle["state"] = "idle"
                    handle["background_tasks"] = []
                    handle["last_message"] = "all done"
                    return {
                        "name": "p", "state": "idle",
                        "stored_state": "idle", "active": False,
                        "stuck_after_s": 600, "activity_age_s": 5.0,
                        "signals": {
                            "live_background_tasks": False,
                            "open_turn": False,
                            "no_stop_yet": False,
                        },
                    }

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=None)

                # Must NOT have returned at polls 1 or 2 (non-empty bg).
                self.assertEqual(call_count[0], 3,
                                 "Should have waited through 2 non-idle polls before returning")
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "all done")

    def test_wait_first_stop_non_empty_bg_then_drains(self):
        """Integration-style: seed handle with bg task, then update it to idle.

        Uses the real _derive_status so we verify the actual state derivation
        correctly keeps the session active while background_tasks is non-empty.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                now = time.time()
                m = _write_transcript(tpath, [_assistant_text("bg running")],
                                      mtime=now - 5)

                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 30"}]
                h = _seed_handle("p", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=m,
                                 background_tasks=bg, last_message="bg running")

                poll_count = [0]

                def controlled_sleep(s):
                    pass  # don't actually sleep

                def controlled_time():
                    # Never hit a timeout — just count calls.
                    return now

                drain_after = 2  # drain to idle after this many polls

                original_read_handle = lib.read_handle

                def fake_read_handle(name):
                    poll_count[0] += 1
                    if poll_count[0] > drain_after:
                        # Simulate draining Stop having fired.
                        h2 = original_read_handle(name)
                        if h2 is not None:
                            h2 = dict(h2)
                            h2["state"] = "idle"
                            h2["background_tasks"] = []
                            h2["last_message"] = "final result"
                        return h2
                    return original_read_handle(name)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(lib, "read_handle", side_effect=fake_read_handle), \
                     patch("time.sleep", side_effect=controlled_sleep), \
                     patch("time.time", side_effect=lambda: now):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=None)

                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "final result")
                # Must have polled at least 3 times (2 non-idle + 1 idle).
                self.assertGreaterEqual(poll_count[0], 3)


class TestWaitFalseIdleGuard(unittest.TestCase):
    """False-idle guard: a freshly-created session must NOT satisfy --wait.

    A session still in spawning / with no mtime_at_stop (no Stop yet) reads
    as "running" (not "idle") per _derive_status (architecture §6 step 2).
    The seen_non_idle flag must be set before we accept an idle state.
    """

    def test_spawning_session_does_not_satisfy_wait_prematurely(self):
        """A session whose handle is still 'spawning' (no Stop yet) must keep
        the poll loop running, never returning idle early."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                # Handle in spawning state — no Stop yet, no mtime_at_stop.
                _seed_handle("p", "/ws/p", str(tpath))  # state=spawning, mtime_at_stop=None

                poll_count = [0]

                def fake_derive(handle, override):
                    poll_count[0] += 1
                    if poll_count[0] <= 3:
                        # Still in spawning / running — no Stop yet.
                        return {
                            "name": "p", "state": "running",
                            "stored_state": "spawning", "active": True,
                            "stuck_after_s": 600, "activity_age_s": 1.0,
                            "signals": {
                                "live_background_tasks": False,
                                "open_turn": False,
                                "no_stop_yet": True,
                            },
                        }
                    # Poll 4: first Stop fired, session now idle.
                    return {
                        "name": "p", "state": "idle",
                        "stored_state": "idle", "active": False,
                        "stuck_after_s": 600, "activity_age_s": 2.0,
                        "signals": {
                            "live_background_tasks": False,
                            "open_turn": False,
                            "no_stop_yet": False,
                        },
                    }

                # Update the handle's last_message for the idle case.
                h = lib.read_handle("p")
                h["last_message"] = "spawned result"
                h["state"] = "idle"
                h["mtime_at_stop"] = time.time()
                lib.write_handle("p", h)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=None)

                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "spawned result")
                # Must have waited through at least 3 "running" polls before
                # accepting idle at poll 4.
                self.assertEqual(poll_count[0], 4,
                                 "Must wait until seen_non_idle before accepting idle")

    def test_false_idle_guard_never_accepts_idle_without_running(self):
        """If _derive_status returns 'idle' on the very first poll (e.g. a
        freshly-booted session whose stored state is still 'spawning' but
        somehow reads as 'idle' — defensive scenario), the guard must block.

        We deliver: poll 1 = idle (should be rejected by guard), poll 2 = running
        (guard satisfied), poll 3 = idle (accepted).
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _seed_handle("p", "/ws/p", str(tpath),
                             last_message="result", state="idle")

                poll_count = [0]

                def fake_derive(handle, override):
                    poll_count[0] += 1
                    if poll_count[0] == 1:
                        # First poll: idle (boot-time / pre-seed idle — must be rejected).
                        return {
                            "name": "p", "state": "idle",
                            "stored_state": "idle", "active": False,
                            "stuck_after_s": 600, "activity_age_s": 0.5,
                            "signals": {
                                "live_background_tasks": False,
                                "open_turn": False,
                                "no_stop_yet": False,
                            },
                        }
                    if poll_count[0] == 2:
                        # Second poll: running (seeded turn started).
                        return {
                            "name": "p", "state": "running",
                            "stored_state": "running", "active": True,
                            "stuck_after_s": 600, "activity_age_s": 1.0,
                            "signals": {
                                "live_background_tasks": False,
                                "open_turn": True,
                                "no_stop_yet": False,
                            },
                        }
                    # Poll 3+: idle (true idle after turn completion).
                    return {
                        "name": "p", "state": "idle",
                        "stored_state": "idle", "active": False,
                        "stuck_after_s": 600, "activity_age_s": 2.0,
                        "signals": {
                            "live_background_tasks": False,
                            "open_turn": False,
                            "no_stop_yet": False,
                        },
                    }

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=None)

                self.assertEqual(outcome, "idle")
                # MUST have polled 3 times: rejected idle at poll 1, seen running
                # at poll 2, accepted idle at poll 3.
                self.assertEqual(poll_count[0], 3,
                                 "Must reject boot-time idle (poll 1) and require "
                                 "running (poll 2) before accepting idle (poll 3)")


class TestWaitTimeout(unittest.TestCase):
    """--timeout returns a clear, distinguishable result without hanging."""

    def test_timeout_returns_timeout_outcome(self):
        """A session that never becomes idle triggers timeout."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _seed_handle("p", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=time.time() - 1)

                # Simulate time advancing past the deadline.
                now_ref = [time.time()]

                def fake_time():
                    # Advance time on each call: first call = now, then +10s each.
                    t = now_ref[0]
                    now_ref[0] += 10
                    return t

                def fake_derive(handle, override):
                    # Always running — will never become idle.
                    return {
                        "name": "p", "state": "running",
                        "stored_state": "running", "active": True,
                        "stuck_after_s": 600, "activity_age_s": 1.0,
                        "signals": {
                            "live_background_tasks": False,
                            "open_turn": True,
                            "no_stop_yet": False,
                        },
                    }

                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"), \
                     patch("time.time", side_effect=fake_time):
                    # 5s timeout; time advances 10s per call so it will expire.
                    outcome, payload = cli._wait_for_idle("p", timeout_s=5.0)

                self.assertEqual(outcome, "timeout")
                self.assertIsNone(payload)

    def test_timeout_with_zero_timeout_returns_immediately(self):
        """timeout_s=0 (or near-zero) should return timeout without any polls."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _seed_handle("p", "/ws/p", str(tpath), state="running")

                now_t = time.time()
                poll_count = [0]

                def fake_derive(handle, override):
                    poll_count[0] += 1
                    return {"name": "p", "state": "running", "active": True,
                            "stuck_after_s": 600, "activity_age_s": 1.0,
                            "signals": {"live_background_tasks": False,
                                        "open_turn": True, "no_stop_yet": False}}

                # time.time returns a value past the deadline (deadline = now + 0).
                with patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"), \
                     patch("time.time", return_value=now_t + 1):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=0.0)

                self.assertEqual(outcome, "timeout")
                # With 0 timeout, the first check at the top of the loop should
                # fire before we poll _derive_status at all (or at most once).
                self.assertEqual(poll_count[0], 0,
                                 "Zero timeout: should exit before polling")

    def test_timeout_handles_terminated_session(self):
        """A terminated session returns 'error', not 'timeout'."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _seed_handle("p", "/ws/p", str(tpath), state="running")

                def fake_derive(handle, override):
                    return {"name": "p", "state": "terminated",
                            "last_state": "running", "stuck_after_s": 600}

                with patch.object(lib, "tmux_has_session", return_value=False), \
                     patch.object(cli, "_derive_status", side_effect=fake_derive), \
                     patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p", timeout_s=30.0)

                self.assertEqual(outcome, "error")
                self.assertIn("terminated", payload)


class TestWaitMissingHandle(unittest.TestCase):
    """A handle that disappears during --wait returns an error."""

    def test_missing_handle_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Don't seed a handle — read_handle returns None.
                with patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("nonexistent", timeout_s=None)
                self.assertEqual(outcome, "error")
                self.assertIn("nonexistent", payload)


# ── cmd_spawn --wait CLI integration (stdout contract) ────────────────────────


class TestCmdSpawnWaitStdoutContract(unittest.TestCase):
    """Verify the stdout contract: last_message on success, AMUX_WAIT_TIMEOUT on
    timeout, nothing useful on error. Diagnostics always go to stderr.
    """

    def _run_spawn_wait(self, wait_for_idle_return, timeout_arg=None):
        """Helper: run cmd_spawn with --wait, mocking _amux_create_detached and
        _wait_for_idle. Returns (returncode, stdout_text, stderr_text).
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "workspace"
            ws.mkdir()

            with _redirect_amux_home(tmp):
                lib.ensure_dirs()

                stdout_buf = io.StringIO()
                stderr_buf = io.StringIO()

                # Build args namespace for cmd_spawn.
                ns = _make_spawn_args(
                    wait=True,
                    notify=False,
                    timeout=timeout_arg,
                    abs_dir=str(ws),
                )

                with patch.object(cli, "_amux_create_detached",
                                  return_value=(0, None)), \
                     patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(lib, "live_tracked_count", return_value=0), \
                     patch.object(lib, "name_in_use", return_value=False), \
                     patch.object(cli, "_wait_for_idle",
                                  return_value=wait_for_idle_return), \
                     patch.object(cli, "resolve_dir",
                                  return_value=(str(ws), None, None)), \
                     patch("sys.stdout", stdout_buf), \
                     patch("sys.stderr", stderr_buf):
                    rc = cli.cmd_spawn(ns, [], "do something")

                return rc, stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_success_prints_last_message_to_stdout(self):
        rc, stdout, stderr = self._run_spawn_wait(("idle", "the final answer"))
        self.assertEqual(rc, 0)
        self.assertIn("the final answer", stdout)
        # stderr should have diagnostics but stdout should be ONLY the payload.
        stdout_lines = [l for l in stdout.splitlines() if l.strip()]
        self.assertEqual(len(stdout_lines), 1,
                         f"stdout should be exactly 1 line (the payload); got: {stdout!r}")
        self.assertEqual(stdout_lines[0], "the final answer")

    def test_timeout_prints_marker_to_stdout_exit_3(self):
        rc, stdout, stderr = self._run_spawn_wait(("timeout", None), timeout_arg="5s")
        self.assertEqual(rc, 3)
        self.assertIn(cli.WAIT_TIMEOUT_MARKER, stdout)
        # The marker line should be the only stdout line.
        stdout_lines = [l for l in stdout.splitlines() if l.strip()]
        self.assertEqual(stdout_lines[0], cli.WAIT_TIMEOUT_MARKER)

    def test_error_returns_exit_1_no_stdout(self):
        rc, stdout, stderr = self._run_spawn_wait(("error", "session died"))
        self.assertEqual(rc, 1)
        # stdout should be empty on error.
        self.assertEqual(stdout.strip(), "")
        self.assertIn("session died", stderr)

    def test_notify_same_as_wait(self):
        """--notify must produce the identical behavior as --wait."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "workspace"
            ws.mkdir()

            with _redirect_amux_home(tmp):
                lib.ensure_dirs()

                stdout_buf = io.StringIO()
                stderr_buf = io.StringIO()

                ns = _make_spawn_args(wait=False, notify=True, timeout=None,
                                      abs_dir=str(ws))

                with patch.object(cli, "_amux_create_detached",
                                  return_value=(0, None)), \
                     patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(lib, "live_tracked_count", return_value=0), \
                     patch.object(lib, "name_in_use", return_value=False), \
                     patch.object(cli, "_wait_for_idle",
                                  return_value=("idle", "notify result")), \
                     patch.object(cli, "resolve_dir",
                                  return_value=(str(ws), None, None)), \
                     patch("sys.stdout", stdout_buf), \
                     patch("sys.stderr", stderr_buf):
                    rc = cli.cmd_spawn(ns, [], "do something")

                self.assertEqual(rc, 0)
                self.assertIn("notify result", stdout_buf.getvalue())


class TestParseTimeout(unittest.TestCase):
    """--timeout uses the same duration parser as --stuck-after."""

    def test_seconds(self):
        self.assertEqual(cli.parse_stuck_after("5s"), 5)

    def test_minutes(self):
        self.assertEqual(cli.parse_stuck_after("2m"), 120)

    def test_bare_integer(self):
        self.assertEqual(cli.parse_stuck_after("30"), 30)

    def test_none_returns_default(self):
        import amux_spawn_lib as _lib
        self.assertEqual(cli.parse_stuck_after(None), _lib.DEFAULT_STUCK_AFTER_S)


class TestWaitTimeoutMarkerConstant(unittest.TestCase):
    """WAIT_TIMEOUT_MARKER is machine-readable and distinct from typical content."""

    def test_marker_is_uppercase_no_spaces(self):
        marker = cli.WAIT_TIMEOUT_MARKER
        self.assertEqual(marker, marker.upper())
        self.assertNotIn(" ", marker)
        self.assertTrue(marker.isidentifier() or "_" in marker)

    def test_marker_is_the_expected_value(self):
        # Pinned value so orchestrators can match it by string.
        self.assertEqual(cli.WAIT_TIMEOUT_MARKER, "AMUX_WAIT_TIMEOUT")


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_spawn_args(*, wait: bool, notify: bool, timeout, abs_dir: str = ""):
    """Build a minimal argparse.Namespace for cmd_spawn.

    Callers must patch ``cli.resolve_dir`` to return ``(abs_dir, None, None)``
    so ``cmd_spawn`` uses the intended workspace dir without touching the
    filesystem or real amux sessions.
    """
    import argparse
    return argparse.Namespace(
        suffix=None,
        detach=False,
        wait=wait,
        notify=notify,
        timeout=timeout,
        dir=None,
        yolo=False,
        run_id=None,
        stuck_after=None,
    )


class TestCmdSpawnWaitNoAttach(unittest.TestCase):
    """--wait/--notify must never attach (even at a TTY)."""

    def test_wait_does_not_call_attach(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "workspace"
            ws.mkdir()

            with _redirect_amux_home(tmp):
                lib.ensure_dirs()

                ns = _make_spawn_args(wait=True, notify=False, timeout=None,
                                      abs_dir=str(ws))

                attach_called = [False]

                def fake_attach(name):
                    attach_called[0] = True

                with patch.object(cli, "_amux_create_detached",
                                  return_value=(0, None)), \
                     patch.object(lib, "tmux_has_session", return_value=True), \
                     patch.object(lib, "live_tracked_count", return_value=0), \
                     patch.object(lib, "name_in_use", return_value=False), \
                     patch.object(cli, "_wait_for_idle",
                                  return_value=("idle", "result")), \
                     patch.object(cli, "_amux_attach", side_effect=fake_attach), \
                     patch.object(cli, "resolve_dir",
                                  return_value=(str(ws), None, None)), \
                     patch("sys.stdin") as mock_stdin, \
                     patch("sys.stdout", io.StringIO()), \
                     patch("sys.stderr", io.StringIO()):
                    # Simulate TTY environment.
                    mock_stdin.isatty.return_value = True
                    rc = cli.cmd_spawn(ns, [], "prompt")

                self.assertEqual(rc, 0)
                self.assertFalse(attach_called[0],
                                 "--wait must never call _amux_attach")


if __name__ == "__main__":
    unittest.main(verbosity=2)
