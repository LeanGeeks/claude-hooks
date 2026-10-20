#!/usr/bin/env python3
"""
Allowed write roots mirror Claude Code's (epic 41, task 41-02).

The redirect gate and the `rm` check accept, beyond the payload cwd and /tmp:
the project root (41-05's settings root), the session's own auto-memory
directory (deferring modes only, brd H5), and user-level
`permissions.additionalDirectories`. Only `~/.claude/settings.json` is read.
The roots are computed once (`pretool_hook.compute_write_roots`), stored on the
permission row as `write_roots`, and used by the router card and the
permissions MCP.

Every tree lives under /var/tmp (never /tmp, itself an allowed root, which
would mask a refusal) with a fake HOME; the real ~/.claude is never read or
written.
"""

import atexit
import io
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

# Same isolation as the sibling modules (setdefault: an outer override wins).
_ISOLATED = Path(tempfile.mkdtemp(prefix="write-roots-store-"))
atexit.register(shutil.rmtree, str(_ISOLATED), ignore_errors=True)
for _var, _name in (
        ("CLAUDE_PERMISSION_STATE_FILE", "permission_requests.jsonl"),
        ("CLAUDE_PERMISSION_AUDIT_FILE", "permission_actions.jsonl"),
        ("CLAUDE_PERMISSION_DEBUG_LOG", "permission_state_debug.log"),
        ("CLAUDE_PERMISSION_ARCHIVE_FILE", "permission_requests.archive.jsonl"),
        ("CLAUDE_MANUAL_CONFIRM_LOG", "bash_manual_confirm.log")):
    os.environ.setdefault(_var, str(_ISOLATED / _name))
os.environ.setdefault("CLAUDE_INSTALL_NO_EXTERNAL", "1")

import permissions_mcp_lib as lib  # noqa: E402
import project_key  # noqa: E402
import telegram_permission_router as router  # noqa: E402
from bash_command_parser import BashCommandParser  # noqa: E402
from permission_state_store import PermissionRequest, create_request, get_request  # noqa: E402
from pretool_hook import (  # noqa: E402
    BashPermissionValidator, build_pretool_output, compute_write_roots)

HOOK = str(_REPO_ROOT / ".claude" / "hooks" / "pretool_hook.py")
ALLOW = ["Bash(echo:*)", "Bash(ls:*)", "Bash(cat:*)"]
_DROP_ENV = ("CLAUDE_WORKSPACE_DIR", "CLAUDE_PROJECT_DIR")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class _FakeLoader:
    def __init__(self, allow):
        self._allow = allow

    def load_all_settings(self):
        return {"permissions": {"allow": self._allow, "deny": [], "ask": []}}


class _Base(unittest.TestCase):
    """A fake HOME with ~/.claude/projects/<own key>/ (transcript inside, plus
    memory/) and a sibling <key2>/memory/, and a project root, all under
    /var/tmp."""

    def setUp(self):
        if not os.path.isdir("/var/tmp"):
            self.skipTest("no /var/tmp")
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="write-roots-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = os.path.join(self.base, "home")
        self.claude = os.path.join(self.home, ".claude")
        self.projects = os.path.join(self.claude, "projects")
        self.root = os.path.join(self.base, "proj_root")
        self.sub = os.path.join(self.root, "web", "src")
        os.makedirs(self.sub)
        self.key = project_key.encode_cli_project_key(self.root)
        self.transcript = self._session(self.key)
        self.memory = os.path.join(self.projects, self.key, "memory")
        self.sibling = os.path.join(self.projects, "-some-other-project", "memory")
        os.makedirs(self.memory)
        os.makedirs(self.sibling)
        env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        env["HOME"] = self.home
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        project_key.clear_cache()
        self.addCleanup(project_key.clear_cache)

    def _session(self, key):
        d = os.path.join(self.projects, key)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "11111111-2222-3333-4444-555555555555.jsonl")
        Path(path).touch()
        return path

    def user_settings(self, data):
        os.makedirs(self.claude, exist_ok=True)
        Path(self.claude, "settings.json").write_text(json.dumps(data))

    def project_settings(self, data, name="settings.json", root=None):
        d = Path(root or self.root, ".claude")
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(data))

    def payload(self, mode, cwd=None, **extra):
        data = {"cwd": cwd or self.root, "permission_mode": mode,
                "transcript_path": self.transcript}
        data.update(extra)
        return data

    def roots(self, mode, cwd=None, env=None, **extra):
        return compute_write_roots(self.payload(mode, cwd, **extra), env=env or {})

    def run_validator(self, command, mode, cwd=None, env=None, allow=None, **extra):
        cwd = cwd or self.root
        validator = BashPermissionValidator(
            _FakeLoader(allow or ALLOW), BashCommandParser(), cwd,
            extra_roots=self.roots(mode, cwd, env, **extra))
        result = validator.validate_bash_command(command)
        return result, build_pretool_output(result, mode)

    def emitted(self, command, mode, **kw):
        _result, output = self.run_validator(command, mode, **kw)
        return None if output is None else output["hookSpecificOutput"]["permissionDecision"]

    def assert_silent_redirect(self, command, mode, **kw):
        """No redirect ask: the allowlisted command is allowed outright."""
        result, _output = self.run_validator(command, mode, **kw)
        self.assertEqual(result["redirect_targets"], [], command)
        self.assertEqual(result["decision"], "allow", (command, result["reason"]))

    def assert_risk(self, command, mode, **kw):
        result, output = self.run_validator(command, mode, **kw)
        self.assertEqual(result["decision"], "ask", command)
        self.assertEqual(result["ask_kind"], "risk", command)
        self.assertTrue(result["redirect_targets"], command)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask", command)

    def run_hook(self, command, mode, cwd=None, env_extra=None, transcript=True):
        cwd = cwd or self.root
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                   "session_id": "s-write-roots", "cwd": cwd, "permission_mode": mode}
        if transcript:
            payload["transcript_path"] = self.transcript
        env = dict(os.environ)
        env["CLAUDE_HOOK_DEBUG"] = "0"
        env.update(env_extra or {})
        proc = subprocess.run([sys.executable, HOOK], capture_output=True, text=True,
                              timeout=30, input=json.dumps(payload), env=env, cwd=cwd)
        out = proc.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


class TestMemoryRoot(_Base):
    def setUp(self):
        super().setUp()
        self.user_settings({"permissions": {"allow": ALLOW}})

    def test_own_memory_write_is_silent_in_auto(self):
        cmd = f"echo x >> {self.memory}/MEMORY.md"
        self.assert_silent_redirect(cmd, "auto")
        self.assertEqual(self.run_hook(cmd, "auto"), "allow")
        # the ~ spelling the operator's card showed
        rel = os.path.relpath(self.memory, self.home)
        self.assert_silent_redirect(f"echo x >> ~/{rel}/MEMORY.md", "auto")

    def test_own_memory_write_asks_in_default_until_h5_is_verified(self):
        cmd = f"echo x >> {self.memory}/MEMORY.md"
        self.assert_risk(cmd, "default")
        self.assert_risk(cmd, "acceptEdits")
        self.assertEqual(self.run_hook(cmd, "default"), "ask")
        for mode in ("auto", "bypassPermissions", "dontAsk"):
            self.assertIn(os.path.realpath(self.memory), self.roots(mode), mode)
        for mode in ("default", "acceptEdits", "plan", ""):
            self.assertNotIn(os.path.realpath(self.memory), self.roots(mode), mode)

    def test_sibling_memory_asks(self):
        for mode in ("auto", "bypassPermissions"):
            self.assert_risk(f"echo x >> {self.sibling}/MEMORY.md", mode)
        self.assertEqual(self.run_hook(f"echo x >> {self.sibling}/MEMORY.md", "auto"), "ask")

    def test_claude_settings_json_asks(self):
        target = os.path.join(self.claude, "settings.json")
        for mode in ("auto", "bypassPermissions"):
            self.assert_risk(f"echo x >> {target}", mode)
            self.assert_risk("echo x >> ~/.claude/settings.json", mode)
        self.assertEqual(self.run_hook(f"echo x >> {target}", "bypassPermissions"), "ask")

    def test_claude_settings_json_asks_even_when_home_is_a_root(self):
        # The user grants all of $HOME; ~/.claude stays protected, and the
        # memory carve-out does not reach beyond memory/.
        self.user_settings({"permissions": {"allow": ALLOW, "additionalDirectories": ["~/"]}})
        self.assertIn(self.home, self.roots("auto"))
        for mode in ("auto", "bypassPermissions"):
            self.assert_risk("echo x >> ~/.claude/settings.json", mode)
            self.assert_risk(f"echo x >> {self.projects}/{self.key}/other.jsonl", mode)
            self.assert_risk(f"echo x >> {self.sibling}/MEMORY.md", mode)
        self.assert_silent_redirect("echo x >> ~/notes.txt", "auto")
        self.assert_silent_redirect(f"echo x >> {self.memory}/MEMORY.md", "auto")

    def test_own_key_outside_memory_asks(self):
        own = os.path.join(self.projects, self.key)
        for target in (f"{own}/other.jsonl", f"{own}/memory/../other.jsonl",
                       f"{own}/memory2/x.md", f"{self.projects}/x.md"):
            self.assert_risk(f"echo x >> {target}", "auto")

    def test_memory_dirs_own_claude_config_stays_protected(self):
        for tail in (".claude/settings.json", ".claude/settings.local.json",
                     ".claude/hooks/x.py"):
            self.assert_risk(f"echo x > {self.memory}/{tail}", "auto")

    def test_symlink_inside_memory_is_judged_on_its_realpath(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        os.symlink(os.path.join(self.claude, "settings.json"),
                   os.path.join(self.memory, "lnk_settings"))
        os.symlink(outside, os.path.join(self.memory, "lnk_out"))
        os.symlink(self.sibling, os.path.join(self.memory, "lnk_sib"))
        for target in ("lnk_settings", "lnk_out/x", "lnk_sib/MEMORY.md"):
            self.assert_risk(f"echo x > {self.memory}/{target}", "auto")
        # unresolvable leaf next to an escaping symlink (41-01's R5)
        self.assert_risk(f"echo x > {self.memory}/lnk_$V", "auto")

    def test_unresolvable_target_in_memory_gets_the_carve_out(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("V", None)
            self.assert_silent_redirect(f"echo x > {self.memory}/note_$V.md", "auto")
            rel = os.path.relpath(self.memory, self.home)
            self.assert_silent_redirect(f"echo x > ~/{rel}/note_$(date +%s).md", "auto",
                                        allow=ALLOW + ["Bash(date:*)"])
            self.assert_risk(f"echo x > {self.memory}/note_$V.md", "default")
            self.assert_risk(f"echo x > {self.sibling}/note_$V.md", "auto")
            self.assert_risk(f"echo x > {self.projects}/{self.key}/note_$V.md", "auto")
            self.assert_risk(f"echo x > {self.claude}/settings$V.json", "auto")
            self.assert_risk(f"echo x > {self.memory}/.claude/x$V", "auto")
            self.assert_risk(f"echo x > {self.memory}/x.claude$V", "auto")
            self.assert_risk(f"echo x > {self.memory}/x${{V:-claude}}", "auto")
            self.assert_risk(f"echo x > {self.memory}/../x$V", "auto")

    def test_rm_in_own_memory_is_allowed_by_the_rm_check(self):
        result, _ = self.run_validator(f"rm {self.memory}/old.md", "auto", allow=[])
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["validation_results"][0]["matched_allow_patterns"],
                         ["workspace_rm"])
        for target in (f"{self.sibling}/old.md", f"{self.claude}/settings.json",
                       f"{self.projects}/{self.key}/x.jsonl", f"-rf {self.memory}/.."):
            result, _ = self.run_validator(f"rm {target}", "auto", allow=[])
            self.assertNotEqual(result["decision"], "allow", target)
        result, _ = self.run_validator(f"rm {self.memory}/old.md", "default", allow=[])
        self.assertNotEqual(result["decision"], "allow")

    def test_rm_never_reaches_protected_paths_through_a_root(self):
        self.user_settings({"permissions": {"allow": ALLOW, "additionalDirectories": ["~/"]}})
        for target in (f"{self.claude}/settings.json", self.claude, f"-rf {self.home}",
                       f"{self.root}/.claude/settings.local.json",
                       f"-rf {self.root}/.claude/hooks", f"-rf {self.memory}"):
            result, _ = self.run_validator(f"rm {target}", "auto", allow=[])
            self.assertNotEqual(result["decision"], "allow", target)
        result, _ = self.run_validator(f"rm {self.home}/notes.txt", "auto", allow=[])
        self.assertEqual(result["decision"], "allow")

    def test_memory_tool_is_not_a_workspace_binary(self):
        for mode in ("auto", "default", "bypassPermissions"):
            validator = BashPermissionValidator(
                _FakeLoader([]), BashCommandParser(), self.root,
                extra_roots=self.roots(mode))
            self.assertFalse(validator._is_workspace_binary(f"{self.memory}/tool --x"), mode)
            result = validator.validate_bash_command(f"{self.memory}/tool")
            self.assertNotEqual(result["decision"], "allow", mode)

    def test_malformed_or_missing_transcript_gives_no_memory_root(self):
        own_key_dir = os.path.join(self.projects, self.key)
        cases = [None, 123, "", "relative/x.jsonl", self.transcript[:-6] + ".json",
                 os.path.join(own_key_dir, "sess", "subagents", "agent-1.jsonl"),
                 os.path.join(own_key_dir, "..", self.key, "x.jsonl"),
                 os.path.join(self.base, "elsewhere", self.key, "x.jsonl"),
                 os.path.join(self.projects, "x.jsonl"),
                 self.transcript + "\x00"]
        for value in cases:
            roots = self.roots("auto", transcript_path=value)
            self.assertEqual(roots, [os.path.realpath(self.root)], repr(value))
        payload = self.payload("auto")
        del payload["transcript_path"]
        self.assertEqual(compute_write_roots(payload, env={}), [os.path.realpath(self.root)])
        self.assertEqual(self.run_hook(f"echo x >> {self.memory}/MEMORY.md", "auto",
                                       transcript=False), "ask")

    def test_memory_root_that_is_a_symlink_out_of_projects_is_not_a_root(self):
        shutil.rmtree(self.memory)
        os.symlink(self.claude, self.memory)
        self.assertEqual(self.roots("auto"), [os.path.realpath(self.root)])
        self.assert_risk("echo x >> ~/.claude/settings.json", "auto")


class TestAutoMemorySettings(_Base):
    def setUp(self):
        super().setUp()
        self.custom = os.path.join(self.home, "custom-mem")
        os.makedirs(self.custom)

    def test_user_level_auto_memory_directory_replaces_the_default(self):
        self.user_settings({"permissions": {"allow": ALLOW},
                            "autoMemoryDirectory": "~/custom-mem"})
        self.assert_silent_redirect(f"echo x >> {self.custom}/MEMORY.md", "auto")
        self.assert_risk(f"echo x >> {self.memory}/MEMORY.md", "auto")
        self.assert_risk(f"echo x >> {self.custom}/MEMORY.md", "default")
        self.assertEqual(self.run_hook(f"echo x >> {self.custom}/MEMORY.md", "auto"), "allow")
        self.assertEqual(self.run_hook(f"echo x >> {self.memory}/MEMORY.md", "auto"), "ask")

    def test_auto_memory_directory_in_project_or_local_settings_is_ignored(self):
        self.user_settings({"permissions": {"allow": ALLOW}})
        for name in ("settings.json", "settings.local.json"):
            self.project_settings({"autoMemoryDirectory": "~/custom-mem"}, name)
            self.assertEqual(self.run_hook(f"echo x >> {self.custom}/MEMORY.md", "auto"),
                             "ask", name)
            self.assertEqual(self.run_hook(f"echo x >> {self.memory}/MEMORY.md", "auto"),
                             "allow", name)
            self.assert_risk(f"echo x >> {self.custom}/MEMORY.md", "auto")

    def test_auto_memory_disabled_gives_no_memory_root(self):
        self.user_settings({"permissions": {"allow": ALLOW}, "autoMemoryEnabled": False})
        self.assert_risk(f"echo x >> {self.memory}/MEMORY.md", "auto")
        self.assertEqual(self.roots("auto"), [os.path.realpath(self.root)])
        self.user_settings({"autoMemoryEnabled": False, "autoMemoryDirectory": "~/custom-mem"})
        self.assertEqual(self.roots("auto"), [os.path.realpath(self.root)])

    def test_overrides_that_would_widen_past_memory_are_refused(self):
        for value in ("~/.claude", "~", "/", "~/.claude/projects", "~/.claude/hooks",
                      "~/.claude/agents", "relative/mem", "~nosuchuser/x", 5):
            self.user_settings({"autoMemoryDirectory": value})
            self.assertEqual(self.roots("auto"), [os.path.realpath(self.root)], repr(value))
        self.user_settings({"autoMemoryDirectory": "~/.claude"})
        self.assert_risk("echo x >> ~/.claude/settings.json", "auto")
        # a memory-shaped override under ~/.claude is honoured
        self.user_settings({"autoMemoryDirectory": f"~/.claude/projects/{self.key}/memory"})
        self.assertIn(os.path.realpath(self.memory), self.roots("auto"))


class TestWorktreeMemory(_Base):
    """The main checkout's memory dir for a registered linked worktree."""

    def setUp(self):
        super().setUp()
        self.user_settings({"permissions": {"allow": ALLOW}})
        self.main = os.path.join(self.base, "main_repo")
        os.makedirs(self.main)
        _git("init", "-q", cwd=self.main)
        _git("commit", "-q", "--allow-empty", "-m", "i", cwd=self.main)
        self.wt = os.path.join(self.base, "main_repo--wt.01")
        _git("worktree", "add", "-q", self.wt, "-b", "wtb", cwd=self.main)
        self.main_key = project_key.encode_cli_project_key(self.main)
        self.main_memory = os.path.join(self.projects, self.main_key, "memory")
        os.makedirs(self.main_memory)
        self.wt_key = project_key.encode_cli_project_key(self.wt)
        self.transcript = self._session(self.wt_key)

    def test_registered_worktree_writes_main_checkout_memory(self):
        cmd = f"echo x >> {self.main_memory}/a.md"
        self.assert_silent_redirect(cmd, "auto", cwd=self.wt)
        self.assertEqual(self.run_hook(cmd, "auto", cwd=self.wt), "allow")
        wsub = os.path.join(self.wt, "d")
        os.makedirs(wsub)
        self.assert_silent_redirect(cmd, "auto", cwd=wsub)
        # its own key's memory dir is the base case and stays a root
        self.assertIn(os.path.join(self.projects, self.wt_key, "memory"),
                      self.roots("auto", cwd=self.wt))
        self.assert_risk(cmd, "default", cwd=self.wt)

    def test_worktree_not_listed_by_the_repo_its_git_file_names(self):
        fake = os.path.join(self.base, "fake_wt")
        os.makedirs(fake)
        Path(fake, ".git").write_text(f"gitdir: {self.main}/.git\n")
        self.assertEqual(project_key.resolve_project_root(fake), self.main)
        self.transcript = self._session(project_key.encode_cli_project_key(fake))
        cmd = f"echo x >> {self.main_memory}/a.md"
        self.assert_risk(cmd, "auto", cwd=fake, env={"CLAUDE_PROJECT_DIR": fake})
        self.assertEqual(self.run_hook(cmd, "auto", cwd=fake,
                                       env_extra={"CLAUDE_PROJECT_DIR": fake}), "ask")
        self.assertNotIn(os.path.realpath(self.main_memory),
                         self.roots("auto", cwd=fake, env={"CLAUDE_PROJECT_DIR": fake}))

    def test_encoding_self_check_failure_adds_no_worktree_root(self):
        # The transcript sits under a key the CLI encoding does not produce
        # for this root ('/' -> '-' only, keeping '_' and '.'), as a CLI with
        # a different encoding would write it.
        wrong = project_key.encode_project_key(self.wt)
        self.assertNotEqual(wrong, self.wt_key)
        self.transcript = self._session(wrong)
        cmd = f"echo x >> {self.main_memory}/a.md"
        self.assert_risk(cmd, "auto", cwd=self.wt)
        self.assertEqual(self.run_hook(cmd, "auto", cwd=self.wt), "ask")
        roots = self.roots("auto", cwd=self.wt)
        self.assertIn(os.path.join(self.projects, wrong, "memory"), roots)  # base case
        self.assertNotIn(os.path.realpath(self.main_memory), roots)

    def test_main_checkout_session_gets_no_extra_worktree_root(self):
        self.transcript = self._session(self.main_key)
        self.assertEqual(self.roots("auto", cwd=self.main),
                         [self.main, os.path.realpath(self.main_memory)])


class TestKeyEncoding(unittest.TestCase):
    def test_observed_pairs(self):
        # Read off ~/.claude/projects (directory name vs the cwd its
        # transcripts record) on 2026-10-20; see the 41-02 report.
        pairs = {
            "/data/sync/work/dr_remi": "-data-sync-work-dr-remi",
            "/data/sync/work/dr_remi/hipaa_form": "-data-sync-work-dr-remi-hipaa-form",
            "/data/sync/Config/bash/ram_stat": "-data-sync-Config-bash-ram-stat",
            "/data/sync/work/delaybuddy/.worktrees/delay-buddy-form--w1-01":
                "-data-sync-work-delaybuddy--worktrees-delay-buddy-form--w1-01",
            "/data/sync/work/activecdn/acdn-workspace/skynet":
                "-data-sync-work-activecdn-acdn-workspace-skynet",
        }
        for path, key in pairs.items():
            self.assertEqual(project_key.encode_cli_project_key(path), key)

    def test_queue_key_encoding_is_unchanged(self):
        self.assertEqual(project_key.encode_project_key("/a/dr_remi/.w"), "-a-dr_remi-.w")


class TestProjectRoot(_Base):
    def setUp(self):
        super().setUp()
        self.user_settings({"permissions": {"allow": ALLOW}})

    def test_subdir_writes_up_to_the_project_dir_root(self):
        env = {"CLAUDE_PROJECT_DIR": self.root}
        self.assert_silent_redirect("ls > ../index.html", "auto", cwd=self.sub, env=env)
        self.assert_silent_redirect("ls > ../index.html", "default", cwd=self.sub, env=env)
        self.assertEqual(self.run_hook("ls > ../index.html", "auto", cwd=self.sub,
                                       env_extra=env), "allow")

    def test_subdir_writes_up_to_the_git_top_level(self):
        _git("init", "-q", cwd=self.root)
        self.assert_silent_redirect("ls > ../index.html", "auto", cwd=self.sub)
        self.assertEqual(self.run_hook("ls > ../index.html", "auto", cwd=self.sub), "allow")

    def test_no_env_not_a_repo_keeps_todays_behaviour(self):
        self.assert_risk("ls > ../index.html", "auto", cwd=self.sub)
        self.assertEqual(self.run_hook("ls > ../index.html", "auto", cwd=self.sub), "ask")

    def test_project_root_config_stays_protected(self):
        env = {"CLAUDE_PROJECT_DIR": self.root}
        for target in ("../../.claude/settings.json", "../../.claude/settings.local.json",
                       "../../.claude/hooks/x.py"):
            self.assert_risk(f"echo x > {target}", "auto", cwd=self.sub, env=env)
        self.assert_risk("echo x > ../../.claude/x$V", "auto", cwd=self.sub, env=env)


class TestAdditionalDirectories(_Base):
    def setUp(self):
        super().setUp()
        self.extra = os.path.join(self.base, "extra_dir")
        os.makedirs(self.extra)

    def test_global_entry_is_a_root(self):
        self.user_settings({"permissions": {"allow": ALLOW,
                                            "additionalDirectories": [self.extra]}})
        self.assert_silent_redirect(f"echo x > {self.extra}/out.txt", "auto")
        self.assert_silent_redirect(f"echo x > {self.extra}/out.txt", "default")
        self.assertEqual(self.run_hook(f"echo x > {self.extra}/out.txt", "auto"), "allow")
        result, _ = self.run_validator(f"rm {self.extra}/out.txt", "auto", allow=[])
        self.assertEqual(result["decision"], "allow")

    def test_tilde_entry_is_a_root_and_relative_entries_are_ignored(self):
        os.makedirs(os.path.join(self.home, "tilde_dir"))
        os.makedirs(os.path.join(self.root, "rel_dir"))
        self.user_settings({"permissions": {"additionalDirectories": [
            "~/tilde_dir", "rel_dir", "../extra_dir", "~nosuchuser/x", 7, None]}})
        roots = self.roots("default")
        self.assertEqual(roots, [os.path.realpath(self.root),
                                 os.path.join(self.home, "tilde_dir")])

    def test_entries_inside_claude_home_are_dropped(self):
        self.user_settings({"permissions": {"additionalDirectories": [
            "~/.claude", "~/.claude/projects", self.sibling]}})
        self.assertEqual(self.roots("default"), [os.path.realpath(self.root)])
        self.assert_risk(f"echo x > {self.sibling}/MEMORY.md", "default")

    def test_entry_only_in_project_or_local_settings_is_ignored(self):
        # No user settings file at all: the allow rules live in the project.
        for name in ("settings.json", "settings.local.json"):
            self.project_settings({"permissions": {"allow": ALLOW,
                                                   "additionalDirectories": [self.extra]}},
                                  name)
            self.assertEqual(self.run_hook(f"echo x > {self.extra}/out.txt", "auto"),
                             "ask", name)
            self.assertEqual(self.roots("auto"),
                             [os.path.realpath(self.root), os.path.realpath(self.memory)])
            Path(self.root, ".claude", name).unlink()

    def test_extra_roots_own_claude_config_stays_protected(self):
        self.user_settings({"permissions": {"allow": ALLOW,
                                            "additionalDirectories": [self.extra]}})
        for tail in (".claude/settings.json", ".claude/settings.local.json",
                     ".claude/hooks/x.py"):
            self.assert_risk(f"echo x > {self.extra}/{tail}", "auto")
        self.assert_risk(f"echo x > {self.extra}/.claude/agents/a$V.md", "auto")
        os.symlink("/etc", os.path.join(self.extra, "etc_link"))
        self.assert_risk(f"echo x > {self.extra}/etc_link/x", "auto")


class TestReplay(_Base):
    def test_replay_has_project_root_and_no_memory_root(self):
        self.user_settings({"permissions": {"allow": ALLOW}})
        _git("init", "-q", cwd=self.root)
        env = dict(os.environ)
        env.update({"CLAUDE_HOOK_DEBUG": "0", "CLAUDE_HOOK_REPLAY": "1"})
        entries = [{"command": "ls > ../index.html", "workspace": self.sub,
                    "permission_mode": "auto"},
                   {"command": f"echo x >> {self.memory}/MEMORY.md",
                    "workspace": self.root, "permission_mode": "auto"}]
        proc = subprocess.run([sys.executable, HOOK, "--dry-run"], capture_output=True,
                              text=True, timeout=30, env=env, cwd=self.base,
                              input="\n".join(json.dumps(e) for e in entries))
        decisions = [line for line in proc.stderr.splitlines() if line.startswith("Decision:")]
        self.assertEqual(decisions, ["Decision: allow", "Decision: ask"], proc.stderr)
        helptext = subprocess.run([sys.executable, HOOK, "--help"], capture_output=True,
                                  text=True, timeout=30, env=env).stdout
        self.assertIn("transcript_path", helptext)
        self.assertIn("no auto-memory root", " ".join(helptext.split()))


def _row(cwd, command, write_roots=None, settings_root=None):
    return PermissionRequest(
        request_id="r41-02", session_id="s1", cwd=cwd, tool_name="Bash",
        tool_input={"command": command}, permission_suggestions=[],
        state="pending", created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z", expires_at="2024-01-01T00:05:00Z",
        settings_root=settings_root, write_roots=write_roots,
    )


class TestRowConsumers(_Base):
    def setUp(self):
        super().setUp()
        self.user_settings({"permissions": {"allow": ALLOW}})
        self.command = f"zzunknown41 run; echo x >> {self.memory}/MEMORY.md"
        self.roots_auto = self.roots("auto")

    def test_row_round_trips_write_roots_and_old_rows_load(self):
        req = create_request("s", self.root, "Bash", {"command": "x"}, [],
                             settings_root=self.root, write_roots=self.roots_auto)
        self.assertEqual(get_request(req.request_id).write_roots, self.roots_auto)
        data = _row(self.root, "x").to_dict()
        del data["write_roots"]
        self.assertIsNone(PermissionRequest.from_dict(data).write_roots)

    def test_router_card_does_not_list_the_memory_target(self):
        ann = router._bash_annotations(_row(self.root, self.command, self.roots_auto,
                                            self.root))
        self.assertFalse(ann.failed)
        self.assertEqual(ann.redirect_targets, [])
        self.assertEqual(ann.unknown, ["zzunknown41 run"])
        self.assertEqual(ann.ask_kind, "unknown")
        body = router.render_permission_body(_row(self.root, self.command, self.roots_auto,
                                                  self.root), "proj")
        self.assertNotIn("📝", body)
        old_body = router.render_permission_body(_row(self.root, self.command, None,
                                                      self.root), "proj")
        self.assertIn("📝", old_body)
        # control: a row without write_roots gets none (cwd and /tmp)
        old = router._bash_annotations(_row(self.root, self.command, None, self.root))
        self.assertEqual(old.redirect_targets, [f"{self.memory}/MEMORY.md"])
        # the sibling target is still listed with the row's roots
        sib = router._bash_annotations(_row(
            self.root, f"zzunknown41 run; echo x >> {self.sibling}/MEMORY.md",
            self.roots_auto, self.root))
        self.assertEqual(sib.redirect_targets, [f"{self.sibling}/MEMORY.md"])

    def test_row_roots_inside_claude_home_never_lift_its_protection(self):
        bogus = [self.claude, self.projects, os.path.join(self.projects, self.key),
                 "relative/dir", 5, None]
        for target in ("~/.claude/settings.json", f"{self.projects}/{self.key}/x.jsonl",
                       f"{self.projects}/x.md", f"{self.sibling}/MEMORY.md"):
            ann = router._bash_annotations(_row(
                self.root, f"zzunknown41 run; echo x >> {target}", bogus, self.root))
            self.assertEqual(ann.redirect_targets, [target])
        validator = BashPermissionValidator(_FakeLoader(ALLOW), BashCommandParser(),
                                            self.root, extra_roots=bogus)
        self.assertEqual(validator.extra_roots,
                         [self.claude, self.projects, os.path.join(self.projects, self.key)])
        self.assertEqual(validator.memory_roots, [])

    def test_mcp_classification_uses_row_write_roots(self):
        with_roots = lib.classify(_row(self.root, self.command, self.roots_auto, self.root))
        self.assertTrue(with_roots.decidable, with_roots.tier_reason)
        self.assertNotIn(lib.REDIRECT_REASON_PREFIX, with_roots.tier_reason)
        without = lib.classify(_row(self.root, self.command, None, self.root))
        self.assertFalse(without.decidable)
        self.assertIn(lib.REDIRECT_REASON_PREFIX, without.tier_reason)

    def test_find_deny_collision_without_a_row_is_unchanged(self):
        self.project_settings({"permissions": {"deny": ["Bash(zzprobe:*)"]}})
        validator = lib._build_validator(self.root)
        self.assertEqual(validator.extra_roots, [])
        deny, all_denies = lib.find_deny_collision(
            lib.parse_pattern("Bash(zzprobe:*)"), self.root)
        self.assertEqual(deny, "Bash(zzprobe:*)")
        self.assertIn("Bash(zzprobe:*)", all_denies)


class TestPermissionRequestHookStoresRoots(_Base):
    def _drive(self, cwd, mode="bypassPermissions", env_extra=None):
        import permission_request_hook as prh
        payload = {"session_id": "s-hook-roots", "cwd": cwd, "tool_name": "Bash",
                   "tool_input": {"command": "zzprobe"}, "permission_suggestions": [],
                   "permission_mode": mode, "transcript_path": self.transcript}
        created = []

        def spy(*a, **kw):
            req = create_request(*a, **kw)
            created.append(req)
            return req

        with patch.dict(os.environ, env_extra or {}), \
                patch.object(prh, "create_request", side_effect=spy), \
                patch.object(prh, "cleanup_expired_requests"), \
                patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                patch("builtins.print"):
            with self.assertRaises(SystemExit):
                prh.main()
        self.assertEqual(len(created), 1)
        return get_request(created[0].request_id)

    def test_row_stores_the_same_roots_pretool_computes(self):
        extra = os.path.join(self.base, "extra_dir")
        os.makedirs(extra)
        self.user_settings({"permissions": {"additionalDirectories": [extra]}})
        env = {"CLAUDE_PROJECT_DIR": self.root}
        row = self._drive(self.sub, env_extra=env)
        self.assertEqual(row.write_roots, [os.path.realpath(self.root), extra,
                                           os.path.realpath(self.memory)])
        self.assertEqual(row.write_roots,
                         self.roots("bypassPermissions", cwd=self.sub, env=env))


class _TierLoader:
    def __init__(self, allow=(), deny=(), ask=()):
        self._p = {"allow": list(allow), "deny": list(deny), "ask": list(ask)}

    def load_all_settings(self):
        return {"permissions": self._p}


class TestDenyAskBeatShortcuts(_Base):
    """41-02 fix: deny/ask outrank the workspace-rm and workspace-binary
    shortcuts for every root (state.md invariant 1)."""

    def setUp(self):
        super().setUp()
        _git("init", "-q", cwd=self.root)
        self.extra = os.path.join(self.base, "extra_dir")
        os.makedirs(self.extra)
        self.user_settings({"permissions": {"additionalDirectories": [self.extra]}})
        self.tmpdir = tempfile.mkdtemp(prefix="deny-rm-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def verdict(self, command, loader, cwd=None, mode="auto"):
        cwd = cwd or self.root
        v = BashPermissionValidator(loader, BashCommandParser(), cwd,
                                    extra_roots=self.roots(mode, cwd))
        return v.validate_bash_command(command)

    def rm_targets(self):
        return {"cwd": os.path.join(self.sub, "f"),
                "project root": os.path.join(self.root, "f"),
                "memory dir": os.path.join(self.memory, "f"),
                "additionalDirectories": os.path.join(self.extra, "f"),
                "/tmp": os.path.join(self.tmpdir, "f")}

    def test_baseline_rm_is_allowed_under_every_root(self):
        for name, target in self.rm_targets().items():
            self.assertEqual(self.verdict(f"rm {target}", _TierLoader(), cwd=self.sub)
                             ["decision"], "allow", name)

    def test_rm_deny_wins_under_every_root(self):
        loader = _TierLoader(deny=["Bash(rm:*)"])
        for name, target in self.rm_targets().items():
            result = self.verdict(f"rm {target}", loader, cwd=self.sub)
            self.assertEqual(result["decision"], "deny", name)

    def test_rm_ask_rule_still_asks_under_every_root(self):
        loader = _TierLoader(ask=["Bash(rm:*)"])
        for name, target in self.rm_targets().items():
            result = self.verdict(f"rm {target}", loader, cwd=self.sub)
            self.assertEqual(result["decision"], "ask", name)

    def test_workspace_binary_deny_and_ask_win(self):
        binary = os.path.join(self.root, "bin", "t")
        os.makedirs(os.path.dirname(binary))
        Path(binary).write_text("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        self.assertEqual(self.verdict(f"{binary} a", _TierLoader())["decision"], "allow")
        self.assertEqual(self.verdict(f"{binary} a", _TierLoader(deny=[f"Bash({binary}:*)"]))
                         ["decision"], "deny")
        self.assertEqual(self.verdict(f"{binary} a", _TierLoader(ask=[f"Bash({binary}:*)"]))
                         ["decision"], "ask")

    def test_rm_deny_in_hook_output(self):
        Path(self.root, ".claude").mkdir(exist_ok=True)
        result = self.verdict(f"rm {self.memory}/f", _TierLoader(deny=["Bash(rm:*)"]))
        self.assertEqual(build_pretool_output(result, "auto")["hookSpecificOutput"]
                         ["permissionDecision"], "deny")


class TestCwdSlashIsNotARoot(_Base):
    """41-02 fix 2: a session launched from `/` behaves as at 6772917. The
    `/`-aware containment is for additionalDirectories only; the cwd, the
    project root and /tmp keep the exact-prefix comparison, and a cwd of `/`
    never makes `/etc`, `/usr/bin` ... an allowed root."""

    def verdict(self, command, extra_roots=None, loader=None):
        validator = BashPermissionValidator(
            loader or _TierLoader(allow=["Bash(echo:*)"]),
            BashCommandParser(), "/", extra_roots=extra_roots)
        return validator.validate_bash_command(command)

    def test_project_root_slash_is_not_a_write_root(self):
        self.assertNotIn("/", compute_write_roots({"cwd": "/", "permission_mode": "default"},
                                                  env={}))

    def test_redirect_outside_still_asks_with_computed_roots(self):
        roots = compute_write_roots({"cwd": "/", "permission_mode": "default"}, env={})
        result = self.verdict("echo x > /etc/zz-write-roots", extra_roots=roots)
        self.assertEqual(result["decision"], "ask")
        self.assertTrue(result["redirect_targets"])

    def test_redirect_outside_still_asks(self):
        self.assertEqual(self.verdict("echo x > /etc/zz-write-roots")["decision"], "ask")

    def test_rm_outside_still_asks(self):
        self.assertEqual(self.verdict("rm /etc/zz-write-roots")["decision"], "ask")

    def test_workspace_binary_outside_still_asks(self):
        for command in ("/usr/bin/curl http://x", "/bin/ls"):
            self.assertEqual(self.verdict(command)["decision"], "ask", command)
        validator = BashPermissionValidator(_TierLoader(), BashCommandParser(), "/")
        self.assertFalse(validator._is_workspace_binary("/usr/bin/curl http://x"))
        self.assertFalse(validator._is_path_inside_workspace("/etc/zz-write-roots"))

    def test_tmp_still_allowed_from_slash(self):
        self.assertEqual(self.verdict("rm /tmp/zz-write-roots")["decision"], "allow")

    def test_slash_extra_root_still_works_from_slash(self):
        # The user-configured `/` is the one place the containment is `/`-aware.
        result = self.verdict("echo x > /var/tmp/zz-write-roots", extra_roots=["/"])
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(self.verdict("rm /var/tmp/zz-write-roots", extra_roots=["/"])
                         ["decision"], "allow")


class TestRootSlashAndFailSafe(_Base):
    def test_slash_entry_is_a_root(self):
        self.user_settings({"permissions": {"allow": ALLOW, "additionalDirectories": ["/"]}})
        self.assertIn("/", self.roots("default"))
        self.assert_silent_redirect(f"echo x > {self.base}/out.txt", "default")
        result, _ = self.run_validator(f"rm {self.base}/out.txt", "default", allow=[])
        self.assertEqual(result["decision"], "allow")

    def test_slash_entry_keeps_claude_config_protected(self):
        self.user_settings({"permissions": {"allow": ALLOW, "additionalDirectories": ["/"]}})
        self.assert_risk("echo x > ~/.claude/settings.json", "default")
        self.assert_risk("echo x > ~/.claude/hooks/x.py", "default")
        self.assert_risk(f"echo x > {self.root}/.claude/settings.json", "default")
        self.assert_risk(f"echo x > {self.root}/.claude/hooks/x.py", "default")
        result, _ = self.run_validator(f"rm -rf {self.root}/.claude", "default", allow=[])
        self.assertEqual(result["decision"], "ask")

    def test_pathological_user_settings_fail_safe_and_the_gate_runs(self):
        os.makedirs(self.claude, exist_ok=True)
        Path(self.claude, "settings.json").write_text("[" * 200000)
        self.assertEqual(self.roots("auto"), [os.path.realpath(self.root)])
        # The hook subprocess must still run the redirect gate, not fail open.
        self.assertEqual(self.run_hook("echo x > /etc/zz-write-roots", "auto"), "ask")

    def test_auto_memory_directory_under_a_foreign_claude_dir_is_no_root(self):
        foreign = os.path.join(self.base, "other_proj", ".claude", "memory")
        os.makedirs(foreign)
        self.user_settings({"autoMemoryDirectory": foreign})
        self.assertEqual(self.roots("auto"), [os.path.realpath(self.root)])


if __name__ == "__main__":
    unittest.main()
