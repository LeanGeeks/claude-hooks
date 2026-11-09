#!/usr/bin/env python3
"""Tests for keep_awake_hook.py (task 44).

The real ``systemd-inhibit`` is never called: ``CLAUDE_KEEP_AWAKE_INHIBIT``
points at a fake that, like the real one, forks the command and waits for it
(so the watcher's parent is the "holder"). Owners are stand-in processes
passed through ``CLAUDE_KEEP_AWAKE_OWNER_PID``.

Run alone:
    python3 tests/run_all_tests.py --module unit_keep_awake
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / ".claude" / "hooks" / "keep_awake_hook.py"
HOOKS_DIR = str(HOOK.parent)

sys.path.insert(0, str(HOOK.parent))
import keep_awake_hook as ka  # noqa: E402

FAKE_INHIBIT = """#!/usr/bin/env python3
import subprocess, sys
args = [a for a in sys.argv[1:]]
while args and args[0].startswith("--"):
    args.pop(0)
sys.exit(subprocess.call(args))
"""

SID = "11111111-2222-3333-4444-555555555555"


def iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def wait_until(pred, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


class KeepAwakeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="keep-awake-test-"))
        self.state_dir = self.tmp / "state"
        self.log = self.tmp / "keep_awake.log"
        self.transcript = self.tmp / f"{SID}.jsonl"
        self.transcript.write_text("{}\n")
        fake = self.tmp / "fake-systemd-inhibit"
        fake.write_text(FAKE_INHIBIT)
        fake.chmod(0o755)
        self.fake_inhibit = str(fake)
        self.procs = []
        self.env = {
            **os.environ,
            "CLAUDE_KEEP_AWAKE_STATE_DIR": str(self.state_dir),
            "CLAUDE_KEEP_AWAKE_LOG": str(self.log),
            "CLAUDE_KEEP_AWAKE_INHIBIT": self.fake_inhibit,
            "CLAUDE_KEEP_AWAKE_POLL_SECONDS": "0.1",
            "CLAUDE_KEEP_AWAKE_IDLE_MINUTES": "30",
            "CLAUDE_KEEP_AWAKE_MAX_HOURS": "24",
            "CLAUDE_KEEP_AWAKE_BACKGROUND_HOURS": "2",
        }
        self.env.pop("CLAUDE_KEEP_AWAKE", None)
        self.owner = self.start_owner()

    def tearDown(self):
        subprocess.run([sys.executable, str(HOOK), "--release-all"], env=self.env,
                       capture_output=True, timeout=10)
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def start_owner(self, script="sleep 600 & wait"):
        p = subprocess.Popen(["bash", "-c", script], start_new_session=True)
        self.procs.append(p)
        self.env["CLAUDE_KEEP_AWAKE_OWNER_PID"] = str(p.pid)
        return p

    def watcher_pids(self, holder):
        return [c for c in ka.descendants(holder) if "--watch" in ka.proc_cmdline(c)]

    def owner_with_child(self, argv0):
        """Start an owner whose child process has the given argv[0]."""
        # The owner must outlive the child (tests kill the child to end the work).
        script = f'(exec -a "{argv0}" sleep 600) & exec sleep 600'
        self.owner = self.start_owner(script)
        assert wait_until(lambda: any(argv0 in " ".join(ka.proc_cmdline(c))
                                      for c in ka.descendants(self.owner.pid)))
        return self.owner

    def kill_children(self, owner):
        for c in ka.descendants(owner.pid):
            try:
                os.kill(c, signal.SIGKILL)
            except Exception:
                pass

    def append_entry(self, kind):
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"type": kind, "timestamp": iso_now()}) + "\n")

    def payload(self, **extra):
        data = {"session_id": SID, "cwd": str(self.tmp),
                "transcript_path": str(self.transcript)}
        data.update(extra)
        return data

    def fire(self, event, payload=None, env=None, stdin=None):
        run_env = dict(self.env)
        if env:
            run_env.update(env)
        data = stdin if stdin is not None else json.dumps(payload or self.payload())
        return subprocess.run([sys.executable, str(HOOK), "--event", event],
                              input=data, env=run_env, capture_output=True,
                              text=True, timeout=20)

    def state(self):
        try:
            return json.loads((self.state_dir / f"{SID}.json").read_text())
        except Exception:
            return None

    def holder_alive(self):
        st = self.state()
        return bool(st) and ka.is_alive(st.get("holder_pid"), st.get("holder_ticks"))

    def log_text(self):
        try:
            return self.log.read_text()
        except FileNotFoundError:
            return ""

    def assert_released(self, holder_pid, reason=None, timeout=8.0):
        def gone():
            return self.state() is None and (
                not os.path.exists(f"/proc/{holder_pid}") or ka.proc_is_zombie(holder_pid))
        self.assertTrue(wait_until(gone, timeout), f"lock not released\nlog:\n{self.log_text()}")
        if reason:
            self.assertTrue(wait_until(lambda: reason in self.log_text(), 3),
                            f"expected reason {reason!r} in log:\n{self.log_text()}")

    def acquire(self):
        r = self.fire("UserPromptSubmit")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.holder_alive(), f"no holder\nlog:\n{self.log_text()}")
        return self.state()["holder_pid"]


class TestAcquireRelease(KeepAwakeBase):
    def test_user_prompt_submit_acquires_with_watcher_under_holder(self):
        holder = self.acquire()
        st = self.state()
        self.assertEqual(st["owner_pid"], self.owner.pid)
        self.assertTrue(st["turn_open"])
        self.assertTrue(wait_until(lambda: self.watcher_pids(holder)),
                        "watcher must run under the inhibit holder")

    def test_hook_is_silent_and_exits_zero(self):
        r = self.fire("UserPromptSubmit")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self.fire("Stop")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_repeat_prompt_reuses_the_live_holder(self):
        holder = self.acquire()
        self.acquire()
        self.assertEqual(self.state()["holder_pid"], holder)

    def test_stop_without_background_releases_now(self):
        holder = self.acquire()
        self.fire("Stop", self.payload(background_tasks=[]))
        self.assert_released(holder, "turn ended", timeout=2)

    def test_session_end_releases(self):
        holder = self.acquire()
        self.fire("SessionEnd", self.payload(reason="prompt_input_exit"))
        self.assert_released(holder, "session ended", timeout=2)

    def test_unknown_owner_takes_no_lock(self):
        r = self.fire("UserPromptSubmit", env={"CLAUDE_KEEP_AWAKE_OWNER_PID": "not-a-pid"})
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(self.state())

    def test_missing_inhibit_binary_takes_no_lock(self):
        r = self.fire("UserPromptSubmit",
                      env={"CLAUDE_KEEP_AWAKE_INHIBIT": str(self.tmp / "absent")})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        # Popen on a missing binary raises; fail-open leaves no state behind.
        self.assertIsNone(self.state())

    def test_malformed_payload_is_a_noop(self):
        for stdin in ("", "not json", "[1,2]", json.dumps({"session_id": "../etc"})):
            r = self.fire("UserPromptSubmit", stdin=stdin)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(any(self.state_dir.glob("*.json")) if self.state_dir.exists() else False)

    def test_disabled_takes_no_lock_and_drops_an_existing_one(self):
        holder = self.acquire()
        self.fire("UserPromptSubmit", env={"CLAUDE_KEEP_AWAKE": "0"})
        self.assert_released(holder, "disabled", timeout=2)
        self.fire("UserPromptSubmit", env={"CLAUDE_KEEP_AWAKE": "0"})
        self.assertIsNone(self.state())

    def test_release_all(self):
        holder = self.acquire()
        subprocess.run([sys.executable, str(HOOK), "--release-all"], env=self.env, timeout=10)
        self.assert_released(holder, "release-all", timeout=2)

    def test_status_lists_held_session(self):
        self.acquire()
        r = subprocess.run([sys.executable, str(HOOK), "--status"], env=self.env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertIn(SID[:8], r.stdout)
        self.assertIn("recent activity", r.stdout)


class TestCrashSafety(KeepAwakeBase):
    def test_owner_sigkill_releases_without_any_hook(self):
        holder = self.acquire()
        os.killpg(self.owner.pid, signal.SIGKILL)
        self.owner.wait(timeout=5)
        self.assert_released(holder, "session process exited", timeout=3)

    def test_owner_exit_is_detected_before_the_poll_interval(self):
        self.env["CLAUDE_KEEP_AWAKE_POLL_SECONDS"] = "60"
        holder = self.acquire()
        # Let the watcher get past its startup owner check into the wait loop;
        # otherwise the startup check, not the pidfd, sees the dead owner.
        self.assertTrue(wait_until(lambda: self.watcher_pids(holder)))
        time.sleep(1.0)
        os.killpg(self.owner.pid, signal.SIGKILL)
        self.owner.wait(timeout=5)
        # pidfd wakes the watcher immediately; a 60 s poll would time this out.
        self.assert_released(holder, "session process exited", timeout=3)

    def test_killed_holder_is_swept_and_reacquired(self):
        holder = self.acquire()
        os.killpg(holder, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not self.holder_alive(), 3))
        new_holder = self.acquire()
        self.assertNotEqual(new_holder, holder)

    def test_dead_owner_state_is_swept_by_another_sessions_event(self):
        holder = self.acquire()
        # Freeze the watcher so only the sweep can notice the dead owner.
        self.assertTrue(wait_until(lambda: self.watcher_pids(holder)))
        os.kill(self.watcher_pids(holder)[0], signal.SIGSTOP)
        os.killpg(self.owner.pid, signal.SIGKILL)
        self.owner.wait(timeout=5)
        other = self.payload(session_id="99999999-0000-0000-0000-000000000000")
        self.fire("SessionEnd", other)
        self.assert_released(holder, "swept: session process exited", timeout=3)

    def test_holder_exits_when_its_state_file_is_removed(self):
        holder = self.acquire()
        (self.state_dir / f"{SID}.json").unlink()
        self.assertTrue(wait_until(lambda: not os.path.exists(f"/proc/{holder}")
                                   or ka.proc_is_zombie(holder), 3),
                        "an unreferenced holder must exit by itself")

    def test_holder_exits_when_replaced(self):
        holder = self.acquire()
        st = self.state()
        st["holder_pid"] = 1                      # another holder now owns the session
        (self.state_dir / f"{SID}.json").write_text(json.dumps(st))
        self.assertTrue(wait_until(lambda: not os.path.exists(f"/proc/{holder}")
                                   or ka.proc_is_zombie(holder), 3))

    def test_idle_after_interrupt_releases(self):
        """Esc: no Stop fires; nothing runs; the transcript goes quiet."""
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"   # 0.6 s
        holder = self.acquire()
        self.assert_released(holder, "idle for", timeout=5)

    def test_max_hold_backstop(self):
        self.env["CLAUDE_KEEP_AWAKE_MAX_HOURS"] = str(0.5 / 3600)   # 0.5 s
        self.owner = self.owner_with_child("bash /x/.claude/shell-snapshots/snap")
        holder = self.acquire()
        self.assert_released(holder, "no session event for", timeout=5)


class TestHolding(KeepAwakeBase):
    def test_running_tool_holds_through_idle_then_releases(self):
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"
        owner = self.owner_with_child("bash /x/.claude/shell-snapshots/snap")
        holder = self.acquire()
        time.sleep(1.5)
        self.assertTrue(self.holder_alive(), self.log_text())
        self.kill_children(owner)
        self.assert_released(holder, "idle for", timeout=5)

    def test_relay_parked_hook_holds(self):
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"
        owner = self.owner_with_child(f"python3 {HOOKS_DIR}/permission_request_hook.py")
        holder = self.acquire()
        time.sleep(1.5)
        self.assertTrue(self.holder_alive(), self.log_text())
        self.kill_children(owner)
        self.assert_released(holder, "idle for", timeout=5)

    def test_terminal_permission_prompt_holds_until_answered(self):
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"
        self.fire("Notification", self.payload(notification_type="permission_prompt"))
        self.assertTrue(self.holder_alive())
        holder = self.state()["holder_pid"]
        # Claude Code may write the tool_use line after the prompt is up, and
        # subagents write their own files: neither is an answer.
        self.append_entry("assistant")
        sub = self.tmp / SID / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(json.dumps({"type": "user", "timestamp": iso_now()}) + "\n")
        time.sleep(1.5)
        self.assertTrue(self.holder_alive(), self.log_text())
        # The human answers (or presses Esc): a user entry in the main transcript.
        self.append_entry("user")
        self.assert_released(holder, "idle for", timeout=5)

    def test_lingering_blocking_hook_stops_counting_once_answered(self):
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"
        self.owner_with_child(f"python3 {HOOKS_DIR}/permission_request_hook.py")
        holder = self.acquire()
        time.sleep(2.0)                         # past the +1.5 s start slack
        self.assertTrue(self.holder_alive(), self.log_text())
        self.append_entry("user")               # answered in the terminal; hook lingers
        self.assert_released(holder, "idle for", timeout=5)

    def test_blocking_hook_matched_by_name_not_resolved_path(self):
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"
        self.owner_with_child("python3 /home/someone/.claude/hooks/permission_request_hook.py")
        self.acquire()
        time.sleep(1.5)
        self.assertTrue(self.holder_alive(), self.log_text())

    def test_open_turn_tool_shell_capped_after_no_activity(self):
        """Esc with a background dev server left running: background cap, not 24h."""
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.01"
        self.env["CLAUDE_KEEP_AWAKE_BACKGROUND_HOURS"] = str(1.0 / 3600)
        self.owner_with_child("bash /x/.claude/shell-snapshots/snap")
        holder = self.acquire()
        self.assert_released(holder, "tool running past", timeout=6)

    def test_stop_with_background_task_holds_then_releases_when_quiet(self):
        self.env["CLAUDE_KEEP_AWAKE_IDLE_MINUTES"] = "0.02"   # 1.2 s
        holder = self.acquire()
        self.fire("Stop", self.payload(background_tasks=[{"id": "b1"}]))
        self.assertTrue(self.holder_alive(), self.log_text())
        self.assertIn("holding after turn end", self.log_text())
        self.assert_released(holder, "background work done", timeout=6)

    def test_stop_with_background_shell_holds_until_it_exits(self):
        owner = self.owner_with_child("bash /x/.claude/shell-snapshots/snap")
        holder = self.acquire()
        self.fire("Stop", self.payload(background_tasks=[]))
        self.assertTrue(self.holder_alive(), self.log_text())
        self.kill_children(owner)
        self.assert_released(holder, "background work done", timeout=5)

    def test_background_cap(self):
        self.env["CLAUDE_KEEP_AWAKE_BACKGROUND_HOURS"] = str(0.5 / 3600)
        self.owner_with_child("bash /x/.claude/shell-snapshots/snap")
        holder = self.acquire()
        self.fire("Stop", self.payload(background_tasks=[{"id": "devserver"}]))
        self.assert_released(holder, "background work past", timeout=5)

    def test_new_prompt_after_background_stop_reopens_the_turn(self):
        self.acquire()
        self.fire("Stop", self.payload(background_tasks=[{"id": "b1"}]))
        self.acquire()
        self.assertTrue(self.state()["turn_open"])


class TestEvaluate(unittest.TestCase):
    """The busy predicate on hand-built state (owner = this test process)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="keep-awake-eval-"))
        self.transcript = self.tmp / "s.jsonl"
        self.transcript.write_text("{}\n")
        pid = os.getpid()
        self.base = {"owner_pid": pid, "owner_ticks": ka.proc_start_ticks(pid),
                     "transcript_path": str(self.transcript), "turn_open": True,
                     "last_event_at": time.time(), "permission_prompt_at": None}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reused_pid_counts_as_dead(self):
        st = dict(self.base, owner_ticks=self.base["owner_ticks"] + 1)
        self.assertEqual(ka.evaluate(st, time.time()), (False, "session process exited"))

    def test_subagent_transcript_counts_as_activity(self):
        old = time.time() - 7200
        os.utime(self.transcript, (old, old))
        st = dict(self.base, last_event_at=old)
        self.assertFalse(ka.evaluate(st, time.time())[0])
        sub = self.tmp / "s" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text("{}\n")
        self.assertEqual(ka.evaluate(st, time.time()), (True, "recent activity"))

    def test_own_hook_and_watcher_are_not_work(self):
        p = subprocess.Popen(["bash", "-c", f'exec -a "python3 {ka.SELF_PATH} --watch x '
                              f'/shell-snapshots/" sleep 30'])
        try:
            wait_until(lambda: p.pid in ka.descendants(os.getpid()))
            self.assertEqual(ka.running_work(os.getpid()), (False, None))
        finally:
            p.kill()
            p.wait()


class TestOwnerDiscovery(unittest.TestCase):
    def test_walks_up_to_a_process_named_claude(self):
        tmp = Path(tempfile.mkdtemp(prefix="keep-awake-owner-"))
        try:
            probe = tmp / "probe.py"
            probe.write_text(
                "import sys; sys.path.insert(0, %r)\n"
                "import keep_awake_hook as ka, os\n"
                "pid = ka.find_owner_pid()\n"
                "print(pid, ka.proc_cmdline(pid)[0] if pid else None)\n" % HOOKS_DIR)
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_KEEP_AWAKE_OWNER_PID"}
            # claude → sh → python: the hook's parent is a shell, as in Claude Code.
            r = subprocess.run(
                ["bash", "-c", f'exec -a claude bash -c "sh -c \'{sys.executable} {probe}; true\'; true"'],
                capture_output=True, text=True, env=env, timeout=10)
            self.assertTrue(r.stdout.strip().endswith(" claude"), r.stdout + r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_claude_ancestor(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_KEEP_AWAKE_OWNER_PID"}
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import keep_awake_hook as ka; "
             "print(ka.find_owner_pid())" % HOOKS_DIR],
            capture_output=True, text=True, env=env, timeout=10)
        # Under the test runner there may be a real claude ancestor (a Claude
        # Code session running the suite); accept either, but never this process.
        out = r.stdout.strip()
        self.assertTrue(out == "None" or ka._looks_like_claude(int(out)), out)


if __name__ == "__main__":
    unittest.main()
