#!/usr/bin/env python3
"""Unit tests for ``amux-spawn rm`` (task 10-06).

Covers refuse-live, force-kill, idle-reap, unknown-handle, terminated-reap,
and JSON output — with ``tmux has-session`` and ``_amux_rm`` stubbed.
No real amux/tmux calls, no network, no real ``~/.amux`` / ``~/.claude``.

Mirrors the existing suite conventions in ``test_unit_amux_reads.py``:
isolated AMUX_HOME via patch.multiple, tmpdir per test, tmux/amux stubbed.

Done-criteria coverage (task 10-06):
- rm on idle/terminated: registry file gone; ls --all no longer lists it; exit 0.
- rm on running/stuck: refuses without --force; with --force kills and removes.
- rm on unknown handle: exit 1 with a stderr message.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
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


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


# ── Refuse live (no --force) ──────────────────────────────────────────────────


class TestRmRefuseLive(unittest.TestCase):
    """rm on a running or stuck session refuses without --force."""

    def test_refuses_running_session_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 30"}]
                m = _write_transcript(tpath, [_assistant_text("kicked off")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m, background_tasks=bg)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        rc = cli.main(["rm", "p-2"])
                self.assertEqual(rc, 1)
                # Error message must name the derived state.
                self.assertIn("running", stderr.getvalue())
                # Handle still exists — nothing was deleted.
                self.assertTrue(lib.handle_path("p-2").exists())
                # Nothing on stdout.
                self.assertEqual(stdout.getvalue().strip(), "")

    def test_refuses_stuck_session_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 9999"}]
                m = _write_transcript(tpath, [_assistant_text("kicked off")], mtime=old)
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m,
                             background_tasks=bg, stuck_after_s=600)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        rc = cli.main(["rm", "p-2"])
                self.assertEqual(rc, 1)
                self.assertIn("stuck", stderr.getvalue())
                self.assertTrue(lib.handle_path("p-2").exists())


# ── Force-kill (--force on a live session) ────────────────────────────────────


class TestRmForceKill(unittest.TestCase):
    """rm --force kills and removes a running/stuck session."""

    def test_force_kills_running_session(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 30"}]
                m = _write_transcript(tpath, [_assistant_text("kicked off")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m, background_tasks=bg)
                amux_calls: list[str] = []

                def fake_amux_rm(name: str) -> None:
                    amux_calls.append(name)

                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm", side_effect=fake_amux_rm):
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2", "--force"])
                self.assertEqual(rc, 0)
                # Handle deleted from registry.
                self.assertFalse(lib.handle_path("p-2").exists())
                # amux rm was invoked.
                self.assertIn("p-2", amux_calls)

    def test_force_json_killed_true_for_running(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                bg = [{"type": "shell", "status": "running", "id": "t1"}]
                m = _write_transcript(tpath, [_assistant_text("running")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m, background_tasks=bg)
                stdout = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm"):
                        with patch("sys.stdout", stdout), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2", "--force", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(stdout.getvalue())
                self.assertEqual(data["name"], "p-2")
                self.assertIn(data["state_at_rm"], ("running", "stuck"))
                self.assertTrue(data["killed"])

    def test_force_json_killed_true_for_stuck(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 9999"}]
                m = _write_transcript(tpath, [_assistant_text("kicked off")], mtime=old)
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m,
                             background_tasks=bg, stuck_after_s=600)
                stdout = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm"):
                        with patch("sys.stdout", stdout), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2", "--force", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(stdout.getvalue())
                self.assertEqual(data["state_at_rm"], "stuck")
                self.assertTrue(data["killed"])


# ── Idle / terminated reap ────────────────────────────────────────────────────


class TestRmIdleReap(unittest.TestCase):
    """rm on an idle/terminated handle removes it cleanly (exit 0)."""

    def test_idle_reap_removes_handle_file(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])
                self.assertTrue(lib.handle_path("p-2").exists())
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm"):
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2"])
                self.assertEqual(rc, 0)
                self.assertFalse(lib.handle_path("p-2").exists())

    def test_idle_reap_not_listed_in_ls_all(self):
        """After rm, ls --all must no longer list the reaped session."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])

                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm"):
                        # Confirm it appears before rm.
                        buf = io.StringIO()
                        with patch("sys.stdout", buf):
                            cli.main(["ls", "--json", "--all"])
                        names_before = [
                            s["name"]
                            for s in json.loads(buf.getvalue())["sessions"]
                        ]
                        self.assertIn("p-2", names_before)

                        # rm it.
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2"])
                        self.assertEqual(rc, 0)

                        # Confirm it is gone from ls --all.
                        buf2 = io.StringIO()
                        with patch("sys.stdout", buf2):
                            cli.main(["ls", "--json", "--all"])
                        names_after = [
                            s["name"]
                            for s in json.loads(buf2.getvalue())["sessions"]
                        ]
                        self.assertNotIn("p-2", names_after)

    def test_terminated_reap_removes_handle_file(self):
        """A session whose tmux is gone (terminated) is still reaped cleanly."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", last_state="idle", mtime_at_stop=m)
                # tmux_has_session=False -> derived state == terminated
                with patch.object(lib, "tmux_has_session", return_value=False):
                    with patch.object(cli, "_amux_rm"):
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2"])
                self.assertEqual(rc, 0)
                self.assertFalse(lib.handle_path("p-2").exists())

    def test_idle_reap_json_output(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])
                stdout = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm"):
                        with patch("sys.stdout", stdout), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(stdout.getvalue())
                self.assertEqual(data["name"], "p-2")
                self.assertEqual(data["state_at_rm"], "idle")
                # idle reap does NOT kill anything.
                self.assertFalse(data["killed"])

    def test_terminated_reap_json_killed_false(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", last_state="idle", mtime_at_stop=m)
                stdout = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False):
                    with patch.object(cli, "_amux_rm"):
                        with patch("sys.stdout", stdout), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-2", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(stdout.getvalue())
                self.assertEqual(data["state_at_rm"], "terminated")
                self.assertFalse(data["killed"])

    def test_amux_rm_called_for_idle_session(self):
        """Even for an idle session, amux rm is called to clean up amux state."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])
                amux_calls: list[str] = []

                def fake_amux_rm(name: str) -> None:
                    amux_calls.append(name)

                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch.object(cli, "_amux_rm", side_effect=fake_amux_rm):
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", io.StringIO()):
                            cli.main(["rm", "p-2"])
                self.assertIn("p-2", amux_calls)


# ── Unknown handle ────────────────────────────────────────────────────────────


class TestRmUnknownHandle(unittest.TestCase):
    """rm on an unknown handle exits 1 with a descriptive stderr message."""

    def test_unknown_handle_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    rc = cli.main(["rm", "nosuchsession"])
                self.assertEqual(rc, 1)
                # Session name must appear in the error message.
                self.assertIn("nosuchsession", stderr.getvalue())
                # Nothing on stdout.
                self.assertEqual(stdout.getvalue().strip(), "")

    def test_unknown_handle_no_crash(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                with patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["rm", "does-not-exist"])
                self.assertEqual(rc, 1)

    def test_unknown_handle_json_arg_still_exits_1(self):
        """Even with --json, an unknown handle exits 1 (parity with status/last)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                stderr = io.StringIO()
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", stderr):
                    rc = cli.main(["rm", "ghost-session", "--json"])
                self.assertEqual(rc, 1)
                self.assertIn("ghost-session", stderr.getvalue())


# ── _amux_rm warning behavior (Fix 1 / task 10-06 review) ────────────────────


class TestAmuxRmWarningBehavior(unittest.TestCase):
    """_amux_rm surfaces unexpected failures as STDERR warnings; tolerates
    the 'unknown session' case silently; handle is always deleted afterwards.
    """

    # -- Direct _amux_rm unit tests (subprocess.run stubbed) ------------------

    def test_unknown_session_stays_silent(self):
        """amux exits 1 with 'not found' on stderr → no warning emitted."""
        result = subprocess.CompletedProcess(
            args=["amux", "rm", "gone"],
            returncode=1,
            stdout="",
            stderr="error: session 'gone' not found",
        )
        stderr = io.StringIO()
        with patch("subprocess.run", return_value=result):
            with patch("sys.stderr", stderr):
                cli._amux_rm("gone")
        self.assertEqual(stderr.getvalue(), "")

    def test_non_zero_non_unknown_warns(self):
        """amux exits non-zero with unrecognised stderr → warning on stderr."""
        result = subprocess.CompletedProcess(
            args=["amux", "rm", "live"],
            returncode=1,
            stdout="",
            stderr="error: permission denied",
        )
        stderr = io.StringIO()
        with patch("subprocess.run", return_value=result):
            with patch("sys.stderr", stderr):
                cli._amux_rm("live")
        self.assertIn("warning", stderr.getvalue())
        self.assertIn("1", stderr.getvalue())  # returncode in message

    def test_non_zero_no_stderr_warns(self):
        """amux exits non-zero with empty stderr → warning that includes returncode."""
        result = subprocess.CompletedProcess(
            args=["amux", "rm", "x"],
            returncode=2,
            stdout="",
            stderr="",
        )
        stderr = io.StringIO()
        with patch("subprocess.run", return_value=result):
            with patch("sys.stderr", stderr):
                cli._amux_rm("x")
        self.assertIn("warning", stderr.getvalue())
        self.assertIn("2", stderr.getvalue())

    def test_file_not_found_warns(self):
        """amux binary not on PATH → warning on stderr, no raise."""
        stderr = io.StringIO()
        with patch("subprocess.run", side_effect=FileNotFoundError("amux: not found")):
            with patch("sys.stderr", stderr):
                cli._amux_rm("anyname")
        self.assertIn("warning", stderr.getvalue())

    def test_timeout_warns(self):
        """amux times out → warning on stderr, no raise."""
        stderr = io.StringIO()
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="amux", timeout=10)):
            with patch("sys.stderr", stderr):
                cli._amux_rm("anyname")
        self.assertIn("warning", stderr.getvalue())

    def test_success_no_warning(self):
        """amux exits 0 → no output at all."""
        result = subprocess.CompletedProcess(
            args=["amux", "rm", "ok"],
            returncode=0,
            stdout="removed ok",
            stderr="",
        )
        stderr = io.StringIO()
        with patch("subprocess.run", return_value=result):
            with patch("sys.stderr", stderr):
                cli._amux_rm("ok")
        self.assertEqual(stderr.getvalue(), "")

    # -- Full cmd_rm integration: handle deleted in both warning cases ---------

    def test_handle_deleted_on_unknown_session(self):
        """Handle is deleted even when amux reports 'not found' (tolerated)."""
        amux_result = subprocess.CompletedProcess(
            args=["amux", "rm", "p-gone"],
            returncode=1,
            stdout="",
            stderr="error: session 'p-gone' not found",
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-gone", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])
                self.assertTrue(lib.handle_path("p-gone").exists())
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch("subprocess.run", return_value=amux_result):
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", io.StringIO()):
                            rc = cli.main(["rm", "p-gone"])
                self.assertEqual(rc, 0)
                self.assertFalse(lib.handle_path("p-gone").exists())

    def test_handle_deleted_on_amux_failure(self):
        """Handle is deleted even when amux exits with unexpected non-zero."""
        amux_result = subprocess.CompletedProcess(
            args=["amux", "rm", "p-fail"],
            returncode=2,
            stdout="",
            stderr="error: socket permission denied",
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-fail", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])
                self.assertTrue(lib.handle_path("p-fail").exists())
                stderr = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    with patch("subprocess.run", return_value=amux_result):
                        with patch("sys.stdout", io.StringIO()), \
                                patch("sys.stderr", stderr):
                            rc = cli.main(["rm", "p-fail"])
                self.assertEqual(rc, 0)
                self.assertFalse(lib.handle_path("p-fail").exists())
                # Warning must have been emitted.
                self.assertIn("warning", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
