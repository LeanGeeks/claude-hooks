#!/usr/bin/env python3
"""Unit tests for Codex reads and supervision (task 20-03).

Covers ``status`` / ``last`` / ``ls`` / ``spawn --wait`` / ``rm`` for bounded
Codex workers against the epic-20 architecture §5 precedence, driven by the
vendored amux 01-01 fixtures in ``tests/fixtures/codex/`` (codex-cli 0.149.0).

Hermetic: throwaway ``~/.amux`` (patched lib path constants), ``tmux
has-session`` stubbed, ``amux`` stubbed, private temp files. No network, no
live codex turn, no real ``~/.amux`` / ``~/.claude`` / ``~/.codex`` — and
``--dangerously-bypass-approvals-and-sandbox`` never appears anywhere.
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
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
_FIXTURES = Path(__file__).parent / "fixtures" / "codex"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402
import codex_event_reducer as reducer  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli_codex_reads",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_codex_reads", str(_BIN)),
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


def _fixture(name: str) -> bytes:
    path = _FIXTURES / f"{name}.jsonl"
    assert path.is_file(), f"missing vendored fixture: {path}"
    return path.read_bytes()


def _stage(tmp: Path, name: str, *, fixture: str | None, rc: int | None = 0,
           result: str | None = None, mtime: float | None = None,
           result_missing: bool = False) -> tuple[Path, Path]:
    """Stage one bounded run's artifacts under ``tmp/spawn`` like amux would.

    ``rc=None`` writes NO ``.rc`` sibling (the killed-wrapper case). The result
    file is pre-created EMPTY unless text is given (amux pre-creates ``R``
    empty; only non-zero size is a signal) — pass ``result_missing=True`` to
    not create it at all.
    """
    spawn = tmp / "spawn"
    spawn.mkdir(parents=True, exist_ok=True)
    events = spawn / f"{name}.events.jsonl"
    events.write_bytes(_fixture(fixture) if fixture else b"")
    if rc is not None:
        Path(f"{events}.rc").write_text(str(rc))  # printf '%s' $? — no newline
    result_path = spawn / f"{name}.result.md"
    if not result_missing:
        result_path.write_text(result if result is not None else "")
    if mtime is not None:
        os.utime(events, (mtime, mtime))
    return events, result_path


def _seed_codex(name: str, tmp: Path, events: Path, result: Path,
                **overrides) -> dict:
    h = lib.new_handle(
        name=name, session_id=None, run_id="rid-codex", abs_dir="/ws/p",
        transcript_path="", stuck_after_s=600, provider=lib.PROVIDER_CODEX,
        activity_path=str(events), result_path=str(result),
    )
    h.update(overrides)
    lib.write_handle(name, h)
    return h


# ── State derivation: the §5 precedence ───────────────────────────────────────


class TestCodexDerivationPrecedence(unittest.TestCase):
    """Each test pins one §5 clause; inverting the clause fails the test."""

    def _derive(self, tmp: Path, name: str, *, alive: bool, **stage_kw):
        events, result = _stage(tmp, name, **stage_kw)
        handle = _seed_codex(name, tmp, events, result)
        with patch.object(lib, "tmux_has_session", return_value=alive):
            return cli._derive_status(handle, None)

    def test_running_while_turn_in_flight(self):
        """tmux alive, thread/turn started, no .rc yet -> running."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=True,
                                 fixture="exec_terminated_sigterm", rc=None)
                self.assertEqual(r["state"], "running")
                self.assertEqual(r["provider"], lib.PROVIDER_CODEX)
                self.assertTrue(r["signals"]["thread_started"])
                self.assertTrue(r["signals"]["turn_started"])
                self.assertFalse(r["signals"]["exit_code_present"])
                self.assertIsNone(r["failure"])

    def test_spawning_before_the_first_event(self):
        """Launch begun, no authoritative event yet -> spawning."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=True, fixture=None, rc=None)
                self.assertEqual(r["state"], "spawning")
                self.assertFalse(r["signals"]["thread_started"])

    def test_activity_falls_back_to_created_at_when_no_artifact(self):
        """§5: activity is the event artifact's mtime, falling back to the
        handle's created_at — a run that never wrote an event still ages into
        stuck off created_at."""
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture=None, rc=None)
                events.unlink()  # the pane never wrote a single event
                handle = _seed_codex(
                    "p-2", tmp, events, result,
                    created_at=(datetime.now(timezone.utc)
                                - timedelta(seconds=1000)).isoformat(),
                )
                with patch.object(lib, "tmux_has_session", return_value=True):
                    r = cli._derive_status(handle, 5)
                self.assertEqual(r["state"], "stuck")

    def test_stale_activity_while_alive_is_stuck(self):
        """Process/tmux alive, activity too old -> stuck (never persisted)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(
                    tmp, "p-2", fixture="exec_terminated_sigterm", rc=None,
                    mtime=time.time() - 1000,
                )
                # Handle threshold 2000s: 1000s-old activity is still running;
                # the 5s per-query override flips it to stuck.
                handle = _seed_codex("p-2", tmp, events, result, stuck_after_s=2000)
                with patch.object(lib, "tmux_has_session", return_value=True):
                    self.assertEqual(cli._derive_status(handle, None)["state"], "running")
                    r = cli._derive_status(handle, 5)  # override flips it
                self.assertEqual(r["state"], "stuck")
                self.assertEqual(r["reason_context"]["codex"]["reason"],
                                 cli.CODEX_REASON_STALE_ACTIVITY)
                # stuck is derived, never written back to the handle.
                self.assertNotEqual(
                    lib.read_handle("p-2").get("state"), "stuck")

    def test_completed_run_with_tmux_gone_is_idle(self):
        """turn.completed + readable result -> idle even though tmux exited."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_success", rc=0, result="OK")
                self.assertEqual(r["state"], "idle")
                self.assertIsNone(r["failure"])
                self.assertNotIn("reason_context", r)

    def test_status_json_carries_the_architecture_s6_field_set(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="OK")
                _seed_codex("p-2", tmp, events, result)
                buf = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", buf):
                    rc = cli.main(["status", "p-2", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(buf.getvalue())
                self.assertEqual(data["provider"], "codex")
                self.assertEqual(data["state"], "idle")
                self.assertEqual(data["thread_id"],
                                 "01a00000-0000-7000-8000-000000000001")
                self.assertEqual(data["attempt"], 1)
                self.assertEqual(data["exit_code"], 0)
                self.assertEqual(data["result_path"], str(result))
                self.assertIsNone(data["failure"])

    def test_sigkill_orphan_exit_137_is_idle(self):
        """§5: turn.completed => idle REGARDLESS of exit code — exit 137 with
        completion is the orphan-child case, NOT a failure. Inverted (exit
        code decides) this reads terminated and the test fails."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_killed_sigkill", rc=137,
                                 result="OK")
                self.assertEqual(r["state"], "idle")
                self.assertEqual(r["exit_code"], 137)  # carried as context only
                self.assertIsNone(r["failure"])
                self.assertTrue(r["signals"]["turn_completed"])
                self.assertFalse(r["signals"]["tmux_alive"])

    def test_sigterm_exit_0_without_completion_is_terminated(self):
        """§5 (the SIGTERM trap): exit 0 with only thread.started/turn.started
        is terminated, NOT idle. Inverted (exit 0 = success) this reads idle
        and the test fails."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_terminated_sigterm", rc=0)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["exit_code"], 0)  # reason context ONLY
                self.assertEqual(r["failure"]["reason"], "no_completion_event")
                self.assertEqual(r["failure"]["exit_code"], 0)

    def test_result_file_existing_without_completion_is_not_idle(self):
        """The --output-last-message file existing is NOT completion evidence."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_terminated_sigterm", rc=0,
                                 result="OK")
                self.assertEqual(r["state"], "terminated")
                self.assertTrue(r["signals"]["result_available"])
                self.assertFalse(r["signals"]["turn_completed"])

    def test_turn_failed_is_terminated_regardless_of_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # exit 0 on top of an explicit turn.failed event.
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_turn_failed", rc=0)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["failure"]["reason"], "turn_failed")
                self.assertEqual(r["failure"]["exit_code"], 0)
                self.assertIn("not supported when using Codex",
                              r["failure"]["message"])

    def test_nonzero_exit_without_completion_is_terminated(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_interrupted_sigint", rc=1)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["failure"]["reason"], "nonzero_exit")
                self.assertEqual(r["failure"]["exit_code"], 1)

    def test_absent_rc_with_process_gone_is_terminated_not_running(self):
        """No .rc at all: the wrapper never finished -> terminated."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_tmux_kill_session", rc=None)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["failure"]["reason"], "no_exit_status")
                self.assertFalse(r["signals"]["exit_code_present"])

    def test_completed_without_readable_result_is_idle_with_context(self):
        """Missing-result case: completion is authoritative (idle), the broken
        result rides as malformed-completion reason context."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                r = self._derive(tmp, "p-2", alive=False,
                                 fixture="exec_success", rc=0,
                                 result_missing=True)
                self.assertEqual(r["state"], "idle")
                self.assertEqual(r["reason_context"]["codex"]["reason"],
                                 cli.CODEX_REASON_MALFORMED_COMPLETION)

    def test_gone_observation_is_rechecked_against_the_artifact(self):
        """The orphan re-check: "process gone, no completion evidence" must be
        re-derivable, never latched — the child can append turn.completed
        ~10 s after the tmux session dies (01-01 SIGKILL evidence)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2",
                                        fixture="exec_terminated_sigterm",
                                        rc=137)
                handle = _seed_codex("p-2", tmp, events, result)
                with patch.object(lib, "tmux_has_session", return_value=False):
                    first = cli._derive_status(handle, None)
                    self.assertEqual(first["state"], "terminated")
                    # ...the orphan child finishes the turn after the parent died.
                    with open(events, "ab") as f:
                        f.write(b'{"type":"turn.completed","usage":{"input_tokens":1}}\n')
                    result.write_text("OK")
                    second = cli._derive_status(handle, None)
                self.assertEqual(second["state"], "idle",
                                 "a re-read must see the appended completion")

    def test_human_status_line_names_the_failure_reason(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2",
                                        fixture="exec_turn_failed", rc=1)
                handle = _seed_codex("p-2", tmp, events, result)
                with patch.object(lib, "tmux_has_session", return_value=False):
                    r = cli._derive_status(handle, None)
                line = cli._human_status_line(r)
                self.assertIn("p-2: terminated", line)
                self.assertIn("turn_failed", line)

    def test_fallback_without_the_reducer_module_is_fail_open(self):
        """A missing reducer degrades to liveness-only, never crashes."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="OK")
                handle = _seed_codex("p-2", tmp, events, result)
                with patch.object(cli, "codex_reducer", None), \
                        patch.object(lib, "tmux_has_session", return_value=False):
                    r = cli._derive_status(handle, None)
                self.assertEqual(r["state"], "terminated")
                self.assertEqual(r["failure"]["reason"], "no_evidence")


class TestDispatchLeavesClaudeAlone(unittest.TestCase):
    """Work item 1: dispatch by provider; the Claude path is untouched."""

    def test_legacy_no_provider_handle_still_derives_as_claude(self):
        """A legacy Claude handle with a COMPLETED codex artifact attached and
        a dead tmux session still derives terminated (Claude precedence:
        liveness + stored state), proving the dispatch key is the provider."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="OK")
                legacy = {
                    "name": "p-2",
                    "session_id": "11111111-2222-3333-4444-555555555555",
                    "run_id": "rid",
                    "dir": "/ws/p",
                    "transcript_path": str(events),
                    "stuck_after_s": 600,
                    "state": "idle",
                    "last_state": None,
                    "last_message": "claude message",
                    "background_tasks": [],
                    "permission_pending": False,
                    "mtime_at_stop": 1e12,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
                lib.write_handle("p-2", legacy)
                with patch.object(lib, "tmux_has_session", return_value=False):
                    r = cli._derive_status(lib.read_handle("p-2"), None)
                self.assertEqual(r["provider"], lib.PROVIDER_CLAUDE)
                self.assertEqual(r["state"], "terminated")  # Claude rule
                self.assertNotIn("thread_id", r)
                self.assertNotIn("exit_code", r)


# ── last: the explicit final-message artifact, bounded ────────────────────────


class TestCodexLast(unittest.TestCase):
    def test_last_reads_the_final_message_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="the answer")
                _seed_codex("p-2", tmp, events, result)
                buf = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", buf):
                    rc = cli.main(["last", "p-2"])
                self.assertEqual(rc, 0)
                self.assertEqual(buf.getvalue().strip(), "the answer")

    def test_last_json_identifies_provider_and_result_path(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="OK")
                _seed_codex("p-2", tmp, events, result)
                buf = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", buf):
                    cli.main(["last", "p-2", "--json"])
                data = json.loads(buf.getvalue())
                self.assertEqual(data["provider"], "codex")
                self.assertEqual(data["last_message"], "OK")
                self.assertEqual(data["result_path"], str(result))

    def test_last_without_a_result_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0)  # result pre-created empty
                _seed_codex("p-2", tmp, events, result)
                stderr = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", stderr):
                    rc = cli.main(["last", "p-2"])
                self.assertEqual(rc, 1)
                self.assertIn("no final-message artifact", stderr.getvalue())

    def test_last_is_bounded_to_the_documented_limit(self):
        """The documented size bound: reducer.MAX_RESULT_BYTES (256 KiB)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="x" * (300 * 1024))
                _seed_codex("p-2", tmp, events, result)
                buf = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", buf):
                    cli.main(["last", "p-2"])
                self.assertEqual(len(buf.getvalue().rstrip("\n")),
                                 reducer.MAX_RESULT_BYTES)

    def test_last_refuses_codex_internal_transcript_paths(self):
        """BRD §4.2: never read (or route around) Codex's internal transcripts."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            fake_codex = tmp / ".codex"
            (fake_codex / "sessions").mkdir(parents=True)
            internal = fake_codex / "sessions" / "rollout.jsonl"
            internal.write_text("internal")
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                handle = lib.new_handle(
                    name="p-2", session_id=None, run_id="r", abs_dir="/ws/p",
                    transcript_path="", stuck_after_s=600,
                    provider=lib.PROVIDER_CODEX,
                    activity_path=str(internal), result_path=str(internal),
                )
                lib.write_handle("p-2", handle)
                with patch.dict(os.environ, {"CODEX_HOME": str(fake_codex)}), \
                        patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["last", "p-2"])
                self.assertEqual(rc, 1)


# ── ls --json: provider + normalized state ────────────────────────────────────


class TestCodexLs(unittest.TestCase):
    def test_ls_json_identifies_provider_and_normalized_state(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # A completed bounded run: tmux gone, completion evidence present.
                events, result = _stage(tmp, "rev-123", fixture="exec_success",
                                        rc=0, result="OK")
                _seed_codex("rev-123", tmp, events, result)
                # A live in-flight run.
                events2, result2 = _stage(tmp, "rev-124",
                                          fixture="exec_terminated_sigterm",
                                          rc=None)
                _seed_codex("rev-124", tmp, events2, result2)
                # A Claude handle in the same workspace (idle + alive).
                lib.write_handle("claude-1", lib.new_handle(
                    name="claude-1", session_id="s", run_id="r", abs_dir="/ws/p",
                    transcript_path="/t.jsonl", stuck_after_s=600,
                ))
                live = {"rev-124", "claude-1"}
                buf = io.StringIO()
                with patch.object(lib, "tmux_has_session",
                                  side_effect=lambda n: n in live), \
                        patch("sys.stdout", buf):
                    rc = cli.main(["ls", "--json", "--dir", "/ws/p"])
                self.assertEqual(rc, 0)
                rows = {s["name"]: s for s in json.loads(buf.getvalue())["sessions"]}
                # Codex rows: provider + derived state — a dead tmux session on
                # a completed run reads idle, NOT terminated.
                self.assertEqual(rows["rev-123"]["provider"], "codex")
                self.assertEqual(rows["rev-123"]["state"], "idle")
                self.assertFalse(rows["rev-123"]["alive"])
                self.assertEqual(rows["rev-124"]["state"], "running")
                # Claude row: the unchanged epic-10 computation.
                self.assertEqual(rows["claude-1"]["provider"], "claude")
                self.assertEqual(rows["claude-1"]["state"], "spawning")

    def test_ls_json_terminated_codex_row(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "dead-1",
                                        fixture="exec_terminated_sigterm",
                                        rc=0)
                _seed_codex("dead-1", tmp, events, result)
                buf = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("sys.stdout", buf):
                    cli.main(["ls", "--json", "--dir", "/ws/p"])
                row = json.loads(buf.getvalue())["sessions"][0]
                self.assertEqual(row["provider"], "codex")
                self.assertEqual(row["state"], "terminated")


# ── wait: exit codes 0 / 1 / 3, stdout-only result, orphan grace ───────────────


class TestCodexWaitForIdle(unittest.TestCase):
    def _seed_running(self, tmp: Path, name="p-2") -> None:
        events, result = _stage(tmp, name, fixture="exec_terminated_sigterm",
                                rc=None)
        _seed_codex(name, tmp, events, result)

    def test_idle_on_first_poll_is_accepted_for_codex(self):
        """Completion evidence is itself proof the turn ran — the Claude
        false-idle gate must not hold a bounded run that finished fast."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2", fixture="exec_success",
                                        rc=0, result="fast answer")
                _seed_codex("p-2", tmp, events, result)
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "fast answer")

    def test_running_then_idle_returns_result_text(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2",
                                        fixture="exec_terminated_sigterm",
                                        rc=None)
                _seed_codex("p-2", tmp, events, result)
                polls = [0]
                real_derive = cli._derive_status

                def derive_then_complete(handle, override):
                    polls[0] += 1
                    if polls[0] == 1:
                        return real_derive(handle, override)  # running
                    # The turn completes between polls.
                    with open(events, "ab") as f:
                        f.write(b'{"type":"turn.completed","usage":{}}\n')
                    Path(f"{events}.rc").write_text("0")
                    result.write_text("the result")
                    return real_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_derive_status",
                                     side_effect=derive_then_complete), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "the result")

    def test_timeout_is_returned_not_error(self):
        """Never-idle bounded run + caller deadline -> timeout (exit 3)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                self._seed_running(tmp)
                now = [time.time()]

                def advancing_time():
                    now[0] += 5
                    return now[0]

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=1.0)
                self.assertEqual(outcome, "timeout")
                self.assertIsNone(payload)

    def test_terminated_is_error_only_after_the_orphan_grace_window(self):
        """Stable terminated past the grace window -> error with the
        normalized failure reason; the reason is in the payload."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "p-2",
                                        fixture="exec_terminated_sigterm",
                                        rc=1)
                _seed_codex("p-2", tmp, events, result)
                with patch.object(cli, "CODEX_ORPHAN_GRACE_S", 0.05), \
                        patch.object(lib, "tmux_has_session",
                                     return_value=False), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "error")
                self.assertIn("terminated", payload)
                self.assertIn("nonzero_exit", payload)

    def test_orphan_completion_during_the_grace_window_flips_to_idle(self):
        """The §5 re-check: "gone, no completion evidence" observed once, then
        the orphan child appends turn.completed — wait must recover to idle."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # SIGKILL mid-turn as first observed: gone, .rc=137, 2 lines.
                events, result = _stage(tmp, "p-2", fixture="exec_killed_sigkill",
                                        rc=None)
                # Truncate to the two pre-kill lines and write .rc=137 as the
                # wrapper saw it.
                lines = events.read_bytes().splitlines(keepends=True)
                events.write_bytes(b"".join(lines[:2]))
                Path(f"{events}.rc").write_text("137")
                _seed_codex("p-2", tmp, events, result)
                polls = [0]
                real_derive = cli._derive_status

                def derive_with_orphan(handle, override):
                    polls[0] += 1
                    if polls[0] >= 2:
                        # The orphan child appends the completion ~10 s later.
                        events.write_bytes(_fixture("exec_killed_sigkill"))
                        result.write_text("OK")
                    return real_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch.object(cli, "_derive_status",
                                     side_effect=derive_with_orphan), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "OK")
                self.assertGreaterEqual(polls[0], 2)

    def test_missing_handle_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                with patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("ghost", timeout_s=1)
                self.assertEqual(outcome, "error")
                self.assertIn("ghost", payload)


class TestCodexSpawnWaitContract(unittest.TestCase):
    """``spawn --provider codex --wait``: exit 0/1/3, stdout-only payload."""

    def _spawn_codex_wait(self, tmp: Path, wait_result, extra=(),
                          wait_flag="--wait"):
        ws = tmp / "myproj"
        ws.mkdir(exist_ok=True)
        live_names: set[str] = set()

        class _FakeCompleted:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, *_a, **_kw):
            if cmd[:2] == ["amux", "exec"]:
                live_names.add(cmd[2])
            return _FakeCompleted(0)

        stdout, stderr = io.StringIO(), io.StringIO()
        with _redirect_amux_home(tmp), \
                patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                patch.object(cli.lib, "list_amux_names", return_value=set()), \
                patch.object(cli.lib, "tmux_has_session",
                             side_effect=lambda n: n in live_names), \
                patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_wait_for_idle", return_value=wait_result), \
                patch("sys.stdin") as stdin, \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            stdin.isatty.return_value = False
            rc = cli.main(["spawn", "--provider", "codex", "--dir", str(ws),
                           *extra, wait_flag, "--", "do the thing"])
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_wait_success_exit_0_payload_on_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, _err = self._spawn_codex_wait(tmp, ("idle", "the answer"))
            self.assertEqual(rc, 0)
            lines = [l for l in out.splitlines() if l.strip()]
            self.assertEqual(lines, ["the answer"])

    def test_wait_timeout_exit_3_marker_on_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, err = self._spawn_codex_wait(
                tmp, ("timeout", None), extra=["--timeout", "5s"])
            self.assertEqual(rc, 3)
            self.assertIn(cli.WAIT_TIMEOUT_MARKER, out)
            self.assertIn("timed out", err)

    def test_wait_error_exit_1_stdout_empty(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, err = self._spawn_codex_wait(
                tmp, ("error", "session 'myproj' terminated before reaching "
                               "idle (turn_failed: exit 1)"))
            self.assertEqual(rc, 1)
            self.assertEqual(out.strip(), "")
            self.assertIn("turn_failed", err)

    def test_notify_is_a_synonym(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, _err = self._spawn_codex_wait(
                tmp, ("idle", "notified"), wait_flag="--notify")
            self.assertEqual(rc, 0)
            self.assertIn("notified", out)


# ── rm: exact-artifact reaping ────────────────────────────────────────────────


class TestCodexRm(unittest.TestCase):
    def _allocate_pair(self, tmp: Path, name: str):
        with _redirect_amux_home(tmp):
            events, result = lib.allocate_codex_artifacts(name)
            Path(f"{events}.err").write_text("stderr")
            Path(f"{events}.rc").write_text("0")
        return events, result

    def test_rm_removes_only_the_exact_handle_artifacts(self):
        """Work item 7 / architecture §9: never a broad glob. Two sibling
        handles' artifacts sit side by side; rm takes only one set."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                ev1, res1 = self._allocate_pair(tmp, "rev-1")
                ev2, res2 = self._allocate_pair(tmp, "rev-2")
                _seed_codex("rev-1", tmp, Path(ev1), Path(res1))
                _seed_codex("rev-2", tmp, Path(ev2), Path(res2))
                # A foreign file sharing the prefix, which a glob would eat.
                foreign = tmp / "spawn" / "rev.json"
                foreign.write_text("{}")
                amux_calls: list[str] = []

                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch.object(cli, "_amux_rm",
                                     side_effect=amux_calls.append), \
                        patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["rm", "rev-1", "--json"])

                self.assertEqual(rc, 0)
                self.assertEqual(amux_calls, ["rev-1"])
                self.assertFalse(lib.handle_path("rev-1").exists())
                # Exactly rev-1's four files are gone...
                for path in (ev1, f"{ev1}.err", f"{ev1}.rc", res1):
                    self.assertFalse(Path(path).exists(), path)
                # ...and rev-2's set + the foreign file survive untouched.
                for path in (ev2, f"{ev2}.err", f"{ev2}.rc", res2):
                    self.assertTrue(Path(path).exists(), path)
                self.assertTrue(foreign.exists())
                self.assertTrue(lib.handle_path("rev-2").exists())

    def test_rm_completed_run_needs_no_force(self):
        """tmux exit after success: state idle -> reaped cleanly (no --force)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "rev-3", fixture="exec_success",
                                        rc=0, result="OK")
                _seed_codex("rev-3", tmp, events, result)
                stdout = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch.object(cli, "_amux_rm"), \
                        patch("sys.stdout", stdout), \
                        patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["rm", "rev-3", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(stdout.getvalue())
                self.assertEqual(data["state_at_rm"], "idle")
                self.assertFalse(data["killed"])
                self.assertFalse(events.exists())
                self.assertFalse(result.exists())
                self.assertFalse(Path(f"{events}.rc").exists())

    def test_rm_refuses_a_running_codex_worker_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "rev-4",
                                        fixture="exec_terminated_sigterm",
                                        rc=None)
                _seed_codex("rev-4", tmp, events, result)
                stderr = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_amux_rm") as amux_rm, \
                        patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", stderr):
                    rc = cli.main(["rm", "rev-4"])
                self.assertEqual(rc, 1)
                amux_rm.assert_not_called()
                self.assertIn("running", stderr.getvalue())
                self.assertTrue(events.exists())
                self.assertTrue(lib.handle_path("rev-4").exists())

    def test_rm_force_reaps_a_running_worker(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "rev-5",
                                        fixture="exec_terminated_sigterm",
                                        rc=None)
                _seed_codex("rev-5", tmp, events, result)
                stdout = io.StringIO()
                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_amux_rm"), \
                        patch("sys.stdout", stdout), \
                        patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["rm", "rev-5", "--force", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(stdout.getvalue())
                self.assertTrue(data["killed"])
                self.assertFalse(events.exists())
                self.assertFalse(lib.handle_path("rev-5").exists())

    def test_rm_spawning_worker_requires_force(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage(tmp, "rev-6", fixture=None, rc=None)
                _seed_codex("rev-6", tmp, events, result)
                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_amux_rm") as amux_rm, \
                        patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", io.StringIO()):
                    rc = cli.main(["rm", "rev-6"])
                self.assertEqual(rc, 1)
                amux_rm.assert_not_called()
                self.assertTrue(lib.handle_path("rev-6").exists())


# ── Epic invariants: no pane scraping, hermeticity ────────────────────────────


class TestNoPaneScraping(unittest.TestCase):
    def test_no_capture_pane_anywhere_on_the_read_path(self):
        """Done-when: lifecycle never comes from terminal-pane text. The CLI
        and the reducer must not even spell ``capture-pane``."""
        for path in (_BIN, _HOOKS / "codex_event_reducer.py",
                     _HOOKS / "amux_spawn_lib.py"):
            self.assertNotIn("capture-pane", path.read_text(), str(path))

    def test_no_yolo_bypass_flag_in_the_reducer_or_lib(self):
        """Hermeticity guard: only amux expands --yolo (architecture §8). The
        reads-side modules never spell Codex's bypass flag at all. (The CLI
        mentions it only in --yolo help/error text; the 20-02 suite pins that
        it never appears in a constructed argv.)"""
        for path in (_HOOKS / "codex_event_reducer.py",
                     _HOOKS / "amux_spawn_lib.py"):
            self.assertNotIn(
                "--dangerously-bypass-approvals-and-sandbox", path.read_text(),
                str(path),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
