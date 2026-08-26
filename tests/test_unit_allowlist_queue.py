#!/usr/bin/env python3
"""
Unit Tests: allowlist writers + the proposal queue (epic 22, task 22-04)

Covers the nine cases in the task's testing table:

1. ``resolve_project_key`` from a real temp repo + a real ``git worktree add``
2. a non-git directory (and a run with no ``git`` on PATH at all)
3. own-workspace ``allowlist_add`` — written once, second call dedupes
4. foreign-workspace ``allowlist_add`` — queued for the target, caller untouched
5. worktree caller + foreign main-checkout target — the D7 key
6. a pattern a deny entry already covers — refused, deny entry named
7. a malformed pattern — refused
8. ``scope="user"`` from elsewhere — queued to the claude-hooks key, H6 note
9. ``report_parser_issue`` — a parseable ``parser_issue`` entry

Plus the shared settings writer (both target files, both refusal corners) and
the queue's move-to-``processed`` contract.

Everything runs against scratch git repos, a scratch HOME and a scratch queue
root (``CLAUDE_PERMISSION_QUEUE_DIR``): nothing here touches the developer's
real ~/.claude, no network, no bot.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / ".claude" / "hooks"))
sys.path.insert(0, str(_REPO_ROOT / "permissions-mcp"))

# Isolate the permission state store before anything imports it, for the case
# where this module runs on its own. ``setdefault`` keeps an outer override
# winning, exactly as run_all_tests.py / conftest.py do.
_ISOLATED = Path(tempfile.mkdtemp(prefix="allowlist-queue-store-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_ISOLATED / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_ISOLATED / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_ISOLATED / "permission_state_debug.log"))

import permission_queue  # noqa: E402
import permissions_mcp_lib as lib  # noqa: E402
import project_key  # noqa: E402
import settings_writer  # noqa: E402
from settings_loader import SettingsLoader  # noqa: E402


CALLER_SESSION = "caller-session-2204"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class AllowlistQueueTestCase(unittest.TestCase):
    """Scratch HOME + scratch queue root; git identity forced so commits work."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="allowlist-queue-"))
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.write_settings(self.home / ".claude" / "settings.json")
        self.queue_root = self.tmp / "permission-queue"
        # The validator merges the *global* settings into every verdict, so a
        # test that did not move HOME would check deny collisions against
        # whatever the developer happens to have denied.
        SettingsLoader._cache.clear()
        project_key.clear_cache()

    def tearDown(self):
        SettingsLoader._cache.clear()
        project_key.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def write_settings(path: Path, allow=None, deny=None, ask=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"permissions": {"allow": allow or [], "deny": deny or [], "ask": ask or []}},
                indent=2,
            )
        )

    def env(self, **extra):
        environment = {
            "HOME": str(self.home),
            permission_queue.QUEUE_DIR_ENV: str(self.queue_root),
        }
        environment.update(extra)
        return environment

    def make_repo(self, name: str) -> Path:
        repo = self.tmp / name
        repo.mkdir(parents=True)
        _git(repo.parent, "init", "-q", "-b", "main", str(repo))
        (repo / "README.md").write_text(f"{name}\n")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init")
        return repo

    def add_worktree(self, repo: Path, name: str, branch: str) -> Path:
        worktree = self.tmp / name
        _git(repo, "worktree", "add", "-q", "-b", branch, str(worktree))
        return worktree

    def identity(self, cwd: Path, session_id=CALLER_SESSION):
        env = {"CLAUDE_PROJECT_DIR": str(cwd)}
        if session_id is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session_id
        return lib.resolve_caller_identity(env)

    def allowlist_add(self, *args, **kwargs):
        env_extra = kwargs.pop("env_extra", {})
        with patch.dict(os.environ, self.env(**env_extra), clear=False):
            return lib.allowlist_add(*args, **kwargs)

    def report_parser_issue(self, *args, **kwargs):
        env_extra = kwargs.pop("env_extra", {})
        with patch.dict(os.environ, self.env(**env_extra), clear=False):
            return lib.report_parser_issue(*args, **kwargs)

    def queue_entries(self, key: str):
        with patch.dict(os.environ, self.env(), clear=False):
            return permission_queue.list_entries(key)


# ---------------------------------------------------------------------------
# Cases 1-2: resolve_project_key — the only project-identity function
# ---------------------------------------------------------------------------


class TestResolveProjectKey(AllowlistQueueTestCase):
    """Invariant 7: one function, and every worktree maps to the main checkout."""

    # -- case 1 --------------------------------------------------------------
    def test_worktree_and_main_checkout_share_one_key(self):
        repo = self.make_repo("proj")
        worktree = self.add_worktree(repo, "proj-feature", "feature-x")
        nested = worktree / "sub" / "deeper"
        nested.mkdir(parents=True)

        main_key = project_key.resolve_project_key(str(repo))
        worktree_key = project_key.resolve_project_key(str(worktree))
        nested_key = project_key.resolve_project_key(str(nested))

        self.assertEqual(main_key, worktree_key)
        self.assertEqual(main_key, nested_key)
        self.assertEqual(main_key, project_key.encode_project_key(os.path.realpath(str(repo))))
        self.assertNotIn("/", main_key)

    def test_worktree_root_is_the_worktree_not_the_main_checkout(self):
        """The write target and the queue key are deliberately different things."""
        repo = self.make_repo("proj")
        worktree = self.add_worktree(repo, "proj-feature", "feature-x")

        self.assertEqual(
            project_key.resolve_workspace_root(str(worktree)), os.path.realpath(str(worktree))
        )
        self.assertEqual(
            project_key.resolve_project_root(str(worktree)), os.path.realpath(str(repo))
        )

    # -- case 2 --------------------------------------------------------------
    def test_non_git_directory_falls_back_to_realpath(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()

        self.assertEqual(
            project_key.resolve_project_root(str(plain)), os.path.realpath(str(plain))
        )
        self.assertEqual(
            project_key.resolve_project_key(str(plain)),
            project_key.encode_project_key(os.path.realpath(str(plain))),
        )

    def test_no_git_binary_does_not_crash(self):
        """git missing entirely: realpath key, no exception (fail open)."""
        repo = self.make_repo("proj")
        project_key.clear_cache()
        with patch.dict(os.environ, {"PATH": str(self.tmp / "empty-bin")}, clear=False):
            key = project_key.resolve_project_key(str(repo))
            root = project_key.resolve_workspace_root(str(repo))

        self.assertEqual(key, project_key.encode_project_key(os.path.realpath(str(repo))))
        self.assertEqual(root, os.path.realpath(str(repo)))

    def test_results_are_cached_per_path(self):
        repo = self.make_repo("proj")
        with patch.object(project_key, "_run_git", wraps=project_key._run_git) as spy:
            project_key.resolve_project_key(str(repo))
            project_key.resolve_project_key(str(repo))
        self.assertEqual(spy.call_count, 1)


# ---------------------------------------------------------------------------
# Case 3: own-workspace allowlist_add writes versioned settings
# ---------------------------------------------------------------------------


class TestOwnWorkspaceWrite(AllowlistQueueTestCase):
    """Invariant 4's A-half: your own checkout, written directly."""

    # -- case 3 --------------------------------------------------------------
    def test_pattern_written_once_and_second_call_dedupes(self):
        repo = self.make_repo("proj")
        identity = self.identity(repo)

        first = self.allowlist_add(
            "Bash(git log:*)", rationale="read-only history browsing", identity=identity
        )
        second = self.allowlist_add(
            "Bash(git log:*)", rationale="read-only history browsing", identity=identity
        )

        self.assertTrue(first["ok"], first)
        self.assertEqual(first["action"], "wrote_settings")
        self.assertTrue(first["added"])
        self.assertTrue(second["ok"], second)
        self.assertFalse(second["added"])
        self.assertIn("already present", second["status"])

        settings = json.loads((repo / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["permissions"]["allow"].count("Bash(git log:*)"), 1)
        # Nothing was queued for the caller's own project.
        self.assertEqual(self.queue_entries(first["target_project_key"]), [])

    def test_write_lands_in_the_callers_own_checkout_from_a_worktree(self):
        """A worktree agent writes its own worktree, never the main checkout."""
        repo = self.make_repo("proj")
        worktree = self.add_worktree(repo, "proj-feature", "feature-x")

        result = self.allowlist_add(
            "Bash(rg:*)",
            target_workspace=str(repo),
            rationale="searching the tree",
            identity=self.identity(worktree),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], str(worktree / ".claude" / "settings.json"))
        self.assertFalse((repo / ".claude" / "settings.json").exists())
        self.assertTrue(
            any("not running in" in note for note in result["notes"]),
            result["notes"],
        )

    def test_existing_settings_content_is_preserved(self):
        repo = self.make_repo("proj")
        self.write_settings(
            repo / ".claude" / "settings.json", allow=["Bash(ls:*)"], deny=["Bash(curl:*)"]
        )
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(curl:*)"]},
                    "model": "opus",
                },
                indent=2,
            )
        )

        result = self.allowlist_add(
            "Bash(rg:*)", rationale="searching", identity=self.identity(repo)
        )

        self.assertTrue(result["ok"], result)
        settings = json.loads((repo / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["permissions"]["deny"], ["Bash(curl:*)"])
        self.assertEqual(settings["permissions"]["allow"], ["Bash(ls:*)", "Bash(rg:*)"])

    def test_non_git_own_workspace_writes_next_to_itself(self):
        plain = self.tmp / "plain-workspace"
        plain.mkdir()

        result = self.allowlist_add(
            "Bash(make:*)", rationale="building", identity=self.identity(plain)
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], str(plain / ".claude" / "settings.json"))


# ---------------------------------------------------------------------------
# Cases 4-5: foreign workspaces enqueue, keyed by the main checkout
# ---------------------------------------------------------------------------


class TestForeignWorkspaceEnqueue(AllowlistQueueTestCase):
    """Invariant 4's C-half: never a write into a checkout we do not run in."""

    # -- case 4 --------------------------------------------------------------
    def test_foreign_workspace_is_queued_and_target_untouched(self):
        caller_repo = self.make_repo("caller")
        target_repo = self.make_repo("target")

        result = self.allowlist_add(
            "Bash(pytest:*)",
            target_workspace=str(target_repo),
            rationale="the suite runs on every task there",
            evidence_request_ids=["req-1", "req-2"],
            identity=self.identity(caller_repo),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "queued")
        target_key = project_key.resolve_project_key(str(target_repo))
        self.assertEqual(result["target_project_key"], target_key)
        self.assertIn("scheduled reviewer applies it", result["status"])

        entries = self.queue_entries(target_key)
        self.assertEqual(len(entries), 1)
        entry = json.loads(entries[0].read_text())
        self.assertEqual(entry["type"], "allowlist_proposal")
        self.assertEqual(entry["pattern"], "Bash(pytest:*)")
        self.assertEqual(entry["scope"], "workspace")
        self.assertEqual(entry["evidence_request_ids"], ["req-1", "req-2"])
        self.assertEqual(entry["source"]["session_id"], CALLER_SESSION)
        self.assertEqual(entry["source"]["cwd"], str(caller_repo))

        # Neither checkout was written: not the target (D6), not the caller.
        self.assertFalse((target_repo / ".claude").exists())
        self.assertFalse((caller_repo / ".claude").exists())
        # And nothing landed under the caller's own key.
        caller_key = project_key.resolve_project_key(str(caller_repo))
        self.assertEqual(self.queue_entries(caller_key), [])

    # -- case 5 --------------------------------------------------------------
    def test_worktree_caller_and_foreign_worktree_target_use_main_checkout_keys(self):
        """The D7 point, from both ends at once."""
        caller_repo = self.make_repo("caller")
        caller_worktree = self.add_worktree(caller_repo, "caller-feature", "caller-feat")
        target_repo = self.make_repo("target")
        target_worktree = self.add_worktree(target_repo, "target-feature", "target-feat")

        result = self.allowlist_add(
            "Bash(npm run build:*)",
            target_workspace=str(target_worktree),
            rationale="every branch of that repo builds the same way",
            identity=self.identity(caller_worktree),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "queued")
        main_key = project_key.resolve_project_key(str(target_repo))
        self.assertEqual(result["target_project_key"], main_key)
        self.assertEqual(
            main_key, project_key.encode_project_key(os.path.realpath(str(target_repo)))
        )

        # The entry is under the MAIN checkout's key, not the worktree's path.
        entries = self.queue_entries(main_key)
        self.assertEqual(len(entries), 1)
        worktree_path_key = project_key.encode_project_key(
            os.path.realpath(str(target_worktree))
        )
        self.assertNotEqual(main_key, worktree_path_key)
        self.assertFalse((self.queue_root / worktree_path_key).exists())

        # No file was created in either foreign checkout.
        self.assertFalse((target_repo / ".claude").exists())
        self.assertFalse((target_worktree / ".claude").exists())


# ---------------------------------------------------------------------------
# Cases 6-7: refusals happen before anything touches disk
# ---------------------------------------------------------------------------


class TestRefusals(AllowlistQueueTestCase):
    """Shape and deny collision, both refused before any write or enqueue."""

    # -- case 6 --------------------------------------------------------------
    def test_deny_collision_is_refused_and_names_the_deny_entry(self):
        repo = self.make_repo("proj")
        self.write_settings(repo / ".claude" / "settings.json", deny=["Bash(git push:*)"])

        result = self.allowlist_add(
            "Bash(git push origin:*)",
            rationale="pushing the branch is routine here",
            identity=self.identity(repo),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["refused"])
        self.assertEqual(result["deny_entry"], "Bash(git push:*)")
        self.assertIn("deny → ask → allow", result["reason"])
        settings = json.loads((repo / ".claude" / "settings.json").read_text())
        self.assertNotIn("Bash(git push origin:*)", settings["permissions"]["allow"])

    def test_deny_collision_on_a_foreign_target_queues_nothing(self):
        caller_repo = self.make_repo("caller")
        target_repo = self.make_repo("target")
        self.write_settings(target_repo / ".claude" / "settings.json", deny=["Bash(rm:*)"])

        result = self.allowlist_add(
            "Bash(rm -rf build:*)",
            target_workspace=str(target_repo),
            rationale="build dir is disposable",
            identity=self.identity(caller_repo),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["deny_entry"], "Bash(rm:*)")
        self.assertEqual(self.queue_entries(result["target_project_key"]), [])

    def test_deny_is_read_from_the_targets_main_checkout(self):
        """A queued proposal is applied centrally, so the main checkout decides."""
        caller_repo = self.make_repo("caller")
        target_repo = self.make_repo("target")
        target_worktree = self.add_worktree(target_repo, "target-feature", "target-feat")
        self.write_settings(target_repo / ".claude" / "settings.json", deny=["Bash(curl:*)"])

        result = self.allowlist_add(
            "Bash(curl -sS:*)",
            target_workspace=str(target_worktree),
            rationale="fetching fixtures",
            identity=self.identity(caller_repo),
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["deny_entry"], "Bash(curl:*)")
        self.assertEqual(self.queue_entries(result["target_project_key"]), [])

    def test_global_deny_entry_also_collides(self):
        """The check reads *merged* settings, global file included."""
        repo = self.make_repo("proj")
        self.write_settings(self.home / ".claude" / "settings.json", deny=["Bash(sudo:*)"])

        result = self.allowlist_add(
            "Bash(sudo apt install:*)", rationale="installing deps", identity=self.identity(repo)
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["deny_entry"], "Bash(sudo:*)")

    def test_bare_tool_deny_collides_with_a_parenthesised_proposal(self):
        repo = self.make_repo("proj")
        self.write_settings(repo / ".claude" / "settings.json", deny=["WebFetch"])

        result = self.allowlist_add(
            "WebFetch(domain:example.com)", rationale="docs", identity=self.identity(repo)
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["deny_entry"], "WebFetch")

    def test_narrower_deny_does_not_block_a_broader_proposal(self):
        """Layering is normal: deny carves a slice out of a wider allow."""
        repo = self.make_repo("proj")
        self.write_settings(repo / ".claude" / "settings.json", deny=["Bash(git push:*)"])

        result = self.allowlist_add(
            "Bash(git:*)", rationale="git is read-mostly here", identity=self.identity(repo)
        )

        self.assertTrue(result["ok"], result)
        settings = json.loads((repo / ".claude" / "settings.json").read_text())
        self.assertIn("Bash(git:*)", settings["permissions"]["allow"])

    # -- case 7 --------------------------------------------------------------
    def test_malformed_patterns_are_refused(self):
        repo = self.make_repo("proj")
        identity = self.identity(repo)
        malformed = [
            "please allow git log",
            "Bash(git log:*",
            "git log",
            "",
            "   ",
            "Bash()",
            "Bash(git log:*)\nBash(rm:*)",
            "git:log",
        ]

        for pattern in malformed:
            with self.subTest(pattern=pattern):
                result = self.allowlist_add(
                    pattern, rationale="because", identity=identity
                )
                self.assertFalse(result["ok"], result)
                self.assertIn("not a permission pattern", result["reason"])

        self.assertFalse((repo / ".claude").exists())

    def test_well_formed_patterns_parse(self):
        for pattern in (
            "Bash(git log:*)",
            "Bash(pwd)",
            "Bash",
            "WebFetch(domain:example.com)",
            "mcp__permissions__allowlist_add",
            "mcp__permissions__*",
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(lib.parse_pattern(pattern))

    def test_missing_rationale_is_refused(self):
        repo = self.make_repo("proj")
        result = self.allowlist_add("Bash(ls:*)", rationale="  ", identity=self.identity(repo))

        self.assertFalse(result["ok"])
        self.assertIn("rationale is required", result["reason"])
        self.assertFalse((repo / ".claude").exists())

    def test_unknown_scope_is_refused(self):
        repo = self.make_repo("proj")
        result = self.allowlist_add(
            "Bash(ls:*)", scope="global", rationale="x", identity=self.identity(repo)
        )

        self.assertFalse(result["ok"])
        self.assertIn("scope must be one of", result["reason"])


# ---------------------------------------------------------------------------
# Case 8: user scope targets the claude-hooks checkout, and says so (H6)
# ---------------------------------------------------------------------------


class TestUserScope(AllowlistQueueTestCase):
    """brd H6 / invariant 8: a repo settings edit names its propagation."""

    def setUp(self):
        super().setUp()
        self.hooks_repo = self.make_repo("claude-hooks")

    # -- case 8 --------------------------------------------------------------
    def test_user_scope_from_another_workspace_queues_to_claude_hooks(self):
        other = self.make_repo("some-project")

        result = self.allowlist_add(
            "Bash(jq:*)",
            scope="user",
            rationale="every workspace parses JSON",
            identity=self.identity(other),
            env_extra={"CLAUDE_HOOKS_REPO": str(self.hooks_repo)},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "queued")
        hooks_key = project_key.resolve_project_key(str(self.hooks_repo))
        self.assertEqual(result["target_project_key"], hooks_key)
        self.assertTrue(any("install-claude-config.sh" in note for note in result["notes"]))

        entries = self.queue_entries(hooks_key)
        self.assertEqual(len(entries), 1)
        entry = json.loads(entries[0].read_text())
        self.assertEqual(entry["scope"], "user")
        self.assertEqual(entry["pattern"], "Bash(jq:*)")
        self.assertFalse((self.hooks_repo / ".claude").exists())

    def test_user_scope_from_inside_claude_hooks_writes_the_repo_settings(self):
        result = self.allowlist_add(
            "Bash(shellcheck:*)",
            scope="user",
            rationale="linting the installer",
            identity=self.identity(self.hooks_repo),
            env_extra={"CLAUDE_HOOKS_REPO": str(self.hooks_repo)},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "wrote_settings")
        self.assertEqual(result["path"], str(self.hooks_repo / ".claude" / "settings.json"))
        settings = json.loads((self.hooks_repo / ".claude" / "settings.json").read_text())
        self.assertIn("Bash(shellcheck:*)", settings["permissions"]["allow"])
        # The H6 note rides along even on the direct-write path.
        self.assertTrue(any("install-claude-config.sh" in note for note in result["notes"]))

    def test_user_scope_ignores_target_workspace_and_says_so(self):
        other = self.make_repo("some-project")

        result = self.allowlist_add(
            "Bash(jq:*)",
            scope="user",
            target_workspace=str(other),
            rationale="every workspace parses JSON",
            identity=self.identity(other),
            env_extra={"CLAUDE_HOOKS_REPO": str(self.hooks_repo)},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["target_project_key"], project_key.resolve_project_key(str(self.hooks_repo))
        )
        self.assertTrue(any("ignored" in note for note in result["notes"]))


# ---------------------------------------------------------------------------
# Case 9: parser issues always queue
# ---------------------------------------------------------------------------


class TestReportParserIssue(AllowlistQueueTestCase):
    def setUp(self):
        super().setUp()
        self.hooks_repo = self.make_repo("claude-hooks")

    # -- case 9 --------------------------------------------------------------
    def test_parser_issue_is_queued_and_parseable(self):
        other = self.make_repo("some-project")

        result = self.report_parser_issue(
            command="foo && bar | tee /tmp/x.log",
            observed="ask: 'Redirects output outside the workspace'",
            expected="allow — /tmp is a sanctioned scratch location",
            notes="hit three times today",
            identity=self.identity(other),
            env_extra={"CLAUDE_HOOKS_REPO": str(self.hooks_repo)},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "queued")
        hooks_key = project_key.resolve_project_key(str(self.hooks_repo))
        self.assertEqual(result["target_project_key"], hooks_key)

        entries = self.queue_entries(hooks_key)
        self.assertEqual(len(entries), 1)
        entry = json.loads(entries[0].read_text())
        self.assertEqual(entry["type"], "parser_issue")
        self.assertEqual(entry["command"], "foo && bar | tee /tmp/x.log")
        self.assertEqual(entry["expected"], "allow — /tmp is a sanctioned scratch location")
        self.assertEqual(entry["notes"], "hit three times today")
        self.assertEqual(entry["source"]["cwd"], str(other))
        # Atomic: no temp file left behind.
        self.assertEqual(list((self.queue_root / hooks_key).glob(".*")), [])

    def test_parser_issue_from_claude_hooks_itself_still_queues(self):
        """No own-workspace shortcut — a uniform path for the reviewer."""
        result = self.report_parser_issue(
            command="git commit -m 'x' && git push",
            observed="deny: matched Bash(git push:*)",
            expected="ask — the compound is not a push on its own",
            identity=self.identity(self.hooks_repo),
            env_extra={"CLAUDE_HOOKS_REPO": str(self.hooks_repo)},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "queued")
        self.assertFalse((self.hooks_repo / ".claude").exists())
        self.assertEqual(
            len(self.queue_entries(project_key.resolve_project_key(str(self.hooks_repo)))), 1
        )

    def test_incomplete_parser_issue_is_refused(self):
        result = self.report_parser_issue(
            command="ls",
            observed="",
            expected="allow",
            identity=self.identity(self.hooks_repo),
            env_extra={"CLAUDE_HOOKS_REPO": str(self.hooks_repo)},
        )

        self.assertFalse(result["ok"])
        self.assertIn("observed", result["reason"])
        self.assertFalse(self.queue_root.exists())


# ---------------------------------------------------------------------------
# The queue's own contract
# ---------------------------------------------------------------------------


class TestQueueMechanics(AllowlistQueueTestCase):
    def test_entries_are_moved_to_processed_never_deleted(self):
        key = "-tmp-example-project"
        with patch.dict(os.environ, self.env(), clear=False):
            path = permission_queue.enqueue(
                key,
                permission_queue.build_parser_issue(
                    command="ls",
                    observed="ask",
                    expected="allow",
                    notes="",
                    session_id="s",
                    actor_agent="a",
                    cwd="/tmp",
                ),
            )
            self.assertEqual(permission_queue.list_entries(key), [path])
            moved = permission_queue.mark_processed(path)
            self.assertEqual(permission_queue.list_entries(key), [])
            self.assertEqual(moved.parent, permission_queue.processed_dir(key))

        self.assertFalse(path.exists())
        self.assertTrue(moved.exists())
        self.assertEqual(permission_queue.read_entry(moved)["type"], "parser_issue")

    def test_queue_root_honors_the_env_override(self):
        with patch.dict(os.environ, self.env(), clear=False):
            self.assertEqual(permission_queue.queue_root(), self.queue_root)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(permission_queue.QUEUE_DIR_ENV, None)
            self.assertEqual(
                permission_queue.queue_root(), Path.home() / ".claude" / "permission-queue"
            )

    def test_entry_filenames_are_unique_and_sortable(self):
        key = "-tmp-example-project"
        with patch.dict(os.environ, self.env(), clear=False):
            paths = [
                permission_queue.enqueue(
                    key,
                    permission_queue.build_allowlist_proposal(
                        pattern=f"Bash(cmd{i}:*)",
                        scope="workspace",
                        rationale="r",
                        session_id="s",
                        actor_agent="a",
                        cwd="/tmp",
                    ),
                )
                for i in range(5)
            ]
            listed = permission_queue.list_entries(key)

        self.assertEqual(len(set(paths)), 5)
        self.assertEqual(listed, sorted(paths))


# ---------------------------------------------------------------------------
# The shared settings writer (§3) — and the router's byte-compatible delegation
# ---------------------------------------------------------------------------


class TestSettingsWriter(AllowlistQueueTestCase):
    def test_targets_versioned_or_local_and_any_list(self):
        workspace = self.tmp / "ws"
        workspace.mkdir()

        versioned = settings_writer.add_permission_pattern(str(workspace), "Bash(ls:*)")
        local = settings_writer.add_permission_pattern(
            str(workspace), "Bash(cat:*)", settings_filename=settings_writer.LOCAL_SETTINGS
        )
        asked = settings_writer.add_permission_pattern(
            str(workspace), "Bash(rm:*)", list_name="ask"
        )

        self.assertTrue(versioned.ok and versioned.added)
        self.assertTrue(local.ok and local.added)
        self.assertTrue(asked.ok and asked.added)
        settings = json.loads((workspace / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["permissions"]["allow"], ["Bash(ls:*)"])
        self.assertEqual(settings["permissions"]["ask"], ["Bash(rm:*)"])
        local_settings = json.loads((workspace / ".claude" / "settings.local.json").read_text())
        self.assertEqual(local_settings["permissions"]["allow"], ["Bash(cat:*)"])

    def test_unknown_list_is_rejected(self):
        workspace = self.tmp / "ws"
        workspace.mkdir()
        result = settings_writer.add_permission_pattern(
            str(workspace), "Bash(ls:*)", list_name="maybe"
        )
        self.assertFalse(result.ok)
        self.assertIn("unknown permission list", result.error)
        self.assertFalse((workspace / ".claude").exists())

    def test_corrupt_json_is_moved_aside_and_rewritten(self):
        workspace = self.tmp / "ws"
        (workspace / ".claude").mkdir(parents=True)
        target = workspace / ".claude" / "settings.json"
        target.write_text("{ not json")

        logged = []
        result = settings_writer.add_permission_pattern(
            str(workspace), "Bash(ls:*)", log=logged.append
        )

        self.assertTrue(result.ok)
        self.assertTrue((workspace / ".claude" / "settings.json.backup").exists())
        self.assertEqual(
            json.loads(target.read_text())["permissions"]["allow"], ["Bash(ls:*)"]
        )
        self.assertTrue(any("Error parsing" in line for line in logged))

    def test_malformed_permissions_key_is_reported_not_overwritten(self):
        workspace = self.tmp / "ws"
        (workspace / ".claude").mkdir(parents=True)
        target = workspace / ".claude" / "settings.json"
        target.write_text(json.dumps({"permissions": ["Bash(ls:*)"]}))

        result = settings_writer.add_permission_pattern(str(workspace), "Bash(rg:*)")

        self.assertFalse(result.ok)
        self.assertIn("not an object", result.error)
        self.assertEqual(json.loads(target.read_text()), {"permissions": ["Bash(ls:*)"]})

    def test_no_temp_file_survives_a_write(self):
        workspace = self.tmp / "ws"
        workspace.mkdir()
        settings_writer.add_permission_pattern(str(workspace), "Bash(ls:*)")
        self.assertEqual(list((workspace / ".claude").glob("*.tmp")), [])

    def test_router_delegation_still_writes_settings_local_json(self):
        """brd §3.2: the Telegram Whitelist button keeps writing local settings."""
        import telegram_permission_router as router

        workspace = self.tmp / "ws"
        workspace.mkdir()

        self.assertTrue(router.update_settings_local_json(str(workspace), "Bash(ls:*)"))
        self.assertTrue(router.update_settings_local_json(str(workspace), "Bash(ls:*)"))

        self.assertFalse((workspace / ".claude" / "settings.json").exists())
        settings = json.loads((workspace / ".claude" / "settings.local.json").read_text())
        self.assertEqual(settings["permissions"]["allow"], ["Bash(ls:*)"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
