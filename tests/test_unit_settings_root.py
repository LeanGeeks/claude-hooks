#!/usr/bin/env python3
"""
Unit/integration tests: settings follow the project root (epic 41, task 41-05).

The hook reads, and the Telegram Whitelist button writes, ``.claude/settings*.json``
at ``project_key.resolve_settings_root`` instead of the payload ``cwd`` (which
follows the agent's ``cd``). The root is stored on the permission row as
``settings_root`` so the router and the permissions MCP use it later.
"""

import atexit
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
_ISOLATED = Path(tempfile.mkdtemp(prefix="settings-root-store-"))
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
from permission_state_store import PermissionRequest, create_request, get_request  # noqa: E402

HOOK = str(_REPO_ROOT / ".claude" / "hooks" / "pretool_hook.py")
PROBE = "zzprobe run"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _write_settings(root, name, allow=None, deny=None):
    d = Path(root) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    perms = {}
    if allow:
        perms["allow"] = allow
    if deny:
        perms["deny"] = deny
    (d / name).write_text(json.dumps({"permissions": perms}))


def _row(cwd, settings_root=None, command=PROBE):
    return PermissionRequest(
        request_id="r1", session_id="s1", cwd=cwd, tool_name="Bash",
        tool_input={"command": command}, permission_suggestions=[],
        state="pending", created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z", expires_at="2024-01-01T00:05:00Z",
        settings_root=settings_root,
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="settings-root-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = os.path.join(self.tmp, "proj")
        self.sub = os.path.join(self.proj, "sub")
        os.makedirs(self.sub)
        project_key.clear_cache()

    def run_hook(self, cwd, command=PROBE, env_extra=None, drop=()):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_WORKSPACE_DIR", "CLAUDE_PROJECT_DIR") and k not in drop}
        env["CLAUDE_HOOK_DEBUG"] = "0"
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, HOOK], capture_output=True, text=True, timeout=20,
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                              "session_id": "s-settings-root", "cwd": cwd,
                              "permission_mode": "default"}),
            env=env, cwd=cwd)
        out = proc.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


class TestHookReadsProjectRoot(_Base):
    def test_project_dir_settings_json_allows_from_subdir(self):
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        self.assertEqual(
            self.run_hook(self.sub, env_extra={"CLAUDE_PROJECT_DIR": self.proj}), "allow")

    def test_project_dir_settings_local_json_allows_from_subdir(self):
        _write_settings(self.proj, "settings.local.json", allow=["Bash(zzprobe:*)"])
        self.assertEqual(
            self.run_hook(self.sub, env_extra={"CLAUDE_PROJECT_DIR": self.proj}), "allow")

    def test_git_top_level_fallback_without_project_dir(self):
        _git("init", "-q", cwd=self.proj)
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        self.assertEqual(self.run_hook(self.sub), "allow")

    def test_no_env_not_a_repo_reads_cwd(self):
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        self.assertEqual(self.run_hook(self.sub), "ask")  # today's behaviour
        _write_settings(self.sub, "settings.json", allow=["Bash(zzprobe:*)"])
        self.assertEqual(self.run_hook(self.sub), "allow")

    def test_workspace_override_wins_over_project_dir_and_git(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        _git("init", "-q", cwd=self.proj)
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        _write_settings(other, "settings.json", deny=["Bash(zzprobe:*)"])
        self.assertEqual(
            self.run_hook(self.sub, env_extra={"CLAUDE_PROJECT_DIR": self.proj,
                                               "CLAUDE_WORKSPACE_DIR": other}),
            "deny")

    def test_worktree_uses_its_own_settings_not_the_main_checkouts(self):
        main = os.path.join(self.tmp, "main")
        os.makedirs(main)
        _git("init", "-q", cwd=main)
        _git("commit", "-q", "--allow-empty", "-m", "i", cwd=main)
        wt = os.path.join(self.tmp, "wt")
        _git("worktree", "add", "-q", wt, "-b", "wtb", cwd=main)
        _write_settings(main, "settings.json", allow=["Bash(zzprobe:*)"])
        _write_settings(wt, "settings.json", deny=["Bash(zzprobe:*)"])
        wsub = os.path.join(wt, "d")
        os.makedirs(wsub)
        self.assertEqual(
            self.run_hook(wsub, env_extra={"CLAUDE_PROJECT_DIR": wt}), "deny")
        self.assertEqual(self.run_hook(wsub), "deny")  # git top level of a worktree

    def test_redirect_gate_accepts_the_settings_root(self):
        """41-02: the settings root is a write root, so `> ../x` from proj/sub
        lands inside it and is allowed; `> ../../x` (outside proj) still asks.
        Needs a tree outside /tmp (itself an allowed root)."""
        if not os.path.isdir("/var/tmp"):
            self.skipTest("no /var/tmp")
        outside = os.path.realpath(tempfile.mkdtemp(prefix="settings-root-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        proj = os.path.join(outside, "proj")
        sub = os.path.join(proj, "sub")
        os.makedirs(sub)
        _write_settings(proj, "settings.json", allow=["Bash(echo:*)"])
        self.assertEqual(
            self.run_hook(sub, command="echo hi > ../x",
                          env_extra={"CLAUDE_PROJECT_DIR": proj}),
            "allow")
        self.assertEqual(
            self.run_hook(sub, command="echo hi > ../../x",
                          env_extra={"CLAUDE_PROJECT_DIR": proj}),
            "ask")
        # control: the same command inside the cwd is fine
        self.assertEqual(
            self.run_hook(sub, command="echo hi > x",
                          env_extra={"CLAUDE_PROJECT_DIR": proj}),
            "allow")


class TestResolver(_Base):
    def test_order(self):
        env = {"CLAUDE_WORKSPACE_DIR": "/a", "CLAUDE_PROJECT_DIR": "/b"}
        self.assertEqual(project_key.resolve_settings_root(self.sub, env), "/a")
        self.assertEqual(
            project_key.resolve_settings_root(self.sub, {"CLAUDE_PROJECT_DIR": "/b"}), "/b")
        self.assertEqual(project_key.resolve_settings_root(self.sub, {}), self.sub)

    def test_fallback_is_logged_at_debug(self):
        with self.assertLogs("project_key", level="DEBUG") as cm:
            project_key.resolve_settings_root(self.sub, {})
        self.assertIn("settings root", cm.output[0])

    def test_settings_root_for_row(self):
        self.assertEqual(project_key.settings_root_for_row("/stored", self.sub), "/stored")
        self.assertEqual(project_key.settings_root_for_row(None, self.sub), self.sub)


class TestReplay(_Base):
    def _replay(self, entries, env_extra=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_WORKSPACE_DIR", "CLAUDE_PROJECT_DIR")}
        env.update({"CLAUDE_HOOK_DEBUG": "0", "CLAUDE_HOOK_REPLAY": "1"})
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, HOOK, "--dry-run"], capture_output=True, text=True,
            timeout=20, env=env, cwd=self.tmp,
            input="\n".join(json.dumps(e) for e in entries))
        return proc.stderr

    def test_replay_resolves_root_per_entry_ignoring_project_dir(self):
        _git("init", "-q", cwd=self.proj)
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        decoy = os.path.join(self.tmp, "decoy")
        os.makedirs(decoy)
        err = self._replay([{"command": PROBE, "workspace": self.sub}],
                           env_extra={"CLAUDE_PROJECT_DIR": decoy})
        self.assertIn("Decision: allow", err)

    def test_replay_workspace_override_first(self):
        _git("init", "-q", cwd=self.proj)
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        err = self._replay([{"command": PROBE, "workspace": self.sub}],
                           env_extra={"CLAUDE_WORKSPACE_DIR": other})
        self.assertIn("Decision: ask", err)


class TestRowAndWhitelist(_Base):
    def test_create_request_round_trips_settings_root(self):
        req = create_request("s", self.sub, "Bash", {"command": "x"}, [],
                             settings_root=self.proj)
        self.assertEqual(get_request(req.request_id).settings_root, self.proj)

    def test_old_row_without_field_loads(self):
        data = _row(self.sub).to_dict()
        del data["settings_root"]
        self.assertIsNone(PermissionRequest.from_dict(data).settings_root)

    def test_whitelist_tap_writes_at_stored_settings_root(self):
        row = _row(self.sub, settings_root=self.proj)
        self.assertTrue(router.process_whitelist_update(
            row, {"updatedPermissions": ["Bash(zzprobe:*)"]}))
        written = json.loads(
            (Path(self.proj) / ".claude" / "settings.local.json").read_text())
        self.assertIn("Bash(zzprobe:*)", written["permissions"]["allow"])
        self.assertFalse((Path(self.sub) / ".claude").exists())

    def test_whitelist_tap_on_old_row_writes_at_git_top_level(self):
        _git("init", "-q", cwd=self.proj)
        row = _row(self.sub, settings_root=None)
        self.assertTrue(router.process_whitelist_update(
            row, {"updatedPermissions": ["Bash(zzprobe:*)"]}))
        self.assertTrue((Path(self.proj) / ".claude" / "settings.local.json").is_file())
        self.assertFalse((Path(self.sub) / ".claude").exists())

    def test_card_annotations_use_stored_root(self):
        _write_settings(self.proj, "settings.json", allow=["Bash(zzprobe:*)"])
        ann = router._bash_annotations(_row(self.sub, settings_root=self.proj))
        self.assertFalse(ann.failed)
        self.assertEqual(ann.unknown, [])
        ann_old = router._bash_annotations(_row(self.sub, settings_root=None))
        self.assertEqual(ann_old.unknown, [PROBE])  # proj is no repo: reads cwd


class TestPermissionRequestHookStoresRoot(_Base):
    """The PermissionRequest hook computes the root and stores it on the row."""

    def _drive(self, cwd, env_extra=None):
        import io
        import permission_request_hook as prh
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_WORKSPACE_DIR", "CLAUDE_PROJECT_DIR")}
        env.update(env_extra or {})
        payload = {"session_id": "s-hook-root", "cwd": cwd, "tool_name": "Bash",
                   "tool_input": {"command": PROBE}, "permission_suggestions": [],
                   "permission_mode": "bypassPermissions"}  # auto-allow: no Telegram
        created = []

        def spy(*a, **kw):
            req = create_request(*a, **kw)
            created.append(req)
            return req

        with patch.dict(os.environ, env, clear=True), \
                patch.object(prh, "create_request", side_effect=spy), \
                patch.object(prh, "cleanup_expired_requests"), \
                patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                patch("builtins.print"):
            with self.assertRaises(SystemExit):
                prh.main()
        self.assertEqual(len(created), 1)
        return get_request(created[0].request_id)

    def test_row_stores_project_dir_root_from_subdir(self):
        row = self._drive(self.sub, {"CLAUDE_PROJECT_DIR": self.proj})
        self.assertEqual(row.cwd, self.sub)
        self.assertEqual(row.settings_root, self.proj)

    def test_row_stores_git_top_level_without_project_dir(self):
        _git("init", "-q", cwd=self.proj)
        row = self._drive(self.sub)
        self.assertEqual(row.settings_root, self.proj)


class TestMcpClassification(_Base):
    def test_classification_uses_row_settings_root(self):
        # Deny lives only at proj; proj is not a git repo, so without the
        # stored root the fallback would read proj/sub and miss it.
        _write_settings(self.proj, "settings.json", deny=["Bash(zzprobe:*)"])
        with_root = lib.classify(_row(self.sub, settings_root=self.proj))
        self.assertFalse(with_root.decidable)
        self.assertIn("deny", with_root.tier_reason)
        without = lib.classify(_row(self.sub, settings_root=None))
        self.assertTrue(without.decidable)

    def test_find_deny_collision_unchanged_reads_given_dir(self):
        _write_settings(self.proj, "settings.json", deny=["Bash(zzprobe:*)"])
        v = lib._build_validator(self.proj)
        self.assertIn("Bash(zzprobe:*)", list(v.denied_patterns))


if __name__ == "__main__":
    unittest.main()
