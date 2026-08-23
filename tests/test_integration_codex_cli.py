#!/usr/bin/env python3
"""End-to-end integration tests for the Codex worker path (task 20-05).

These exercise the REAL ``.claude/bin/amux-spawn`` CLI as a subprocess — no
unit mocks — through PATH-shimmed fakes built by ``fake_amux_env``: a fake
``amux`` implementing the pinned amux contract surface, a fake ``codex`` that
replays the vendored ``tests/fixtures/codex/`` JSONL captures and writes the
artifacts amux's wrapper would, and a state-file fake ``tmux`` (this module is
fully hermetic: no tmux binary, no network, no OpenAI credentials, and no real
codex invocation anywhere).

Scenario coverage (task work item 3): launch, JSONL event flow, result
capture, failure, timeout, resume, and cleanup — plus the argv-boundary and
--yolo-translation guarantees the BRD pins. The real-tmux pane path is proven
separately in ``test_integration_codex_tmux``.
"""

import json
import os
import stat
import time
import unittest
from pathlib import Path

from fake_amux_env import (
    FIXTURES,
    CLAUDE_YOLO_EXPANSION,
    CODEX_YOLO_EXPANSION,
    new_env,
)

FIXTURE_THREAD_ID = "01a00000-0000-7000-8000-000000000001"
SUCCESS_FIXTURE = (FIXTURES / "exec_success.jsonl").read_bytes()


class CodexCliIntegrationBase(unittest.TestCase):
    """One isolated FakeAmuxEnv per test, torn down to zero leftovers."""

    def setUp(self):
        self.env, self.tmp = new_env(tmux_mode="state")
        self.addCleanup(self._tear_down)

    def _tear_down(self):
        self.env.teardown()
        # Hygiene gates: no live fake-tmux sessions and no stray pane pids may
        # survive a test (standing rule: zero live tmux sessions, zero stray
        # artifacts outside the temp dir, which self-cleans via addCleanup).
        self.assertEqual(self.env.live_sessions(), [])
        for pid in self.env.env_pids():
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            self.fail(f"pane pid {pid} still alive after teardown")

    def spawn_worker(self, suffix="w1", prompt="reply with the single word OK",
                     codex_mode="success", *extra):
        return self.env.spawn(suffix, prompt, *extra, codex_mode=codex_mode)

    def await_idle(self, name, timeout=20.0):
        result = self.env.await_status(name, ("idle",), timeout=timeout)
        self.assertEqual(result.get("state"), "idle",
                         f"worker never reached idle: {result}")
        return result

    def await_status_where(self, name, predicate, timeout=20.0):
        """Poll `status` until *predicate* holds (stable-terminal conditions
        only — e.g. a failure reason that appears once .rc lands)."""
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            last = self.env.status(name)
            try:
                if predicate(last):
                    return last
            except Exception:
                pass
            time.sleep(0.15)
        return last


# ── launch ────────────────────────────────────────────────────────────────────

class TestLaunch(CodexCliIntegrationBase):

    def test_detached_spawn_creates_tracked_codex_worker(self):
        """Launch: spawn returns 0 and the whole tracked-worker state exists —
        handle, 0600 artifacts, amux registration, captured thread id — and
        the worker settles into idle on real completion evidence."""
        proc = self.spawn_worker()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("codex", proc.stdout)
        self.assertIn("tracked", proc.stdout)

        name = f"{self.env.tmp.name}-w1"
        handle = self.env.handle(name)
        self.assertIsNotNone(handle)
        self.assertEqual(handle["provider"], "codex")
        self.assertEqual(handle["attempt"], 1)
        self.assertEqual(handle["state"], "spawning")
        self.assertTrue(handle["activity_path"].endswith(".events.jsonl"))
        self.assertTrue(handle["result_path"].endswith(".result.md"))

        # The artifacts this repository allocates are created 0600 before the
        # pane exists (architecture s3).
        for key in ("activity_path", "result_path"):
            mode = stat.S_IMODE(os.stat(handle[key]).st_mode)
            self.assertEqual(mode, 0o600, key)

        # amux-side registration: <name>.env carries the persisted provider
        # keys (synchronous, written by fake amux at launch).
        env_file = self.env.cc_home / "sessions" / f"{name}.env"
        self.assertTrue(env_file.is_file())
        text = env_file.read_text()
        self.assertIn('CC_PROVIDER="codex"', text)
        self.assertIn('CC_AGENT_MODE="exec"', text)

        self.await_idle(name)

        # The wrapper captured the fixture thread id into meta.json as the
        # first thread.started line arrived (authoritative identity source).
        self.assertEqual(self.env.meta(name).get("codex_session_id"),
                         FIXTURE_THREAD_ID)

    def test_multiline_prompt_keeps_argv_boundaries(self):
        """BRD s4.1: a prompt with newlines, quotes and shell metacharacters
        arrives at the provider as exactly one unexpanded argument."""
        prompt = "line one\nline 'two' with \"quotes\" and $HOME `id` ; rm -rf /"
        proc = self.spawn_worker(prompt=prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.await_idle(f"{self.env.tmp.name}-w1")
        calls = self.env.codex_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"][-1], prompt)

    def test_yolo_is_translated_to_the_codex_flag_not_claudes(self):
        """architecture s8: amux-spawn forwards --yolo verbatim; amux expands
        it per provider. The recorded provider argv must carry Codex's bypass
        flag and never Claude's --dangerously-skip-permissions."""
        proc = self.env.spawn("w1", "reply with the single word OK", "--yolo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('CC_YOLO="1"',
                      (self.env.cc_home / "sessions" /
                       f"{self.env.tmp.name}-w1.env").read_text())
        self.await_idle(f"{self.env.tmp.name}-w1")
        argv = self.env.codex_calls()[0]["argv"]
        self.assertIn(CODEX_YOLO_EXPANSION, argv)
        self.assertNotIn(CLAUDE_YOLO_EXPANSION, argv)

    def test_spawn_without_prompt_is_refused_before_anything_is_created(self):
        proc = self.env.run_cli("spawn", "w-noprompt", "--provider", "codex",
                                "--dir", str(self.env.tmp))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("needs a prompt", proc.stderr)
        # Nothing was created anywhere: no handle, no artifacts, no .env. The
        # refusal happens before the spawn lock, so even the registry dir is
        # absent (or empty).
        self.assertIsNone(self.env.handle(f"{self.env.tmp.name}-w-noprompt"))
        self.assertEqual(list((self.env.cc_home / "sessions").glob("*.env")), [])
        spawn_dir = self.env.cc_home / "spawn"
        leftover = [p.name for p in spawn_dir.glob("*")] if spawn_dir.is_dir() else []
        self.assertEqual(leftover, [])


# ── JSONL event flow + result capture ────────────────────────────────────────

class TestEventFlowAndResult(CodexCliIntegrationBase):

    def test_event_artifact_is_fixture_shaped_jsonl(self):
        """The pane's stdout reaches the event artifact byte-for-byte; the
        CLI's derived signals come from the reducer over that artifact."""
        proc = self.spawn_worker()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        name = f"{self.env.tmp.name}-w1"
        self.await_idle(name)
        handle = self.env.handle(name)
        self.assertEqual(Path(handle["activity_path"]).read_bytes(),
                         SUCCESS_FIXTURE)
        # amux's wrapper wrote the exit-status sibling (no trailing newline).
        self.assertEqual(Path(handle["activity_path"] + ".rc").read_text(), "0")

        status = self.env.status(name)
        self.assertEqual(status["signals"]["thread_started"], True)
        self.assertEqual(status["signals"]["turn_started"], True)
        self.assertEqual(status["signals"]["turn_completed"], True)
        self.assertEqual(status["thread_id"], FIXTURE_THREAD_ID)
        self.assertEqual(status["exit_code"], 0)
        self.assertIsNone(status["failure"])

    def test_last_returns_final_message_artifact(self):
        self.spawn_worker()
        name = f"{self.env.tmp.name}-w1"
        self.await_idle(name)

        proc = self.env.run_cli("last", name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.rstrip("\n"), "OK")

        jproc = self.env.run_cli("last", name, "--json")
        payload = json.loads(jproc.stdout)
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["last_message"], "OK")
        self.assertTrue(payload["result_path"].endswith(".result.md"))

    def test_wait_prints_result_and_exits_zero(self):
        """--wait: stdout IS the result payload; exit 0 on true idle."""
        proc = self.env.spawn("ww", "do the bounded thing", "--wait",
                              codex_mode="success")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.rstrip("\n"), "OK")
        self.assertIn("waiting for session", proc.stderr)


# ── failure ──────────────────────────────────────────────────────────────────

class TestFailure(CodexCliIntegrationBase):

    def test_turn_failed_is_terminated_with_normalized_reason(self):
        """A turn.failed fixture run maps to terminated regardless of exit
        code; the failure carries the normalized reason and the exit code as
        context, and no result artifact is claimed."""
        proc = self.spawn_worker(codex_mode="fail")
        self.assertEqual(proc.returncode, 0, proc.stderr)  # launch succeeded
        name = f"{self.env.tmp.name}-w1"
        result = self.env.await_status(name, ("terminated",))
        self.assertEqual(result["state"], "terminated")
        self.assertEqual(result["failure"]["reason"], "turn_failed")
        self.assertEqual(result["failure"]["exit_code"], 1)
        self.assertTrue(result["signals"]["turn_failed"])

        # No final message was produced: `last` reports that, exit 1.
        lproc = self.env.run_cli("last", name)
        self.assertEqual(lproc.returncode, 1)
        self.assertIn("no final-message artifact", lproc.stderr)

        # The spawn lock is the only registry leftover; cleanup via rm.
        rproc = self.env.run_cli("rm", name)
        self.assertEqual(rproc.returncode, 0, rproc.stderr)


# ── timeout ──────────────────────────────────────────────────────────────────

class TestTimeout(CodexCliIntegrationBase):

    def test_wait_timeout_marks_and_exits_3_then_force_reap(self):
        """An in-flight run (thread/turn started, no verdict) reads running;
        --wait --timeout expires with the AMUX_WAIT_TIMEOUT marker and exit 3
        (NOT an error). rm --force then reaps the live run completely."""
        proc = self.env.spawn("wt", "hang around", "--wait", "--timeout", "1s",
                              codex_mode="hang")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "AMUX_WAIT_TIMEOUT")

        name = f"{self.env.tmp.name}-wt"
        status = self.env.status(name)
        self.assertEqual(status["state"], "running")
        self.assertTrue(status["signals"]["thread_started"])
        self.assertTrue(status["signals"]["turn_started"])
        self.assertFalse(status["signals"]["turn_completed"])
        self.assertIn(f"amux-{name}", self.env.live_sessions())

        # rm refuses a live run without --force (kill safety).
        refused = self.env.run_cli("rm", name)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--force", refused.stderr)

        forced = self.env.run_cli("rm", name, "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIsNone(self.env.handle(name))
        handle_artifacts = list((self.env.cc_home / "spawn").glob(f"{name}*"))
        self.assertEqual(handle_artifacts, [])
        self.assertFalse((self.env.cc_home / "sessions" / f"{name}.env").exists())
        self.assertNotIn(f"amux-{name}", self.env.live_sessions())


# ── resume ───────────────────────────────────────────────────────────────────

class TestResume(CodexCliIntegrationBase):

    def _spawn_and_complete(self, suffix="r1"):
        proc = self.spawn_worker(suffix=suffix)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        name = f"{self.env.tmp.name}-{suffix}"
        self.await_idle(name)
        return name

    def test_resume_appends_segment_bumps_attempt_keeps_thread(self):
        name = self._spawn_and_complete()
        first_segment = Path(self.env.handle(name)["activity_path"]).read_bytes()

        rproc = self.env.run_cli(
            "resume", name, "--json", "--",
            "what single word did you just reply?",
            codex_mode="resume_success")
        self.assertEqual(rproc.returncode, 0, rproc.stderr)
        payload = json.loads(rproc.stdout)
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["thread_id"], FIXTURE_THREAD_ID)
        self.assertEqual(payload["state"], "spawning")

        self.await_idle(name)
        status = self.env.status(name)
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["attempt"], 2)
        self.assertEqual(status["thread_id"], FIXTURE_THREAD_ID)

        # Evidence is append-only: segment 1 still prefixes the artifact and
        # segment 2 was appended after it (never truncated).
        events = Path(self.env.handle(name)["activity_path"]).read_bytes()
        self.assertTrue(events.startswith(first_segment))
        self.assertEqual(events[len(first_segment):],
                         (FIXTURES / "exec_resume_success.jsonl").read_bytes())

        # The resumed provider argv: exec resume, NO -C, thread id then prompt.
        # (The recorded argv[0] "codex" is consumed as the executable name by
        # the shebang dispatch, so the log starts at the subcommand.)
        argv = self.env.codex_calls()[-1]["argv"]
        self.assertEqual(argv[:2], ["exec", "resume"])
        self.assertNotIn("-C", argv)
        self.assertIn(FIXTURE_THREAD_ID, argv)
        self.assertEqual(argv[-1], "what single word did you just reply?")

        # last returns the NEWEST successful turn's final message.
        lproc = self.env.run_cli("last", name)
        self.assertEqual(lproc.stdout.rstrip("\n"), "OK, second turn")

    def test_resume_refused_while_attempt_in_flight(self):
        name = self._spawn_and_complete()
        hproc = self.env.spawn("rh", "hang around", codex_mode="hang")
        self.assertEqual(hproc.returncode, 0, hproc.stderr)
        live = f"{self.env.tmp.name}-rh"
        rproc = self.env.run_cli("resume", live, "--", "second turn",
                                 codex_mode="resume_success")
        self.assertEqual(rproc.returncode, 1)
        self.assertIn("refusing", rproc.stderr)
        # The refused resume never launched a provider process.
        modes = [c["mode"] for c in self.env.codex_calls()]
        self.assertNotIn("resume_success", modes)
        self.env.run_cli("rm", live, "--force")

    def test_resume_thread_mismatch_is_quarantined_not_completed(self):
        """amux's rc-66 fail-closed path end to end: a foreign thread id in
        the new segment never reaches the event artifact; the run reports
        thread_mismatch and the FIRST turn's result survives untouched."""
        name = self._spawn_and_complete()
        handle = self.env.handle(name)
        events_before = Path(handle["activity_path"]).read_bytes()
        result_before = Path(handle["result_path"]).read_bytes()

        rproc = self.env.run_cli("resume", name, "--", "drift away",
                                 codex_mode="newthread")
        self.assertEqual(rproc.returncode, 0, rproc.stderr)  # launch was fine

        # The terminal condition is the rc-66 landing in .rc; poll for the
        # stable reason rather than the first "terminated" observation.
        status = self.await_status_where(
            name,
            lambda s: s.get("state") == "terminated"
            and (s.get("failure") or {}).get("reason") == "thread_mismatch")
        self.assertEqual(status["state"], "terminated")
        self.assertEqual(status["failure"]["reason"], "thread_mismatch")
        self.assertEqual(status["failure"]["exit_code"], 66)

        # Prior evidence intact: events unchanged, result still segment 1's.
        self.assertEqual(Path(handle["activity_path"]).read_bytes(),
                         events_before)
        self.assertEqual(Path(handle["result_path"]).read_bytes(),
                         result_before)
        # The foreign output lives in the .err quarantine, prefixed.
        err = Path(handle["activity_path"] + ".err").read_text()
        self.assertIn("amux-rejected|", err)

        self.env.run_cli("rm", name)


# ── cleanup ──────────────────────────────────────────────────────────────────

class TestCleanup(CodexCliIntegrationBase):

    def test_rm_reaps_everything_for_a_settled_worker(self):
        """rm on an idle worker: handle, all four bounded artifacts, and the
        amux registration (env + meta) are gone; nothing else is touched."""
        other = self.spawn_worker(suffix="keep")  # registry neighbour
        self.assertEqual(other.returncode, 0, other.stderr)
        keep_name = f"{self.env.tmp.name}-keep"

        name = self._spawn_ok()
        self.await_idle(name)

        rproc = self.env.run_cli("rm", name, "--json")
        self.assertEqual(rproc.returncode, 0, rproc.stderr)
        self.assertEqual(json.loads(rproc.stdout),
                         {"name": name, "state_at_rm": "idle", "killed": False})

        self.assertIsNone(self.env.handle(name))
        self.assertEqual(
            list((self.env.cc_home / "spawn").glob(f"{name}*")), [])
        self.assertFalse((self.env.cc_home / "sessions" / f"{name}.env").exists())
        self.assertFalse(
            (self.env.cc_home / "sessions" / f"{name}.meta.json").exists())
        self.assertNotIn(f"amux-{name}", self.env.live_sessions())

        # The neighbour is untouched — rm never broad-globs (architecture s9).
        self.assertIsNotNone(self.env.handle(keep_name))
        self.assertTrue(self.env.handle(keep_name)["activity_path"])
        self.env.run_cli("rm", keep_name)

    def _spawn_ok(self, suffix="gone"):
        proc = self.spawn_worker(suffix=suffix)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return f"{self.env.tmp.name}-{suffix}"


if __name__ == "__main__":
    unittest.main()
