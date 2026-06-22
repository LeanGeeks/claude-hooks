#!/usr/bin/env python3
"""Unit tests for the epic-10 read subcommands (task 10-03).

Covers ``amux-spawn status / last / ls`` + cause-agnostic reason-context against a
throwaway ``~/.amux`` (redirected via the shared lib's path constants) and an
isolated permission state store, with ``tmux has-session`` patched. No network, no
real tmux, no real ~/.amux / ~/.claude.

Mirrors the task's Testing bullets:
- status returns running / idle / stuck / terminated across the lifecycle.
- --stuck-after override flips a quiet-with-background session to stuck.
- a session stuck on (a) a background task, (b) a hung foreground command,
  (c) a pending permission each surface as stuck WITH the matching reason-context.
- cause-agnostic: a stuck session with BOTH a background task AND a pending
  permission lists BOTH.
- re-activation: after idle, an `amux send` follow-up (a user message newer than
  the last Stop) -> running; then idle again after a new Stop.
- hung first turn: spawning / no mtime_at_stop with old activity + --stuck-after 5s
  -> stuck (not a booting false-positive); a fresh healthy spawning session ->
  running, never idle.
- last returns last_message; ls lists the workspace's tracked sessions + marks dead.
"""

import importlib.machinery
import importlib.util
import json
import os
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
import permission_state_store as store  # noqa: E402


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


def _write_transcript(path: Path, lines: list[dict], mtime: float | None = None) -> float:
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


def _assistant_tool_use(tool_use_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id,
                         "name": name, "input": tool_input}]}}


def _tool_result(tool_use_id: str) -> dict:
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                         "content": "ok"}]}}


def _bg_completion_notification(task_id: str) -> dict:
    # Harness injects a background completion as a user turn with a task-notification.
    return {"type": "user", "isMeta": True, "message": {"role": "user",
            "content": f"<task-notification><task-id>{task_id}</task-id>"
                       f"<status>completed</status></task-notification>"}}


class _StoreEnv:
    """Context manager that isolates the permission_state_store to a temp file."""

    def __init__(self, tmp: Path):
        self.tmp = tmp

    def __enter__(self):
        self._patch = patch.multiple(
            store,
            STATE_FILE=self.tmp / "permission_requests.jsonl",
            AUDIT_LOG_FILE=self.tmp / "permission_actions.jsonl",
        )
        self._patch.start()
        # cli imports the store lazily; make sure that import returns our patched one.
        self._cli_patch = patch.object(cli, "_import_permission_store", return_value=store)
        self._cli_patch.start()
        return self

    def __exit__(self, *exc):
        self._cli_patch.stop()
        self._patch.stop()


# ── State derivation (the core of 10-03) ──────────────────────────────────────


class TestStateDerivation(unittest.TestCase):
    def test_idle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_user_turn("go"), _assistant_text("done")])
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="idle", mtime_at_stop=m, background_tasks=[])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "idle")
                # idle attaches no reason-context.
                self.assertNotIn("reason_context", r)

    def test_running_with_live_background(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("kicked off")])
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 30"}]
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=m, background_tasks=bg)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "running")
                self.assertTrue(r["signals"]["live_background_tasks"])

    def test_stuck_on_background_past_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                m = _write_transcript(tpath, [_assistant_text("kicked off")], mtime=old)
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 9999"}]
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=m,
                                 background_tasks=bg, stuck_after_s=600)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "stuck")
                # reason-context lists the background task.
                self.assertEqual(len(r["reason_context"]["background_tasks"]), 1)
                self.assertEqual(r["reason_context"]["background_tasks"][0]["command"],
                                 "sleep 9999")

    def test_stuck_after_override_flips_running_to_stuck(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 30  # 30s ago: under default 600s, over 5s.
                m = _write_transcript(tpath, [_assistant_text("kicked off")], mtime=old)
                bg = [{"type": "shell", "status": "running", "id": "t1"}]
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=m,
                                 background_tasks=bg, stuck_after_s=600)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    # default 600s -> running
                    self.assertEqual(cli._derive_status(h, None)["state"], "running")
                    # override 5s -> stuck
                    self.assertEqual(cli._derive_status(h, 5)["state"], "stuck")

    def test_terminated_reports_last_state_when_tmux_gone(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="idle", last_state="running", mtime_at_stop=m)
                with patch.object(lib, "tmux_has_session", return_value=False):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["last_state"], "running")

    def test_terminated_from_stored_state(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="terminated", last_state="idle", mtime_at_stop=m)
                # Even with tmux alive, stored terminated wins.
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["last_state"], "idle")


class TestReactivation(unittest.TestCase):
    def test_idle_then_amux_send_followup_reads_running_then_idle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                # 1) idle: Stop recorded, mtime snapshot == current.
                base = time.time() - 100
                m0 = _write_transcript(
                    tpath, [_user_turn("go"), _assistant_text("done")], mtime=base)
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="idle", mtime_at_stop=m0, background_tasks=[])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    self.assertEqual(cli._derive_status(h, None)["state"], "idle")

                    # 2) `amux send` appends a NEW user message; mtime advances past
                    #    mtime_at_stop. Stale state is still idle, but open-turn ->
                    #    running.
                    m1 = _write_transcript(
                        tpath,
                        [_user_turn("go"), _assistant_text("done"),
                         _user_turn("now do more")],
                        mtime=base + 5)
                    self.assertGreater(m1, m0)
                    h2 = lib.read_handle("p-2")  # still state=idle, mtime_at_stop=m0
                    r = cli._derive_status(h2, None)
                    self.assertEqual(r["state"], "running")
                    self.assertTrue(r["signals"]["open_turn"])

                    # 3) New Stop recorded -> idle again (producer updates mtime_at_stop
                    #    to the now-current mtime; no open turn after it).
                    m2 = _write_transcript(
                        tpath,
                        [_user_turn("go"), _assistant_text("done"),
                         _user_turn("now do more"), _assistant_text("done again")],
                        mtime=base + 10)
                    h3 = lib.read_handle("p-2")
                    h3["state"] = "idle"
                    h3["mtime_at_stop"] = m2
                    lib.write_handle("p-2", h3)
                    self.assertEqual(
                        cli._derive_status(lib.read_handle("p-2"), None)["state"], "idle")

    def test_background_completion_notification_does_not_flip_idle(self):
        # A lone background-completion notification bumps mtime but is NOT an open
        # turn -> stays idle (the load-bearing false-flip guard).
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                base = time.time() - 100
                m0 = _write_transcript(
                    tpath, [_user_turn("go"), _assistant_text("done")], mtime=base)
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="idle", mtime_at_stop=m0, background_tasks=[])
                # Append a background-completion notification (mtime advances).
                _write_transcript(
                    tpath,
                    [_user_turn("go"), _assistant_text("done"),
                     _bg_completion_notification("t1")],
                    mtime=base + 5)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertEqual(r["state"], "idle")
                self.assertFalse(r["signals"]["open_turn"])


class TestHungFirstTurn(unittest.TestCase):
    def test_spawning_old_activity_goes_stuck(self):
        # state=spawning / no mtime_at_stop, old transcript activity, --stuck-after 5s.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 100
                _write_transcript(tpath, [_user_turn("go")], mtime=old)
                h = _seed_handle("p-2", "/ws/p", str(tpath))  # spawning, mtime_at_stop None
                self.assertEqual(h["state"], "spawning")
                self.assertIsNone(h["mtime_at_stop"])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, 5)
                self.assertEqual(r["state"], "stuck")
                self.assertTrue(r["signals"]["no_stop_yet"])

    def test_spawning_no_transcript_uses_created_at(self):
        # A session that hangs before writing any transcript still ages into stuck
        # off created_at.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "missing.jsonl"  # never created
                h = _seed_handle("p-2", "/ws/p", str(tpath))
                # Force created_at far in the past.
                from datetime import datetime, timezone, timedelta
                h["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
                lib.write_handle("p-2", h)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), 5)
                self.assertEqual(r["state"], "stuck")

    def test_fresh_healthy_spawning_is_running_never_idle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _write_transcript(tpath, [_user_turn("go")])  # fresh now
                h = _seed_handle("p-2", "/ws/p", str(tpath))  # spawning
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)  # default 600s
                self.assertEqual(r["state"], "running")
                self.assertNotEqual(r["state"], "idle")


class TestReasonContextForeground(unittest.TestCase):
    def test_hung_foreground_tool_surfaces_as_stuck_with_context(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                # An assistant tool_use with NO matching tool_result -> in-flight.
                m = _write_transcript(
                    tpath,
                    [_user_turn("go"),
                     _assistant_tool_use("tu_1", "Bash", {"command": "sleep 9999"})],
                    mtime=old)
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=old - 1,
                                 background_tasks=[], stuck_after_s=600)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "stuck")
                ft = r["reason_context"]["foreground_tool"]
                self.assertIsNotNone(ft)
                self.assertEqual(ft["tool"], "Bash")
                self.assertEqual(ft["command"], "sleep 9999")
                self.assertIn("age_s", ft)

    def test_resolved_tool_is_not_inflight(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _write_transcript(
                    tpath,
                    [_assistant_tool_use("tu_1", "Bash", {"command": "ls"}),
                     _tool_result("tu_1")])
                self.assertIsNone(lib.inflight_foreground_tool(str(tpath)))


class TestReasonContextPermission(unittest.TestCase):
    def test_pending_permission_surfaces_with_precise_session_match(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp), _StoreEnv(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                # A permission block = a gated tool_use that never returned (open
                # turn) after the last assistant text + frozen mtime.
                m = _write_transcript(
                    tpath,
                    [_assistant_text("about to run a gated tool"),
                     _assistant_tool_use("tu_g", "Bash", {"command": "rm -rf x"})],
                    mtime=old)
                sid = "11111111-2222-3333-4444-555555555555"
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=old - 1,
                                 permission_pending=True, stuck_after_s=600)
                # A pending permission request for THIS session.
                store.create_request(
                    session_id=sid, cwd="/ws/p", tool_name="Bash",
                    tool_input={"command": "rm -rf x"},
                    permission_suggestions=["Bash(rm:*)"])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "stuck")
                pp = r["reason_context"]["pending_permission"]
                self.assertIsNotNone(pp)
                self.assertEqual(pp["tool_name"], "Bash")

    def test_permission_for_other_session_does_not_match(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp), _StoreEnv(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                _write_transcript(tpath, [_assistant_text("x")], mtime=old)
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=old - 1,
                                 background_tasks=[], stuck_after_s=600)
                # Pending request for a DIFFERENT session id.
                store.create_request(
                    session_id="99999999-0000-0000-0000-000000000000",
                    cwd="/ws/p", tool_name="Bash", tool_input={"command": "x"},
                    permission_suggestions=[])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertIsNone(r["reason_context"]["pending_permission"])


class TestCauseAgnostic(unittest.TestCase):
    def test_stuck_lists_both_foreground_tool_and_permission(self):
        # A dangling tool_use AFTER the boundary (in-flight foreground tool) AND a
        # permission-store entry for the same session_id must BOTH appear in
        # reason_context simultaneously (cause-agnostic, additive).
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp), _StoreEnv(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                # Transcript: assistant text (Stop boundary), then a dangling tool_use
                # with no matching tool_result — in-flight foreground tool.
                m = _write_transcript(
                    tpath,
                    [_user_turn("go"),
                     _assistant_text("about to run gated tool"),
                     _assistant_tool_use("tu_p", "Bash", {"command": "rm -rf /tmp/x"})],
                    mtime=old)
                sid = "11111111-2222-3333-4444-555555555555"
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=old - 1,
                                 background_tasks=[], permission_pending=True,
                                 stuck_after_s=600)
                # Pending permission for THIS session (same tool).
                store.create_request(
                    session_id=sid, cwd="/ws/p", tool_name="Bash",
                    tool_input={"command": "rm -rf /tmp/x"},
                    permission_suggestions=["Bash(rm:*)"])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "stuck")
                rc = r["reason_context"]
                # BOTH signals listed — foreground_tool from the dangling tool_use,
                # pending_permission from the store.
                self.assertIsNotNone(rc["foreground_tool"],
                                     "foreground_tool must be non-None")
                self.assertEqual(rc["foreground_tool"]["tool"], "Bash")
                self.assertIsNotNone(rc["pending_permission"],
                                     "pending_permission must be non-None")
                self.assertEqual(rc["pending_permission"]["tool_name"], "Bash")

    def test_stuck_lists_both_background_and_permission(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp), _StoreEnv(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 1000
                m = _write_transcript(tpath, [_assistant_text("kicked off + gated")],
                                      mtime=old)
                sid = "11111111-2222-3333-4444-555555555555"
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "sleep 9999"}]
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=old - 1,
                                 background_tasks=bg, permission_pending=True,
                                 stuck_after_s=600)
                store.create_request(
                    session_id=sid, cwd="/ws/p", tool_name="Write",
                    tool_input={"file_path": "/etc/x"}, permission_suggestions=[])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                self.assertEqual(r["state"], "stuck")
                rc = r["reason_context"]
                # BOTH causes listed — not collapsed to one.
                self.assertEqual(len(rc["background_tasks"]), 1)
                self.assertIsNotNone(rc["pending_permission"])


class TestBackgroundTaskOutputFile(unittest.TestCase):
    def test_background_reason_context_includes_bounded_output_tail(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                proj = tmp / "p"
                tpath = proj / "sid.jsonl"
                old = time.time() - 1000
                _write_transcript(tpath, [_assistant_text("bg")], mtime=old)
                # Output file under .../tasks/<id>.output
                tasks = proj / "tasks"
                tasks.mkdir(parents=True)
                (tasks / "t1.output").write_text(
                    "\n".join(f"line {i}" for i in range(50)) + "\n")
                bg = [{"type": "shell", "status": "running", "id": "t1",
                       "command": "long build"}]
                h = _seed_handle("p-2", "/ws/p", str(tpath),
                                 state="running", mtime_at_stop=old - 1,
                                 background_tasks=bg, stuck_after_s=600)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(h, None)
                bgctx = r["reason_context"]["background_tasks"][0]
                self.assertTrue(bgctx["output_file"].endswith("t1.output"))
                # Tail is bounded (default 20 lines).
                self.assertEqual(len(bgctx["output_tail"].splitlines()), 20)
                self.assertIn("output_mtime", bgctx)


# ── CLI command surface (status / last / ls) ──────────────────────────────────


class TestCmdStatusJson(unittest.TestCase):
    def test_status_json_and_human(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                m = _write_transcript(tpath, [_assistant_text("done")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="idle", mtime_at_stop=m, background_tasks=[])
                import io
                with patch.object(lib, "tmux_has_session", return_value=True):
                    # --json
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["status", "p-2", "--json"])
                    self.assertEqual(rc, 0)
                    data = json.loads(buf.getvalue())
                    self.assertEqual(data["state"], "idle")
                    self.assertEqual(data["name"], "p-2")
                    # human line
                    buf2 = io.StringIO()
                    with patch("sys.stdout", buf2):
                        rc = cli.main(["status", "p-2"])
                    self.assertEqual(rc, 0)
                    self.assertIn("p-2: idle", buf2.getvalue())

    def test_status_missing_handle_is_unknown_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = cli.main(["status", "nope", "--json"])
                self.assertEqual(rc, 1)
                data = json.loads(buf.getvalue())
                self.assertEqual(data["state"], "unknown")

    def test_status_stuck_after_cli_override(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                old = time.time() - 30
                m = _write_transcript(tpath, [_assistant_text("bg")], mtime=old)
                bg = [{"type": "shell", "status": "running", "id": "t1"}]
                _seed_handle("p-2", "/ws/p", str(tpath),
                             state="running", mtime_at_stop=m,
                             background_tasks=bg, stuck_after_s=600)
                import io
                with patch.object(lib, "tmux_has_session", return_value=True):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["status", "p-2", "--json", "--stuck-after", "5s"])
                    self.assertEqual(rc, 0)
                    self.assertEqual(json.loads(buf.getvalue())["state"], "stuck")


class TestCmdLast(unittest.TestCase):
    def test_last_returns_message(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _write_transcript(tpath, [_assistant_text("x")])
                _seed_handle("p-2", "/ws/p", str(tpath),
                             last_message="the final answer")
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = cli.main(["last", "p-2"])
                self.assertEqual(rc, 0)
                self.assertEqual(buf.getvalue().strip(), "the final answer")

    def test_last_json(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                _write_transcript(tpath, [_assistant_text("x")])
                _seed_handle("p-2", "/ws/p", str(tpath), last_message="hi there")
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = cli.main(["last", "p-2", "--json"])
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(buf.getvalue())["last_message"], "hi there")

    def test_last_missing_handle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                import io
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["last", "nope"])
                self.assertEqual(rc, 1)


class TestCmdLs(unittest.TestCase):
    def test_ls_lists_workspace_and_marks_dead(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                ws = "/ws/p"
                for nm in ("p", "p-2", "p-3"):
                    _seed_handle(nm, ws, str(tmp / nm / "sid.jsonl"),
                                 state="idle", run_id="rid")
                # one in another workspace
                _seed_handle("other", "/ws/other", str(tmp / "o.jsonl"),
                             state="idle", run_id="rid")
                live = {"p", "p-2"}  # p-3 dead
                import io
                with patch.object(lib, "tmux_has_session",
                                  side_effect=lambda n: n in live):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["ls", "--json", "--dir", ws])
                self.assertEqual(rc, 0)
                data = json.loads(buf.getvalue())
                names = {s["name"]: s for s in data["sessions"]}
                self.assertEqual(set(names), {"p", "p-2", "p-3"})  # other excluded
                self.assertTrue(names["p"]["alive"])
                self.assertFalse(names["p-3"]["alive"])
                self.assertEqual(names["p-3"]["state"], "terminated")  # dead -> terminated

    def test_ls_run_id_filter(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                ws = "/ws/p"
                _seed_handle("p", ws, str(tmp / "a.jsonl"), run_id="run-A")
                _seed_handle("p-2", ws, str(tmp / "b.jsonl"), run_id="run-B")
                import io
                with patch.object(lib, "tmux_has_session", return_value=True):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["ls", "--json", "--dir", ws, "--run-id", "run-A"])
                self.assertEqual(rc, 0)
                sessions = json.loads(buf.getvalue())["sessions"]
                self.assertEqual([s["name"] for s in sessions], ["p"])

    def test_ls_all_lists_every_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _seed_handle("p", "/ws/p", str(tmp / "a.jsonl"))
                _seed_handle("q", "/ws/q", str(tmp / "b.jsonl"))
                import io
                with patch.object(lib, "tmux_has_session", return_value=True):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["ls", "--json", "--all"])
                self.assertEqual(rc, 0)
                names = {s["name"] for s in json.loads(buf.getvalue())["sessions"]}
                self.assertEqual(names, {"p", "q"})

    def test_ls_empty_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = cli.main(["ls", "--json", "--dir", "/nothing/here"])
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(buf.getvalue())["sessions"], [])


class TestFailSoft(unittest.TestCase):
    def test_partial_handle_missing_optional_fields_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # A minimal handle missing most optional fields.
                lib.handle_path("p-2").write_text(json.dumps(
                    {"name": "p-2", "state": "spawning"}))
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                # spawning + no transcript + no created_at -> active, age None -> running
                self.assertIn(r["state"], ("running", "stuck", "idle"))

    def test_malformed_transcript_tail_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tpath = tmp / "t.jsonl"
            tpath.write_text("{not json\nalso bad\n")
            self.assertEqual(lib.read_transcript_tail(str(tpath)), [])
            self.assertFalse(lib.detect_open_turn(str(tpath)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
