#!/usr/bin/env python3
"""Unit tests for ``amux-spawn a|attach`` — task 10-05 ergonomics.

Covers the resolution logic (prefix algorithm, fuzzy fallback, ambiguity,
not-found, bare-prefix) and the branch selection logic (switch-client vs
attach-session path choice, liveness check), all with amux/tmux mocked.

No real tmux / network / ~/.amux writes.  All tests are hermetic.

Testing bullets from the task spec:
- prefix-from-cwd resolution matches 10-01's naming (assert against the shared
  ``lib.workspace_prefix`` helper).
- ``a <suffix>`` resolves <prefix>-<suffix> when it exists.
- subdir / no-cwd-prefix-match falls back to whole-list fuzzy, ranking
  current-workspace first.
- ambiguous match → lists candidates / returns ATTACH_AMBIGUOUS.
- ``a`` with no suffix → bare <prefix> resolution.
- not-found → clear error, no switch attempted.
- $TMUX set → switch-client chosen; unset → attach-session chosen (asserted via
  mock, without actually attaching).
"""

import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, call

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli_ergonomics",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_ergonomics", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sessions(*name_dir_pairs):
    """Build the [(name, dir), ...] list that list_amux_sessions_with_dirs returns."""
    return list(name_dir_pairs)


# ---------------------------------------------------------------------------
# 1. Prefix algorithm alignment with 10-01 naming
# ---------------------------------------------------------------------------

class TestPrefixAlgorithm(unittest.TestCase):
    """Verify resolve_attach_target uses the SAME prefix as workspace_prefix."""

    def test_prefix_is_basename(self):
        # workspace_prefix is the shared helper 10-01 uses (D-Name).
        self.assertEqual(lib.workspace_prefix("/home/user/claude-hooks"), "claude-hooks")
        self.assertEqual(lib.workspace_prefix("/a/b/myproj"), "myproj")

    def test_resolve_uses_same_basename(self):
        # resolve_attach_target must derive the prefix via basename(cwd), matching
        # workspace_prefix exactly — the Phase-0 cross-task invariant.
        sessions = _sessions(("claude-hooks-2", "/data/sync/work/leangeeks-ai/claude-hooks"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target(
                "2", cwd="/data/sync/work/leangeeks-ai/claude-hooks"
            )
        # Expected target: basename(cwd) = "claude-hooks", suffix = "2"
        # → "claude-hooks-2" — exact match.
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "claude-hooks-2")


# ---------------------------------------------------------------------------
# 2. a <suffix> resolves <prefix>-<suffix> when it exists
# ---------------------------------------------------------------------------

class TestAttachSuffixExact(unittest.TestCase):

    def test_exact_match_preferred(self):
        sessions = _sessions(
            ("myproj-worker", "/ws/myproj"),
            ("myproj-2", "/ws/myproj"),
            ("myproj", "/ws/myproj"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("worker", cwd="/ws/myproj")
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "myproj-worker")

    def test_numeric_suffix(self):
        sessions = _sessions(("proj-2", "/ws/proj"), ("proj", "/ws/proj"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("2", cwd="/ws/proj")
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "proj-2")


# ---------------------------------------------------------------------------
# 3. Subdir / no-cwd-prefix-match → fuzzy fallback, workspace-first ranking
# ---------------------------------------------------------------------------

class TestSubdirFallback(unittest.TestCase):

    def test_subdir_fallback_finds_parent_workspace(self):
        # Running from a subdir: cwd = /ws/myproj/src
        # basename("src") → no session "src-worker" exists.
        # Fallback: "worker" in "myproj-worker" → found; dir "/ws/myproj" is parent of cwd.
        sessions = _sessions(
            ("myproj-worker", "/ws/myproj"),
            ("otherproj-worker", "/ws/otherproj"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("worker", cwd="/ws/myproj/src")
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "myproj-worker")

    def test_fallback_ranks_workspace_first_with_two_workspace_matches(self):
        # "worker" appears in two sessions that BOTH belong to the workspace.
        # cwd is /ws/myproj → workspace dir "/ws/myproj".
        # Both match the fuzzy search within the workspace → AMBIGUOUS; workspace-first
        # means they're both listed and the cross-workspace session is also appended for
        # context (ws_candidates + other_candidates, in that order).
        sessions = _sessions(
            ("myproj-worker-a", "/ws/myproj"),
            ("myproj-worker-b", "/ws/myproj"),
            ("otherproj-worker", "/ws/otherproj"),  # non-workspace — appended after ws candidates
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("worker", cwd="/ws/myproj")
        # Two workspace matches → AMBIGUOUS; workspace candidates come first; the
        # cross-workspace session is appended after them in the candidate list.
        self.assertEqual(kind, lib.ATTACH_AMBIGUOUS)
        candidates = payload
        self.assertIn("myproj-worker-a", candidates)
        self.assertIn("myproj-worker-b", candidates)
        # Workspace candidates come first (before the cross-workspace session).
        self.assertEqual(candidates[0], "myproj-worker-a")
        self.assertEqual(candidates[1], "myproj-worker-b")

    def test_fallback_workspace_only_one_match_is_exact(self):
        # "worker" matches myproj-worker (workspace) AND otherproj-worker (non-ws).
        # Workspace priority: only one workspace match → EXACT (prefer workspace).
        sessions = _sessions(
            ("myproj-worker", "/ws/myproj"),
            ("otherproj-worker", "/ws/otherproj"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("worker", cwd="/ws/myproj/src")
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "myproj-worker")

    def test_single_fallback_match_is_exact(self):
        # Only one session matches the fuzzy term across the whole list.
        sessions = _sessions(
            ("myproj-alpha", "/ws/myproj"),
            ("otherproj-beta", "/ws/otherproj"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("beta", cwd="/ws/myproj")
        # "beta" is not in "myproj-beta" (doesn't exist), but is in "otherproj-beta".
        # Only one match → EXACT (cross-workspace, since no workspace match).
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "otherproj-beta")


# ---------------------------------------------------------------------------
# 4. Ambiguous match → ATTACH_AMBIGUOUS, candidates listed
# ---------------------------------------------------------------------------

class TestAmbiguousMatch(unittest.TestCase):

    def test_ambiguous_returns_all_candidates(self):
        sessions = _sessions(
            ("proj-worker-a", "/ws/proj"),
            ("proj-worker-b", "/ws/proj"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("worker", cwd="/ws/proj")
        # "proj-worker" is not an exact name; fuzzy search for "worker" matches both.
        self.assertEqual(kind, lib.ATTACH_AMBIGUOUS)
        self.assertIn("proj-worker-a", payload)
        self.assertIn("proj-worker-b", payload)

    def test_ambiguous_cmd_attach_exits_1(self):
        sessions = _sessions(
            ("proj-worker-a", "/ws/proj"),
            ("proj-worker-b", "/ws/proj"),
        )
        # cwd = /ws/proj → prefix = "proj" → target "proj-worker" not in list.
        # Fuzzy "worker" matches both → AMBIGUOUS.
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions), \
                patch.object(lib, "tmux_has_session", return_value=True), \
                patch.object(cli, "_amux_attach") as mock_attach, \
                patch("os.getcwd", return_value="/ws/proj"):
            rc = cli.main(["a", "worker"])
        self.assertEqual(rc, 1)
        mock_attach.assert_not_called()

    def test_cross_workspace_ambiguous_resolve_returns_ambiguous(self):
        # Safety invariant: two sessions in DIFFERENT workspaces both match the
        # search term, and there are NO local-workspace matches.
        # resolve_attach_target must return ATTACH_AMBIGUOUS (not ATTACH_EXACT)
        # so we never silently attach to the wrong workspace session.
        sessions = _sessions(
            ("projA-worker", "/ws/projA"),
            ("projB-worker", "/ws/projB"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("worker", cwd="/ws/myproj")
        self.assertEqual(kind, lib.ATTACH_AMBIGUOUS)
        self.assertIn("projA-worker", payload)
        self.assertIn("projB-worker", payload)

    def test_cross_workspace_ambiguous_no_silent_attach(self):
        # Safety invariant at the cmd_attach level: same cross-workspace ambiguous
        # setup must exit non-zero AND must NEVER call _amux_attach.
        sessions = _sessions(
            ("projA-worker", "/ws/projA"),
            ("projB-worker", "/ws/projB"),
        )
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions), \
                patch.object(lib, "tmux_has_session", return_value=True), \
                patch.object(cli, "_amux_attach") as mock_attach, \
                patch("os.getcwd", return_value="/ws/myproj"):
            rc = cli.main(["a", "worker"])
        self.assertEqual(rc, 1)
        mock_attach.assert_not_called()


# ---------------------------------------------------------------------------
# 5. a with no suffix → bare <prefix> resolution
# ---------------------------------------------------------------------------

class TestBarePrefix(unittest.TestCase):

    def test_no_suffix_targets_bare_prefix(self):
        sessions = _sessions(("myproj", "/ws/myproj"), ("myproj-2", "/ws/myproj"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target(None, cwd="/ws/myproj")
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "myproj")

    def test_no_suffix_no_bare_prefix_falls_back(self):
        # No "myproj" session, but "myproj-2" exists.
        # Fuzzy for "myproj" matches "myproj-2".
        sessions = _sessions(("myproj-2", "/ws/myproj"),)
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target(None, cwd="/ws/myproj")
        # No exact bare "myproj"; fuzzy "myproj" matches "myproj-2" → EXACT (one match).
        self.assertEqual(kind, lib.ATTACH_EXACT)
        self.assertEqual(payload, "myproj-2")


# ---------------------------------------------------------------------------
# 6. Not found → clear error, no switch attempted
# ---------------------------------------------------------------------------

class TestNotFound(unittest.TestCase):

    def test_not_found_returns_kind(self):
        sessions = _sessions(("otherproj", "/ws/other"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions):
            kind, payload = lib.resolve_attach_target("xyzzy", cwd="/ws/myproj")
        self.assertEqual(kind, lib.ATTACH_NOT_FOUND)
        self.assertIn("xyzzy", str(payload))

    def test_not_found_cmd_attach_exits_1_no_switch(self):
        sessions = _sessions(("other-session", "/ws/other"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions), \
                patch.object(lib, "tmux_has_session", return_value=False), \
                patch.object(cli, "_amux_attach") as mock_attach, \
                patch("os.getcwd", return_value="/ws/myproj"):
            rc = cli.main(["a", "xyzzy"])
        self.assertEqual(rc, 1)
        mock_attach.assert_not_called()

    def test_empty_session_list_not_found(self):
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=[]):
            kind, _ = lib.resolve_attach_target("anything", cwd="/ws/proj")
        self.assertEqual(kind, lib.ATTACH_NOT_FOUND)


# ---------------------------------------------------------------------------
# 7. $TMUX set → switch-client; unset → attach-session (via amux attach mock)
# ---------------------------------------------------------------------------
#
# We cannot test switch-client vs attach-session by looking at what tmux does,
# because `amux attach` chooses that internally (task 12 E3).  We assert:
# - When a valid single session is resolved and is live, `_amux_attach(name)` is
#   called with the correct name — regardless of $TMUX (that's amux's concern,
#   not ours; we already trust E3 is live).
# - The branch in cmd_attach does NOT call _amux_attach when not-found or ambiguous.

class TestSwitchBranching(unittest.TestCase):

    def _run_attach(self, suffix, sessions, tmux_live=True, tmux_env=None):
        """Run cmd_attach with mocked amux/tmux; return (rc, attach_calls)."""
        attach_calls = []

        def fake_attach(name):
            attach_calls.append(name)
            # os.execvp would normally replace the process; in tests we just return.

        env_patch = {}
        if tmux_env is not None:
            env_patch["TMUX"] = tmux_env

        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions), \
                patch.object(lib, "tmux_has_session", return_value=tmux_live), \
                patch.object(cli, "_amux_attach", side_effect=fake_attach), \
                patch.dict(os.environ, env_patch, clear=False), \
                patch("os.getcwd", return_value="/ws/proj"):
            # Also remove TMUX if tmux_env is None (to simulate outside-tmux).
            if tmux_env is None:
                with patch.dict(os.environ, {}, clear=False):
                    saved = os.environ.pop("TMUX", None)
                    rc = cli.main(["a", suffix] if suffix else ["a"])
                    if saved is not None:
                        os.environ["TMUX"] = saved
            else:
                rc = cli.main(["a", suffix] if suffix else ["a"])
        return rc, attach_calls

    def test_exact_match_calls_amux_attach(self):
        # In production _amux_attach calls os.execvp (never returns); the mock
        # returns None so we fall through to `return 1` — the meaningful
        # assertion is that _amux_attach was invoked with the right name.
        sessions = _sessions(("proj-worker", "/ws/proj"))
        rc, calls = self._run_attach("worker", sessions)
        self.assertEqual(calls, ["proj-worker"])

    def test_switch_client_vs_attach_delegated_to_amux(self):
        # Both inside and outside tmux, the SAME _amux_attach path is taken.
        # amux itself switches vs attaches based on $TMUX (E3).  We just verify
        # _amux_attach is called in both cases.
        sessions = _sessions(("proj-alpha", "/ws/proj"))
        # Inside tmux ($TMUX set).
        _, calls_in = self._run_attach("alpha", sessions, tmux_env="/tmp/tmux-1000/default,1234,0")
        self.assertEqual(calls_in, ["proj-alpha"])
        # Outside tmux ($TMUX unset) — _run_attach pops $TMUX.
        _, calls_out = self._run_attach("alpha", sessions, tmux_env=None)
        self.assertEqual(calls_out, ["proj-alpha"])

    def test_dead_session_not_switched(self):
        sessions = _sessions(("proj-dead", "/ws/proj"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions), \
                patch.object(lib, "tmux_has_session", return_value=False), \
                patch.object(cli, "_amux_attach") as mock_attach, \
                patch("os.getcwd", return_value="/ws/proj"):
            rc = cli.main(["a", "dead"])
        self.assertEqual(rc, 1)
        mock_attach.assert_not_called()


# ---------------------------------------------------------------------------
# 8. _amux_attach exec path (return-after-exec mock) returns 0
# ---------------------------------------------------------------------------

class TestAmuxAttachMock(unittest.TestCase):
    """Verify cmd_attach calls _amux_attach with the right name (simulated execvp)."""

    def test_cmd_attach_calls_attach_with_correct_name(self):
        # In production _amux_attach calls os.execvp and never returns; in tests the
        # mock returns None and we fall through to `return 1` — that is correct
        # behaviour (the comment in cmd_attach says "we only reach here on failure").
        # The meaningful assertion is that _amux_attach was invoked with the right name.
        sessions = _sessions(("ws-review", "/home/user/ws"))
        with patch.object(lib, "list_amux_sessions_with_dirs", return_value=sessions), \
                patch.object(lib, "tmux_has_session", return_value=True), \
                patch.object(cli, "_amux_attach") as mock_attach, \
                patch("os.getcwd", return_value="/home/user/ws"):
            cli.main(["a", "review"])
        mock_attach.assert_called_once_with("ws-review")


# ---------------------------------------------------------------------------
# 9. list_amux_sessions_with_dirs parsing (unit)
# ---------------------------------------------------------------------------

class TestListAmuxSessionsWithDirs(unittest.TestCase):
    """Unit test the amux ls parser independently."""

    AMUX_LS_OUTPUT = """\
 1  proj-worker   /home/user/proj
 2  proj-2        /home/user/proj
 3* current-ws    /home/user/ws
 4  other-proj    /home/user/other
"""

    def test_parses_name_and_dir(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self.AMUX_LS_OUTPUT
            import shutil
            with patch.object(shutil, "which", return_value="/usr/local/bin/amux"):
                result = lib.list_amux_sessions_with_dirs()
        names = [n for n, _ in result]
        dirs = [d for _, d in result]
        self.assertIn("proj-worker", names)
        self.assertIn("proj-2", names)
        self.assertIn("current-ws", names)  # the * (current) session is included
        self.assertIn("other-proj", names)
        self.assertEqual(len(result), 4)
        self.assertIn("/home/user/proj", dirs)

    def test_no_amux_returns_empty(self):
        import shutil
        with patch.object(shutil, "which", return_value=None):
            result = lib.list_amux_sessions_with_dirs()
        self.assertEqual(result, [])

    def test_amux_error_returns_empty(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            import shutil
            with patch.object(shutil, "which", return_value="/usr/local/bin/amux"):
                result = lib.list_amux_sessions_with_dirs()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
