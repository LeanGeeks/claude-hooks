#!/usr/bin/env python3
"""
Unit tests for install.sh — task 29-02.

Tests the feature registry, manifest, probes, and the real-$HOME guard.
All tests require CLAUDE_INSTALL_NO_EXTERNAL=1 (asserted in setUp).

Run this module alone:
    python3 tests/run_all_tests.py --module unit_installer
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
FROZEN_SCRIPT = REPO / "install-claude-config.sh"

# Minimal settings.json that satisfies the installer's project-config check.
_MINIMAL_PROJECT_SETTINGS = json.dumps(
    {
        "permissions": {
            "allow": ["Bash(git status:*)", "Bash(git log:*)"],
            "deny": [],
            "ask": [],
        },
        "hooks": {},
    },
    indent=2,
)


def _make_project_tree(home: Path) -> None:
    """Create the .claude/ project tree the installer expects inside the repo area.
    We point HOME at tmp_home; the SCRIPT_DIR references are in the real repo."""
    pass  # The installer reads project files from SCRIPT_DIR (the real repo).


def run_installer(
    tmp_home: Path,
    extra_args: list | None = None,
    extra_env: dict | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run install.sh with HOME=tmp_home and CLAUDE_INSTALL_NO_EXTERNAL=1.

    This is the canonical test helper. Every test that exercises the installer
    goes through this function.
    """
    env = {
        **os.environ,
        "HOME": str(tmp_home),
        "CLAUDE_INSTALL_NO_EXTERNAL": "1",
        "CLAUDE_INSTALL_ASSUME_TTY": "0",
        # Prevent the real-$HOME guard from firing (tmp_home != real HOME anyway,
        # but set it explicitly in case getent behaves oddly in CI).
        "CLAUDE_INSTALL_EPIC29_LIVE": "1",
    }
    if extra_env:
        env.update(extra_env)

    cmd = ["bash", str(INSTALL_SH)] + (extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=60,
    )


def run_probe(tmp_home: Path, feature: str) -> bool:
    """
    Run `install.sh --probe <feature>` with HOME=tmp_home.
    Returns True if probe exits 0 (installed), False otherwise.
    """
    result = run_installer(tmp_home, extra_args=["--probe", feature])
    return result.returncode == 0


def read_settings(tmp_home: Path) -> dict:
    """Read ~/.claude/settings.json from tmp_home."""
    settings_path = tmp_home / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}
    return json.loads(settings_path.read_text())


def read_claude_json(tmp_home: Path) -> dict:
    """Read ~/.claude.json from tmp_home."""
    claude_json_path = tmp_home / ".claude.json"
    if not claude_json_path.exists():
        return {}
    return json.loads(claude_json_path.read_text())


def read_manifest(tmp_home: Path) -> dict:
    """Read ~/.claude/install-manifest.json from tmp_home."""
    manifest_path = tmp_home / ".claude" / "install-manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text())


class InstallerTestBase(unittest.TestCase):
    """Base class: asserts CLAUDE_INSTALL_NO_EXTERNAL=1 is set in the environment."""

    def setUp(self):
        # Every test in this module must run with the external-surface guard set.
        # This assertion fires when a contributor runs the tests without the env
        # var, preventing accidental crontab/systemd/tmux edits on the developer's
        # machine.
        self.assertIn(
            "CLAUDE_INSTALL_NO_EXTERNAL",
            os.environ,
            "CLAUDE_INSTALL_NO_EXTERNAL must be set to run installer tests. "
            "Use: CLAUDE_INSTALL_NO_EXTERNAL=1 python3 tests/run_all_tests.py --module unit_installer",
        )
        self.tmp_home = Path(tempfile.mkdtemp(prefix="claude-hooks-installer-test-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp_home), ignore_errors=True)


# =============================================================================
# Source tests — install-claude-config.sh must keep the FROZEN header
# =============================================================================

class TestFrozenHeader(unittest.TestCase):
    """Source test: install-claude-config.sh still carries the FROZEN header."""

    def setUp(self):
        self.assertIn("CLAUDE_INSTALL_NO_EXTERNAL", os.environ,
                      "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1")

    def test_frozen_header_present(self):
        """install-claude-config.sh must have the FROZEN header from §0."""
        text = FROZEN_SCRIPT.read_text()
        self.assertIn(
            "FROZEN for epic 29",
            text,
            "install-claude-config.sh is missing the FROZEN header. "
            "A later task in the epic must not have started editing it. "
            "See tasks/29_installer_interactive/state.md.",
        )

    def test_frozen_script_exists(self):
        """install-claude-config.sh must still exist alongside install.sh."""
        self.assertTrue(
            FROZEN_SCRIPT.is_file(),
            f"install-claude-config.sh not found at {FROZEN_SCRIPT}",
        )
        self.assertTrue(
            INSTALL_SH.is_file(),
            f"install.sh not found at {INSTALL_SH}",
        )


# =============================================================================
# Real-$HOME guard
# =============================================================================

class TestRealHomeGuard(unittest.TestCase):
    """install.sh refuses to run against the real $HOME without override."""

    def setUp(self):
        self.assertIn("CLAUDE_INSTALL_NO_EXTERNAL", os.environ,
                      "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1")

    def test_guard_fires_on_real_home(self):
        """With HOME=real home and no CLAUDE_INSTALL_EPIC29_LIVE, install.sh exits non-zero."""
        real_home = Path.home()
        tmp_dir = Path(tempfile.mkdtemp(prefix="claude-hooks-guard-test-"))
        try:
            env = {
                **os.environ,
                "HOME": str(real_home),
                "CLAUDE_INSTALL_NO_EXTERNAL": "1",
                # Do NOT set CLAUDE_INSTALL_EPIC29_LIVE — that's what triggers the guard
            }
            if "CLAUDE_INSTALL_EPIC29_LIVE" in env:
                del env["CLAUDE_INSTALL_EPIC29_LIVE"]

            result = subprocess.run(
                ["bash", str(INSTALL_SH)],
                capture_output=True,
                text=True,
                cwd=str(REPO),
                env=env,
                timeout=10,
            )
            self.assertNotEqual(
                result.returncode, 0,
                "install.sh must exit non-zero when run against real $HOME without override. "
                f"stdout: {result.stdout[:200]}",
            )
            # Verify the error message names the alternatives
            self.assertIn(
                "install-claude-config.sh",
                result.stderr + result.stdout,
                "Guard message should name ./install-claude-config.sh as an alternative",
            )
            # Belt-and-braces: verify nothing was written to tmp_dir
            # (tmp_dir never got HOME pointed at it so this is trivially true,
            # but it's a named checkpoint so a future refactor of this test can't
            # accidentally miss the assertion)
            self.assertEqual(
                list(tmp_dir.iterdir()),
                [],
                "No files should have been written to tmp_dir",
            )
        finally:
            shutil.rmtree(str(tmp_dir), ignore_errors=True)

    def test_override_allows_run(self):
        """CLAUDE_INSTALL_EPIC29_LIVE=1 overrides the guard."""
        tmp_home = Path(tempfile.mkdtemp(prefix="claude-hooks-guard-override-"))
        try:
            result = run_installer(
                tmp_home,
                extra_env={"CLAUDE_INSTALL_EPIC29_LIVE": "1"},
            )
            # We expect it to run (not fail with the guard message).
            # It may fail for other reasons (missing project files in edge cases),
            # but if it fails it must NOT be the guard message.
            if result.returncode != 0:
                self.assertNotIn(
                    "under construction",
                    result.stderr + result.stdout,
                    "With CLAUDE_INSTALL_EPIC29_LIVE=1, the guard should not fire",
                )
        finally:
            shutil.rmtree(str(tmp_home), ignore_errors=True)


# =============================================================================
# Registry integrity
# =============================================================================

class TestRegistryIntegrity(InstallerTestBase):
    """
    The three startup assertions fire on a broken registry and pass on the real one.

    Since the assertions are embedded in install.sh and run at startup, we verify
    them by inspecting the script source for structural correctness. We also run
    the installer with a clean home and assert it does NOT produce a registry
    error.
    """

    def test_module_owners_covers_required_hooks(self):
        """Every MODULE_OWNERS key appears in REQUIRED_HOOKS and vice versa."""
        text = INSTALL_SH.read_text()

        # Extract REQUIRED_HOOKS list from source
        import re
        rh_match = re.search(
            r'REQUIRED_HOOKS=\(([^)]+)\)', text, re.DOTALL
        )
        self.assertIsNotNone(rh_match, "REQUIRED_HOOKS not found in install.sh")
        required_hooks = set(re.findall(r'"([^"]+\.py)"', rh_match.group(1)))
        self.assertGreater(len(required_hooks), 0, "REQUIRED_HOOKS should be non-empty")

        # Extract MODULE_OWNERS keys from source
        mo_match = re.search(
            r'declare -A MODULE_OWNERS=\(([^)]+)\)', text, re.DOTALL
        )
        self.assertIsNotNone(mo_match, "MODULE_OWNERS not found in install.sh")
        mo_keys = set(re.findall(r'\[([^\]]+\.py)\]', mo_match.group(1)))
        self.assertGreater(len(mo_keys), 0, "MODULE_OWNERS should be non-empty")

        # They must match exactly
        only_in_rh = required_hooks - mo_keys
        only_in_mo = mo_keys - required_hooks
        self.assertEqual(
            only_in_rh, set(),
            f"REQUIRED_HOOKS entries missing from MODULE_OWNERS: {only_in_rh}",
        )
        self.assertEqual(
            only_in_mo, set(),
            f"MODULE_OWNERS keys missing from REQUIRED_HOOKS: {only_in_mo}",
        )

    def test_registry_assertion_fires_on_missing_module_owner(self):
        """
        If MODULE_OWNERS is missing an entry, assert_registry_integrity exits non-zero.

        We simulate this by creating a patched install.sh that has an extra
        REQUIRED_HOOKS entry with no corresponding MODULE_OWNERS key.
        """
        text = INSTALL_SH.read_text()
        # Inject a fake module into REQUIRED_HOOKS. The array in install.sh is
        # multi-line with ) on its own line, so we insert after the last entry.
        patched = text.replace(
            '    "questions_listen_lib.py"\n)',
            '    "questions_listen_lib.py"\n    "fake_orphan_module.py"\n)',
            1,
        )
        self.assertIn("fake_orphan_module.py", patched, "Patch must apply")

        patched_path = self.tmp_home / "install_patched.sh"
        patched_path.write_text(patched)
        patched_path.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(self.tmp_home),
            "CLAUDE_INSTALL_NO_EXTERNAL": "1",
            "CLAUDE_INSTALL_EPIC29_LIVE": "1",
            # Point back at the real repo so PROJECT_CONFIG resolves correctly
            "CLAUDE_INSTALL_SCRIPT_DIR": str(REPO),
        }
        result = subprocess.run(
            ["bash", str(patched_path)],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0,
            "Patched script with orphaned REQUIRED_HOOKS entry must exit non-zero")
        self.assertIn(
            "Registry",
            result.stderr + result.stdout,
            "Error must mention Registry",
        )

    def test_registry_assertion_fires_on_unsatisfied_requires(self):
        """
        If a feature has a _requires() pointing to a non-existent id, assert fires.
        """
        text = INSTALL_SH.read_text()
        # Patch telegram's _requires to depend on a non-existent feature
        patched = text.replace(
            'feature_telegram_requires()   { echo "permission-hooks"; }',
            'feature_telegram_requires()   { echo "permission-hooks nonexistent-feature"; }',
            1,
        )
        self.assertIn("nonexistent-feature", patched, "Patch must apply")

        patched_path = self.tmp_home / "install_patched_requires.sh"
        patched_path.write_text(patched)
        patched_path.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(self.tmp_home),
            "CLAUDE_INSTALL_NO_EXTERNAL": "1",
            "CLAUDE_INSTALL_EPIC29_LIVE": "1",
            "CLAUDE_INSTALL_SCRIPT_DIR": str(REPO),
        }
        result = subprocess.run(
            ["bash", str(patched_path)],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0,
            "Patched script with unsatisfied _requires must exit non-zero")
        self.assertIn(
            "Registry",
            result.stderr + result.stdout,
        )

    def test_registry_assertion_fires_on_wrong_order(self):
        """
        If a feature appears before its _requires(), assert fires.

        We patch telegram's _requires to say 'amux' (which comes after telegram in
        FEATURES), violating the ordering invariant.
        """
        text = INSTALL_SH.read_text()
        patched = text.replace(
            'feature_telegram_requires()   { echo "permission-hooks"; }',
            'feature_telegram_requires()   { echo "permission-hooks amux"; }',
            1,
        )
        self.assertIn(
            'echo "permission-hooks amux"', patched, "Patch must apply"
        )

        patched_path = self.tmp_home / "install_patched_order.sh"
        patched_path.write_text(patched)
        patched_path.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(self.tmp_home),
            "CLAUDE_INSTALL_NO_EXTERNAL": "1",
            "CLAUDE_INSTALL_EPIC29_LIVE": "1",
            "CLAUDE_INSTALL_SCRIPT_DIR": str(REPO),
        }
        result = subprocess.run(
            ["bash", str(patched_path)],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0,
            "Patched script with out-of-order _requires must exit non-zero")
        self.assertIn(
            "Registry",
            result.stderr + result.stdout,
        )

    def test_real_registry_passes_assertions(self):
        """Running install.sh with a clean home passes all registry assertions."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline,permission-hooks,profiles,permissions-allowlist,context-mcp"],
        )
        # If registry assertions fire, the error mentions "Registry integrity"
        combined = result.stdout + result.stderr
        self.assertNotIn(
            "Registry integrity",
            combined,
            f"Registry assertions must pass on the real script. Output: {combined[:500]}",
        )


# =============================================================================
# Probe matrix
# =============================================================================

class TestProbeMatrix(InstallerTestBase):
    """
    Probe correctness: clean home → not installed; after --only <id> → installed;
    artifact without wiring → not installed.
    """

    def test_clean_home_all_not_installed(self):
        """On a clean tmp_home, all feature probes return not-installed."""
        features_to_probe = [
            "statusline",
            "permission-hooks",
            "telegram",
            "profiles",
            "permissions-allowlist",
            "context-mcp",
        ]
        for feature in features_to_probe:
            with self.subTest(feature=feature):
                installed = run_probe(self.tmp_home, feature)
                self.assertFalse(
                    installed,
                    f"Feature '{feature}' should not be installed on clean home",
                )

    def test_only_statusline_installs_statusline(self):
        """After --only statusline, the statusline probe returns installed."""
        result = run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        self.assertEqual(
            result.returncode, 0,
            f"--only statusline should succeed. stderr: {result.stderr[:300]}",
        )
        self.assertTrue(
            run_probe(self.tmp_home, "statusline"),
            "statusline probe should return installed after --only statusline",
        )

    def test_only_statusline_leaves_others_not_installed(self):
        """After --only statusline, other features remain not-installed."""
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        for feature in ["permission-hooks", "telegram", "permissions-allowlist", "context-mcp"]:
            with self.subTest(feature=feature):
                installed = run_probe(self.tmp_home, feature)
                self.assertFalse(
                    installed,
                    f"Feature '{feature}' should not be installed when only statusline was selected",
                )

    def test_statusline_artifact_without_wiring_is_not_installed(self):
        """
        A statusline.py on disk with no .statusLine key in settings.json is NOT installed.

        This is architecture §4's critical rule: files alone are not enough.
        """
        # Manually create the statusline artifact without running the installer
        statusline_dir = self.tmp_home / ".claude" / "statusline"
        statusline_dir.mkdir(parents=True, exist_ok=True)
        (statusline_dir / "statusline.py").write_text("# fake statusline\n")

        # Probe must return not-installed (no .statusLine in settings.json)
        installed = run_probe(self.tmp_home, "statusline")
        self.assertFalse(
            installed,
            "statusline.py on disk without settings.json wiring must probe as not-installed",
        )

    def test_permission_hooks_artifact_without_wiring_is_not_installed(self):
        """Hook .py present but no hooks.PreToolUse entry = not installed."""
        hooks_dir = self.tmp_home / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "pretool_hook.py").write_text("# fake hook\n")
        # Create settings.json without hooks entry
        settings_dir = self.tmp_home / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text("{}")

        installed = run_probe(self.tmp_home, "permission-hooks")
        self.assertFalse(
            installed,
            "pretool_hook.py without hooks.PreToolUse wiring must probe as not-installed",
        )


# =============================================================================
# Manifest
# =============================================================================

class TestManifest(InstallerTestBase):
    """Manifest round-trip, unknown schema, and aborted-run invariant."""

    def test_manifest_written_after_successful_run(self):
        """A successful run writes ~/.claude/install-manifest.json."""
        result = run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        self.assertEqual(result.returncode, 0, f"Run should succeed. stderr: {result.stderr[:300]}")
        manifest = read_manifest(self.tmp_home)
        self.assertIn("schema", manifest, "Manifest must have 'schema' key")
        self.assertEqual(manifest["schema"], 1)
        self.assertIn("features", manifest, "Manifest must have 'features' key")

    def test_manifest_round_trip(self):
        """The manifest can be written and re-read with the same schema."""
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        manifest = read_manifest(self.tmp_home)
        self.assertEqual(manifest.get("schema"), 1)
        self.assertIn("repo", manifest)
        self.assertIn("updated_at", manifest)
        # statusline should be recorded as installed
        features = manifest.get("features", {})
        self.assertIn("statusline", features, "Installed feature must appear in manifest")
        self.assertEqual(features["statusline"]["state"], "installed")

    def test_manifest_unknown_schema_is_ignored(self):
        """
        A manifest with unknown schema is ignored (not a hard error), and the
        installer overwrites it at the end of a successful run.
        """
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "schema": 999,
            "features": {"statusline": {"state": "installed"}},
        }))

        result = run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        self.assertEqual(result.returncode, 0, f"Should succeed with unknown-schema manifest. stderr: {result.stderr[:300]}")

        # The installer should have warned about the unknown schema
        combined = result.stdout + result.stderr
        self.assertIn("unknown schema", combined, "Should warn about unknown schema")

        # At the end of the run, the manifest should be overwritten with schema 1
        manifest = read_manifest(self.tmp_home)
        self.assertEqual(manifest.get("schema"), 1, "Manifest should be overwritten with schema 1")

    def test_aborted_run_leaves_prior_manifest(self):
        """
        A run that fails partway through leaves the prior manifest byte-identical.

        We simulate an abort by using a patched installer that exits after the
        settings write but before manifest_write() is called.
        """
        # First, install statusline to create a known-good manifest
        first_result = run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        self.assertEqual(
            first_result.returncode, 0,
            f"Pre-condition: first run should succeed. stderr: {first_result.stderr[:300]}",
        )
        manifest_before = read_manifest(self.tmp_home)
        self.assertNotEqual(manifest_before, {}, "Pre-condition: manifest should exist")

        # Record the manifest bytes
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        manifest_bytes_before = manifest_path.read_bytes()

        # Patch install.sh to exit non-zero just before manifest_write() call.
        # Use the specific comment+call pair as the patch target to be unambiguous.
        text = INSTALL_SH.read_text()
        target = "# Write manifest once at end of successful run\nmanifest_write"
        replacement = "# Write manifest once at end of successful run\nexit 99  # simulated abort\nmanifest_write"
        patched = text.replace(target, replacement, 1)
        self.assertIn("exit 99", patched, "Patch must apply")

        patched_path = self.tmp_home / "install_abort.sh"
        patched_path.write_text(patched)
        patched_path.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(self.tmp_home),
            "CLAUDE_INSTALL_NO_EXTERNAL": "1",
            "CLAUDE_INSTALL_EPIC29_LIVE": "1",
            # Critical: point back at the real repo so PROJECT_CONFIG resolves.
            # Without this, SCRIPT_DIR = self.tmp_home and PROJECT_CONFIG
            # ($tmp/.claude/settings.json) is the installed file, not the source.
            "CLAUDE_INSTALL_SCRIPT_DIR": str(REPO),
        }
        result = subprocess.run(
            ["bash", str(patched_path), "--only", "statusline"],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )
        self.assertEqual(
            result.returncode, 99,
            f"Patched script should exit 99 (simulated abort). "
            f"stdout: {result.stdout[:200]} stderr: {result.stderr[:200]}",
        )

        # Manifest must be byte-identical to before the aborted run
        manifest_bytes_after = manifest_path.read_bytes()
        self.assertEqual(
            manifest_bytes_before,
            manifest_bytes_after,
            "Aborted run must leave the prior manifest byte-identical",
        )

    def test_first_run_no_manifest_is_not_error(self):
        """A missing manifest on first run is not an error."""
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        self.assertFalse(manifest_path.exists(), "Pre-condition: no manifest on clean home")

        result = run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        self.assertEqual(
            result.returncode, 0,
            f"First run without manifest should succeed. stderr: {result.stderr[:300]}",
        )


# =============================================================================
# Probe via --probe CLI flag
# =============================================================================

class TestProbeFlag(InstallerTestBase):
    """The --probe <feature> flag exits 0 if installed, 1 if not."""

    def test_probe_exits_1_on_clean_home(self):
        """--probe statusline exits 1 on clean home."""
        result = run_installer(self.tmp_home, extra_args=["--probe", "statusline"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not-installed", result.stdout + result.stderr)

    def test_probe_exits_0_after_install(self):
        """--probe statusline exits 0 after successful install."""
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_installer(self.tmp_home, extra_args=["--probe", "statusline"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("installed", result.stdout + result.stderr)

    def test_probe_unknown_feature_exits_2(self):
        """--probe with unknown feature id exits 2."""
        result = run_installer(self.tmp_home, extra_args=["--probe", "no-such-feature"])
        self.assertEqual(result.returncode, 2)

    def test_probe_statusline_wiring_check(self):
        """
        --probe statusline exits 1 when statusline.py exists but settings.json
        lacks .statusLine (artifact without wiring is not installed).
        """
        statusline_dir = self.tmp_home / ".claude" / "statusline"
        statusline_dir.mkdir(parents=True, exist_ok=True)
        (statusline_dir / "statusline.py").write_text("# fake\n")
        # No settings.json → probe must return not-installed
        result = run_installer(self.tmp_home, extra_args=["--probe", "statusline"])
        self.assertEqual(
            result.returncode, 1,
            "Probe must return not-installed when wiring is absent",
        )


if __name__ == "__main__":
    # When run directly, set CLAUDE_INSTALL_NO_EXTERNAL for convenience
    os.environ.setdefault("CLAUDE_INSTALL_NO_EXTERNAL", "1")
    unittest.main()
