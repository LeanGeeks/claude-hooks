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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402
import permission_state_store as store  # noqa: E402
import lifecycle_events  # noqa: E402


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


def _write_lifecycle_log(
    spawn_dir: Path,
    name: str,
    events: list[dict],
) -> None:
    """Write a lifecycle log directly for testing (appends one event at a time)."""
    log_path = spawn_dir / f"{name}.lifecycle.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _make_lifecycle_event(
    event: str,
    state: str,
    seq: int = 1,
    session_id: str = "11111111-2222-3333-4444-555555555555",
    background_tasks_count: int = 0,
    permission_pending: bool = False,
    last_state: str | None = None,
    last_message: str | None = None,
    ts: str | None = None,
) -> dict:
    """Build a lifecycle event record for test fixtures."""
    rec: dict = {
        "seq": seq,
        "ts": ts if ts is not None else datetime.now(timezone.utc).isoformat(),
        "event": event,
        "state": state,
        "session_id": session_id,
        "background_tasks_count": background_tasks_count,
        "permission_pending": permission_pending,
    }
    if last_state is not None:
        rec["last_state"] = last_state
    if last_message is not None:
        rec["last_message"] = last_message
    return rec


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
        """A follow-up turn after idle reads running from the event log alone.

        This is the case that open_turn transcript-tail parsing existed for
        (epic 10 §6.0). After 37-02 the lifecycle log is the authoritative
        source — a turn_start event is recorded by UserPromptSubmit, and a
        subsequent stop with bg=0 settles back to idle. No transcript is read.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # 1) idle: seed handle with idle state; write event log showing a
                #    completed turn (turn_start → stop with bg=0).
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[])
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "idle", seq=2,
                                         background_tasks_count=0),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    self.assertEqual(cli._derive_status(h, None)["state"], "idle")

                    # 2) UserPromptSubmit fires (amux send follow-up): producer appends
                    #    a turn_start event; handle state is updated to "running" by the
                    #    producer.  The event log is now the source of truth.
                    h2 = lib.read_handle("p-2")
                    h2["state"] = "running"
                    lib.write_handle("p-2", h2)
                    _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                        _make_lifecycle_event("turn_start", "running", seq=1),
                        _make_lifecycle_event("stop", "idle", seq=2,
                                             background_tasks_count=0),
                        _make_lifecycle_event("turn_start", "running", seq=3),
                    ])
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                    self.assertEqual(r["state"], "running")
                    self.assertTrue(r["signals"]["turn_open"])
                    self.assertTrue(r["signals"]["open_turn"])  # legacy alias

                    # 3) New Stop recorded → idle again.
                    h3 = lib.read_handle("p-2")
                    h3["state"] = "idle"
                    lib.write_handle("p-2", h3)
                    _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                        _make_lifecycle_event("turn_start", "running", seq=1),
                        _make_lifecycle_event("stop", "idle", seq=2,
                                             background_tasks_count=0),
                        _make_lifecycle_event("turn_start", "running", seq=3),
                        _make_lifecycle_event("stop", "idle", seq=4,
                                             background_tasks_count=0,
                                             last_message="done again"),
                    ])
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
        """A spawning handle with no events and an old created_at goes stuck.

        The activity clock for a pre-first-Stop handle falls back to created_at
        (no event log, no stopped_at, no mtime_at_stop).  Forced to 100 s ago
        so it trips the 5 s threshold.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                tpath = tmp / "p" / "sid.jsonl"
                h = _seed_handle("p-2", "/ws/p", str(tpath))  # spawning, mtime_at_stop None
                self.assertEqual(h["state"], "spawning")
                self.assertIsNone(h["mtime_at_stop"])
                # Force created_at 100 s in the past so the watchdog fires at 5 s.
                h["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
                lib.write_handle("p-2", h)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), 5)
                self.assertEqual(r["state"], "stuck")
                # No stop event seen yet: the log is absent / has no stop entry.
                self.assertFalse(r["signals"]["has_stop_event"])

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


# ── 37-02: lifecycle event log fixtures ──────────────────────────────────────
#
# These tests cover the key shapes called out in the task's "Done when" section:
# no event log, no transcript, legacy handle, permission_pending at top level,
# a finished-but-live worker, and a follow-up turn from the event log alone.


class TestNoTranscriptWorker(unittest.TestCase):
    """A worker that never had a transcript reads correct state from the event log."""

    def test_no_transcript_idle_on_completion(self):
        """The main 37-02 fixture: a worker whose transcript was never written
        (CLAUDE_CODE_CHILD_SESSION=1 silences persistence) reads 'idle' after
        its turn completes, not 'stuck'."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Handle: session completed, no transcript path matters.
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[])
                # Event log: turn_start → stop(idle, bg=0). No transcript file created.
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "idle", seq=2,
                                         background_tasks_count=0,
                                         last_message="done"),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertEqual(r["state"], "idle")
                self.assertNotIn("reason_context", r)

    def test_finished_live_worker_is_idle_not_stuck(self):
        """A finished worker with a live tmux pane reads 'idle', not 'stuck'.

        This is the core invariant: a finished-but-live worker that used to
        read 'stuck' at stuck_after now reads 'idle', so cmd_rm can reap it
        without --force.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[], stuck_after_s=5)
                # Old handle fields that used to cause the bug (mtime_at_stop=None).
                h2 = lib.read_handle("p-2")
                h2["mtime_at_stop"] = None  # would have triggered no_stop_yet = True
                # Force created_at far in the past — used to force stuck
                h2["created_at"] = (datetime.now(timezone.utc)
                                    - timedelta(seconds=1000)).isoformat()
                lib.write_handle("p-2", h2)
                # Event log says: the turn completed cleanly.
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "idle", seq=2,
                                         background_tasks_count=0),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                # Must be idle, not stuck — even though created_at is 1000 s ago
                # and the tmux pane is alive.
                self.assertEqual(r["state"], "idle",
                                 "finished-but-live worker must read idle, not stuck")

    def test_running_background_work_outstanding(self):
        """A worker whose stop had bg tasks is running until the final stop."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="running",
                                 background_tasks=[{"id": "t1", "status": "running"}])
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "running", seq=2,
                                         background_tasks_count=1),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertEqual(r["state"], "running")
                self.assertEqual(r["signals"]["background_tasks_count"], 1)

    def test_subagent_stop_bg_zero_still_running(self):
        """A subagent_stop that drains bg to 0 must NOT produce idle.

        The main turn is still open (the agent is about to respond to the
        subagent completion). Only the subsequent stop settles it.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="running",
                                 background_tasks=[{"id": "t1", "status": "running"}])
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "running", seq=2,
                                         background_tasks_count=1),
                    _make_lifecycle_event("subagent_stop", "running", seq=3,
                                         background_tasks_count=0),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                # Must still be running — the subagent drained bg, but the final
                # stop has not fired yet.
                self.assertEqual(r["state"], "running",
                                 "subagent_stop(bg=0) must not produce idle")

    def test_permission_pending_promoted_to_top_level(self):
        """permission_pending appears at the top level in --json output."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="running", permission_pending=True)
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("permission_prompt", "running", seq=2,
                                         permission_pending=True),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertTrue(r["permission_pending"],
                                "permission_pending must be at the top level")
                self.assertTrue(r["signals"]["permission_pending"])

    def test_no_event_log_degrades_to_stored_state(self):
        """A pre-37-01 handle with no event log uses stored state (backward compat)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Legacy handle: state=idle, no event log.
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertEqual(r["state"], "idle")
                self.assertFalse(r["signals"]["log_available"])

    def test_legacy_handle_no_provider_key_reads_as_claude(self):
        """A handle with no 'provider' key is treated as a Claude handle."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                lib.handle_path("p-2").write_text(json.dumps({
                    "name": "p-2", "state": "idle", "background_tasks": [],
                    "stuck_after_s": 600, "created_at": lib.iso_now(),
                }))
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertEqual(r["provider"], "claude")
                self.assertIn(r["state"], ("idle", "running"))

    def test_event_log_removed_degrades_gracefully(self):
        """A handle whose log was removed reads from stored state (not crash)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[])
                # Write and then remove the log.
                log_path = lib.SPAWN_DIR / "p-2.lifecycle.jsonl"
                log_path.write_text(
                    json.dumps(_make_lifecycle_event("stop", "idle")) + "\n"
                )
                log_path.unlink()
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                # Degrades to stored state: idle.
                self.assertEqual(r["state"], "idle")

    def test_status_json_exposes_permission_pending(self):
        """status --json includes permission_pending at the top level."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="running", permission_pending=True)
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("permission_prompt", "running", seq=2,
                                         permission_pending=True),
                ])
                import io
                with patch.object(lib, "tmux_has_session", return_value=True):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["status", "p-2", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(buf.getvalue())
                self.assertIn("permission_pending", data)
                self.assertTrue(data["permission_pending"])

    def test_ls_uses_derived_state_for_claude_rows(self):
        """cmd_ls now reports derived state for Claude rows (task 37-02 item 8).

        A finished-but-live worker must appear as 'idle' in ls, not 'stuck'.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                ws = "/ws/p"
                h = _seed_handle("p-2", ws, "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[])
                # Force old created_at — would have caused stuck before 37-02.
                h2 = lib.read_handle("p-2")
                h2["created_at"] = (datetime.now(timezone.utc)
                                    - timedelta(seconds=1000)).isoformat()
                h2["mtime_at_stop"] = None
                h2["stuck_after_s"] = 5
                lib.write_handle("p-2", h2)
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "idle", seq=2,
                                         background_tasks_count=0),
                ])
                import io
                with patch.object(lib, "tmux_has_session", return_value=True):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        rc = cli.main(["ls", "--json", "--dir", ws])
                self.assertEqual(rc, 0)
                sessions = json.loads(buf.getvalue())["sessions"]
                p2 = next(s for s in sessions if s["name"] == "p-2")
                self.assertEqual(p2["state"], "idle",
                                 "ls must report derived idle, not stuck")


class TestActivityThresholdDoesNotEraseKnownState(unittest.TestCase):
    """The watchdog (stuck) must NOT overwrite a known idle state."""

    def test_stuck_only_fires_on_genuinely_active_worker(self):
        """stuck fires on a running worker that is stale, not on a finished one."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Running worker — turn is open; no stop yet.
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="running", stuck_after_s=5)
                old_ts = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
                h2 = lib.read_handle("p-2")
                h2["created_at"] = old_ts
                lib.write_handle("p-2", h2)
                # Event is also old — so the activity clock reads as stale.
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1, ts=old_ts),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                # A genuinely running (turn open) + stale worker → stuck.
                self.assertEqual(r["state"], "stuck")
                self.assertTrue(r["signals"]["stale_activity"])

    def test_idle_worker_is_never_stuck_regardless_of_age(self):
        """An idle worker is never overwritten by stuck, even with huge age."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                h = _seed_handle("p-2", "/ws/p", "/ws/p/sid.jsonl",
                                 state="idle", background_tasks=[], stuck_after_s=1)
                h2 = lib.read_handle("p-2")
                h2["created_at"] = (datetime.now(timezone.utc)
                                    - timedelta(seconds=86400)).isoformat()
                lib.write_handle("p-2", h2)
                _write_lifecycle_log(lib.SPAWN_DIR, "p-2", [
                    _make_lifecycle_event("turn_start", "running", seq=1),
                    _make_lifecycle_event("stop", "idle", seq=2,
                                         background_tasks_count=0),
                ])
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                # idle — stuck must never overwrite a known finished state.
                self.assertEqual(r["state"], "idle")
                self.assertFalse(r["signals"]["stale_activity"])


# ── Epic invariants: no transcript reads on the verdict path ─────────────────


class TestNoTranscriptReadsOnVerdictPath(unittest.TestCase):
    """Invariant 1: no status verdict depends on a transcript file read.

    Analogous to TestNoPaneScraping in test_unit_amux_codex_reads.py.
    The verdict path (_derive_claude_status) must not call detect_open_turn.
    That helper lives in the lib and may appear in _reason_context (additive
    only), but the state-deciding function must never read a transcript.
    """

    def _extract_function_source(self, path: Path, func_name: str) -> str:
        """Extract the source lines of a top-level function from a Python file."""
        lines = path.read_text().splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.startswith(f"def {func_name}("):
                start = i
                break
        if start is None:
            return ""
        body_lines = [lines[start]]
        for line in lines[start + 1:]:
            # Stop at the next top-level def or class (non-indented, non-blank).
            if line and not line[0].isspace() and (
                line.startswith("def ") or line.startswith("class ")
            ):
                break
            body_lines.append(line)
        return "\n".join(body_lines)

    def test_derive_claude_status_does_not_call_detect_open_turn(self):
        """detect_open_turn must not appear in _derive_claude_status.

        It may still exist in _reason_context (which is additive, not
        verdict-affecting), but the core state-deciding function must be
        transcript-free (architecture §7 invariant 1, state.md §7).
        """
        source = self._extract_function_source(_BIN, "_derive_claude_status")
        self.assertTrue(source,
                        "_derive_claude_status not found in amux-spawn")
        self.assertNotIn(
            "detect_open_turn", source,
            "_derive_claude_status must not call detect_open_turn "
            "(invariant 1: no transcript reads on the verdict path)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
