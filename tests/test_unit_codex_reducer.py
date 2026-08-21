#!/usr/bin/env python3
"""Unit tests for the Codex event reducer and the provider-neutral handle (20-01).

Two layers:

1. ``TestHandleSchemaMigration`` / ``TestLegacyHandleDerivesLikeClaude`` — the
   epic-20 §3 schema migration: the new fields exist, a **legacy handle with no
   ``provider`` key is a Claude handle** (cross-task invariant 2), and a legacy
   and a migrated Claude handle derive the *same* status through the real
   ``amux-spawn`` derivation code.

2. ``TestReduce*`` — the pure, bounded reducer, driven by the fixtures captured
   live from codex-cli 0.149.0 by the amux 01-01 lifecycle spike and vendored
   into ``tests/fixtures/codex/`` (see that directory's ``SOURCE.txt``). The
   critical assertions encode architecture §5's priority rule: completion
   evidence beats the exit code in both directions.

Hermetic: no network, no tmux, no subprocess, nothing touches the developer's
real ``~/.claude`` / ``~/.amux`` / ``~/.codex``.
"""

import ast
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
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
    """Import the executable ``amux-spawn`` (no .py extension) as a module."""
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli_codex",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_codex", str(_BIN)),
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


def _fixture(name: str) -> Path:
    """Path to a vendored Codex fixture (asserts it exists so a typo is loud)."""
    path = _FIXTURES / f"{name}.jsonl"
    assert path.is_file(), f"missing vendored fixture: {path}"
    return path


def _fixture_meta(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.meta.json").read_text())


def _stage(tmp: Path, name: str, *, rc: int | None = None, result: str | None = None) -> Path:
    """Copy a fixture into *tmp* as a live artifact, with optional .rc / result files.

    Returns the staged event-artifact path. ``rc=None`` deliberately writes NO
    ``.rc`` file — that is the killed-session case.
    """
    dst = tmp / f"{name}.jsonl"
    dst.write_bytes(_fixture(name).read_bytes())
    if rc is not None:
        Path(f"{dst}.rc").write_text(str(rc))  # printf '%s' $? — no trailing NL
    if result is not None:
        (tmp / f"{name}.last.md").write_text(result)
    return dst


def _result_path(tmp: Path, name: str) -> str:
    return str(tmp / f"{name}.last.md")


# ── 1. Handle schema migration ───────────────────────────────────────────────

# A handle exactly as epic 10 wrote it, before the epic-20 migration. Frozen on
# purpose: it must keep deriving as a Claude handle forever.
LEGACY_HANDLE = {
    "name": "proj-2",
    "session_id": "11111111-2222-3333-4444-555555555555",
    "run_id": "rid-legacy",
    "dir": "/ws/proj",
    "transcript_path": "/tmp/does-not-matter/sid.jsonl",
    "stuck_after_s": 600,
    "state": "idle",
    "last_state": None,
    "last_message": "legacy message",
    "background_tasks": [],
    "permission_pending": False,
    "mtime_at_stop": 1000.0,
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


class TestHandleSchemaMigration(unittest.TestCase):
    def test_schema_carries_epic20_fields_and_keeps_transcript_path(self):
        for field in (
            "provider", "session_id", "activity_path", "result_path",
            "process_pid", "exit_code", "failure",
        ):
            self.assertIn(field, lib.HANDLE_FIELDS, f"{field} missing from HANDLE_FIELDS")
        # transcript_path is KEPT during the migration (architecture §3) so
        # installed Claude hooks and old readers do not break.
        self.assertIn("transcript_path", lib.HANDLE_FIELDS)
        # No duplicate entries.
        self.assertEqual(len(lib.HANDLE_FIELDS), len(set(lib.HANDLE_FIELDS)))

    def test_new_handle_defaults_to_claude_and_mirrors_activity_path(self):
        h = lib.new_handle(
            name="proj-2", session_id="abc", run_id="rid",
            abs_dir="/abs", transcript_path="/t.jsonl", stuck_after_s=600,
        )
        self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))
        self.assertEqual(h["provider"], lib.PROVIDER_CLAUDE)
        self.assertEqual(h["activity_path"], "/t.jsonl")
        self.assertIsNone(h["result_path"])
        self.assertIsNone(h["process_pid"])
        self.assertIsNone(h["exit_code"])
        self.assertIsNone(h["failure"])

    def test_new_handle_can_build_a_codex_handle(self):
        h = lib.new_handle(
            name="review-123", session_id="01a00000-0000-7000-8000-000000000001",
            run_id="rid", abs_dir="/abs", transcript_path="",
            stuck_after_s=600, provider=lib.PROVIDER_CODEX,
            activity_path="/amux/spawn/review-123.events.jsonl",
            result_path="/amux/spawn/review-123.last.md",
            process_pid=4242,
        )
        self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))
        self.assertTrue(lib.is_codex_handle(h))
        self.assertFalse(lib.is_claude_handle(h))
        self.assertEqual(lib.handle_activity_path(h), "/amux/spawn/review-123.events.jsonl")
        self.assertEqual(h["process_pid"], 4242)

    def test_legacy_handle_without_provider_key_is_claude(self):
        """Cross-task invariant 2 — the load-bearing compatibility guarantee."""
        self.assertNotIn("provider", LEGACY_HANDLE)
        self.assertEqual(lib.handle_provider(LEGACY_HANDLE), lib.PROVIDER_CLAUDE)
        self.assertTrue(lib.is_claude_handle(LEGACY_HANDLE))
        self.assertFalse(lib.is_codex_handle(LEGACY_HANDLE))

    def test_provider_defaults_are_fail_soft(self):
        self.assertEqual(lib.handle_provider({}), lib.PROVIDER_CLAUDE)
        self.assertEqual(lib.handle_provider({"provider": ""}), lib.PROVIDER_CLAUDE)
        self.assertEqual(lib.handle_provider({"provider": None}), lib.PROVIDER_CLAUDE)
        self.assertEqual(lib.handle_provider({"provider": 7}), lib.PROVIDER_CLAUDE)
        self.assertEqual(lib.handle_provider(None), lib.PROVIDER_CLAUDE)
        self.assertEqual(lib.handle_provider({"provider": " CODEX "}), lib.PROVIDER_CODEX)
        # An unknown provider must NOT be mistaken for claude.
        self.assertFalse(lib.is_claude_handle({"provider": "gemini"}))

    def test_activity_path_falls_back_to_transcript_path(self):
        self.assertEqual(
            lib.handle_activity_path(LEGACY_HANDLE), LEGACY_HANDLE["transcript_path"]
        )
        self.assertIsNone(lib.handle_activity_path({}))
        self.assertIsNone(lib.handle_activity_path({"transcript_path": ""}))

    def test_upgrade_handle_is_pure_and_fills_defaults(self):
        upgraded = lib.upgrade_handle(LEGACY_HANDLE)
        self.assertNotIn("provider", LEGACY_HANDLE, "upgrade_handle mutated its input")
        self.assertEqual(set(lib.HANDLE_FIELDS) - set(upgraded), set())
        self.assertEqual(upgraded["provider"], lib.PROVIDER_CLAUDE)
        self.assertEqual(upgraded["activity_path"], LEGACY_HANDLE["transcript_path"])
        self.assertIsNone(upgraded["result_path"])
        self.assertIsNone(upgraded["exit_code"])
        self.assertIsNone(upgraded["failure"])
        # Unknown keys from a newer writer survive an older reader.
        self.assertEqual(lib.upgrade_handle({"future_field": 1})["future_field"], 1)

    def test_upgrade_handle_does_not_clobber_codex_values(self):
        codex = {
            "provider": "codex",
            "activity_path": "/a/events.jsonl",
            "result_path": "/a/last.md",
            "exit_code": 137,
            "failure": {"reason": "turn_failed"},
        }
        upgraded = lib.upgrade_handle(codex)
        self.assertEqual(upgraded["provider"], "codex")
        self.assertEqual(upgraded["activity_path"], "/a/events.jsonl")
        self.assertEqual(upgraded["exit_code"], 137)
        self.assertEqual(upgraded["failure"], {"reason": "turn_failed"})

    def test_legacy_handle_roundtrips_through_the_registry_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                lib.write_handle("proj-2", LEGACY_HANDLE)
                back = lib.read_handle("proj-2")
                # read_handle does NOT rewrite/migrate on disk.
                self.assertEqual(back, LEGACY_HANDLE)
                self.assertNotIn("provider", back)
                self.assertTrue(lib.is_claude_handle(back))
                self.assertEqual(list(lib.SPAWN_DIR.glob(".proj-2.*.tmp")), [])


class TestLegacyHandleDerivesLikeClaude(unittest.TestCase):
    """Done-when: legacy and new Claude handle fixtures derive the same results."""

    def _derive_pair(self, transcript: Path, stored_state: str, mtime_at_stop):
        legacy = dict(LEGACY_HANDLE)
        legacy["transcript_path"] = str(transcript)
        legacy["state"] = stored_state
        legacy["mtime_at_stop"] = mtime_at_stop

        migrated = lib.new_handle(
            name=legacy["name"], session_id=legacy["session_id"],
            run_id=legacy["run_id"], abs_dir=legacy["dir"],
            transcript_path=str(transcript), stuck_after_s=legacy["stuck_after_s"],
        )
        migrated["state"] = stored_state
        migrated["last_message"] = legacy["last_message"]
        migrated["mtime_at_stop"] = mtime_at_stop
        migrated["created_at"] = legacy["created_at"]
        migrated["updated_at"] = legacy["updated_at"]
        return legacy, migrated

    def _assert_same(self, alive: bool, stored_state: str, mtime_at_stop):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            transcript = tmp / "sid.jsonl"
            transcript.write_text(
                json.dumps({"type": "assistant",
                            "message": {"role": "assistant",
                                        "content": [{"type": "text", "text": "hi"}]}}) + "\n"
            )
            legacy, migrated = self._derive_pair(transcript, stored_state, mtime_at_stop)
            with patch.object(lib, "tmux_has_session", return_value=alive), \
                    patch.object(cli, "_reason_context", return_value={}):
                a = cli._derive_status(legacy, None)
                b = cli._derive_status(migrated, None)
            # activity_age_s is a wall-clock read; compare everything else exactly.
            a.pop("activity_age_s", None)
            b.pop("activity_age_s", None)
            self.assertEqual(a, b)
            return a

    def test_idle_session_derives_identically(self):
        result = self._assert_same(alive=True, stored_state="idle", mtime_at_stop=1e12)
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["provider"], lib.PROVIDER_CLAUDE)

    def test_running_session_derives_identically(self):
        result = self._assert_same(alive=True, stored_state="spawning", mtime_at_stop=None)
        self.assertEqual(result["state"], "running")

    def test_dead_session_derives_identically(self):
        result = self._assert_same(alive=False, stored_state="idle", mtime_at_stop=1e12)
        self.assertEqual(result["state"], "terminated")
        self.assertEqual(result["provider"], lib.PROVIDER_CLAUDE)


# ── 2. Reducer purity / bounds ───────────────────────────────────────────────

class TestReducerPurity(unittest.TestCase):
    def test_reducer_imports_only_the_standard_library(self):
        """Architecture §9: Codex reducers do not import Telegram or permission
        modules — and being pure, they launch nothing either.

        Checked on the parsed AST (not a text grep), so a docstring mentioning a
        module name cannot fake a pass or a failure.
        """
        tree = ast.parse((_HOOKS / "codex_event_reducer.py").read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        for forbidden in (
            "subprocess", "telegram", "telegram_permission_router",
            "permission_state_store", "permission_request_hook",
            "amux_spawn_lib", "notification_hook", "requests", "socket", "urllib",
        ):
            self.assertNotIn(
                forbidden, imported,
                f"codex_event_reducer must not import {forbidden}",
            )
        # Whatever it does import must be stdlib-only (no third-party, no
        # sibling hook modules).
        self.assertTrue(imported.issubset(set(sys.stdlib_module_names)), imported)

    def test_reducer_refuses_codex_internal_transcripts(self):
        """Invariant 5: never read ``~/.codex/sessions/`` rollout files."""
        with tempfile.TemporaryDirectory() as d:
            fake_codex_home = Path(d) / ".codex"
            rollout_dir = fake_codex_home / "sessions" / "2026" / "08" / "21"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-2026-08-21T14-57-16-01a0.jsonl"
            rollout.write_bytes(_fixture("exec_success").read_bytes())

            with patch.dict(os.environ, {"CODEX_HOME": str(fake_codex_home)}):
                self.assertTrue(reducer.is_codex_internal_path(rollout))
                out = reducer.reduce_events(rollout)
                self.assertTrue(out["refused_path"])
                # Nothing was parsed out of the internal file.
                self.assertFalse(out["turn_completed"])
                self.assertIsNone(out["thread_id"])
                self.assertEqual(out["events"], 0)
                self.assertIsNone(out["state_hint"])
                # ...and a result path inside ~/.codex is refused too.
                self.assertIsNone(reducer.read_result_text(fake_codex_home / "x.md"))

    def test_internal_path_check_is_lexical_and_precise(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / ".codex"
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertTrue(reducer.is_codex_internal_path(home / "sessions" / "a.jsonl"))
                self.assertTrue(reducer.is_codex_internal_path(home))
                # A sibling directory sharing the prefix is NOT internal.
                self.assertFalse(reducer.is_codex_internal_path(str(home) + "-backup/a.jsonl"))
                self.assertFalse(reducer.is_codex_internal_path(Path(d) / "amux" / "e.jsonl"))
                self.assertFalse(reducer.is_codex_internal_path(None))

    def test_symlink_into_codex_home_is_rejected(self):
        """A symlink outside CODEX_HOME that resolves into it must be refused.

        This test constructs a real symlink so it is meaningful: abspath would
        NOT catch this (it normalizes lexically only), while realpath does.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # Fake ~/.codex with a real target file inside it.
            fake_codex_home = tmp / ".codex"
            sessions_dir = fake_codex_home / "sessions"
            sessions_dir.mkdir(parents=True)
            internal_file = sessions_dir / "internal.jsonl"
            internal_file.write_bytes(_fixture("exec_success").read_bytes())

            # A symlink living OUTSIDE ~/.codex that points INTO it.
            outside_dir = tmp / "amux" / "sessions"
            outside_dir.mkdir(parents=True)
            symlink_path = outside_dir / "session.events.jsonl"
            symlink_path.symlink_to(internal_file)

            with patch.dict(os.environ, {"CODEX_HOME": str(fake_codex_home)}):
                # The symlink path is outside CODEX_HOME lexically …
                self.assertFalse(
                    str(symlink_path).startswith(str(fake_codex_home)),
                    "precondition: symlink lives outside CODEX_HOME lexically",
                )
                # … but the guard must still detect it as internal.
                self.assertTrue(
                    reducer.is_codex_internal_path(symlink_path),
                    "symlink into CODEX_HOME must be treated as internal",
                )
                # And reduce_events must refuse it.
                out = reducer.reduce_events(symlink_path)
                self.assertTrue(out["refused_path"])
                self.assertEqual(out["events"], 0)

    def test_oversized_artifact_is_sampled_head_and_tail(self):
        """Bounded: a huge artifact keeps thread identity AND the turn outcome."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "big.jsonl"
            filler = json.dumps({
                "type": "item.completed",
                "item": {"id": "item_1", "type": "command_execution",
                         "command": "x", "aggregated_output": "y" * 900,
                         "exit_code": 0, "status": "completed"},
            }) + "\n"
            with open(path, "w") as f:
                f.write('{"type":"thread.started","thread_id":"01a00000-0000-7000-8000-000000000001"}\n')
                f.write('{"type":"turn.started"}\n')
                for _ in range(3000):
                    f.write(filler)
                f.write('{"type":"turn.completed","usage":{"input_tokens":1}}\n')
            size = path.stat().st_size
            self.assertGreater(size, 512 * 1024)

            out = reducer.reduce_events(path, max_bytes=64 * 1024)
            self.assertTrue(out["sampled"])
            self.assertEqual(out["thread_id"], "01a00000-0000-7000-8000-000000000001")
            self.assertTrue(out["turn_completed"])
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            # The unsampled read of the same file agrees on the verdict.
            full = reducer.reduce_events(path)
            self.assertFalse(full["sampled"])
            self.assertEqual(full["state_hint"], reducer.STATE_IDLE)
            self.assertEqual(full["thread_id"], out["thread_id"])

    def test_missing_artifact_is_fail_soft(self):
        out = reducer.reduce_events("/nonexistent/definitely/not/here.jsonl")
        self.assertEqual(out["events"], 0)
        self.assertIsNone(out["thread_id"])
        self.assertIsNone(out["state_hint"])
        self.assertEqual(out["outcome"], reducer.OUTCOME_INCOMPLETE)
        self.assertIsNone(reducer.reduce_events(None)["state_hint"])


# ── 3. Reducer against the captured fixtures ─────────────────────────────────

class TestReduceSuccess(unittest.TestCase):
    def test_success_fixture(self):
        meta = _fixture_meta("exec_success")
        self.assertEqual(meta["exit_code"], 0)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_success", rc=0, result="OK")
            out = reducer.reduce_events(
                path, result_path=_result_path(tmp, "exec_success"), read_result=True
            )
            self.assertEqual(out["thread_id"], "01a00000-0000-7000-8000-000000000001")
            self.assertEqual(out["thread_ids"], ["01a00000-0000-7000-8000-000000000001"])
            self.assertTrue(out["thread_started"])
            self.assertTrue(out["turn_started"])
            self.assertTrue(out["turn_completed"])
            self.assertFalse(out["turn_failed"])
            self.assertEqual(out["segments"], 1)
            self.assertEqual(out["events"], 4)
            self.assertEqual(out["last_agent_message"], "OK")
            self.assertEqual(out["exit_code"], 0)
            self.assertTrue(out["exit_code_present"])
            self.assertTrue(out["result_available"])
            self.assertEqual(out["result_text"], "OK")
            self.assertEqual(out["outcome"], reducer.OUTCOME_COMPLETED)
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            self.assertIsNone(out["failure"])
            self.assertFalse(out["truncated_tail"])
            self.assertEqual(out["malformed_lines"], 0)
            self.assertIsNotNone(out["activity_mtime"])

    def test_started_only_is_not_completion(self):
        """thread.started + turn.started while the process is live => no verdict."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # No .rc file yet: the wrapper has not reached its printf.
            path = _stage(tmp, "exec_terminated_sigterm", rc=None)
            out = reducer.reduce_events(path)
            self.assertTrue(out["thread_started"])
            self.assertTrue(out["turn_started"])
            self.assertFalse(out["turn_completed"])
            self.assertEqual(out["outcome"], reducer.OUTCOME_INCOMPLETE)
            self.assertIsNone(out["state_hint"], "a live run must not be latched terminal")

    def test_resume_segment_reuses_the_thread_id(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # amux appends the resume segment to the same event log.
            path = tmp / "segments.jsonl"
            path.write_bytes(
                _fixture("exec_success").read_bytes()
                + _fixture("exec_resume_success").read_bytes()
            )
            out = reducer.reduce_events(path, exit_code=0, exit_code_present=True)
            self.assertEqual(out["segments"], 2)
            self.assertEqual(out["thread_ids"], ["01a00000-0000-7000-8000-000000000001"])
            self.assertEqual(out["thread_id"], "01a00000-0000-7000-8000-000000000001")
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)

    def test_resume_name_miss_exposes_a_different_thread_id(self):
        """The launcher fails closed on this; the reducer must surface the fact."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "segments.jsonl"
            path.write_bytes(
                _fixture("exec_success").read_bytes()
                + _fixture("exec_resume_name_miss_new_thread").read_bytes()
            )
            out = reducer.reduce_events(path, exit_code=0, exit_code_present=True)
            self.assertEqual(out["segments"], 2)
            self.assertEqual(
                out["thread_ids"],
                ["01a00000-0000-7000-8000-000000000001",
                 "01a00000-0000-7000-8000-000000000008"],
            )
            # First thread.started stays the authoritative identity.
            self.assertEqual(out["thread_id"], "01a00000-0000-7000-8000-000000000001")


class TestReduceFailure(unittest.TestCase):
    def test_turn_failed_fixture(self):
        meta = _fixture_meta("exec_turn_failed")
        self.assertEqual(meta["exit_code"], 1)
        self.assertIsNone(meta["output_last_message"])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_turn_failed", rc=1)
            out = reducer.reduce_events(
                path, result_path=_result_path(tmp, "exec_turn_failed")
            )
            self.assertTrue(out["turn_failed"])
            self.assertFalse(out["turn_completed"])
            self.assertEqual(out["outcome"], reducer.OUTCOME_FAILED)
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["failure"]["reason"], "turn_failed")
            self.assertEqual(out["failure"]["exit_code"], 1)
            # Nested JSON error document unwrapped to a sentence (architecture §3:
            # "normalized summary, not raw terminal scrape").
            self.assertIn("not supported when using Codex", out["failure"]["message"])
            self.assertNotIn('"status": 400', out["failure"]["message"])
            # turn.failed writes no result artifact.
            self.assertFalse(out["result_available"])

    def test_turn_failed_is_terminated_even_with_exit_zero(self):
        """Architecture §5: turn.failed => terminated REGARDLESS of exit code."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_turn_failed", rc=0)
            out = reducer.reduce_events(path)
            self.assertEqual(out["exit_code"], 0)
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["outcome"], reducer.OUTCOME_FAILED)

    def test_sigterm_exit_zero_without_completion_is_terminated(self):
        """The evidence that exit 0 is NOT a success signal."""
        meta = _fixture_meta("exec_terminated_sigterm")
        self.assertEqual(meta["exit_code"], 0)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_terminated_sigterm", rc=0)
            out = reducer.reduce_events(path)
            self.assertEqual(out["exit_code"], 0)
            self.assertFalse(out["turn_completed"])
            self.assertEqual(out["outcome"], reducer.OUTCOME_INCOMPLETE)
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["failure"]["reason"], "no_completion_event")
            self.assertEqual(out["failure"]["exit_code"], 0)

    def test_sigint_nonzero_exit_without_completion_is_terminated(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_interrupted_sigint", rc=1)
            out = reducer.reduce_events(path)
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["failure"]["reason"], "nonzero_exit")
            self.assertEqual(out["failure"]["exit_code"], 1)

    def test_empty_stream_launch_failures_are_terminated(self):
        """Launch-time failures emit no events at all, only a nonzero .rc."""
        for name, rc in (("exec_invalid_config", 1), ("exec_bad_cwd", 1)):
            with self.subTest(fixture=name):
                with tempfile.TemporaryDirectory() as d:
                    tmp = Path(d)
                    self.assertEqual(_fixture_meta(name)["exit_code"], rc)
                    path = _stage(tmp, name, rc=rc)
                    out = reducer.reduce_events(path)
                    self.assertEqual(out["events"], 0)
                    self.assertFalse(out["thread_started"])
                    self.assertIsNone(out["thread_id"])
                    self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
                    self.assertEqual(out["failure"]["reason"], "nonzero_exit")

    def test_result_file_without_completion_is_not_success(self):
        """A ``--output-last-message`` file is never completion evidence."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_terminated_sigterm", rc=0, result="OK")
            out = reducer.reduce_events(
                path, result_path=_result_path(tmp, "exec_terminated_sigterm"),
                read_result=True,
            )
            self.assertTrue(out["result_available"])
            self.assertEqual(out["result_text"], "OK")
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["outcome"], reducer.OUTCOME_INCOMPLETE)


class TestProcessDisappeared(unittest.TestCase):
    def test_tmux_kill_session_leaves_no_rc_and_is_terminated(self):
        """No ``.rc`` at all: amux's wrapper never finished => terminated."""
        self.assertIsNone(_fixture_meta("exec_tmux_kill_session")["exit_code"])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_tmux_kill_session", rc=None)
            self.assertFalse(Path(reducer.rc_path_for(path)).exists())
            out = reducer.reduce_events(path, process_gone=True)
            self.assertFalse(out["exit_code_present"])
            self.assertIsNone(out["exit_code"])
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["failure"]["reason"], "no_exit_status")

    def test_absent_rc_while_not_yet_observed_gone_stays_undecided(self):
        """The orphan hazard: don't latch 'gone' before re-checking the artifact."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_tmux_kill_session", rc=None)
            out = reducer.reduce_events(path, process_gone=False)
            self.assertIsNone(out["state_hint"])

    def test_sigkill_orphan_completed_the_turn_despite_exit_137(self):
        """Architecture §5: turn.completed => idle even with exit 137."""
        meta = _fixture_meta("exec_killed_sigkill")
        self.assertEqual(meta["exit_code"], 137)
        self.assertEqual(meta["output_last_message"], "OK")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_killed_sigkill", rc=137, result="OK")
            out = reducer.reduce_events(
                path,
                result_path=_result_path(tmp, "exec_killed_sigkill"),
                process_gone=True,
                read_result=True,
            )
            self.assertEqual(out["exit_code"], 137)
            self.assertTrue(out["turn_completed"])
            self.assertEqual(out["outcome"], reducer.OUTCOME_COMPLETED)
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            self.assertIsNone(out["failure"])
            self.assertEqual(out["result_text"], "OK")

    def test_orphan_appends_completion_after_a_gone_observation(self):
        """Re-running the reducer after the artifact grows flips the verdict."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "events.jsonl"
            path.write_bytes(_fixture("exec_terminated_sigterm").read_bytes())
            Path(f"{path}.rc").write_text("137")
            first = reducer.reduce_events(path, process_gone=True)
            self.assertEqual(first["state_hint"], reducer.STATE_TERMINATED)
            # ~10 s later the orphan child appends its completion.
            with open(path, "ab") as f:
                f.write(b'{"type":"turn.completed","usage":{"input_tokens":1}}\n')
            second = reducer.reduce_events(path, process_gone=True)
            self.assertEqual(second["state_hint"], reducer.STATE_IDLE)


class TestUnknownAndTruncated(unittest.TestCase):
    def test_unknown_events_are_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_unknown_event", rc=0)
            out = reducer.reduce_events(path)
            self.assertEqual(out["unknown_event_types"], ["thread.future_event_type_v2"])
            self.assertEqual(out["unknown_item_types"], ["future_item_kind"])
            # ...and the real turn still reduces to a completion.
            self.assertTrue(out["turn_completed"])
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            self.assertEqual(out["last_agent_message"], "OK")
            self.assertEqual(out["malformed_lines"], 0)
            self.assertEqual(out["events"], 6)

    def test_unknown_events_are_retained_in_the_raw_artifact(self):
        """§4: unknown types are ignored by the reader, never stripped on disk."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_unknown_event", rc=0)
            before = path.read_bytes()
            reducer.reduce_events(path, read_result=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertIn(b"thread.future_event_type_v2", path.read_bytes())

    def test_truncated_final_line_is_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # Live write: no .rc yet.
            path = _stage(tmp, "exec_truncated_final_line", rc=None)
            out = reducer.reduce_events(path)
            self.assertTrue(out["truncated_tail"])
            self.assertEqual(out["malformed_lines"], 0)
            # Everything before the partial line was still reduced.
            self.assertEqual(out["thread_id"], "01a00000-0000-7000-8000-000000000001")
            self.assertTrue(out["turn_started"])
            self.assertEqual(out["last_agent_message"], "OK")
            # The partial turn.completed is NOT completion evidence.
            self.assertFalse(out["turn_completed"])
            self.assertIsNone(out["state_hint"])

    def test_truncated_line_in_the_middle_is_a_malformed_line(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "events.jsonl"
            path.write_bytes(
                _fixture("exec_truncated_final_line").read_bytes()
                + b'\n{"type":"turn.completed","usage":{"input_tokens":1}}\n'
            )
            out = reducer.reduce_events(path, exit_code=0, exit_code_present=True)
            self.assertFalse(out["truncated_tail"])
            self.assertEqual(out["malformed_lines"], 1)
            self.assertTrue(out["turn_completed"])
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)

    def test_garbage_and_non_object_lines_do_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "events.jsonl"
            path.write_text(
                "not json at all\n"
                "[1,2,3]\n"
                '{"no_type_key": true}\n'
                "\n"
                '{"type":"thread.started","thread_id":"01a00000-0000-7000-8000-000000000001"}\n'
                '{"type":"turn.completed","usage":{}}\n'
            )
            out = reducer.reduce_events(path)
            self.assertEqual(out["malformed_lines"], 3)
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            self.assertEqual(out["thread_id"], "01a00000-0000-7000-8000-000000000001")


class TestOutcomeOrdering(unittest.TestCase):
    """Appended resume segments: the LATEST segment's outcome governs."""

    def _two_segments(self, tmp: Path, first: str, second: str) -> Path:
        path = tmp / "segments.jsonl"
        path.write_bytes(_fixture(first).read_bytes() + _fixture(second).read_bytes())
        return path

    def test_failed_then_resumed_successfully_is_idle(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._two_segments(tmp, "exec_turn_failed", "exec_resume_success")
            out = reducer.reduce_events(path, exit_code=0, exit_code_present=True)
            self.assertTrue(out["turn_failed"])
            self.assertTrue(out["turn_completed"])
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            self.assertIsNone(out["failure"])

    def test_completed_then_failed_resume_is_terminated(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._two_segments(tmp, "exec_success", "exec_turn_failed")
            out = reducer.reduce_events(path, exit_code=1, exit_code_present=True)
            self.assertEqual(out["state_hint"], reducer.STATE_TERMINATED)
            self.assertEqual(out["failure"]["reason"], "turn_failed")


class TestSmallReaders(unittest.TestCase):
    def test_read_exit_code_strips_and_parses(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "e.jsonl"
            path.write_text("")
            Path(f"{path}.rc").write_text("137")  # no trailing newline, as printf writes
            self.assertEqual(reducer.read_exit_code(path), 137)
            Path(f"{path}.rc").write_text("0\n")
            self.assertEqual(reducer.read_exit_code(path), 0)
            Path(f"{path}.rc").write_text("junk")
            self.assertIsNone(reducer.read_exit_code(path))
            Path(f"{path}.rc").unlink()
            self.assertIsNone(reducer.read_exit_code(path))
            self.assertIsNone(reducer.read_exit_code(None))

    def test_read_result_text_is_bounded_and_fail_soft(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "last.md"
            path.write_text("OK")  # written without a trailing newline
            self.assertEqual(reducer.read_result_text(path), "OK")
            path.write_text("x" * 10_000)
            self.assertEqual(len(reducer.read_result_text(path, max_bytes=100)), 100)
            path.write_text("")
            self.assertIsNone(reducer.read_result_text(path))
            self.assertIsNone(reducer.read_result_text(tmp / "missing.md"))
            self.assertIsNone(reducer.read_result_text(None))

    def test_reduce_handle_uses_the_handle_schema(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _stage(tmp, "exec_success", rc=0, result="OK")
            handle = lib.new_handle(
                name="review-123", session_id="01a00000-0000-7000-8000-000000000001",
                run_id="rid", abs_dir=str(tmp), transcript_path="",
                stuck_after_s=600, provider=lib.PROVIDER_CODEX,
                activity_path=str(path), result_path=_result_path(tmp, "exec_success"),
            )
            out = reducer.reduce_handle(handle, read_result=True)
            self.assertEqual(out["state_hint"], reducer.STATE_IDLE)
            self.assertEqual(out["result_text"], "OK")
            self.assertEqual(out["thread_id"], handle["session_id"])
            self.assertEqual(reducer.reduce_handle({})["state_hint"], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
