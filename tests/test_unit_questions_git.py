#!/usr/bin/env python3
"""Tests for ``questions_git`` (epic 46, task 46-03) — against real repositories.

Every test builds a bare "remote" and one or more clones in a temp directory and
drives the engine through its public entry point, ``run``.  Nothing is mocked
except where the point of the test is that git is *not* called.

What is being proved, in the brd's words:

* D6  — an id is claimed by a commit on the upstream branch, and a lost push
        race re-allocates without leaving anything behind locally.
* D7  — the tool pushes one commit that touches only queue files, however far
        ahead the local branch is.
* D8  — the checkout is only touched through a pathspec.
* D9  — every degraded state still writes the entry and names itself.
* D13 — ``sync = "off"`` starts no git process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _REPO_ROOT / ".claude" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

_ISOLATION_DIR = tempfile.mkdtemp(prefix="questions-git-test-")
_ORIGINAL_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

import questions_git as qg  # noqa: E402
import questions_listen_lib as qll  # noqa: E402
import questions_ops as qops  # noqa: E402
import questions_store as qs  # noqa: E402


def tearDownModule():
    if _ORIGINAL_PROJECT_DIR is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = _ORIGINAL_PROJECT_DIR
    shutil.rmtree(_ISOLATION_DIR, ignore_errors=True)


ROLES_TOML = """\
workspace_id = "demo-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"

[questions]
dir    = "docs/questions"
anchor = "worktree"
sync   = "{sync}"

[questions.queue]
hpl = "for-product-lead.md"
{extra}
"""

QUEUE = "docs/questions/for-product-lead.md"
BASE_QUEUE = "# Questions\n\n## Q-001 — first  [resolved]\n\nBody one.\n"

#: Isolate every git process — the test's own and the engine's — from the
#: developer's global configuration (signing, hooksPath, default branch).
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


#: The driver command exactly as the engine builds it, for a stand-in CLI:
#: this checkout's, with an empty HOME so it loads this checkout's modules
#: rather than whatever is installed under ~/.claude.
_TEST_CLI = Path(_ISOLATION_DIR) / "claude-questions"
_TEST_CLI.write_text(
    "#!/bin/sh\n"
    f'HOME="{_ISOLATION_DIR}" exec "{sys.executable}" '
    f'"{_REPO_ROOT / "shell" / "claude-questions"}" "$@"\n',
    encoding="utf-8",
)
_TEST_CLI.chmod(0o755)
TEST_MERGE_DRIVER = qg.merge_driver_command(str(_TEST_CLI))


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


class GitSyncTestCase(unittest.TestCase):
    sync = "push"
    extra = ""

    def setUp(self):
        patcher = patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The merge driver the engine registers must be this checkout's CLI,
        # never whatever is installed under ~/.claude on the machine.
        driver = patch.object(qg, "MERGE_DRIVER_COMMAND", TEST_MERGE_DRIVER)
        driver.start()
        self.addCleanup(driver.stop)

        self.tmp = Path(tempfile.mkdtemp(prefix="qgit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(self.remote))

        # Seed the remote from a first clone.
        self.a = self.tmp / "a"
        git(self.tmp, "clone", "--quiet", str(self.remote), str(self.a))
        git(self.a, "checkout", "--quiet", "-b", "main")
        self.write(self.a, ".claude/roles.toml", ROLES_TOML.format(sync=self.sync, extra=self.extra))
        self.write(self.a, QUEUE, BASE_QUEUE)
        self.write(self.a, "src/app.txt", "v1\n")
        git(self.a, "add", "-A")
        git(self.a, "commit", "--quiet", "-m", "base")
        git(self.a, "push", "--quiet", "-u", "origin", "main")

    # ── helpers ──

    def write(self, repo: Path, rel: str, text: str) -> Path:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    def read(self, repo: Path, rel: str = QUEUE) -> str:
        with open(repo / rel, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def clone(self, name: str) -> Path:
        path = self.tmp / name
        git(self.tmp, "clone", "--quiet", str(self.remote), str(path))
        return path

    def store(self, repo: Path) -> qs.QuestionsStore:
        roles = repo / ".claude" / "roles.toml"
        config = qs.load_questions_config(str(repo), roles_path=roles)
        self.assertIsNotNone(config)
        return qs.store_for_root(config, repo)

    def ask(self, repo: Path, title: str = "a question", **kwargs):
        store = self.store(repo)
        return qg.run(
            store,
            lambda s: s.create_entry(title=title, role="hpl", **kwargs),
            summary=lambda e: f"ask {e.qid}",
        )

    def remote_log(self) -> list[str]:
        return git(self.remote, "log", "--format=%s", "main").splitlines()

    def remote_file(self, rel: str = QUEUE) -> str:
        return subprocess.run(
            ["git", "show", f"main:{rel}"], cwd=str(self.remote),
            capture_output=True, text=True, check=True,
        ).stdout

    def remote_tip(self) -> str:
        return git(self.remote, "rev-parse", "main")

    def files_in(self, repo: Path, rev: str) -> list[str]:
        return git(repo, "show", "--format=", "--name-only", rev).splitlines()

    def status(self, repo: Path) -> str:
        return git(repo, "status", "--porcelain")

    def assertCleanCheckout(self, repo: Path):
        self.assertEqual(self.status(repo), "", "the checkout must be left clean")
        leftovers = [p.name for p in (repo / ".git").iterdir() if p.name.startswith("claude-questions-index")]
        self.assertEqual(leftovers, [], "the private index must be removed")


# ── D13: off means off ────────────────────────────────────────────────────────


class TestSyncOff(GitSyncTestCase):
    sync = "off"

    def test_no_git_process_is_started(self):
        store = self.store(self.a)
        with patch.object(subprocess, "run", side_effect=AssertionError("git must not be called")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("git must not be called")):
            entry, report = qg.run(store, lambda s: s.create_entry(title="t", role="hpl"))
        self.assertIsNone(report)
        self.assertEqual(entry.qid, "Q-002")
        self.assertIn("## Q-002 — t  [open]", self.read(self.a))

    def test_nothing_is_committed_or_recorded(self):
        head = git(self.a, "rev-parse", "HEAD")
        self.ask(self.a)
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), head)
        self.assertFalse((self.a / ".git" / qg.STATE_FILENAME).exists())
        self.assertFalse(qg.is_pending(self.store(self.a)))


# ── D6 / D7: the happy paths ──────────────────────────────────────────────────


class TestPushInSync(GitSyncTestCase):

    def test_ask_is_one_upstream_commit_touching_one_file(self):
        before = self.remote_tip()
        entry, report = self.ask(self.a, "Ship it?")

        self.assertEqual(entry.qid, "Q-002")
        self.assertTrue(report.pushed)
        self.assertTrue(report.committed)
        self.assertFalse(report.twin, "an in-sync checkout must not grow a twin")
        self.assertFalse(report.pending)
        self.assertEqual(report.attempts, 1)
        self.assertEqual(report.upstream, "origin/main")
        self.assertEqual(report.files, [QUEUE])

        tip = self.remote_tip()
        self.assertEqual(git(self.remote, "rev-parse", "main~1"), before)
        self.assertEqual(self.files_in(self.remote, tip), [QUEUE])
        self.assertEqual(self.remote_log()[0], "chore(questions): ask Q-002")
        self.assertIn("## Q-002 — Ship it?  [open]", self.remote_file())

        # The local branch is on the very commit that was pushed.
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.read(self.a), self.remote_file())
        self.assertCleanCheckout(self.a)
        self.assertEqual(git(self.a, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_two_writes_in_a_row_stay_in_sync(self):
        self.ask(self.a, "one")
        store = self.store(self.a)
        _res, report = qg.run(
            store, lambda s: s.mark_dispatched("Q-002", 4187, role="hpl"),
            summary="dispatched Q-002 (relay #4187)",
        )
        self.assertTrue(report.pushed)
        self.assertFalse(report.twin)
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), self.remote_tip())
        self.assertEqual(self.remote_log()[:2], [
            "chore(questions): dispatched Q-002 (relay #4187)",
            "chore(questions): ask Q-002",
        ])
        self.assertCleanCheckout(self.a)

    def test_a_mutation_that_writes_nothing_makes_no_commit(self):
        self.ask(self.a, "one")
        store = self.store(self.a)
        qg.run(store, lambda s: s.apply_answer("Q-002", "yes", "@hpl", 9, role="hpl"), summary="answer Q-002")
        tip = self.remote_tip()
        # The replay: idempotent in the store, so there is nothing to publish.
        result, report = qg.run(
            self.store(self.a),
            lambda s: s.apply_answer("Q-002", "yes", "@hpl", 9, role="hpl"),
            summary="answer Q-002",
        )
        self.assertIs(result, qs.ApplyResult.APPLIED)
        self.assertFalse(report.changed)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(report.to_dict().get("changed"), False)

    def test_the_commit_message_template_is_the_workspaces(self):
        self.write(self.a, ".claude/roles.toml", ROLES_TOML.format(
            sync="push", extra='\n[questions.git]\nmessage = "docs(questions): {summary} [skip ci]"\n'))
        self.ask(self.a)
        self.assertEqual(self.remote_log()[0], "docs(questions): ask Q-002 [skip ci]")

    def test_a_new_ledger_file_is_added_and_pushed(self):
        store = self.store(self.a)
        ids, report = qg.run(store, lambda s: s.claim(2, note="hf-123"), summary=lambda i: f"claim {i[0]}–{i[-1]}")
        self.assertEqual(ids, ["Q-002", "Q-003"])
        self.assertTrue(report.pushed)
        self.assertIn("- Q-002 · ", self.remote_file("docs/questions/claims.md"))
        self.assertCleanCheckout(self.a)
        # …and the claim binds the next ask, here and on any other machine.
        self.assertEqual(self.ask(self.a)[0].qid, "Q-004")
        b = self.clone("b")
        self.assertEqual(self.ask(b)[0].qid, "Q-005")

    def test_workspace_hooks_are_not_run(self):
        """A pre-commit hook that runs a test suite must not run inside ``ask``
        — nor may a failing one block the question."""
        self.write(self.a, "src/app.txt", "v2\n")
        git(self.a, "commit", "--quiet", "-am", "local work")   # forces the twin path
        hooks = self.a / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "commit-msg", "pre-push", "post-commit"):
            hook = hooks / name
            hook.write_text("#!/bin/sh\necho ran >> \"$(git rev-parse --git-dir)/hook-ran\"\nexit 1\n")
            hook.chmod(0o755)
        _entry, report = self.ask(self.a)
        self.assertTrue(report.pushed)
        self.assertTrue(report.committed)
        self.assertTrue(report.twin)
        self.assertFalse((self.a / ".git" / "hook-ran").exists())


class TestPushWhenAhead(GitSyncTestCase):
    """brd §2.2 — the reference workspace's ``main`` was 26 commits ahead."""

    def setUp(self):
        super().setUp()
        for n in range(3):
            self.write(self.a, "src/app.txt", f"unpushed {n}\n")
            git(self.a, "commit", "--quiet", "-am", f"unpushed work {n}")
        self.local_only = git(self.a, "rev-parse", "HEAD")

    def test_only_the_queue_commit_is_published(self):
        before = self.remote_tip()
        entry, report = self.ask(self.a, "While ahead")

        self.assertTrue(report.pushed)
        self.assertTrue(report.twin)
        tip = self.remote_tip()
        self.assertEqual(git(self.remote, "rev-list", "--count", f"{before}..{tip}"), "1")
        self.assertEqual(self.files_in(self.remote, tip), [QUEUE])
        self.assertEqual(self.remote_file("src/app.txt"), "v1\n", "unpushed work must stay unpublished")
        self.assertNotIn(self.local_only, git(self.remote, "rev-list", "main").splitlines())

        # Locally: one more commit, touching one file, on top of the unpushed work.
        self.assertEqual(git(self.a, "rev-parse", "HEAD~1"), self.local_only)
        self.assertEqual(self.files_in(self.a, "HEAD"), [QUEUE])
        self.assertIn("## Q-002 — While ahead  [open]", self.read(self.a))
        self.assertCleanCheckout(self.a)

    def test_the_operators_later_rebase_is_clean_and_drops_the_twin(self):
        self.ask(self.a, "one")
        self.ask(self.a, "two")
        git(self.a, "pull", "--quiet", "--rebase")
        self.assertEqual(self.status(self.a), "")
        log = git(self.a, "log", "--format=%s", "origin/main..HEAD").splitlines()
        self.assertEqual(log, ["unpushed work 2", "unpushed work 1", "unpushed work 0"],
                         "the twins must drop out; only the real unpushed work remains")
        self.assertIn("## Q-003 — two  [open]", self.read(self.a))

    def test_dirty_and_staged_files_are_left_exactly_as_they_were(self):
        self.write(self.a, "src/app.txt", "dirty\n")
        self.write(self.a, "src/staged.txt", "staged\n")
        git(self.a, "add", "src/staged.txt")
        self.write(self.a, "notes.txt", "untracked\n")
        before = self.status(self.a)

        _entry, report = self.ask(self.a)
        self.assertTrue(report.pushed)
        self.assertEqual(self.status(self.a), before)
        self.assertEqual(self.files_in(self.a, "HEAD"), [QUEUE])
        self.assertEqual(self.files_in(self.remote, self.remote_tip()), [QUEUE])

    def test_an_earlier_uncommitted_edit_to_the_queue_file_travels_and_is_flagged(self):
        self.write(self.a, QUEUE, BASE_QUEUE + "\n## Q-002 — filed by hand  [open]\n")
        entry, report = self.ask(self.a, "tool")
        self.assertEqual(entry.qid, "Q-003")
        self.assertTrue(report.carried_local_changes)
        self.assertTrue(report.to_dict()["carried_local_changes"])
        self.assertIn("## Q-002 — filed by hand  [open]", self.remote_file())
        self.assertIn("## Q-003 — tool  [open]", self.remote_file())
        self.assertCleanCheckout(self.a)


# ── D6: two machines ──────────────────────────────────────────────────────────


class TestTwoMachines(GitSyncTestCase):

    def setUp(self):
        super().setUp()
        self.b = self.clone("b")

    def test_a_stale_checkout_allocates_above_the_upstream(self):
        self.assertEqual(self.ask(self.a, "from A")[0].qid, "Q-002")
        entry, report = self.ask(self.b, "from B")            # B has not fetched
        self.assertEqual(entry.qid, "Q-003")
        self.assertTrue(report.pushed)
        text = self.read(self.b)
        self.assertIn("## Q-002 — from A  [open]", text, "the upstream's entry must be integrated")
        self.assertIn("## Q-003 — from B  [open]", text)
        self.assertEqual(text, self.remote_file())
        self.assertCleanCheckout(self.b)
        # B was behind, so its local commit is a twin; a pull reconciles it.
        git(self.b, "pull", "--quiet", "--rebase")
        self.assertEqual(git(self.b, "rev-parse", "HEAD"), self.remote_tip())

    def test_a_lost_push_race_reallocates_and_leaves_nothing_behind(self):
        """The race the feature exists for: both machines fetch the same tip,
        both would allocate Q-002, one push loses."""
        calls = []

        def op(store):
            calls.append(store.next_id())
            if len(calls) == 1:
                # Between A's fetch and A's push, B asks and wins.
                self.assertEqual(self.ask(self.b, "B wins")[0].qid, "Q-002")
            return store.create_entry(title="A retries", role="hpl")

        store = self.store(self.a)
        entry, report = qg.run(store, op, summary=lambda e: f"ask {e.qid}")

        self.assertEqual(calls, ["Q-002", "Q-003"], "the id must be re-allocated, not reused")
        self.assertEqual(entry.qid, "Q-003")
        self.assertEqual(report.attempts, 2)
        self.assertTrue(report.pushed)
        text = self.read(self.a)
        self.assertEqual(text.count("## Q-002"), 1)
        self.assertIn("## Q-002 — B wins  [open]", text)
        self.assertIn("## Q-003 — A retries  [open]", text)
        self.assertEqual(text, self.remote_file())
        self.assertNotIn("A retries", self.remote_log()[1])
        self.assertCleanCheckout(self.a)

    def test_retries_exhausted_degrades_to_a_local_commit(self):
        self.write(self.a, ".claude/roles.toml", ROLES_TOML.format(
            sync="push", extra="\n[questions.git]\nretries = 1\n"))
        n = [0]

        def op(store):
            n[0] += 1
            if n[0] <= 2:                           # lose both allowed attempts
                self.ask(self.b, f"B again {n[0]}")
            return store.create_entry(title="A never wins", role="hpl")

        entry, report = qg.run(self.store(self.a), op, summary=lambda e: f"ask {e.qid}")
        self.assertFalse(report.pushed)
        self.assertTrue(report.committed, "the question is still committed locally")
        self.assertTrue(report.pending)
        self.assertIn("lost the push race 2 time(s)", report.reason)
        self.assertIn("A never wins", self.read(self.a))
        self.assertGreaterEqual(int(entry.qid.split("-")[1]), 4, "above everything the upstream had")
        self.assertTrue(qg.is_pending(self.store(self.a)))

    def test_local_unpushed_entry_and_upstream_entry_are_merged(self):
        """Both sides appended different entries: keep both, then add ours."""
        self.write(self.a, QUEUE, BASE_QUEUE + "\n## Q-050 — hand-filed on A  [open]\n\nBody.\n")
        git(self.a, "commit", "--quiet", "-am", "docs(questions): file Q-050 by hand")
        self.assertEqual(self.ask(self.b, "from B")[0].qid, "Q-002")

        entry, report = self.ask(self.a, "tool on A")
        self.assertEqual(entry.qid, "Q-051")
        self.assertTrue(report.pushed, report.reason)
        text = self.remote_file()
        for heading in ("## Q-050 — hand-filed on A", "## Q-002 — from B", "## Q-051 — tool on A"):
            self.assertEqual(text.count(heading), 1, heading)
        self.assertEqual(self.read(self.a), text)

    def test_the_same_id_on_both_sides_is_not_merged(self):
        """A collision that already happened cannot be merged away: say so,
        keep the question, allocate above both."""
        self.write(self.a, QUEUE, BASE_QUEUE + "\n## Q-002 — hand-filed on A  [open]\n")
        git(self.a, "commit", "--quiet", "-am", "docs(questions): file Q-002 by hand")
        self.assertEqual(self.ask(self.b, "from B")[0].qid, "Q-002")
        tip = self.remote_tip()

        entry, report = self.ask(self.a, "tool on A")
        self.assertFalse(report.pushed)
        self.assertTrue(report.committed)
        self.assertTrue(report.pending)
        self.assertIn(QUEUE, report.reason)
        self.assertIn("cannot be merged automatically", report.reason)
        self.assertIn("Q-002 was given to two different questions", report.reason,
                      "the reason must name the id a human has to deal with")
        self.assertEqual(entry.qid, "Q-003")
        self.assertEqual(self.remote_tip(), tip, "nothing may be pushed over a real collision")
        self.assertIn("## Q-003 — tool on A  [open]", self.read(self.a))

    def test_the_documented_way_out_of_an_id_collision(self):
        """docs/async-questions.md, "When the files cannot be merged": withdraw
        the local question, delete its entry, sync, ask again."""
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        entry, report = self.ask(self.a, "asked offline")
        self.assertEqual(entry.qid, "Q-002")
        self.assertFalse(report.pushed)
        self.assertEqual(self.ask(self.b, "from B")[0].qid, "Q-002")
        git(self.a, "remote", "set-url", "origin", str(self.remote))

        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pushed)
        self.assertIn("Q-002 was given to two different questions", report.reason)

        store = self.store(self.a)
        _result, report = qg.run(
            store, lambda s: s.withdraw("Q-002", "id collision; asked again", withdrawn_by="tester"),
            summary="withdraw Q-002",
        )
        self.assertFalse(report.pushed, "still unmergeable: written locally, said so")
        text = self.read(self.a)
        start = text.index("## Q-002 — asked offline")
        self.write(self.a, QUEUE, text[:start].rstrip("\n") + "\n")

        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pending, report.reason)
        self.assertFalse(qg.is_pending(self.store(self.a)))
        self.assertIn("## Q-002 — from B  [open]", self.read(self.a))
        self.assertNotIn("asked offline", self.read(self.a))

        entry, report = self.ask(self.a, "asked again")
        self.assertEqual(entry.qid, "Q-003")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(self.read(self.a), self.remote_file())
        self.assertNotIn("asked offline", self.remote_file())


# ── D9: every degraded state ──────────────────────────────────────────────────


class TestDegradedStates(GitSyncTestCase):

    def assertWrittenLocally(self, repo: Path, entry, report, *, committed: bool, reason: str):
        self.assertIn(f"## {entry.qid} — ", self.read(repo), "the question must never be lost")
        self.assertFalse(report.pushed)
        self.assertEqual(report.committed, committed, report.reason)
        self.assertTrue(report.pending)
        self.assertIn(reason, report.reason)
        self.assertTrue(qg.is_pending(self.store(repo)))
        self.assertEqual(qg.read_state(self.store(repo))["pending_reason"], report.reason)

    def test_network_down(self):
        tip = self.remote_tip()
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=True, reason="fetch failed")
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.files_in(self.a, "HEAD"), [QUEUE])

    def test_network_down_still_allocates_above_the_last_known_upstream(self):
        b = self.clone("b")
        self.ask(b, "from B")                       # upstream now has Q-002
        git(self.a, "fetch", "--quiet")             # A knows, but its files are stale
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        entry, _report = self.ask(self.a)
        self.assertEqual(entry.qid, "Q-003")

    def test_detached_head(self):
        git(self.a, "checkout", "--quiet", "--detach")
        head = git(self.a, "rev-parse", "HEAD")
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=False, reason="detached HEAD")
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), head)
        # Written, and left as an ordinary unstaged modification.
        self.assertEqual(git(self.a, "diff", "--name-only").splitlines(), [QUEUE])
        self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "")

    def test_primary_checkout_on_another_branch(self):
        self.write(self.a, ".claude/roles.toml", ROLES_TOML.format(
            sync="push", extra='\n[questions.git]\nbranch = "main"\n'))
        git(self.a, "commit", "--quiet", "-am", "config")
        git(self.a, "push", "--quiet")
        git(self.a, "checkout", "--quiet", "-b", "feature/x")
        head = git(self.a, "rev-parse", "HEAD")
        tip = self.remote_tip()

        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=False, reason="'feature/x'")
        self.assertIn("'main'", report.reason)
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), head, "no commit may land on the feature branch")
        self.assertEqual(self.remote_tip(), tip)

    def test_merge_in_progress(self):
        (self.a / ".git" / "MERGE_HEAD").write_text(git(self.a, "rev-parse", "HEAD") + "\n")
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=False, reason="a merge is in progress")

    def test_rebase_in_progress(self):
        (self.a / ".git" / "rebase-merge").mkdir()
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=False, reason="a rebase is in progress")

    def test_index_locked_by_another_git_process(self):
        (self.a / ".git" / "index.lock").write_text("")
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=False, reason="index.lock")

    def test_no_remote(self):
        git(self.a, "remote", "remove", "origin")
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=True, reason="no upstream")

    def test_upstream_branch_missing(self):
        self.write(self.a, ".claude/roles.toml", ROLES_TOML.format(
            sync="push", extra='\n[questions.git]\nbranch = "trunk"\n'))
        git(self.a, "checkout", "--quiet", "-b", "trunk")
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=True, reason="does not exist on 'origin'")

    def test_push_refused_by_the_remote(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n")
        hook.chmod(0o755)
        entry, report = self.ask(self.a)
        self.assertWrittenLocally(self.a, entry, report, committed=True, reason="push refused")
        self.assertEqual(report.attempts, 1, "a refusal is not a race; it must not be retried")

    def test_not_a_git_work_tree(self):
        plain = self.tmp / "plain"
        self.write(plain, ".claude/roles.toml", ROLES_TOML.format(sync="push", extra=""))
        entry, report = self.ask(plain)
        self.assertIn("## Q-001 — ", self.read(plain))
        self.assertFalse(report.committed)
        self.assertFalse(report.pushed)
        self.assertIn("not a git work tree", report.reason)

    def test_a_git_failure_never_raises(self):
        store = self.store(self.a)
        with patch.object(qg._Git, "raw", side_effect=qg.GitError("boom")):
            entry, report = qg.run(store, lambda s: s.create_entry(title="t", role="hpl"))
        self.assertIn(f"## {entry.qid} — t", self.read(self.a))
        self.assertFalse(report.pushed)

    def test_a_store_error_from_the_mutation_propagates(self):
        with self.assertRaises(qs.QuestionsStoreError):
            qg.run(self.store(self.a), lambda s: s.create_entry(title="t", role="hpl", qid="Q-999"))
        self.assertEqual(self.status(self.a), "")


# ── D9 / D12: what is owed gets paid ──────────────────────────────────────────


class TestPendingRetry(GitSyncTestCase):

    def test_sync_pushes_what_a_dead_network_left_behind(self):
        url = git(self.a, "remote", "get-url", "origin")
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.ask(self.a, "offline one")
        self.ask(self.a, "offline two")
        self.assertTrue(qg.is_pending(self.store(self.a)))

        git(self.a, "remote", "set-url", "origin", url)
        report = qg.sync(self.store(self.a))
        self.assertTrue(report.pushed, report.reason)
        self.assertFalse(report.pending)
        self.assertFalse(qg.is_pending(self.store(self.a)))
        self.assertIn("## Q-002 — offline one", self.remote_file())
        self.assertIn("## Q-003 — offline two", self.remote_file())
        self.assertEqual(self.remote_log()[0], "chore(questions): sync queue files")
        self.assertEqual(self.files_in(self.remote, self.remote_tip()), [QUEUE])

    def test_a_burst_during_an_outage_waits_for_the_network_only_once(self):
        """After a fetch has failed, the remote is not tried again for
        NETWORK_COOLDOWN_S: the second ask of a burst is written at once."""
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        _e1, first = self.ask(self.a, "one")
        self.assertIn("fetch failed", first.reason)

        seen = []
        real = qg._Git.raw

        def raw(git_self, *args, **kwargs):
            seen.append(args)
            return real(git_self, *args, **kwargs)

        with patch.object(qg._Git, "raw", raw):
            entry, second = self.ask(self.a, "two")
        self.assertFalse([a for a in seen if "fetch" in a or "push" in a], "no network call")
        self.assertIn("push deferred: the remote was unreachable", second.reason)
        self.assertTrue(second.committed)
        self.assertTrue(second.pending)
        self.assertIn(f"## {entry.qid} — two", self.read(self.a))

    def test_the_next_ordinary_write_also_pays_the_debt(self):
        url = git(self.a, "remote", "get-url", "origin")
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.ask(self.a, "offline")
        git(self.a, "remote", "set-url", "origin", url)
        with patch.object(qg, "NETWORK_COOLDOWN_S", 0.0):     # the outage is over
            _entry, report = self.ask(self.a, "online")
        self.assertTrue(report.pushed)
        self.assertFalse(qg.is_pending(self.store(self.a)))
        self.assertIn("## Q-002 — offline", self.remote_file())
        self.assertIn("## Q-003 — online", self.remote_file())

    def test_sync_commits_an_entry_left_uncommitted_by_a_blocked_checkout(self):
        (self.a / ".git" / "MERGE_HEAD").write_text(git(self.a, "rev-parse", "HEAD") + "\n")
        self.ask(self.a, "during a merge")
        (self.a / ".git" / "MERGE_HEAD").unlink()
        report = qg.sync(self.store(self.a))
        self.assertTrue(report.pushed, report.reason)
        self.assertTrue(report.committed)
        self.assertEqual(self.status(self.a), "")
        self.assertIn("## Q-002 — during a merge", self.remote_file())

    def test_sync_with_nothing_owed_is_a_no_op(self):
        tip = self.remote_tip()
        report = qg.sync(self.store(self.a))
        self.assertFalse(report.changed)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), tip)


# ── commit mode ───────────────────────────────────────────────────────────────


class TestCommitMode(GitSyncTestCase):
    sync = "commit"

    def test_commits_locally_and_never_talks_to_the_remote(self):
        tip = self.remote_tip()
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))   # would fail if touched
        entry, report = self.ask(self.a, "local only")
        self.assertTrue(report.committed)
        self.assertFalse(report.pushed)
        self.assertFalse(report.pending)
        self.assertEqual(report.reason, "")
        self.assertEqual(self.files_in(self.a, "HEAD"), [QUEUE])
        self.assertEqual(git(self.a, "log", "-1", "--format=%s"), "chore(questions): ask Q-002")
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.status(self.a), "")


# ── what the engine refuses to publish (brd D7, enforced here) ───────────────


class TestOnlyQueueFiles(GitSyncTestCase):

    def test_a_symlinked_queue_file_is_never_synced(self):
        """A link's blob is its target path.  Reading that as the queue's
        content, appending to it and pushing it overwrote the link's *target*
        upstream with garbage — found by review, reproduced, now refused."""
        self.write(self.a, "docs/real-queue.md", BASE_QUEUE)
        (self.a / QUEUE).unlink()
        os.symlink("../real-queue.md", self.a / QUEUE)
        git(self.a, "add", "-A")
        git(self.a, "commit", "--quiet", "-m", "queue file is a symlink")
        git(self.a, "push", "--quiet")
        tip = self.remote_tip()

        entry, report = self.ask(self.a, "through a link")
        self.assertFalse(report.pushed)
        self.assertFalse(report.committed)
        self.assertIn("symbolic link", report.reason)
        self.assertEqual(self.remote_tip(), tip, "nothing may be pushed")
        self.assertEqual(self.remote_file("docs/real-queue.md"), BASE_QUEUE)
        self.assertEqual(entry.qid, "Q-002", "the real content was read, so the id is right")

    def test_an_upstream_symlink_is_not_read_as_a_queue_file(self):
        b = self.clone("b")
        self.write(b, "docs/elsewhere.md", "## Q-900 — far away  [open]\n")
        os.symlink("../elsewhere.md", b / "docs/questions/for-designer.md")
        git(b, "add", "-A")
        git(b, "commit", "--quiet", "-m", "a link that matches the queue glob")
        git(b, "push", "--quiet")

        entry, report = self.ask(self.a, "after the link appeared")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(entry.qid, "Q-002", "a link's target path is not queue content")
        self.assertFalse((self.a / "docs/questions/for-designer.md").exists(),
                         "the link must not be materialized as a file")

    def test_a_path_outside_the_queue_set_is_written_but_never_committed_or_pushed(self):
        """The index hands ``apply_answer`` a rel_path.  Whatever it says, the
        engine pushes queue files only."""
        self.write(self.a, "src/design.md", "## Q-001 — not a queue  [open]\n")
        git(self.a, "add", "-A")
        git(self.a, "commit", "--quiet", "-m", "a document that looks like a queue")
        git(self.a, "push", "--quiet")
        tip = self.remote_tip()
        head = git(self.a, "rev-parse", "HEAD")

        result, report = qg.run(
            self.store(self.a),
            lambda s: s.apply_answer("Q-001", "yes", "@hpl", 77, rel_path="src/design.md"),
            summary="answer Q-001",
        )
        self.assertIs(result, qs.ApplyResult.APPLIED, "the store did what it was asked")
        self.assertFalse(report.pushed)
        self.assertFalse(report.committed)
        self.assertIn("src/design.md is not one of this workspace's queue files", report.reason)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), head)

    def test_a_wide_glob_does_not_make_sync_publish_stray_files(self):
        self.write(self.a, ".claude/roles.toml", ROLES_TOML.format(
            sync="push", extra='\n[questions.format]\nglob = "*"\n'))
        git(self.a, "commit", "--quiet", "-am", "wide glob")
        git(self.a, "push", "--quiet")
        self.write(self.a, "docs/questions/notes.txt", "private notes\n")
        self.write(self.a, "docs/questions/README.md", "# How we use this folder\n")
        self.write(self.a, "docs/questions/.lock", "")
        self.write(self.a, "docs/questions/.for-x.md.abc.tmp", "half a write\n")
        self.write(self.a, "docs/questions/for-designer.md", "## Q-050 — a real queue file  [open]\n")

        report = qg.sync(self.store(self.a))
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(self.files_in(self.remote, self.remote_tip()),
                         ["docs/questions/for-designer.md"],
                         "only a file that parses as a queue is the tool's to publish")

    def test_a_gitignored_queue_is_refused_with_a_reason(self):
        self.write(self.a, ".gitignore", "docs/questions/claims.md\n")
        git(self.a, "add", ".gitignore")
        git(self.a, "commit", "--quiet", "-m", "ignore the ledger")
        git(self.a, "push", "--quiet")
        tip = self.remote_tip()
        ids, report = qg.run(self.store(self.a), lambda s: s.claim())
        self.assertEqual(ids, ["Q-002"])
        self.assertFalse(report.pushed)
        self.assertIn("ignored by git", report.reason)
        self.assertEqual(self.remote_tip(), tip)


# ── integration: the entry-level merge ────────────────────────────────────────


class TestIntegration(GitSyncTestCase):

    def setUp(self):
        super().setUp()
        self.ask(self.a, "shared question")                       # Q-002, pushed
        self.b = self.clone("b")

    def _answer(self, repo, message_id, text):
        return qg.run(
            self.store(repo),
            lambda s: s.apply_answer("Q-002", text, "@hpl", message_id, role="hpl"),
            summary=f"answer Q-002 (relay #{message_id})",
        )

    def test_two_answers_to_one_entry_are_never_merged_by_line(self):
        """The defect a line merge had: two machines answer the same entry;
        ``git merge-file --union`` produced one unclosed fence with the other
        answer's marker inside it — a file that parsed, and was pushed."""
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self._answer(self.a, 11, "A says yes")                     # offline on A
        self._answer(self.b, 12, "B says no")                      # pushed by B
        git(self.a, "remote", "set-url", "origin", str(self.remote))
        upstream = self.remote_file()

        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pushed)
        self.assertTrue(report.pending)
        self.assertIn("cannot be merged automatically", report.reason)
        self.assertEqual(self.remote_file(), upstream, "the upstream copy is untouched")
        text = self.read(self.a)
        self.assertIn("A says yes", text, "the local answer is still there, intact")
        self.assertEqual(text.count("```text"), 1)
        self.assertEqual(text.count("<!-- answer:"), 1)

    def test_an_upstream_answer_and_a_local_question_merge_cleanly(self):
        """Different entries changed on the two sides: both survive whole."""
        self._answer(self.b, 12, "B says no")
        self.write(self.a, QUEUE, self.read(self.a) + "\n## Q-050 — hand-filed on A  [open]\n\nBody.\n")
        git(self.a, "commit", "--quiet", "-am", "docs(questions): file Q-050 by hand")

        entry, report = self.ask(self.a, "after both")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(entry.qid, "Q-051")
        text = self.remote_file()
        self.assertEqual(text, self.read(self.a))
        self.assertIn("## Q-002 — shared question  [resolved]", text)
        self.assertEqual(text.count("<!-- answer:12 -->"), 1)
        self.assertEqual(text.count("```"), 2, "exactly one fence, opened and closed")
        for heading in ("## Q-050 — hand-filed on A", "## Q-051 — after both"):
            self.assertEqual(text.count(heading), 1)
        self.assertLess(text.index("## Q-002"), text.index("## Q-050"))

    def test_the_recorded_base_makes_a_second_upstream_change_a_plain_update(self):
        """After a twin commit git's own merge base stops advancing.  Without
        the recorded sync base, an entry this machine pushed once and the
        upstream then changed again would look "changed on both sides"."""
        self.write(self.a, "src/app.txt", "local work\n")
        git(self.a, "commit", "--quiet", "-am", "unpushed work")          # A is ahead: twins from now on
        _r, report = qg.run(self.store(self.a), lambda s: s.mark_dispatched("Q-002", 4187, role="hpl"),
                            summary="dispatched Q-002")
        self.assertTrue(report.twin)
        state = qg.read_state(self.store(self.a))
        self.assertEqual(state["upstream"], "origin/main")
        self.assertEqual(state["base"][QUEUE], git(self.remote, "rev-parse", f"main:{QUEUE}"))

        git(self.b, "pull", "--quiet")
        self._answer(self.b, 4187, "B answers")                            # upstream changes Q-002 again

        entry, report = self.ask(self.a, "next")
        self.assertTrue(report.pushed, report.reason)
        text = self.read(self.a)
        self.assertIn("## Q-002 — shared question  [resolved]", text)
        self.assertEqual(text.count("<!-- answer:4187 -->"), 1)
        self.assertEqual(text.count("**Dispatched:**"), 1)
        self.assertEqual(text, self.remote_file())

    def test_local_file_untouched_since_the_last_sync_just_takes_the_upstream(self):
        self._answer(self.b, 12, "B says no")
        before = self.read(self.a)
        _ids, report = qg.run(self.store(self.a), lambda s: s.claim())     # touches only the ledger
        self.assertTrue(report.pushed)
        self.assertNotEqual(self.read(self.a), before)
        self.assertEqual(self.read(self.a), self.remote_file())
        self.assertEqual(self.files_in(self.remote, self.remote_tip()), ["docs/questions/claims.md"])

    def test_upstream_ids_in_answered_raise_the_floor(self):
        self.write(self.b, "docs/questions/answered/Q-777.md", "archived\n")
        git(self.b, "add", "-A")
        git(self.b, "commit", "--quiet", "-m", "archive")
        git(self.b, "push", "--quiet")
        self.assertEqual(self.ask(self.a, "after the archive")[0].qid, "Q-778")

    def test_the_same_id_claimed_on_two_machines_is_a_collision(self):
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        qg.run(self.store(self.a), lambda s: s.claim(note="A"))            # Q-003, local only
        qg.run(self.store(self.b), lambda s: s.claim(note="B"))            # Q-003, pushed
        git(self.a, "remote", "set-url", "origin", str(self.remote))
        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pushed)
        self.assertIn("claims.md", report.reason)
        self.assertIn("cannot be merged automatically", report.reason)


class TestMergeFunctions(unittest.TestCase):
    """The merge, without a repository — every rule of architecture §4.3."""

    FMT = qs.QuestionsFormat()
    BASE = "# Q\n\n## Q-001 — a  [open]\n\nBody a.\n\n## Q-002 — b  [open]\n\nBody b.\n"

    def merge(self, ours, theirs, base=BASE):
        return qg.merge_queue_text(ours, base, theirs, self.FMT)

    def test_only_one_side_changed_an_entry(self):
        theirs = self.BASE.replace("Body a.", "Body a, edited upstream.")
        ours = self.BASE.replace("Body b.", "Body b, edited here.")
        merged = self.merge(ours, theirs)
        self.assertIn("Body a, edited upstream.", merged)
        self.assertIn("Body b, edited here.", merged)

    def test_both_sides_changed_the_same_entry_is_refused(self):
        self.assertIsNone(self.merge(self.BASE.replace("Body a.", "one"), self.BASE.replace("Body a.", "two")))

    def test_identical_changes_on_both_sides_are_fine(self):
        same = self.BASE.replace("[open]", "[resolved]", 1)
        self.assertEqual(self.merge(same, same), same)

    # ── the later state of one entry (review round 3) ──

    ASKED = "## Q-005 — c  [open]\n\nBody c.\n"
    DISPATCHED = "## Q-005 — c  [open]\n\nBody c.\n\n**Dispatched:** relay #7\n"
    ANSWERED = ("## Q-005 — c  [resolved]\n\nBody c.\n\n**Dispatched:** relay #7\n\n"
                "<!-- answer:7 -->\n> yes\n")

    def test_an_entry_new_on_both_sides_takes_its_later_state(self):
        """Neither side's version is in the base — a twin carries the other
        machine's question as it was; the upstream has since answered it."""
        for earlier, later in ((self.ASKED, self.DISPATCHED), (self.DISPATCHED, self.ANSWERED),
                               (self.ASKED, self.ANSWERED)):
            for ours, theirs in ((earlier, later), (later, earlier)):
                merged = self.merge(self.BASE + "\n" + ours, self.BASE + "\n" + theirs)
                self.assertIsNotNone(merged, (ours, theirs))
                self.assertEqual(merged.count("## Q-005"), 1)
                self.assertIn(later.strip(), merged)
                # …and with no base at all.
                self.assertIsNotNone(self.merge(self.BASE + "\n" + ours, self.BASE + "\n" + theirs, None))

    def test_two_different_answers_are_not_one_history(self):
        other = self.ANSWERED.replace("> yes", "> no")
        self.assertIsNone(self.merge(self.BASE + "\n" + self.ANSWERED, self.BASE + "\n" + other))

    def test_two_questions_under_one_id_are_not_one_history(self):
        other = self.ASKED.replace("— c", "— something else")
        self.assertIsNone(self.merge(self.BASE + "\n" + self.ASKED, self.BASE + "\n" + other))

    def test_a_closed_entry_never_reopens_because_the_open_one_is_longer(self):
        """Closed here, added to (still open) there: that is two changes."""
        closed = self.ASKED.replace("[open]", "[resolved]")
        longer = self.ASKED + "\nA note added while it was open.\n"
        self.assertIsNone(self.merge(self.BASE + "\n" + closed, self.BASE + "\n" + longer))
        self.assertIsNone(self.merge(self.BASE + "\n" + longer, self.BASE + "\n" + closed))

    def test_only_the_status_differs_the_closed_one_stands(self):
        closed = self.ASKED.replace("[open]", "[withdrawn]")
        merged = self.merge(self.BASE + "\n" + self.ASKED, self.BASE + "\n" + closed)
        self.assertIn("## Q-005 — c  [withdrawn]", merged)
        merged = self.merge(self.BASE + "\n" + closed, self.BASE + "\n" + self.ASKED)
        self.assertIn("## Q-005 — c  [withdrawn]", merged)

    def test_two_different_closures_are_for_a_human(self):
        """Resolved on one side, withdrawn on the other: even when one body
        happens to contain the other's lines, neither is the other's later
        state (review round 4)."""
        resolved = self.ASKED.replace("[open]", "[resolved]")
        withdrawn = self.ASKED.replace("[open]", "[withdrawn]") + "\n<!-- withdrawn -->\n> moot\n"
        self.assertIsNone(self.merge(self.BASE + "\n" + resolved, self.BASE + "\n" + withdrawn))
        self.assertIsNone(self.merge(self.BASE + "\n" + withdrawn, self.BASE + "\n" + resolved))
        # A withdrawal and then a late answer is one history, same word.
        late = withdrawn + "\n<!-- answer:7 -->\n**Late answer**\n> yes\n"
        merged = self.merge(self.BASE + "\n" + withdrawn, self.BASE + "\n" + late)
        self.assertIn("**Late answer**", merged)

    def test_a_deleted_line_is_an_edit_not_an_earlier_state(self):
        """Review round 5.  The operator deletes a line from an open entry;
        the other machine answers it.  The shorter version is contained in the
        answered one — but it is not an earlier state, the base says so, and
        taking the answered one whole would quietly put the line back."""
        base = self.BASE + "\n## Q-005 — c  [open]\n\nLine one.\nLine two.\nLine three.\n"
        trimmed = base.replace("Line two.\n", "")
        answered = base.replace("## Q-005 — c  [open]", "## Q-005 — c  [resolved]") \
            + "\n<!-- answer:7 -->\n> yes\n"
        self.assertIsNone(self.merge(trimmed, answered, base))
        self.assertIsNone(self.merge(answered, trimmed, base))

    def test_the_base_still_decides_when_it_can(self):
        """A hand edit that *shortens* an entry upstream is theirs to make:
        the base says only they changed it, and the longer local copy must
        not win just for being longer."""
        base = self.BASE + "\n" + self.DISPATCHED
        trimmed = self.BASE + "\n" + self.ASKED
        self.assertEqual(self.merge(base, trimmed, base).count("**Dispatched:**"), 0)

    def test_new_entries_on_both_sides_are_all_kept_once(self):
        ours = self.BASE + "\n## Q-010 — mine  [open]\n"
        theirs = self.BASE + "\n## Q-020 — theirs  [open]\n\nT body.\n"
        merged = self.merge(ours, theirs)
        keys = [e.qid for e in qs.parse_entries(merged.split("\n"), self.FMT)]
        self.assertEqual(keys, ["Q-001", "Q-002", "Q-010", "Q-020"])
        self.assertTrue(merged.startswith(ours.rstrip("\n")), "ours is kept byte for byte")
        self.assertTrue(merged.endswith("T body.\n"))

    def test_the_same_new_id_on_both_sides_is_refused(self):
        ours = self.BASE + "\n## Q-010 — mine  [open]\n"
        theirs = self.BASE + "\n## Q-010 — theirs  [open]\n"
        self.assertIsNone(self.merge(ours, theirs))

    def test_a_deletion_is_never_decided_automatically(self):
        without_b = "# Q\n\n## Q-001 — a  [open]\n\nBody a.\n"
        changed = self.BASE.replace("Body a.", "Body a2.")
        self.assertIsNone(self.merge(changed, without_b))      # deleted upstream
        self.assertIsNone(self.merge(without_b, changed))      # deleted here

    def test_an_unknown_base_only_merges_what_is_unambiguous(self):
        ours = self.BASE + "\n## Q-010 — mine  [open]\n"
        theirs = self.BASE + "\n## Q-020 — theirs  [open]\n"
        merged = qg.merge_queue_text(ours, None, theirs, self.FMT)
        self.assertEqual(len(qs.parse_entries(merged.split("\n"), self.FMT)), 4)
        self.assertIsNone(qg.merge_queue_text(
            self.BASE.replace("Body a.", "x"), None, self.BASE, self.FMT))

    def test_a_file_with_a_duplicated_id_is_not_merged(self):
        dup = self.BASE + "\n## Q-001 — again  [open]\n"
        self.assertIsNone(self.merge(dup, self.BASE + "\n## Q-030 — new  [open]\n"))

    def test_text_between_entries_travels_with_the_entry_before_it(self):
        ours = self.BASE.replace("Body a.\n", "Body a.\n\n## Not an entry\n\nProse.\n")
        theirs = self.BASE + "\n## Q-020 — theirs  [open]\n"
        merged = qg.merge_queue_text(ours, ours, theirs.replace("Body a.\n", "Body a.\n\n## Not an entry\n\nProse.\n"), self.FMT)
        self.assertIn("## Not an entry\n\nProse.\n", merged)
        self.assertEqual(merged.count("## Not an entry"), 1)

    def test_crlf_files_stay_crlf(self):
        base = self.BASE.replace("\n", "\r\n")
        ours = base + "\r\n## Q-010 — mine  [open]\r\n"
        theirs = base.replace("Body a.", "Body a, upstream.")
        merged = self.merge(ours, theirs, base)
        self.assertIn("Body a, upstream.\r\n", merged)
        self.assertNotIn("\n", merged.replace("\r\n", ""))
        # An entry appended from the upstream gets a blank line in the file's
        # own line ending, not a bare LF.
        theirs = base + "\r\n## Q-011 — theirs  [open]\r\n"
        merged = self.merge(ours, theirs, base)
        self.assertIn("## Q-011 — theirs", merged)
        self.assertNotIn("\n", merged.replace("\r\n", ""))
        # …also when the local file has no final newline.
        merged = self.merge(ours[:-2], theirs, base)
        self.assertIn("mine  [open]\r\n\r\n## Q-011", merged)

    def test_ledger_union_prune_and_collision(self):
        claim_re = qs.QuestionsStore(qs.QuestionsConfig(), Path("/nonexistent")).claim_line_re()
        base = "# Claims\n\n- Q-001 · t · a\n- Q-002 · t · a\n"
        ours = base + "- Q-003 · t · mine\n"
        theirs = "# Claims\n\n- Q-001 · t · a\n- Q-004 · t · theirs\n"        # pruned Q-002, added Q-004
        merged = qg.merge_ledger_text(ours, base, theirs, claim_re)
        self.assertEqual(
            [l for l in merged.split("\n") if l.startswith("- ")],
            ["- Q-001 · t · a", "- Q-003 · t · mine", "- Q-004 · t · theirs"],
        )
        self.assertIsNone(qg.merge_ledger_text(
            base + "- Q-003 · t · mine\n", base, base + "- Q-003 · t · theirs\n", claim_re))


# ── what happens when the local half fails after the remote accepted ─────────


class TestAfterTheRemoteAccepted(GitSyncTestCase):

    def test_a_disk_error_keeps_the_result_and_records_the_debt(self):
        """The push is in.  A full disk now must not cost the caller the id:
        ``ask`` still has to send the question."""
        store = self.store(self.a)
        with patch.object(qs.QuestionsStore, "materialize", side_effect=OSError(28, "No space left on device")):
            entry, report = qg.run(store, lambda s: s.create_entry(title="t", role="hpl"),
                                   summary=lambda e: f"ask {e.qid}")
        self.assertEqual(entry.qid, "Q-002")
        self.assertTrue(report.pushed)
        self.assertFalse(report.committed)
        self.assertTrue(report.pending)
        self.assertIn("the local write failed", report.reason)
        self.assertIn("## Q-002 — t", self.remote_file())
        self.assertNotIn("## Q-002 — t", self.read(self.a), "the local write is what failed")
        self.assertTrue(qg.is_pending(self.store(self.a)))
        # Paid later: the upstream copy comes down, the branch catches up — and
        # the entry that was pushed is NOT pushed away again.  (It was, before
        # the sync base was tied to the local write: the stale local file
        # looked like "the upstream has not moved, ours wins".)
        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pending, report.reason)
        self.assertIn("## Q-002 — t", self.remote_file(), "the published entry must survive the retry")
        self.assertIn("## Q-002 — t", self.read(self.a))
        self.assertFalse(qg.is_pending(self.store(self.a)))
        self.assertEqual(self.status(self.a), "")

    def test_a_failed_local_commit_is_not_forgotten_when_the_repository_already_owed(self):
        """Review finding: ``_record_base`` cleared ``pending`` right after
        ``_finish_local`` had set it, whenever the repository was already
        pending — so the debt vanished from the state file."""
        self.write(self.a, "src/app.txt", "unpushed\n")
        git(self.a, "commit", "--quiet", "-am", "unpushed work")   # ahead → the twin path
        (self.a / ".git" / "MERGE_HEAD").write_text(git(self.a, "rev-parse", "HEAD") + "\n")
        self.ask(self.a, "during a merge")                         # → pending
        (self.a / ".git" / "MERGE_HEAD").unlink()
        self.assertTrue(qg.is_pending(self.store(self.a)))

        with patch.object(qg._Transaction, "_commit_only", side_effect=qg.GitError("index is locked")):
            _entry, report = self.ask(self.a, "push ok, commit fails")
        self.assertTrue(report.pushed)
        self.assertTrue(report.pending)
        self.assertTrue(qg.is_pending(self.store(self.a)), "the debt must still be on record")
        self.assertIn("local commit failed", qg.read_state(self.store(self.a))["pending_reason"])

    def test_a_push_that_timed_out_after_the_remote_took_it_counts_as_accepted(self):
        """Otherwise the local path would allocate the same id again."""
        real = qg._Transaction._push_commit

        def push_then_time_out(txn, git_, checkout, commit):
            verdict, _detail = real(txn, git_, checkout, commit)
            assert verdict == "ok"
            raise qg.GitError("git push timed out after 20s")

        with patch.object(qg._Transaction, "_push_commit", push_then_time_out):
            entry, report = self.ask(self.a, "slow link")
        self.assertTrue(report.pushed)
        self.assertEqual(entry.qid, "Q-002")
        self.assertEqual(self.read(self.a).count("## Q-002"), 1)
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), self.remote_tip())
        self.assertNotIn("in_flight", qg.read_state(self.store(self.a)))

    def test_a_corrupt_state_file_reads_as_owing(self):
        self.ask(self.a)
        (self.a / ".git" / qg.STATE_FILENAME).write_text("{not json", encoding="utf-8")
        self.assertTrue(qg.is_pending(self.store(self.a)))
        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pending)
        self.assertFalse(qg.is_pending(self.store(self.a)))


# ── budgets for callers that must not wait on the network ────────────────────


class TestBudgets(GitSyncTestCase):

    def _spy(self):
        """Record every git invocation the engine makes, with its timeout."""
        seen = []
        real = qg._Git.raw

        def raw(git_self, *args, **kwargs):
            seen.append((args, kwargs.get("timeout")))
            return real(git_self, *args, **kwargs)

        return seen, patch.object(qg._Git, "raw", raw)

    @staticmethod
    def _verbs(seen) -> list:
        verbs = []
        for args, _timeout in seen:
            plain = [a for a in args if not a.startswith("-") and "=" not in a]
            verbs.append(plain[0] if plain else "")
        return verbs

    def test_offline_writes_at_once_and_never_talks_to_the_remote(self):
        tip = self.remote_tip()
        seen, spy = self._spy()
        with spy:
            entry, report = qg.run(
                self.store(self.a), lambda s: s.create_entry(title="t", role="hpl"),
                summary=lambda e: f"ask {e.qid}", offline=True,
            )
        verbs = self._verbs(seen)
        self.assertNotIn("fetch", verbs)
        self.assertNotIn("push", verbs)
        self.assertIn("commit", verbs)
        self.assertEqual(self.remote_tip(), tip)
        self.assertTrue(report.committed)
        self.assertFalse(report.pushed)
        self.assertTrue(report.pending)
        self.assertIn("push deferred", report.reason)
        self.assertIn(f"## {entry.qid} — t", self.read(self.a))
        # …and the debt is paid by the next call that may use the network.
        self.assertTrue(qg.sync(self.store(self.a)).pushed)
        self.assertIn(f"## {entry.qid} — t", self.remote_file())

    def test_network_timeout_is_the_callers(self):
        seen, spy = self._spy()
        with spy:
            qg.run(
                self.store(self.a), lambda s: s.create_entry(title="t", role="hpl"),
                network_timeout=3,
            )
        timeouts = {
            verb: timeout for (args, timeout), verb in zip(seen, self._verbs(seen))
            if verb in ("fetch", "push")
        }
        self.assertEqual(timeouts, {"fetch": 3.0, "push": 3.0})

    def test_retries_is_the_callers(self):
        b = self.clone("b")
        calls = []

        def op(store):
            calls.append(1)
            if len(calls) == 1:
                self.ask(b, "B wins")
            return store.create_entry(title="A", role="hpl")

        _entry, report = qg.run(self.store(self.a), op, retries=0)
        self.assertEqual(report.attempts, 1)
        self.assertFalse(report.pushed)
        self.assertIn("lost the push race 1 time(s)", report.reason)

    def test_no_new_attempt_starts_once_the_time_budget_is_spent(self):
        b = self.clone("b")
        calls = []

        def op(store):
            calls.append(1)
            self.ask(b, f"B wins {len(calls)}")
            return store.create_entry(title="A", role="hpl")

        with patch.object(qg, "TOTAL_BUDGET_S", 0.0):
            _entry, report = qg.run(self.store(self.a), op, retries=5)
        self.assertEqual(report.attempts, 1, "one attempt, then the budget stops the loop")
        self.assertIn("ran out of time", report.reason)
        self.assertTrue(report.committed)

    def test_timeout_message_names_the_subcommand(self):
        self.assertEqual(qg._subcommand(("-c", "gc.auto=0", "fetch", "--quiet")), "fetch")
        self.assertEqual(qg._subcommand(("push", "--porcelain")), "push")

    def test_hint_lines_are_not_the_reason(self):
        raw = b"error: failed to push some refs\nhint: Updates were rejected\nhint: use git pull"
        self.assertEqual(qg._tail(raw), "error: failed to push some refs")

    def test_git_errors_never_carry_url_credentials(self):
        raw = b"fatal: unable to access 'https://deploy:s3cr3t-t0ken@git.example.com/x.git/': timeout"
        text = qg._tail(raw)
        self.assertNotIn("s3cr3t", text)
        self.assertNotIn("deploy", text)
        self.assertIn("https://…@git.example.com/x.git/", text)
        self.assertEqual(qg._tail(b"fatal: no route to git@github.com:org/repo.git"),
                         "fatal: no route to git@github.com:org/repo.git")


# ── D12: the listener commits what it writes; the shared ops sync too ─────────


# ── review round 2: races, pulls, stray glob matches, interrupted writes ─────


class TestConcurrentAsks(GitSyncTestCase):
    """Two checkouts asking at the same instant — the case the feature is for.

    A simultaneous ref update is not reported as "non-fast-forward": the loser
    is told ``[remote rejected] (failed to update ref)``.  Read as a refusal,
    it sent the question down the local path with the id the winner had just
    taken (review round 2, issue 1)."""

    def test_simultaneous_asks_never_share_an_id(self):
        import threading

        self.b = self.clone("b")
        for repo in (self.a, self.b):
            self.write(repo, ".claude/roles.toml", ROLES_TOML.format(
                sync="push", extra="\n[questions.git]\nretries = 10\n"))
        results: list = []
        errors: list = []
        start = threading.Barrier(2)

        def worker(repo: Path, name: str) -> None:
            try:
                start.wait(timeout=30)
                for n in range(4):
                    entry, report = self.ask(repo, f"{name} {n}")
                    results.append((entry.qid, report.pushed, report.reason))
            except Exception as exc:  # noqa: BLE001 — surfaced below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(repo, name))
                   for repo, name in ((self.a, "A"), (self.b, "B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=240)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        ids = [qid for qid, _pushed, _reason in results]
        self.assertEqual(len(set(ids)), 8, f"an id was handed out twice: {sorted(ids)}")
        self.assertEqual([r for r in results if not r[1]], [], "every ask must have been pushed")
        upstream = self.remote_file()
        for qid in ids:
            self.assertEqual(upstream.count(f"## {qid} "), 1, qid)
        for repo in (self.a, self.b):
            self.assertFalse(qg.is_pending(self.store(repo)))

    def test_a_simultaneous_update_is_a_lost_race_not_a_refusal(self):
        class _Proc:
            returncode = 1
            stdout = (b"To /remote.git\n"
                      b"!\tabc:refs/heads/main\t[remote rejected] (failed to update ref)\n"
                      b"Done\n")
            stderr = b"error: cannot lock ref 'refs/heads/main': is at 111 but expected 222\n"

        class _FakeGit:
            def raw(self, *args, **kwargs):
                return _Proc()

        txn = qg._Transaction.__new__(qg._Transaction)
        txn.network_timeout = 5.0
        checkout = type("C", (), {"remote": "origin", "sync_branch": "main"})()
        self.assertEqual(txn._push_commit(_FakeGit(), checkout, "abc")[0], "rejected")

        _Proc.stdout = b"!\tabc:refs/heads/main\t[remote rejected] (pre-receive hook declined)\n"
        _Proc.stderr = b""
        self.assertEqual(txn._push_commit(_FakeGit(), checkout, "abc")[0], "refused")

    def test_a_refusal_while_the_branch_moved_is_retried(self):
        """Whatever words the remote used: not accepted + the branch moved off
        the tip this attempt was built on = a race.  Re-allocate."""
        self.b = self.clone("b")
        real = qg._Transaction._push_commit
        calls = []

        def push(txn, git_, checkout, commit):
            calls.append(commit)
            if len(calls) == 1:
                self.assertEqual(self.ask(self.b, "B wins")[0].qid, "Q-002")
                return "refused", "[remote rejected] (some wording nobody listed)"
            return real(txn, git_, checkout, commit)

        with patch.object(qg._Transaction, "_push_commit", push):
            entry, report = self.ask(self.a, "A retries")
        self.assertEqual(entry.qid, "Q-003")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(report.attempts, 2)
        self.assertEqual(self.read(self.a), self.remote_file())

    def test_a_refusal_with_the_branch_where_it_was_is_still_a_refusal(self):
        with patch.object(qg._Transaction, "_push_commit",
                          lambda *a: ("refused", "[remote rejected] (protected branch)")):
            entry, report = self.ask(self.a, "kept locally")
        self.assertFalse(report.pushed)
        self.assertEqual(report.attempts, 1)
        self.assertIn("push refused", report.reason)
        self.assertIn("kept locally", self.read(self.a))


class TestPullAfterTwins(GitSyncTestCase):
    """The operator's own ``git pull`` after two machines asked in turn.

    A twin made while behind holds the upstream's entries *and* the new one;
    once the upstream moves again a line merge calls that a conflict.  The
    engine registers a merge driver for the queue files in the repository's
    local configuration, and the pull is clean (review round 2, issue 2)."""

    def setUp(self):
        super().setUp()
        self.b = self.clone("b")
        self.ask(self.a, "A one")            # fast-forward on A
        self.ask(self.b, "B one")            # B is behind: twin holds A's entry too
        self.ask(self.a, "A two")            # the upstream moves again

    def _entries(self, text: str) -> list:
        return [line for line in text.splitlines() if line.startswith("## Q-")]

    def test_a_plain_pull_is_clean(self):
        git(self.b, "pull", "--quiet", "--no-rebase", "--no-edit")
        self.assertEqual(self._entries(self.read(self.b)), self._entries(self.remote_file()))
        self.assertCleanCheckout(self.b)

    def test_a_rebasing_pull_is_clean_and_drops_the_twins(self):
        git(self.b, "pull", "--quiet", "--rebase")
        self.assertEqual(git(self.b, "rev-parse", "HEAD"), self.remote_tip())
        self.assertEqual(self.read(self.b), self.remote_file())

    def test_an_answer_twin_and_a_later_upstream_entry(self):
        store = self.store(self.b)
        qg.run(store, lambda s: s.apply_answer("Q-003", "yes", "tester", 7),
               summary="answer Q-003")
        self.ask(self.a, "A three")
        git(self.b, "pull", "--quiet", "--no-rebase", "--no-edit")
        self.assertEqual(self._entries(self.read(self.b)), self._entries(self.remote_file()))

    def _answer(self, repo: Path, qid: str, relay_id: int) -> None:
        _result, report = qg.run(
            self.store(repo), lambda s: s.apply_answer(qid, "yes", "tester", relay_id),
            summary=f"answer {qid}",
        )
        self.assertTrue(report.pushed, report.reason)

    def test_the_upstream_answered_an_entry_the_twin_carried(self):
        """Review round 3, H1.  B's twin holds A's Q-002 as it was — open.  A
        then answers it.  Neither version is in the merge base; the answered
        one is simply the open one's later state."""
        # B's twin (setUp) holds A's Q-002, open.  The upstream answers it,
        # B asks again, the upstream answers B's own Q-003 too.
        for flag in ("--no-rebase", "--rebase"):
            with self.subTest(flag):
                b = self.tmp / ("b-" + flag.strip("-"))
                shutil.copytree(self.b, b, symlinks=True)
                self._answer(self.a, "Q-002", 11) if flag == "--no-rebase" else None
                self.ask(b, "B two " + flag)
                self._answer(self.a, "Q-003", 12) if flag == "--no-rebase" else None
                git(b, "pull", "--quiet", flag, "--no-edit")
                self.assertEqual(self._entries(self.read(b)), self._entries(self.remote_file()))
                self.assertIn("<!-- answer:11 -->", self.read(b))
                self.assertIn("<!-- answer:12 -->", self.read(b))
                entry, report = self.ask(b, "B three " + flag)
                self.assertTrue(report.pushed, report.reason)

    def test_a_driver_that_fails_falls_back_to_gits_own_merge(self):
        """Review round 3, M2.  A CLI too old to know the flag (exit 2), or
        one that crashes, must not leave a file that looks resolved: the
        command falls back to git's merge, which writes markers."""
        failing = self.tmp / "old-cli"
        failing.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        failing.chmod(0o755)
        for cli in (str(failing), str(self.tmp / "no-such-cli")):
            with self.subTest(cli):
                c = self.clone("c-" + Path(cli).name)
                self.ask(self.a, "A before " + Path(cli).name)       # C is now behind
                with patch.object(qg, "MERGE_DRIVER_COMMAND", qg.merge_driver_command(cli)):
                    _entry, report = self.ask(c, "C one")            # a twin
                    self.assertTrue(report.twin)
                self.ask(self.a, "A again " + Path(cli).name)
                proc = subprocess.run(["git", "pull", "--no-rebase", "--no-edit"], cwd=str(c),
                                      capture_output=True, text=True)
                self.assertNotEqual(proc.returncode, 0, "a line merge of two tails conflicts")
                text = self.read(c)
                self.assertIn("<<<<<<<", text, "a failed driver must leave git's markers")
                self.assertIn("A again", text, "…and the upstream's side of them")

    def test_a_line_merge_that_duplicates_an_id_is_not_taken(self):
        """Review round 4.  git's own three-way merge of these three versions
        is "clean" — and ends with two entries headed Q-002.  The driver must
        merge by entry first, so the answered Q-002 is the only one."""
        base = "# Q\n\n## Q-001 — t1  [open]\n\n"
        upstream = (
            "# Q\n\n## Q-001 — t1  [withdrawn]\n\n"
            "## Q-002 — t2  [resolved]\n\n<!-- answer:9 -->\n> yes\n\n"
            "## Q-003 — t3  [resolved]\n\n<!-- answer:10 -->\n> ok\n"
        )
        twin = "# Q\n\n## Q-001 — t1  [open]\n\n## Q-002 — t2  [open]\n"
        repo = self.clone("fixture")
        qg.sync(self.store(repo))                    # registers the driver, publishes nothing
        self.write(repo, QUEUE, base)
        git(repo, "commit", "--quiet", "-am", "base")
        git(repo, "branch", "up")
        self.write(repo, QUEUE, twin)
        git(repo, "commit", "--quiet", "-am", "the twin")
        git(repo, "checkout", "--quiet", "up")
        self.write(repo, QUEUE, upstream)
        git(repo, "commit", "--quiet", "-am", "upstream")
        git(repo, "checkout", "--quiet", "main")

        git(repo, "merge", "--quiet", "--no-edit", "up")
        text = self.read(repo)
        self.assertEqual(text.count("## Q-002"), 1, text)
        self.assertIn("## Q-002 — t2  [resolved]", text)
        self.assertIn("<!-- answer:9 -->", text)
        self.assertIn("## Q-001 — t1  [withdrawn]", text)
        self.assertEqual(text.count("## Q-003"), 1)

    def test_a_hand_deletion_survives_the_other_machines_answer(self):
        """Review round 5, end to end: the entry merge declines, git's own
        line merge takes over, and the deleted line stays deleted."""
        base = "# Q\n\n## Q-001 — t1  [open]\n\nLine one.\nLine two.\nLine three.\n"
        repo = self.clone("deletion")
        qg.sync(self.store(repo))
        self.write(repo, QUEUE, base)
        git(repo, "commit", "--quiet", "-am", "base")
        git(repo, "branch", "up")
        self.write(repo, QUEUE, base.replace("Line two.\n", ""))
        git(repo, "commit", "--quiet", "-am", "the operator trims the question")
        git(repo, "checkout", "--quiet", "up")
        self.write(repo, QUEUE, base.replace("[open]", "[resolved]") + "\n<!-- answer:9 -->\n> yes\n")
        git(repo, "commit", "--quiet", "-am", "answered elsewhere")
        git(repo, "checkout", "--quiet", "main")
        git(repo, "merge", "--quiet", "--no-edit", "up")
        text = self.read(repo)
        self.assertNotIn("Line two.", text, "the deletion must not be undone")
        self.assertIn("## Q-001 — t1  [resolved]", text)
        self.assertIn("<!-- answer:9 -->", text)

    def test_a_cli_that_dies_is_not_mistaken_for_one_that_wrote_markers(self):
        """Review round 4.  An interpreter that dies exits 1 — the status a
        merge driver uses for "conflict markers written".  The CLI therefore
        answers with statuses of its own, and anything else falls back."""
        for name, status in (("dies", 1), ("lies", 0)):
            with self.subTest(name):
                cli = self.tmp / f"cli-{name}"
                cli.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
                cli.chmod(0o755)
                c = self.clone("c-" + name)
                self.ask(self.a, "A before " + name)
                with patch.object(qg, "MERGE_DRIVER_COMMAND", qg.merge_driver_command(str(cli))):
                    self.assertTrue(self.ask(c, "C one")[1].twin)
                self.ask(self.a, "A again " + name)
                proc = subprocess.run(["git", "pull", "--no-rebase", "--no-edit"], cwd=str(c),
                                      capture_output=True, text=True)
                self.assertNotEqual(proc.returncode, 0)
                text = self.read(c)
                self.assertIn("<<<<<<<", text)
                self.assertIn("A again", text)

    def test_the_tool_still_works_after_the_pull(self):
        git(self.b, "pull", "--quiet", "--no-rebase", "--no-edit")
        entry, report = self.ask(self.b, "B two")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(entry.qid, "Q-005")

    def test_a_real_conflict_is_still_a_conflict(self):
        """The driver merges what the tools would merge and nothing more."""
        for repo, word in ((self.a, "alpha"), (self.b, "beta")):
            git(repo, "pull", "--quiet", "--no-rebase", "--no-edit")
        for repo, word in ((self.a, "alpha"), (self.b, "beta")):
            self.write(repo, QUEUE, self.read(repo).replace("Body one.", f"Body {word}."))
            git(repo, "commit", "--quiet", "-am", f"hand edit {word}")
        git(self.a, "push", "--quiet", "origin", "main")
        proc = subprocess.run(["git", "pull", "--no-rebase", "--no-edit"], cwd=str(self.b),
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("<<<<<<<", self.read(self.b))

    def test_registration_is_local_idempotent_and_keeps_other_attributes(self):
        attributes = self.b / ".git" / "info" / "attributes"
        first = attributes.read_text(encoding="utf-8")
        self.assertIn(f"/{QUEUE} merge=claude-questions", first.splitlines())
        self.assertIn("/docs/questions/claims.md merge=claude-questions", first.splitlines())
        self.assertEqual(git(self.b, "config", "--local", "merge.claude-questions.driver"),
                         TEST_MERGE_DRIVER)
        self.assertEqual(git(self.b, "status", "--porcelain"), "", "nothing tracked is touched")

        attributes.write_text("*.png binary\n" + first, encoding="utf-8")
        state = self.b / ".git" / qg.STATE_FILENAME
        import json as _json
        data = _json.loads(state.read_text(encoding="utf-8"))
        data.pop("merge_driver")
        state.write_text(_json.dumps(data), encoding="utf-8")
        self.ask(self.b, "again")
        after = attributes.read_text(encoding="utf-8").splitlines()
        self.assertIn("*.png binary", after)
        self.assertEqual(sum("merge=claude-questions" in line for line in after),
                         sum("merge=claude-questions" in line for line in first.splitlines()))

    def test_merge_driver_false_registers_nothing(self):
        c = self.clone("c")
        self.write(c, ".claude/roles.toml", ROLES_TOML.format(
            sync="push", extra="\n[questions.git]\nmerge_driver = false\n"))
        self.ask(c, "C one")
        self.assertFalse((c / ".git" / "info" / "attributes").exists()
                         and "claude-questions" in (c / ".git" / "info" / "attributes").read_text())
        self.assertEqual(git(c, "config", "--local", "--get", "merge.claude-questions.driver",
                             check=False), "")


class TestGlobMatchesThatAreNotQueues(GitSyncTestCase):
    """Under ``glob = "*.md"`` a README beside the queue matched the glob and
    was merged, brought down and committed as if it were a queue file — and a
    README changed on both sides blocked every push (review round 2, issue 3)."""

    extra = '\n[questions.format]\nglob = "*.md"\n'
    README = "docs/questions/README.md"

    def setUp(self):
        super().setUp()
        self.write(self.a, self.README, "# How we use this folder\n")
        git(self.a, "add", "-A")
        git(self.a, "commit", "--quiet", "-m", "readme")
        git(self.a, "push", "--quiet")
        self.b = self.clone("b")
        self.write(self.b, self.README, "# How we use this folder\n\nUpstream edit.\n")
        git(self.b, "commit", "--quiet", "-am", "edit the readme")
        git(self.b, "push", "--quiet")

    def test_an_upstream_change_to_it_is_not_brought_into_the_question_commit(self):
        entry, report = self.ask(self.a, "from A")
        self.assertTrue(report.pushed, report.reason)
        self.assertFalse(report.carried_local_changes)
        self.assertEqual(self.read(self.a, self.README), "# How we use this folder\n")
        self.assertEqual(git(self.a, "show", "--name-only", "--format=", "HEAD").split(), [QUEUE])
        self.assertEqual(self.files_in(self.remote, self.remote_tip()), [QUEUE])

    def test_changed_on_both_sides_it_does_not_block_the_push(self):
        self.write(self.a, self.README, "# How we use this folder\n\nLocal edit, uncommitted.\n")
        entry, report = self.ask(self.a, "from A")
        self.assertTrue(report.pushed, report.reason)
        self.assertFalse(report.pending)
        self.assertIn("Local edit, uncommitted.", self.read(self.a, self.README))
        self.assertEqual(self.files_in(self.remote, self.remote_tip()), [QUEUE])


class TestInterruptedAfterThePush(GitSyncTestCase):
    """A process killed between "the remote accepted" and "the file is
    written" leaves the entry upstream and a marker in the state file.  The
    marker was written and never read (review round 2, issue 5)."""

    def _interrupt(self) -> None:
        """Push an entry from A exactly as the engine does, then die."""
        class _Killed(BaseException):
            pass

        def die(*args, **kwargs):
            raise _Killed()

        with patch.object(qg._Transaction, "_finish_local", die):
            with self.assertRaises(_Killed):
                self.ask(self.a, "pushed, never written")
        self.assertIn("pushed, never written", self.remote_file())
        self.assertNotIn("pushed, never written", self.read(self.a))

    def _age_marker(self, seconds: float) -> None:
        import json as _json
        from datetime import datetime, timedelta, timezone
        path = self.a / ".git" / qg.STATE_FILENAME
        data = _json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("in_flight", data, "the marker must have been written before the push")
        then = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        data["in_flight"]["since"] = then.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        path.write_text(_json.dumps(data), encoding="utf-8")

    def test_a_fresh_marker_is_a_live_transaction_an_old_one_is_a_debt(self):
        self._interrupt()
        self.assertFalse(qg.is_pending(self.store(self.a)),
                         "a marker seconds old may belong to a transaction still running")
        self._age_marker(qg.IN_FLIGHT_STALE_S + 60)
        self.assertTrue(qg.is_pending(self.store(self.a)))

    def test_sync_brings_the_entry_down_and_clears_the_marker(self):
        self._interrupt()
        self._age_marker(qg.IN_FLIGHT_STALE_S + 60)
        report = qg.sync(self.store(self.a))
        self.assertFalse(report.pending, report.reason)
        self.assertEqual(self.read(self.a), self.remote_file())
        self.assertFalse(qg.is_pending(self.store(self.a)))
        self.assertNotIn("in_flight", qg.read_state(self.store(self.a)))
        self.assertCleanCheckout(self.a)

    def test_a_healed_entry_can_then_be_answered_upstream(self):
        """Review round 3, H2.  Bringing an entry down recorded no base, so the
        upstream's next change to it looked like "two questions, one id" and
        every later write on this clone was refused."""
        self._interrupt()
        self._age_marker(qg.IN_FLIGHT_STALE_S + 60)
        self.assertFalse(qg.sync(self.store(self.a)).pending)

        b = self.clone("b")
        _result, report = qg.run(
            self.store(b), lambda s: s.apply_answer("Q-002", "yes", "tester", 9),
            summary="answer Q-002",
        )
        self.assertTrue(report.pushed, report.reason)

        entry, report = self.ask(self.a, "after the answer")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(self.read(self.a), self.remote_file())
        self.assertIn("<!-- answer:9 -->", self.read(self.a))

    def test_the_next_ask_allocates_above_it_and_heals_the_checkout(self):
        self._interrupt()
        entry, report = self.ask(self.a, "the next one")
        self.assertEqual(entry.qid, "Q-003")
        self.assertTrue(report.pushed, report.reason)
        self.assertEqual(self.read(self.a), self.remote_file())
        self.assertNotIn("in_flight", qg.read_state(self.store(self.a)))


class TestSyncThenUpstreamChange(GitSyncTestCase):
    """Review round 3, H2, the plain form: one clone only ever *receives* an
    entry (a sync brings it down), the other then changes it."""

    def test_a_brought_down_entry_changed_upstream_is_a_plain_update(self):
        b = self.clone("b")
        self.ask(self.a, "from A")
        report = qg.sync(self.store(b))
        self.assertFalse(report.pending, report.reason)
        self.assertIn("from A", self.read(b))
        state = qg.read_state(self.store(b))
        self.assertIn(QUEUE, state.get("base", {}), "what was brought down is the new base")

        qg.run(self.store(self.a), lambda s: s.apply_answer("Q-002", "yes", "tester", 5),
               summary="answer Q-002")
        report = qg.sync(self.store(b))
        self.assertFalse(report.pending, report.reason)
        self.assertEqual(self.read(b), self.remote_file())
        entry, report = self.ask(b, "from B")
        self.assertTrue(report.pushed, report.reason)


class TestThreeMachinesAtRandom(GitSyncTestCase):
    """Three checkouts doing what real ones do, in a seeded random order:
    ask, answer, withdraw, claim, sync, and the operator's own pull (merge or
    rebase).  Whatever the order: every pull is clean, no id is used twice,
    nothing asked is lost, nothing answered or withdrawn reopens, and once
    everyone has synced and pulled, every checkout reads as the upstream does.

    Review round 3 ran this shape by hand and found 25 failing seeds in 60."""

    SEEDS = range(8)
    STEPS = 28

    def _run(self, seed: int) -> None:
        import random
        rng = random.Random(seed)
        repos = [self.a, self.clone(f"b{seed}"), self.clone(f"c{seed}")]
        asked: dict[str, str] = {}          # id → title
        closed: dict[str, str] = {}         # id → the status word it must keep
        relay = [1000 * (seed + 1)]     # a relay message id is never reused

        def open_ids(repo):
            store = self.store(repo)
            return [e.qid for e in store.entries_in(repo / QUEUE)
                    if e.status == store.fmt.open_status.lower() and e.qid in asked]

        for step in range(self.STEPS):
            repo = rng.choice(repos)
            action = rng.choice(["ask", "ask", "ask", "answer", "answer", "withdraw",
                                 "claim", "sync", "pull", "pull-rebase", "hand"])
            where = f"seed {seed} step {step} {action} in {repo.name}"
            if action == "ask":
                title = f"q{seed}-{step}"
                entry, report = self.ask(repo, title)
                self.assertTrue(report.pushed, f"{where}: {report.reason}")
                self.assertNotIn(entry.qid, asked, f"{where}: {entry.qid} handed out twice")
                asked[entry.qid] = title
            elif action in ("answer", "withdraw"):
                candidates = open_ids(repo)
                if not candidates:
                    continue
                qid = rng.choice(candidates)
                relay[0] += 1
                if action == "answer":
                    op = lambda s, q=qid, n=relay[0]: s.apply_answer(q, f"answer {n}", "tester", n)
                else:
                    op = lambda s, q=qid: s.withdraw(q, "no longer needed", withdrawn_by="tester")
                _result, report = qg.run(self.store(repo), op, summary=f"{action} {qid}")
                # Either it was published, or — the upstream having closed the
                # entry already — there was nothing to write.
                self.assertFalse(report.pending, f"{where}: {report.reason}")
                self.assertTrue(report.pushed or not report.changed, f"{where}: {report.reason}")
                if qid not in closed:
                    # Whoever reached the upstream first decided (brd D5); a
                    # stale checkout's later attempt is recorded, not applied.
                    for entry in qs.parse_entries(self.remote_file().split("\n"), qs.QuestionsFormat()):
                        if entry.qid == qid:
                            closed[qid] = entry.status
            elif action == "hand":
                # The operator files a question with an editor and plain git:
                # claims an id, appends an entry (no blank line after it — that
                # is what shifts git's line alignment), commits, pulls, pushes
                # the branch.
                ids, report = qg.run(self.store(repo), lambda s: s.claim(), summary="claim")
                self.assertTrue(report.pushed, f"{where}: {report.reason}")
                qid, title = ids[0], f"hand{seed}-{step}"
                self.assertNotIn(qid, asked, where)
                text = self.read(repo)
                self.write(repo, QUEUE, text + ("" if text.endswith("\n") else "\n")
                           + f"\n## {qid} — {title}  [open]\nFiled by hand.")
                git(repo, "commit", "--quiet", "-am", f"docs(questions): file {qid} by hand")
                proc = subprocess.run(["git", "pull", "--quiet", "--no-rebase", "--no-edit"],
                                      cwd=str(repo), capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{where}:\n{proc.stdout}\n{proc.stderr}")
                git(repo, "push", "--quiet", "origin", "HEAD:main")
                asked[qid] = title
            elif action == "claim":
                ids, report = qg.run(self.store(repo), lambda s: s.claim(), summary="claim")
                self.assertTrue(report.pushed, f"{where}: {report.reason}")
                self.assertFalse(set(ids) & set(asked), where)
                asked.update({qid: "" for qid in ids})
            elif action == "sync":
                report = qg.sync(self.store(repo))
                self.assertFalse(report.pending, f"{where}: {report.reason}")
            else:
                flag = "--rebase" if action == "pull-rebase" else "--no-rebase"
                proc = subprocess.run(["git", "pull", "--quiet", flag, "--no-edit"], cwd=str(repo),
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{where}:\n{proc.stdout}\n{proc.stderr}")
                self.assertEqual(self.status(repo), "", where)

        # Everyone catches up.
        for repo in repos:
            report = qg.sync(self.store(repo))
            self.assertFalse(report.pending, f"seed {seed} final sync {repo.name}: {report.reason}")
        upstream = self.remote_file()
        fmt = qs.QuestionsFormat()
        entries = qs.parse_entries(upstream.split("\n"), fmt)
        ids = [e.qid for e in entries]
        self.assertEqual(len(ids), len(set(ids)), f"seed {seed}: an id heads two entries upstream")
        for qid, title in asked.items():
            if title:
                self.assertEqual(upstream.count(f"## {qid} — {title}"), 1, f"seed {seed}: {qid} lost")
        for entry in entries:
            if entry.qid in closed:
                self.assertEqual(entry.status, closed[entry.qid], f"seed {seed}: {entry.qid} changed state")
        self.assertNotIn("<<<<<<<", upstream)
        for repo in repos:
            proc = subprocess.run(["git", "pull", "--quiet", "--no-rebase", "--no-edit"], cwd=str(repo),
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"seed {seed} final pull {repo.name}:\n{proc.stdout}{proc.stderr}")
            local = [(e.qid, e.status) for e in qs.parse_entries(self.read(repo).split("\n"), fmt)]
            self.assertEqual(sorted(local), sorted((e.qid, e.status) for e in entries),
                             f"seed {seed}: {repo.name} does not read as the upstream does")

    def test_every_order_ends_the_same(self):
        seeds = os.environ.get("QUESTIONS_GIT_SEEDS")
        chosen = range(int(seeds)) if seeds else self.SEEDS
        for seed in chosen:
            with self.subTest(seed=seed):
                self._run(seed)


class _FeedClient:
    """The two calls the listener makes: read the feed, finalize the message."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.edits = []

    def get_answers(self, after: int = 0, wait: int = 0):
        return sorted((r for r in self.rows if int(r["id"]) > after), key=lambda r: int(r["id"]))

    def edit_message(self, message_id: int, text=None):
        self.edits.append((int(message_id), text))


class TestListenerAndOpsWithGit(GitSyncTestCase):

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.index_path = self.state / "async_questions.json"
        self.index_lock = self.state / "async_questions.json.lock"
        self.now = 1_800_000_000.0
        # A dispatched question, as ``ask`` leaves it.
        self.ask(self.a, "Ship on Friday?")
        qg.run(self.store(self.a), lambda s: s.mark_dispatched("Q-002", 4187, role="hpl"),
               summary="dispatched Q-002 (relay #4187)")
        qll.add_message_entry(4187, qll.IndexEntry(
            workspace_id="demo-ws", anchor="worktree", root=str(self.a), rel_path=QUEUE,
            qid="Q-002", role="hpl", created_at="2026-11-15T10:00:00Z",
        ), path=self.index_path, lock_path=self.index_lock)
        self.client = _FeedClient([{
            "id": 4187, "kind": "question", "answer_text": "Yes, ship it",
            "via": "button", "option_idx": 0, "answered_at": "2026-11-16T09:00:00Z",
        }])

    def listener(self) -> qll.Listener:
        config = qll.ListenConfig(
            enabled=True, server_url="http://relay.test", tokens=("rly_primary_token",),
            poll_seconds=25, max_attempts=10, config_path=self.state / "config.toml",
        )
        return qll.Listener(
            config,
            index_path=self.index_path, index_lock_path=self.index_lock,
            status_path=self.state / "status.json",
            client_factory=lambda url, token: self.client,
            clock=lambda: self.now, sleeper=lambda s: None,
        )

    def test_an_answer_with_no_session_running_is_committed_and_pushed(self):
        self.listener().run_cycle()

        self.assertEqual(self.remote_log()[0], "chore(questions): answer Q-002 (relay #4187)")
        self.assertEqual(self.files_in(self.remote, self.remote_tip()), [QUEUE])
        self.assertIn("## Q-002 — Ship on Friday?  [resolved]", self.remote_file())
        self.assertIn("    Yes, ship it", self.remote_file())
        self.assertEqual(git(self.a, "rev-parse", "HEAD"), self.remote_tip())
        self.assertCleanCheckout(self.a)
        self.assertEqual(self.client.edits[0][0], 4187)

    def test_an_answer_during_a_rebase_is_written_then_synced_once_the_rebase_ends(self):
        """The answer must never wait for git; git catches up afterwards."""
        (self.a / ".git" / "rebase-merge").mkdir()
        tip = self.remote_tip()
        listener = self.listener()
        listener.run_cycle()

        self.assertIn("[resolved]", self.read(self.a), "applied at once, whatever git is doing")
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(qll.read_index(self.index_path).pending, [], "an apply, not a failure")
        self.assertTrue(qg.is_pending(self.store(self.a)))
        # Visible in --status from the same cycle, with the reason…
        self.assertEqual(listener.sync_pending[0]["workspace_id"], "demo-ws")
        self.assertIn("a rebase is in progress", listener.sync_pending[0]["reason"])
        # …and not retried until the interval has passed.
        self.assertEqual(listener.retry_sync_pending(), 0)

        (self.a / ".git" / "rebase-merge").rmdir()
        self.now += qll.SYNC_RETRY_INTERVAL_S + 1
        self.assertEqual(listener.retry_sync_pending(), 1)
        self.assertIn("[resolved]", self.remote_file())
        self.assertFalse(qg.is_pending(self.store(self.a)))
        self.assertEqual(listener.sync_pending, [])
        self.assertCleanCheckout(self.a)

    def test_withdraw_is_one_pushed_commit(self):
        calls = []

        class Relay:
            def edit_message(self, mid, text=None):
                calls.append(("edit", mid))

            def cancel_message(self, mid, final_text=None):
                calls.append(("cancel", mid))
                return {"final_text_shown": "edit"}

        bindings = qops.roles_config.Bindings(
            server_url="http://relay.test", default_token="rly_default_token",
            roles={"hpl": "rly_hpl_token"}, workspace_roles={}, escalate_after={},
            workspace_escalate_after={}, errors=(),
        )
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.a)}), \
             patch.object(qops.roles_config, "load_bindings", return_value=bindings), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            result = qops.withdraw(
                str(self.a), qid="Q-002", reason="went ahead", by="tester",
                client_for=lambda token: Relay(),
            )
        self.assertTrue(result["withdrawn"], result)
        self.assertTrue(result["relay_cancelled"], result)
        self.assertEqual(calls, [("cancel", 4187)])
        self.assertTrue(result["sync"]["pushed"])
        self.assertEqual(result["sync"]["mode"], "push")
        self.assertEqual(self.remote_log()[0], "chore(questions): withdraw Q-002")
        self.assertIn("## Q-002 — Ship on Friday?  [withdrawn]", self.remote_file())
        self.assertCleanCheckout(self.a)

    def test_claim_reports_whether_it_reached_the_upstream(self):
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.a)}):
            pushed = qops.claim(str(self.a), count=1, note="hf-9", by="tester")
            self.assertEqual(pushed["ids"], ["Q-003"])
            self.assertTrue(pushed["claimed_on_remote"])
            self.assertTrue(pushed["sync"]["pushed"])
            self.assertNotIn("note", pushed)

            git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
            local = qops.claim(str(self.a), count=1, by="tester")
            self.assertEqual(local["ids"], ["Q-004"])
            self.assertFalse(local["claimed_on_remote"])
            self.assertIn("this checkout only", local["note"])
            self.assertIn("fetch failed", local["note"])
            self.assertTrue(local["sync"]["pending"])

            synced = qops.sync(str(self.a))
            self.assertTrue(synced["sync"]["pending"], "still no network")


# ── ask() itself, with sync on (epic 23 invariant 1 through the engine) ───────


class TestAskWithSyncOn(GitSyncTestCase):
    """Everything above drives the engine directly.  This drives
    ``questions_mcp_lib.ask`` — the caller that matters — through a real
    repository, to pin the ordering the engine must not disturb: **the entry is
    on disk before the first relay call**, whatever git managed to do."""

    def setUp(self):
        super().setUp()
        mcp_dir = str(_REPO_ROOT / "questions-mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        import questions_mcp_lib

        self.lib = questions_mcp_lib
        self.state = self.tmp / "state"
        self.state.mkdir()
        bindings = qops.roles_config.Bindings(
            server_url="http://relay.test", default_token="rly_default_token",
            roles={"hpl": "rly_hpl_token"}, workspace_roles={}, escalate_after={},
            workspace_escalate_after={}, errors=(),
        )
        for target, name, value in (
            (qll, "INDEX_PATH", self.state / "async_questions.json"),
            (qll, "_LOCK_PATH", self.state / "async_questions.json.lock"),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.a)}),
            patch.object(qops.roles_config, "load_bindings", return_value=bindings),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for cache, lock in (
            (self.lib._ws_cache, self.lib._ws_lock),
            (self.lib._last_ask_time, self.lib._rate_lock),
            (self.lib._clients, self.lib._clients_lock),
        ):
            with lock:
                cache.clear()
        self.sent = []

    def _client(self):
        test = self

        class Relay:
            def send_message(self, **kwargs):
                # The invariant, checked at the only moment it can be: now.
                text = test.read(test.a)
                test.sent.append({
                    "entry_on_disk": "## Q-002 — Sync on  [open]" in text,
                    "status": test.status(test.a),
                })
                handle = type("H", (), {})()
                handle.message_id = 4321
                handle.telegram_message_id = 4321
                return handle

        return Relay()

    def _ask(self):
        with patch.object(self.lib, "_client", return_value=self._client()):
            return self.lib.ask(title="Sync on", body="b")

    def test_pushed_ask_reports_sync_and_two_commits(self):
        result = self._ask()
        self.assertEqual(result["id"], "Q-002")
        self.assertTrue(result["dispatched"])
        self.assertEqual(self.sent, [{"entry_on_disk": True, "status": ""}],
                         "written AND committed before the relay is called")
        self.assertEqual(result["sync"]["mode"], "push")
        self.assertTrue(result["sync"]["pushed"])
        self.assertNotIn("dispatch_marker", result["sync"], "both commits fared the same")
        self.assertEqual(self.remote_log()[:2], [
            "chore(questions): dispatched Q-002 (relay #4321)",
            "chore(questions): ask Q-002",
        ])
        self.assertCleanCheckout(self.a)

    def test_with_the_network_down_the_question_is_still_written_then_sent(self):
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        result = self._ask()
        self.assertTrue(result["dispatched"], "git's trouble must not stop the question")
        self.assertEqual(result["message_id"], 4321)
        self.assertTrue(self.sent[0]["entry_on_disk"])
        self.assertFalse(result["sync"]["pushed"])
        self.assertTrue(result["sync"]["committed"])
        self.assertTrue(result["sync"]["pending"])
        self.assertIn("fetch failed", result["sync"]["reason"])
        self.assertIn("relay #4321", self.read(self.a))


if __name__ == "__main__":
    unittest.main(verbosity=2)
