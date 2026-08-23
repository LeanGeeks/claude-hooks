#!/usr/bin/env python3
"""Private-tmux integration test for the Codex worker path (task 20-05).

Real tmux, private socket: a ``tmux`` shim on the fake bin PATH prefixes every
invocation with ``-L <per-test socket>``, so the launcher's liveness probes,
the fake amux's pane creation, and the test all talk to one private tmux
server. The provider executable is stubbed (``fake_amux_env``'s fake
``codex`` replaying the vendored fixtures), so the REAL pane/argv path runs
end to end — a genuine tmux pane executes the launch wrapper, streams the
JSONL, writes the exit-status sibling, and dies on its own when the bounded
turn finishes — with no OpenAI credentials and nothing touching the
developer's real tmux server or ``~/.amux``.

Skips cleanly when no real tmux binary exists.
"""

import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from fake_amux_env import FIXTURES, new_env

FIXTURE_THREAD_ID = "01a00000-0000-7000-8000-000000000001"
REAL_TMUX = shutil.which("tmux")


def _private_tmux(env, *args, timeout=15.0):
    """Run a command against the test's private tmux server."""
    return subprocess.run(
        [env.real_tmux, "-L", env.socket, *args],
        capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(REAL_TMUX, "real tmux binary not available")
class TestRealTmuxPanePath(unittest.TestCase):
    """The real pane/argv path: spawn -> live pane -> bounded exit -> idle."""

    def setUp(self):
        self.env, self.tmp = new_env(tmux_mode="real", real_tmux=REAL_TMUX)
        self.addCleanup(self._tear_down)

    def _tear_down(self):
        self.env.teardown()  # kill-server on the private socket + pid sweep
        after = _private_tmux(self.env, "list-sessions")
        self.assertNotEqual(after.returncode, 0,
                            "private tmux server still alive after teardown")

    def test_bounded_worker_runs_in_a_real_pane_and_settles_idle(self):
        # A short provider delay keeps the pane observably alive mid-turn.
        proc = self.env.spawn(
            "rt", "verify the pane argv end to end",
            codex_mode="success", FAKE_CODEX_DELAY="2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        name = f"{self.env.tmp.name}-rt"

        # While the turn is in flight the tmux session REALLY exists, the
        # pane's start command is the launch wrapper around the provider
        # argv, and the CLI derives running off that live pane.
        deadline = time.monotonic() + 10
        pane_cmd = ""
        while time.monotonic() < deadline:
            panes = _private_tmux(
                self.env, "list-panes", "-a", "-F",
                "#{session_name} #{pane_start_command}")
            for line in panes.stdout.splitlines():
                if line.startswith(f"amux-{name} "):
                    pane_cmd = line[len(f"amux-{name} "):]
                    break
            if pane_cmd:
                break
            time.sleep(0.1)
        self.assertTrue(pane_cmd, "no live pane found for the worker")
        self.assertIn("pane_wrapper.py", pane_cmd)      # amux's wrapper
        self.assertIn("verify the pane argv end to end", pane_cmd)  # provider argv
        self.assertIn(".events.jsonl", pane_cmd)        # artifact redirection

        status = self.env.status(name)
        self.assertIn(status["state"], ("spawning", "running"))

        # The bounded turn finishes; the pane exits BY ITSELF, the real tmux
        # session disappears, and completion evidence — not liveness — maps
        # the dead session to idle (architecture s5).
        result = self.env.await_status(name, ("idle",), timeout=20)
        self.assertEqual(result["state"], "idle", result)
        gone = _private_tmux(self.env, "has-session", "-t", f"=amux-{name}")
        self.assertNotEqual(gone.returncode, 0,
                            "tmux session outlived the bounded turn")
        self.assertEqual(result["thread_id"], FIXTURE_THREAD_ID)

        # The pane's artifacts are real files with the fixture-shaped stream
        # and the wrapper's exit-status sibling.
        handle = self.env.handle(name)
        events = Path(handle["activity_path"]).read_bytes()
        self.assertEqual(events,
                         (FIXTURES / "exec_success.jsonl").read_bytes())
        self.assertEqual(Path(handle["activity_path"] + ".rc").read_text(),
                         "0")
        self.assertEqual(Path(handle["result_path"]).read_text(), "OK")

        # And the reap: rm removes the handle + artifacts + registration.
        rproc = self.env.run_cli("rm", name)
        self.assertEqual(rproc.returncode, 0, rproc.stderr)
        self.assertIsNone(self.env.handle(name))
        self.assertEqual(
            list((self.env.cc_home / "spawn").glob(f"{name}*")), [])

        # The provider really ran inside the pane with the documented argv.
        calls = self.env.codex_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertIn("--json", argv)
        self.assertIn("-C", argv)
        self.assertEqual(argv[-1], "verify the pane argv end to end")


if __name__ == "__main__":
    unittest.main()
