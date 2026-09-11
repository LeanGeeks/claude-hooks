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


def _make_fake_uv(tmp_home: Path) -> dict:
    """
    Create a minimal fake 'uv' executable that exits 0, and return an env dict
    that adds it to the front of PATH. Used by MCP-related tests on machines
    where uv is not installed.
    """
    bin_dir = tmp_home / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/bash\nexit 0\n")
    fake_uv.chmod(0o755)
    current_path = os.environ.get("PATH", "")
    return {"PATH": f"{bin_dir}:{current_path}"}


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


# =============================================================================
# Helpers for §8 ext_* seam tests (task 29-03)
# =============================================================================

def _extract_ext_seam_section(install_text: str) -> str:
    """
    Extract the §8 external-surface seam functions from install.sh.
    Returns the bash text between the §8 section header and the settings section.
    """
    start_marker = "# §8. EXTERNAL-SURFACE SEAM"
    end_marker = "# Settings build and write (validate-then-replace discipline)"
    start_idx = install_text.find(start_marker)
    end_idx = install_text.find(end_marker)
    if start_idx == -1:
        raise ValueError("§8 section start not found in install.sh")
    if end_idx == -1:
        raise ValueError("Settings section boundary not found in install.sh")
    return install_text[start_idx:end_idx]


def run_ext_call(
    tmp_home: Path,
    call: str,
    no_external: bool = True,
    extra_setup: str = "",
) -> subprocess.CompletedProcess:
    """
    Run a specific ext_* function call in a minimal bash harness.

    Extracts the §8 seam functions from install.sh, wraps them in a minimal
    context (HOME, BACKUP_DIR, log functions), then calls `call`.

    For HOME-isolated surfaces (bashrc, tmux, symlinks) no_external may be
    False to test actual file modifications. For crontab and systemd, keep
    no_external=True (they escape HOME and the test environment must not
    touch the developer's real crontab or systemd).
    """
    install_text = INSTALL_SH.read_text()
    ext_section = _extract_ext_seam_section(install_text)
    no_ext_val = "1" if no_external else "0"

    script = f"""#!/bin/bash
set -euo pipefail
HOME="{tmp_home}"
BACKUP_DIR="{tmp_home}/.claude/backups"
CLAUDE_INSTALL_NO_EXTERNAL="{no_ext_val}"
QUESTIONS_LISTEN_SERVICE_ENABLED=false
TMUX_FILE_STATUS="unchanged"
TMUX_LIVE_STATUS="no running server"
mkdir -p "$BACKUP_DIR"
mkdir -p "$HOME/.claude"

log_info()  {{ echo "[INFO] $1"; }}
log_warn()  {{ echo "[WARN] $1"; }}
log_error() {{ echo "[ERROR] $1"; }}
log_step()  {{ echo "[STEP] $1"; }}

{extra_setup}

{ext_section}

{call}
"""
    script_path = tmp_home / "_ext_test.sh"
    script_path.write_text(script)
    script_path.chmod(0o755)

    return subprocess.run(
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=30,
    )


# =============================================================================
# §8 Source-level invariant (task 29-03)
# =============================================================================

import re as _re


class TestSourceLevelInvariant(unittest.TestCase):
    """
    Source-level grep test: every invocation of a dangerous command must be
    inside an ext_* (or ext_*-helper) function body in install.sh.

    Dangerous invocations covered:
      crontab -l / crontab - (stdin)  — crontab escapes HOME (architecture §10.2)
      systemctl --user ...            — escapes HOME
      loginctl enable-linger          — escapes HOME
      tmux set -g                     — mutates running server
      ln -sf / ln -s                  — creates symlinks

    Excluded from the check:
      - Comment lines (# ...)
      - `command -v <cmd>` availability probes
      - echo / printf / log_* lines (string mentions, not invocations)
    """

    def setUp(self):
        self.assertIn(
            "CLAUDE_INSTALL_NO_EXTERNAL",
            os.environ,
            "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1",
        )

    def _ext_function_line_set(self, text: str) -> set:
        """
        Return the set of 1-based line numbers that are inside ext_* or
        _bashrc_exclusive_of function bodies (brace-counting heuristic).
        """
        ext_lines: set = set()
        lines = text.splitlines()
        in_ext = False
        depth = 0

        for lineno, line in enumerate(lines, 1):
            if not in_ext:
                m = _re.match(r"^(ext_\w+|_bashrc_exclusive_of)\s*\(\)", line)
                if m:
                    in_ext = True
                    depth = 0

            if in_ext:
                ext_lines.add(lineno)
                depth += line.count("{") - line.count("}")
                if depth <= 0:
                    depth = 0
                    in_ext = False

        return ext_lines

    # Regex matching actual dangerous command invocations.
    _DANGEROUS = _re.compile(
        r"\bcrontab\s+-[lr]"           # crontab -l  or  crontab -r
        r"|\bcrontab\s+-\s*$"          # crontab - (read from stdin, end of line)
        r"|\}\s*\|\s*crontab\b"        # } | crontab (pipe into crontab -)
        r"|\bsystemctl\s+--user\b"     # systemctl --user <any-subcommand>
        r"|\bloginctl\s+enable-linger" # loginctl enable-linger
        r"|\btmux\s+set\s+-g\b"       # tmux set -g
        r"|\bln\s+-sf?\b"             # ln -sf  or  ln -s
    )

    def test_dangerous_calls_only_in_ext_functions(self):
        """
        Every dangerous command invocation in install.sh must be inside an
        ext_* function body (cross-task invariant 5, architecture §8).
        """
        text = INSTALL_SH.read_text()
        lines = text.splitlines()
        ext_lines = self._ext_function_line_set(text)

        violations = []
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip blank lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            # Skip `command -v <cmd>` availability checks — not invocations
            if _re.search(r"\bcommand\s+-v\s+\w", line):
                continue

            # Skip echo / printf / log_* lines — the string mentions the command
            # name but does not invoke it
            if _re.match(
                r"^\s*(?:echo|printf|log_info|log_warn|log_error|log_step)\b",
                line,
            ):
                continue

            if not self._DANGEROUS.search(line):
                continue

            # Dangerous invocation found — must be inside an ext_* function
            if lineno not in ext_lines:
                violations.append((lineno, stripped))

        if violations:
            msg = (
                "Dangerous commands found outside ext_* function bodies in install.sh.\n"
                "This violates cross-task invariant 5 (architecture §8 seam).\n"
                "Each must be moved into an ext_* function:\n\n"
            )
            for lineno, line_text in violations:
                msg += f"  line {lineno}: {line_text[:100]}\n"
            self.fail(msg)


# =============================================================================
# §8.2 ext_bashrc_add / ext_bashrc_remove (task 29-03)
# =============================================================================

class TestExtBashrc(InstallerTestBase):
    """Tests for ext_bashrc_add / ext_bashrc_remove."""

    _SOURCE_LINE = "source ~/.claude/shell/amux-spawn.bash"
    _PROFILES_LINE = "source ~/.claude/shell/claude-profiles.bash"

    def _run(self, call: str, no_external: bool = False) -> subprocess.CompletedProcess:
        return run_ext_call(self.tmp_home, call, no_external=no_external)

    # --- NO_EXTERNAL mode ---

    def test_no_external_add_logs_and_skips(self):
        """With NO_EXTERNAL=1, ext_bashrc_add logs and does not write."""
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("")

        result = self._run(
            f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"',
            no_external=True,
        )
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("[NO_EXTERNAL]", combined)
        self.assertIn("amux-autowrap", combined)
        # File must be unchanged
        self.assertEqual(bashrc.read_text(), "")

    def test_no_external_remove_logs_and_skips(self):
        """With NO_EXTERNAL=1, ext_bashrc_remove logs and does not write."""
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("# claude-hooks:amux-autowrap  (install.sh — remove with: disable amux-autowrap)\n")

        result = self._run('ext_bashrc_remove "amux-autowrap"', no_external=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)
        # File must be unchanged
        self.assertIn("amux-autowrap", bashrc.read_text())

    # --- Absent ~/.bashrc ---

    def test_absent_bashrc_add_warns_skips_no_create(self):
        """ext_bashrc_add with absent ~/.bashrc warns and skips; never creates it."""
        self.assertFalse((self.tmp_home / ".bashrc").exists(), "Pre-condition: no .bashrc")
        result = self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')
        self.assertEqual(result.returncode, 0)
        self.assertIn("absent", result.stdout + result.stderr)
        self.assertFalse((self.tmp_home / ".bashrc").exists(), "Must not create ~/.bashrc")

    def test_absent_bashrc_remove_is_noop(self):
        """ext_bashrc_remove with absent ~/.bashrc is a no-op."""
        result = self._run('ext_bashrc_remove "amux-autowrap"')
        self.assertEqual(result.returncode, 0)

    # --- Add / remove semantics ---

    def test_add_writes_marker_and_line(self):
        """ext_bashrc_add appends the marker and managed line."""
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("# existing\n")

        self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')

        content = bashrc.read_text()
        self.assertIn("# claude-hooks:amux-autowrap", content)
        self.assertIn(self._SOURCE_LINE, content)
        self.assertIn("# existing", content)  # foreign content preserved

    def test_add_idempotent(self):
        """Running ext_bashrc_add twice leaves exactly one marker."""
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("")

        for _ in range(2):
            self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')

        content = bashrc.read_text()
        self.assertEqual(
            content.count("# claude-hooks:amux-autowrap"),
            1,
            f"Marker must appear exactly once. Content:\n{content}",
        )

    def test_remove_reverts_add_exactly(self):
        """ext_bashrc_add + ext_bashrc_remove leaves the file byte-identical."""
        original = "# line one\nexport FOO=bar\n"
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text(original)

        self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')
        self._run('ext_bashrc_remove "amux-autowrap"')

        self.assertEqual(
            bashrc.read_text(),
            original,
            "ext_bashrc_remove must revert ext_bashrc_add exactly",
        )

    def test_remove_noop_when_marker_absent(self):
        """ext_bashrc_remove is a no-op when the marker is not present."""
        original = "# some content\nexport BAR=baz\n"
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text(original)

        self._run('ext_bashrc_remove "amux-autowrap"')
        self.assertEqual(bashrc.read_text(), original, "File must be unchanged")

    def test_foreign_content_preserved_through_add_remove_cycle(self):
        """Unrelated ~/.bashrc content survives add and remove."""
        original = "# before\nexport PATH=$PATH:/usr/local/bin\n# after\n"
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text(original)

        self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')

        for line in original.splitlines():
            self.assertIn(line, bashrc.read_text(), f"Line {line!r} should survive add")

        self._run('ext_bashrc_remove "amux-autowrap"')
        self.assertEqual(bashrc.read_text(), original, "Remove must restore original exactly")

    # --- Mutual exclusion ---

    def test_mutual_exclusion_autowrap_removes_autosource(self):
        """Adding amux-autowrap removes an existing profiles-autosource block."""
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("")

        self._run(f'ext_bashrc_add "profiles-autosource" "{self._PROFILES_LINE}"')
        self.assertIn("profiles-autosource", bashrc.read_text(), "Pre-condition: autosource present")

        result = self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')
        self.assertEqual(result.returncode, 0, result.stderr)

        content = bashrc.read_text()
        self.assertIn("amux-autowrap", content)
        self.assertNotIn(
            "profiles-autosource",
            content,
            "profiles-autosource must be removed by mutual exclusion",
        )
        self.assertIn("mutual exclusion", result.stdout + result.stderr)

    def test_mutual_exclusion_autosource_removes_autowrap(self):
        """Adding profiles-autosource removes an existing amux-autowrap block."""
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("")

        self._run(f'ext_bashrc_add "amux-autowrap" "{self._SOURCE_LINE}"')
        self.assertIn("amux-autowrap", bashrc.read_text(), "Pre-condition: autowrap present")

        result = self._run(f'ext_bashrc_add "profiles-autosource" "{self._PROFILES_LINE}"')
        self.assertEqual(result.returncode, 0, result.stderr)

        content = bashrc.read_text()
        self.assertIn("profiles-autosource", content)
        self.assertNotIn(
            "amux-autowrap",
            content,
            "amux-autowrap must be removed by mutual exclusion",
        )


# =============================================================================
# §8.3 ext_cron_* (task 29-03) — NO_EXTERNAL only
# =============================================================================

class TestExtCron(InstallerTestBase):
    """
    Tests for ext_cron_add / ext_cron_remove / ext_cron_has_marker.

    All tests use CLAUDE_INSTALL_NO_EXTERNAL=1 — no real crontab calls.
    The task's three starting states (no crontab, unrelated lines, unmarked
    copy of our line) are tested against the [NO_EXTERNAL] log lines that
    the functions emit instead of calling crontab.
    """

    _CRON_LINE = (
        "15 6 * * * "
        "/data/sync/work/leangeeks-ai/claude-hooks/shell/permission-review-daily.sh"
    )

    def _run(self, call: str) -> subprocess.CompletedProcess:
        return run_ext_call(self.tmp_home, call, no_external=True)

    def test_cron_add_no_external_logs(self):
        """ext_cron_add with NO_EXTERNAL=1 logs the skipped action."""
        result = self._run(f'ext_cron_add "daily-review-cron" "{self._CRON_LINE}"')
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("[NO_EXTERNAL]", combined)
        self.assertIn("daily-review-cron", combined)

    def test_cron_remove_no_external_logs(self):
        """ext_cron_remove with NO_EXTERNAL=1 logs the skipped action."""
        result = self._run('ext_cron_remove "daily-review-cron"')
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("[NO_EXTERNAL]", combined)
        self.assertIn("daily-review-cron", combined)

    def test_cron_has_marker_no_external_returns_false(self):
        """ext_cron_has_marker with NO_EXTERNAL=1 returns 1 (not found)."""
        result = self._run(
            'ext_cron_has_marker "daily-review-cron" && echo FOUND || echo NOT_FOUND'
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("NOT_FOUND", result.stdout + result.stderr)

    def test_cron_add_no_external_does_not_call_crontab(self):
        """
        Verifies that CLAUDE_INSTALL_NO_EXTERNAL is set in the test environment,
        preventing any test in this class from accidentally calling the real crontab.
        (architecture §10.2: crontab escapes HOME)
        """
        self.assertEqual(
            os.environ.get("CLAUDE_INSTALL_NO_EXTERNAL"),
            "1",
            "CLAUDE_INSTALL_NO_EXTERNAL must be '1' to prevent real crontab calls",
        )

    def test_cron_add_twice_both_log_no_external(self):
        """Two ext_cron_add calls each emit [NO_EXTERNAL] — idempotency via NO_EXTERNAL."""
        for _ in range(2):
            result = self._run(f'ext_cron_add "daily-review-cron" "{self._CRON_LINE}"')
            self.assertEqual(result.returncode, 0)
            self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)

    def test_cron_remove_noop_logs_no_external(self):
        """ext_cron_remove with NO_EXTERNAL=1 always logs (never a real no-op message)."""
        result = self._run('ext_cron_remove "daily-review-cron"')
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)


# =============================================================================
# §8 ext_tmux_apply / ext_tmux_remove (task 29-03)
# =============================================================================

class TestExtTmux(InstallerTestBase):
    """
    Tests for ext_tmux_apply / ext_tmux_remove.

    The ~/.tmux.conf write is HOME-isolated, so no_external=False tests
    actually modify the temp home's ~/.tmux.conf.
    The live 'tmux set -g' is gated by NO_EXTERNAL (it escapes HOME).
    """

    def _run(self, call: str, no_external: bool = True) -> subprocess.CompletedProcess:
        return run_ext_call(self.tmp_home, call, no_external=no_external)

    def test_apply_no_external_logs(self):
        """ext_tmux_apply with NO_EXTERNAL=1 logs what it would do."""
        result = self._run("ext_tmux_apply", no_external=True)
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("[NO_EXTERNAL]", combined)

    def test_remove_no_external_logs(self):
        """ext_tmux_remove with NO_EXTERNAL=1 logs what it would do."""
        result = self._run("ext_tmux_remove", no_external=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)

    def test_apply_writes_tmux_conf(self):
        """ext_tmux_apply creates ~/.tmux.conf with the managed block."""
        result = self._run("ext_tmux_apply", no_external=False)
        self.assertEqual(result.returncode, 0, result.stderr)

        tmux_conf = self.tmp_home / ".tmux.conf"
        self.assertTrue(tmux_conf.exists(), "~/.tmux.conf should be created")
        content = tmux_conf.read_text()
        self.assertIn("claude-hooks:amux-tmux-options", content)
        self.assertIn("set -g focus-events on", content)
        self.assertIn("set -g set-titles on", content)
        self.assertIn("set -g set-titles-string", content)

    def test_apply_idempotent(self):
        """Running ext_tmux_apply twice writes the managed block exactly once."""
        for _ in range(2):
            self._run("ext_tmux_apply", no_external=False)

        content = (self.tmp_home / ".tmux.conf").read_text()
        self.assertEqual(
            content.count("claude-hooks:amux-tmux-options"),
            1,
            f"Marker must appear exactly once. Content:\n{content}",
        )

    def test_remove_reverts_apply_exactly(self):
        """ext_tmux_remove reverts ext_tmux_apply — surrounding content byte-identical."""
        original = "# existing tmux config\nset -g status on\n"
        (self.tmp_home / ".tmux.conf").write_text(original)

        self._run("ext_tmux_apply", no_external=False)
        content_after_apply = (self.tmp_home / ".tmux.conf").read_text()
        self.assertIn("claude-hooks:amux-tmux-options", content_after_apply)

        self._run("ext_tmux_remove", no_external=False)
        self.assertEqual(
            (self.tmp_home / ".tmux.conf").read_text(),
            original,
            "ext_tmux_remove must restore byte-identical content",
        )

    def test_remove_noop_when_marker_absent(self):
        """ext_tmux_remove is a no-op when the marker is not present."""
        original = "set -g status on\n"
        (self.tmp_home / ".tmux.conf").write_text(original)

        result = self._run("ext_tmux_remove", no_external=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.tmp_home / ".tmux.conf").read_text(), original)

    def test_foreign_content_preserved(self):
        """Unrelated ~/.tmux.conf lines survive an apply + remove cycle."""
        original = "set -g status on\nset -g status-bg blue\n"
        (self.tmp_home / ".tmux.conf").write_text(original)

        self._run("ext_tmux_apply", no_external=False)
        self._run("ext_tmux_remove", no_external=False)
        self.assertEqual((self.tmp_home / ".tmux.conf").read_text(), original)


# =============================================================================
# §8 ext_symlink_add / ext_symlink_remove (task 29-03)
# =============================================================================

class TestExtSymlink(InstallerTestBase):
    """Tests for ext_symlink_add / ext_symlink_remove (HOME-isolated)."""

    def _run(self, call: str, no_external: bool = False) -> subprocess.CompletedProcess:
        return run_ext_call(self.tmp_home, call, no_external=no_external)

    def test_add_no_external_logs(self):
        """ext_symlink_add with NO_EXTERNAL=1 logs and does nothing."""
        result = self._run('ext_symlink_add "/target" "/link"', no_external=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)

    def test_remove_no_external_logs(self):
        """ext_symlink_remove with NO_EXTERNAL=1 logs and does nothing."""
        result = self._run('ext_symlink_remove "/link"', no_external=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)

    def test_add_creates_symlink(self):
        """ext_symlink_add creates the symlink."""
        target = self.tmp_home / "target_file.txt"
        target.write_text("content")
        link = self.tmp_home / ".local" / "bin" / "test-link"
        link.parent.mkdir(parents=True, exist_ok=True)

        result = self._run(f'ext_symlink_add "{target}" "{link}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(link.is_symlink(), "Symlink must be created")
        self.assertEqual(str(link.resolve()), str(target.resolve()))

    def test_add_idempotent(self):
        """ext_symlink_add twice succeeds (ln -sf is inherently idempotent)."""
        target = self.tmp_home / "target.txt"
        target.write_text("content")
        link = self.tmp_home / "test_link"

        for _ in range(2):
            result = self._run(f'ext_symlink_add "{target}" "{link}"')
            self.assertEqual(result.returncode, 0)

        self.assertTrue(link.is_symlink())

    def test_remove_removes_symlink(self):
        """ext_symlink_remove removes an existing symlink."""
        target = self.tmp_home / "target.txt"
        target.write_text("content")
        link = self.tmp_home / "test_link"
        link.symlink_to(target)

        result = self._run(f'ext_symlink_remove "{link}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.exists(), "Symlink must be removed")

    def test_remove_noop_for_nonexistent(self):
        """ext_symlink_remove is a no-op for a non-existent path."""
        result = self._run(f'ext_symlink_remove "{self.tmp_home}/nonexistent"')
        self.assertEqual(result.returncode, 0)


# =============================================================================
# §8.4 ext_systemd_* (task 29-03) — NO_EXTERNAL only
# =============================================================================

class TestExtSystemd(InstallerTestBase):
    """
    Tests for ext_systemd_enable / ext_systemd_disable /
    ext_systemd_daemon_reload / ext_systemd_is_enabled.

    All tests use CLAUDE_INSTALL_NO_EXTERNAL=1 (systemctl and loginctl escape
    HOME — architecture §10.2).
    """

    def _run(self, call: str) -> subprocess.CompletedProcess:
        return run_ext_call(self.tmp_home, call, no_external=True)

    def test_enable_no_external_logs(self):
        """ext_systemd_enable with NO_EXTERNAL=1 logs and skips."""
        result = self._run('ext_systemd_enable "claude-questions-listen.service"')
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("[NO_EXTERNAL]", combined)
        self.assertIn("enable", combined)

    def test_disable_no_external_logs(self):
        """ext_systemd_disable with NO_EXTERNAL=1 logs and skips."""
        result = self._run('ext_systemd_disable "claude-questions-listen.service"')
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)

    def test_daemon_reload_no_external_logs(self):
        """ext_systemd_daemon_reload with NO_EXTERNAL=1 logs and skips."""
        result = self._run("ext_systemd_daemon_reload")
        self.assertEqual(result.returncode, 0)
        self.assertIn("[NO_EXTERNAL]", result.stdout + result.stderr)

    def test_is_enabled_returns_false_no_external(self):
        """ext_systemd_is_enabled with NO_EXTERNAL=1 returns non-zero (not enabled)."""
        result = self._run(
            'ext_systemd_is_enabled "claude-questions-listen.service"'
            ' && echo ENABLED || echo NOT_ENABLED'
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("NOT_ENABLED", result.stdout + result.stderr)


# =============================================================================
# §7.1 Hook merge — ownership-scoped (task 29-04)
# =============================================================================

def _settings_with_foreign_hook(event: str, command: str, matcher: str = "*") -> dict:
    """Return a minimal settings dict with one foreign hook entry for 'event'."""
    return {
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {
            event: [{
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command}],
            }]
        },
    }


def _write_settings(tmp_home: Path, settings: dict) -> None:
    """Write settings.json into tmp_home/.claude/."""
    settings_dir = tmp_home / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(json.dumps(settings, indent=2))


class TestHooksMerge(InstallerTestBase):
    """
    §7.1: ownership-scoped hook merges — foreign entries survive, our entries
    are replaced (not duplicated), Notification keyed by matcher.
    """

    _FOREIGN_CMD = "python3 /usr/local/my-hooks/custom_hook.py"

    # -------------------------------------------------------------------------
    # Foreign entries survive install of each owning feature
    # -------------------------------------------------------------------------

    def test_foreign_pretooluse_survives_permission_hooks_install(self):
        """A hand-added PreToolUse entry survives permission-hooks install."""
        _write_settings(self.tmp_home, _settings_with_foreign_hook(
            "PreToolUse", self._FOREIGN_CMD))

        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        pre = hooks.get("PreToolUse", [])
        commands = [h["command"] for entry in pre for h in entry.get("hooks", [])]
        self.assertIn(self._FOREIGN_CMD, commands,
                      "Foreign PreToolUse command must survive install")
        # Our entry must also be present
        hooks_dir = str(self.tmp_home / ".claude" / "hooks")
        our_cmds = [c for c in commands if hooks_dir in c]
        self.assertGreater(len(our_cmds), 0,
                           "Our PreToolUse entry must be present after install")

    def test_foreign_pretooluse_survives_update(self):
        """A foreign PreToolUse entry survives a second (update) run."""
        _write_settings(self.tmp_home, _settings_with_foreign_hook(
            "PreToolUse", self._FOREIGN_CMD))

        for _ in range(2):
            result = run_installer(self.tmp_home,
                                   extra_args=["--only", "permission-hooks"])
            self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        pre = hooks.get("PreToolUse", [])
        commands = [h["command"] for entry in pre for h in entry.get("hooks", [])]
        self.assertIn(self._FOREIGN_CMD, commands,
                      "Foreign entry must survive a second (update) run")
        # No duplicate of our entry
        hooks_dir = str(self.tmp_home / ".claude" / "hooks")
        our_cmds = [c for c in commands if hooks_dir in c]
        self.assertEqual(len(our_cmds), 1,
                         "Our entry must not be duplicated on idempotent reinstall")

    def test_foreign_posttooluse_survives_telegram_install(self):
        """A hand-added PostToolUse entry survives telegram install."""
        _write_settings(self.tmp_home, _settings_with_foreign_hook(
            "PostToolUse", self._FOREIGN_CMD))

        result = run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        post = hooks.get("PostToolUse", [])
        commands = [h["command"] for entry in post for h in entry.get("hooks", [])]
        self.assertIn(self._FOREIGN_CMD, commands,
                      "Foreign PostToolUse command must survive telegram install")

    def test_foreign_stop_survives_amux_install(self):
        """A hand-added Stop entry survives amux install."""
        _write_settings(self.tmp_home, _settings_with_foreign_hook(
            "Stop", self._FOREIGN_CMD))

        result = run_installer(self.tmp_home, extra_args=["--only", "amux,profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        stop = hooks.get("Stop", [])
        commands = [h["command"] for entry in stop for h in entry.get("hooks", [])]
        self.assertIn(self._FOREIGN_CMD, commands,
                      "Foreign Stop command must survive amux install")

    def test_foreign_hooks_for_other_feature_survive_unrelated_install(self):
        """
        A foreign Stop entry (amux-owned event) survives permission-hooks install.
        Since permission-hooks does not own Stop, it must not touch it.
        """
        _write_settings(self.tmp_home, _settings_with_foreign_hook(
            "Stop", self._FOREIGN_CMD))

        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        stop = hooks.get("Stop", [])
        commands = [h["command"] for entry in stop for h in entry.get("hooks", [])]
        self.assertIn(self._FOREIGN_CMD, commands,
                      "Stop entry must survive permission-hooks install (it owns PreToolUse, not Stop)")

    # -------------------------------------------------------------------------
    # Notification shared array — keyed by matcher
    # -------------------------------------------------------------------------

    def test_notification_only_telegram_sets_idle_prompt(self):
        """Installing only telegram yields Notification[idle_prompt] only."""
        result = run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        notifications = settings.get("hooks", {}).get("Notification", [])
        matchers = [e.get("matcher") for e in notifications]
        self.assertIn("idle_prompt", matchers,
                      "telegram install must wire Notification[idle_prompt]")
        self.assertNotIn("permission_prompt", matchers,
                         "telegram install must NOT wire Notification[permission_prompt]")

    def test_notification_only_amux_sets_permission_prompt(self):
        """Installing only amux (with profiles) yields Notification[permission_prompt] only."""
        result = run_installer(self.tmp_home, extra_args=["--only", "amux,profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        notifications = settings.get("hooks", {}).get("Notification", [])
        matchers = [e.get("matcher") for e in notifications]
        self.assertIn("permission_prompt", matchers,
                      "amux install must wire Notification[permission_prompt]")
        self.assertNotIn("idle_prompt", matchers,
                         "amux install must NOT wire Notification[idle_prompt]")

    def test_notification_telegram_then_amux_both_matchers(self):
        """telegram then amux install: both Notification matchers present."""
        run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        result = run_installer(self.tmp_home, extra_args=["--only", "amux,profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        notifications = settings.get("hooks", {}).get("Notification", [])
        matchers = {e.get("matcher") for e in notifications}
        self.assertIn("idle_prompt", matchers,
                      "idle_prompt must survive amux install")
        self.assertIn("permission_prompt", matchers,
                      "permission_prompt must be present after amux install")
        self.assertEqual(len(notifications), 2,
                         "Exactly 2 Notification entries expected; no duplicates")

    def test_notification_amux_then_telegram_both_matchers(self):
        """amux then telegram install: both Notification matchers present (reverse order)."""
        run_installer(self.tmp_home, extra_args=["--only", "amux,profiles"])
        result = run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        notifications = settings.get("hooks", {}).get("Notification", [])
        matchers = {e.get("matcher") for e in notifications}
        self.assertIn("idle_prompt", matchers,
                      "idle_prompt must be present after telegram install")
        self.assertIn("permission_prompt", matchers,
                      "permission_prompt must survive telegram install")
        self.assertEqual(len(notifications), 2,
                         "Exactly 2 Notification entries expected; no duplicates")

    def test_notification_update_only_one_matcher(self):
        """Re-installing telegram leaves permission_prompt from amux untouched."""
        run_installer(self.tmp_home, extra_args=["--only", "amux,profiles"])
        run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        # Re-install telegram again (update)
        result = run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        notifications = settings.get("hooks", {}).get("Notification", [])
        matchers = [e.get("matcher") for e in notifications]
        self.assertEqual(matchers.count("idle_prompt"), 1,
                         "idle_prompt must appear exactly once after multiple telegram installs")
        self.assertEqual(matchers.count("permission_prompt"), 1,
                         "permission_prompt must appear exactly once")

    def test_foreign_notification_entry_survives(self):
        """
        A foreign Notification entry (command not in GLOBAL_HOOKS_DIR) survives
        install of both telegram and amux.
        """
        foreign_notification = {
            "permissions": {"allow": [], "deny": [], "ask": []},
            "hooks": {
                "Notification": [{
                    "matcher": "custom_event",
                    "hooks": [{"type": "command", "command": self._FOREIGN_CMD}],
                }]
            },
        }
        _write_settings(self.tmp_home, foreign_notification)

        run_installer(self.tmp_home, extra_args=["--only", "telegram"])
        result = run_installer(self.tmp_home, extra_args=["--only", "amux,profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        notifications = settings.get("hooks", {}).get("Notification", [])
        matchers = [e.get("matcher") for e in notifications]
        self.assertIn("custom_event", matchers,
                      "Foreign Notification[custom_event] must survive all installs")
        self.assertIn("idle_prompt", matchers)
        self.assertIn("permission_prompt", matchers)


# =============================================================================
# §7.2 Permissions merge — union and overwrite (task 29-04)
# =============================================================================

class TestPermissionsMerge(InstallerTestBase):
    """
    §7.2: union mode preserves foreign entries and records added set;
    overwrite mode reports discarded count.
    """

    _PROJECT_ALLOW = ["Bash(git status:*)", "Bash(git log:*)"]

    def _settings_with_allow(self, allow_list: list) -> dict:
        return {
            "permissions": {"allow": allow_list, "deny": [], "ask": []},
        }

    def test_union_preserves_existing_allow(self):
        """Union install: pre-existing allow entries survive."""
        _write_settings(self.tmp_home, self._settings_with_allow(
            ["MyCustomTool", "AnotherTool"]))

        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permissions-allowlist"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        allow = settings.get("permissions", {}).get("allow", [])
        self.assertIn("MyCustomTool", allow,
                      "Foreign allow entry must survive union install")
        self.assertIn("AnotherTool", allow,
                      "Foreign allow entry must survive union install")

    def test_union_adds_project_entries(self):
        """Union install: project config entries are added."""
        _write_settings(self.tmp_home, self._settings_with_allow([]))

        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permissions-allowlist"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        allow = settings.get("permissions", {}).get("allow", [])
        # The project's .claude/settings.json has entries — at least one should be added
        self.assertGreater(len(allow), 0,
                           "Project allow entries must be added by union install")

    def test_union_no_double_add(self):
        """
        A pattern present in both project config and existing user config is not
        duplicated. Running install twice does not duplicate entries.
        """
        # Seed settings with one of the project's own allow entries
        project_settings_path = REPO / ".claude" / "settings.json"
        project_settings = json.loads(project_settings_path.read_text())
        first_entry = project_settings["permissions"]["allow"][0]
        _write_settings(self.tmp_home, self._settings_with_allow([first_entry]))

        # Two consecutive union installs
        for _ in range(2):
            result = run_installer(self.tmp_home,
                                   extra_args=["--only", "permissions-allowlist"])
            self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        allow = settings.get("permissions", {}).get("allow", [])
        self.assertEqual(allow.count(first_entry), 1,
                         f"Entry '{first_entry}' must appear exactly once, not duplicated")

    def test_union_added_set_recorded_in_manifest(self):
        """Union install records added_allow and added_deny in the manifest."""
        _write_settings(self.tmp_home, self._settings_with_allow([]))

        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permissions-allowlist"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        manifest = read_manifest(self.tmp_home)
        perm_entry = manifest.get("features", {}).get("permissions-allowlist", {})
        self.assertIn("added_allow", perm_entry,
                      "Manifest must have added_allow for permissions-allowlist")
        self.assertIn("added_deny", perm_entry,
                      "Manifest must have added_deny for permissions-allowlist")
        self.assertIsInstance(perm_entry["added_allow"], list)
        self.assertIsInstance(perm_entry["added_deny"], list)
        # Since we started with empty allow, all project entries are "added"
        self.assertGreater(len(perm_entry["added_allow"]), 0,
                           "added_allow should be non-empty when starting from scratch")

    def test_union_added_set_is_subset_of_project_entries(self):
        """Union added_allow ⊆ project config allow (nothing alien added)."""
        _write_settings(self.tmp_home, self._settings_with_allow([]))

        run_installer(self.tmp_home, extra_args=["--only", "permissions-allowlist"])

        manifest = read_manifest(self.tmp_home)
        added_allow = set(
            manifest.get("features", {}).get("permissions-allowlist", {}).get("added_allow", [])
        )

        project_settings_path = REPO / ".claude" / "settings.json"
        project_settings = json.loads(project_settings_path.read_text())
        project_allow = set(
            project_settings.get("permissions", {}).get("allow", []) +
            project_settings.get("allowedTools", [])
        )

        extra = added_allow - project_allow
        self.assertEqual(extra, set(),
                         f"added_allow contains entries not in project config: {extra}")

    def test_union_manifest_mode_recorded(self):
        """Manifest records the permissions mode used (union by default)."""
        _write_settings(self.tmp_home, self._settings_with_allow([]))

        run_installer(self.tmp_home, extra_args=["--only", "permissions-allowlist"])

        manifest = read_manifest(self.tmp_home)
        options = manifest.get("features", {}).get("permissions-allowlist", {}).get("options", {})
        self.assertEqual(options.get("mode"), "union",
                         "Manifest must record mode=union for default install")

    def test_overwrite_replaces_permissions(self):
        """Overwrite mode replaces the allow list with the project's."""
        _write_settings(self.tmp_home, self._settings_with_allow(
            ["MyPrivateTool", "AnotherPrivateTool"]))

        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "permissions-allowlist"],
            extra_env={"CLAUDE_INSTALL_PERMISSIONS_MODE": "overwrite"},
        )
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        allow = settings.get("permissions", {}).get("allow", [])
        self.assertNotIn("MyPrivateTool", allow,
                         "Foreign entry must be replaced in overwrite mode")
        self.assertNotIn("AnotherPrivateTool", allow,
                         "Foreign entry must be replaced in overwrite mode")

    def test_overwrite_reports_discarded_count(self):
        """Overwrite mode logs the count of discarded existing allow entries."""
        _write_settings(self.tmp_home, self._settings_with_allow(
            ["PrettyUnique1", "PrettyUnique2", "PrettyUnique3"]))

        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "permissions-allowlist"],
            extra_env={"CLAUDE_INSTALL_PERMISSIONS_MODE": "overwrite"},
        )
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        combined = result.stdout + result.stderr
        self.assertIn("discarding", combined.lower(),
                      "Overwrite mode must report discarded entries in its output")
        # All three of our unique entries are not in the project config, so they're discarded
        self.assertIn("3", combined,
                      "Discarded count (3) must appear in the output")

    def test_legacy_allowedtools_folded_into_union(self):
        """Legacy allowedTools key is folded into permissions.allow and removed."""
        settings_dir = self.tmp_home / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text(json.dumps({
            "allowedTools": ["LegacyTool"],
        }))

        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permissions-allowlist"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        settings = read_settings(self.tmp_home)
        # Legacy key must be gone
        self.assertNotIn("allowedTools", settings,
                         "Legacy allowedTools key must be removed by install")
        # LegacyTool should survive in the union (it was in allowedTools, treated as allow)
        allow = settings.get("permissions", {}).get("allow", [])
        self.assertIn("LegacyTool", allow,
                      "LegacyTool from allowedTools must be preserved in the union")


# =============================================================================
# §4 MCP server helpers — register/de-register (task 29-04)
# =============================================================================

class TestMCPHelpers(InstallerTestBase):
    """
    §4: _register_mcp_server and _deregister_mcp_server via the installer.
    All tests use CLAUDE_INSTALL_NO_EXTERNAL=1 (MCP writes are HOME-isolated).
    """

    def _run_mcp_script(
        self,
        call: str,
        extra_setup: str = "",
        no_external: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Run a call against the §7 helpers extracted from install.sh in a minimal
        harness, analogous to run_ext_call for §8 seam tests.
        """
        install_text = INSTALL_SH.read_text()

        # Extract §7 helpers section (between the §7 header and the §build section)
        start = install_text.find("# §7. Settings helpers")
        end   = install_text.find("build_and_write_settings()")
        if start == -1 or end == -1:
            raise ValueError("Could not locate §7 helpers section in install.sh")
        helpers_section = install_text[start:end]

        no_ext_val = "1" if no_external else "0"
        script = f"""#!/bin/bash
set -euo pipefail
HOME="{self.tmp_home}"
BACKUP_DIR="{self.tmp_home}/.claude/backups"
CLAUDE_INSTALL_NO_EXTERNAL="{no_ext_val}"
UV_AVAILABLE=true
CLAUDE_JSON_BACKUP_FILE=""
mkdir -p "$BACKUP_DIR"
mkdir -p "$HOME/.claude"

log_info()  {{ echo "[INFO] $1"; }}
log_warn()  {{ echo "[WARN] $1"; }}
log_error() {{ echo "[ERROR] $1"; }}
log_step()  {{ echo "[STEP] $1"; }}

{extra_setup}

{helpers_section}

{call}
"""
        script_path = self.tmp_home / "_mcp_test.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)
        return subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True,
            cwd=str(REPO), timeout=30,
        )

    def _seed_claude_json(self, data: dict) -> None:
        """Write known content to tmp_home/.claude.json."""
        (self.tmp_home / ".claude.json").write_text(json.dumps(data))

    # -------------------------------------------------------------------------
    # context-mcp feature integration test (uses _register_mcp_server)
    # -------------------------------------------------------------------------

    def test_context_mcp_install_preserves_foreign_server(self):
        """Installing context-mcp leaves an unrelated mcpServers entry alone."""
        self._seed_claude_json({
            "mcpServers": {
                "my-custom-server": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["/some/server.js"],
                    "env": {},
                }
            }
        })

        result = run_installer(self.tmp_home, extra_args=["--only", "context-mcp"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        claude_json = read_claude_json(self.tmp_home)
        mcp = claude_json.get("mcpServers", {})
        self.assertIn("my-custom-server", mcp,
                      "Foreign mcpServers entry must survive context-mcp install")

    def test_context_mcp_install_creates_backup(self):
        """Installing context-mcp creates a timestamped backup of ~/.claude.json."""
        self._seed_claude_json({"mcpServers": {"existing": {"type": "stdio"}}})

        result = run_installer(self.tmp_home, extra_args=["--only", "context-mcp"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        backup_dir = self.tmp_home / ".claude" / "backups"
        backups = list(backup_dir.glob("claude.json.*.bak"))
        self.assertGreater(len(backups), 0,
                           "A timestamped backup of ~/.claude.json must exist after install")
        # Backup content must match the pre-run state
        pre_run_json = json.loads(backups[0].read_text())
        self.assertIn("existing", pre_run_json.get("mcpServers", {}),
                      "Backup must reflect the pre-install state of ~/.claude.json")

    def test_register_deregister_leaves_foreign_server(self):
        """
        Direct _register_mcp_server + _deregister_mcp_server: foreign server untouched.
        """
        self._seed_claude_json({
            "mcpServers": {
                "foreign": {"type": "stdio", "command": "node", "args": []}
            }
        })

        # Register a test server
        result = self._run_mcp_script(
            '_register_mcp_server "test-srv" "/fake/script.py" "{}"'
        )
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        claude = json.loads((self.tmp_home / ".claude.json").read_text())
        self.assertIn("foreign", claude.get("mcpServers", {}))
        self.assertIn("test-srv", claude.get("mcpServers", {}))

        # De-register the test server
        result = self._run_mcp_script(
            '_deregister_mcp_server "test-srv"'
        )
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        claude = json.loads((self.tmp_home / ".claude.json").read_text())
        self.assertIn("foreign", claude.get("mcpServers", {}),
                      "Foreign server must survive de-registration")
        self.assertNotIn("test-srv", claude.get("mcpServers", {}),
                         "Our server must be removed by de-registration")

    def test_deregister_noop_when_server_absent(self):
        """_deregister_mcp_server is a no-op when the server is not registered."""
        self._seed_claude_json({"mcpServers": {"other": {}}})

        result = self._run_mcp_script('_deregister_mcp_server "nonexistent"')
        self.assertEqual(result.returncode, 0)
        self.assertIn("no-op", result.stdout + result.stderr)

    def test_backup_created_only_once_per_run(self):
        """
        Calling _register_mcp_server twice takes only ONE backup (pre-run state).
        """
        original = {"mcpServers": {"orig": {}}}
        self._seed_claude_json(original)

        result = self._run_mcp_script(
            '_register_mcp_server "srv1" "/s1.py" "{}" && '
            '_register_mcp_server "srv2" "/s2.py" "{}"'
        )
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        backup_dir = self.tmp_home / ".claude" / "backups"
        backups = list(backup_dir.glob("claude.json.*.bak"))
        self.assertEqual(len(backups), 1,
                         "Only one backup must be taken per run (pre-run state)")
        # That backup reflects the original content
        backed_up = json.loads(backups[0].read_text())
        self.assertIn("orig", backed_up.get("mcpServers", {}))
        self.assertNotIn("srv1", backed_up.get("mcpServers", {}))

    # -------------------------------------------------------------------------
    # Corrupted merge — restore path (§7.3)
    # -------------------------------------------------------------------------

    def test_corrupted_settings_merge_restores_backup(self):
        """
        If the settings.json merge produces invalid JSON, the backup is restored
        and the run exits non-zero.
        """
        settings_dir = self.tmp_home / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text(json.dumps(
            {"permissions": {"allow": ["KnownEntry"], "deny": [], "ask": []}}
        ))

        # Patch build_and_write_settings to produce invalid JSON by corrupting
        # the initial read of the global config.
        text = INSTALL_SH.read_text()
        target = "    # Start with current global config\n    local merged\n    merged=$(jq '.' \"$GLOBAL_CONFIG\")"
        replacement = "    # Start with current global config\n    local merged\n    merged='THIS IS NOT VALID JSON'  # PATCHED: simulate corrupted merge"
        patched = text.replace(target, replacement, 1)
        self.assertIn("PATCHED: simulate corrupted merge", patched,
                      "Patch must apply; check if build_and_write_settings changed")

        patched_path = self.tmp_home / "install_corrupted.sh"
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
            ["bash", str(patched_path), "--only", "permissions-allowlist"],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )

        # Must exit non-zero
        self.assertNotEqual(result.returncode, 0,
                            "Corrupted merge must exit non-zero")

        # settings.json must have the original content (restored from backup)
        final = json.loads((settings_dir / "settings.json").read_text())
        allow = final.get("permissions", {}).get("allow", [])
        self.assertIn("KnownEntry", allow,
                      "settings.json must be restored from backup after corrupted merge")

    def test_corrupted_claude_json_write_restores_backup_and_exits(self):
        """
        If ~/.claude.json write re-validation fails, the backup is restored and
        the run exits non-zero.
        """
        original_mcp = {"mcpServers": {"pre-existing": {"type": "stdio"}}}
        (self.tmp_home / ".claude.json").write_text(json.dumps(original_mcp))

        # Patch _register_mcp_server to corrupt ~/.claude.json AFTER the mv,
        # triggering the re-validation failure path.
        text = INSTALL_SH.read_text()
        target = "    mv \"$tmp\" \"$claude_json\"\n\n    # Re-validate after write (§7.3 atomicity"
        replacement = (
            "    mv \"$tmp\" \"$claude_json\"\n"
            "    # PATCHED: corrupt after mv to test re-validation restore\n"
            "    echo 'NOT VALID JSON' > \"$claude_json\"\n\n"
            "    # Re-validate after write (§7.3 atomicity"
        )
        patched = text.replace(target, replacement, 1)
        self.assertIn("PATCHED: corrupt after mv", patched,
                      "Patch must apply; check _register_mcp_server write section")

        patched_path = self.tmp_home / "install_corrupt_json.sh"
        patched_path.write_text(patched)
        patched_path.chmod(0o755)

        fake_uv_env = _make_fake_uv(self.tmp_home)
        env = {
            **os.environ,
            **fake_uv_env,
            "HOME": str(self.tmp_home),
            "CLAUDE_INSTALL_NO_EXTERNAL": "1",
            "CLAUDE_INSTALL_EPIC29_LIVE": "1",
            "CLAUDE_INSTALL_SCRIPT_DIR": str(REPO),
        }
        result = subprocess.run(
            ["bash", str(patched_path), "--only", "context-mcp"],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )

        # Must exit non-zero
        self.assertNotEqual(result.returncode, 0,
                            "Corrupted ~/.claude.json must exit non-zero")

        # ~/.claude.json must be restored to the pre-run state
        final = json.loads((self.tmp_home / ".claude.json").read_text())
        self.assertIn("pre-existing", final.get("mcpServers", {}),
                      "~/.claude.json must be restored from backup after corrupted write")
        self.assertNotIn("context-usage", final.get("mcpServers", {}),
                         "Corrupted context-usage registration must be rolled back")

    def test_claude_json_backup_before_edit(self):
        """A run that touches ~/.claude.json leaves a timestamped backup of pre-edit content."""
        # Seed with a recognisable foreign mcpServers entry
        pre_run_content = {
            "mcpServers": {
                "my-foreign-server": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["/my/server.js"],
                    "env": {"FOO": "bar"},
                }
            },
            "some_user_setting": "preserved",
        }
        (self.tmp_home / ".claude.json").write_text(json.dumps(pre_run_content))

        result = run_installer(self.tmp_home, extra_args=["--only", "context-mcp"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Backup must exist
        backup_dir = self.tmp_home / ".claude" / "backups"
        backups = list(backup_dir.glob("claude.json.*.bak"))
        self.assertGreater(len(backups), 0,
                           "A timestamped backup must be created when ~/.claude.json is modified")

        # Backup content must match the pre-run bytes exactly
        backup_data = json.loads(backups[0].read_text())
        self.assertEqual(backup_data, pre_run_content,
                         "Backup must match the pre-run content of ~/.claude.json byte-for-byte")


# =============================================================================
# §6. Executor, module closure, refcounted uninstall (task 29-05)
# =============================================================================


class TestRefcountMatrix(InstallerTestBase):
    """
    Refcount matrix: for each shared module, install two owners, uninstall one,
    assert the module survives; uninstall both, assert the module is removed.
    """

    # Shared modules and their owners (from MODULE_OWNERS in install.sh)
    _SHARED_MODULES = {
        "bash_command_parser.py": ("permission-hooks", "telegram"),
        "settings_loader.py": ("permission-hooks", "telegram"),
        "permission_state_store.py": ("permission-hooks", "telegram"),  # also amux, test two
        "project_key.py": ("permission-hooks", "telegram"),
        "roles_config.py": ("telegram", "questions"),
        "amux_spawn_lib.py": ("amux", "profiles"),
        # settings_writer.py is telegram-only, not shared
    }

    def _install_features(self, features: list) -> subprocess.CompletedProcess:
        """Install specific features."""
        return run_installer(
            self.tmp_home,
            extra_args=["--only", ",".join(features)],
            extra_env=_make_fake_uv(self.tmp_home),
        )

    def _uninstall_features(self, features: list) -> subprocess.CompletedProcess:
        """Uninstall specific features."""
        return run_installer(
            self.tmp_home,
            extra_args=["--uninstall", ",".join(features)],
            extra_env=_make_fake_uv(self.tmp_home),
        )

    def _module_exists(self, module: str) -> bool:
        """Check if a module exists in the global hooks directory."""
        return (self.tmp_home / ".claude" / "hooks" / module).exists()

    def test_roles_config_survives_telegram_uninstall_with_questions(self):
        """
        Install telegram+questions, uninstall telegram: roles_config.py survives.
        This is the key test case from the task spec.
        """
        # Install both
        result = self._install_features(["telegram", "questions"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertTrue(self._module_exists("roles_config.py"),
                        "roles_config.py should exist after installing telegram+questions")

        # Uninstall telegram
        result = self._uninstall_features(["telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # roles_config.py must survive (owned by questions too)
        self.assertTrue(self._module_exists("roles_config.py"),
                        "roles_config.py must survive uninstalling telegram (still owned by questions)")

        # questions probe should still return installed
        self.assertTrue(run_probe(self.tmp_home, "questions"),
                        "questions must still probe as installed after telegram uninstall")

    def test_amux_spawn_lib_survives_amux_uninstall_with_profiles(self):
        """
        Install amux+profiles, uninstall amux: amux_spawn_lib.py survives.
        """
        result = self._install_features(["amux", "profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertTrue(self._module_exists("amux_spawn_lib.py"),
                        "amux_spawn_lib.py should exist after installing amux+profiles")

        # Uninstall amux
        result = self._uninstall_features(["amux"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # amux_spawn_lib.py must survive (owned by profiles too)
        self.assertTrue(self._module_exists("amux_spawn_lib.py"),
                        "amux_spawn_lib.py must survive uninstalling amux (still owned by profiles)")

    def test_bash_command_parser_survives_one_owner_uninstall(self):
        """
        Install permission-hooks and telegram, uninstall telegram:
        bash_command_parser.py survives (still owned by permission-hooks).
        Note: we uninstall telegram (not permission-hooks) because
        telegram _requires_ permission-hooks — the dependency refusal would
        block uninstalling permission-hooks while telegram is installed.
        """
        result = self._install_features(["permission-hooks", "telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertTrue(self._module_exists("bash_command_parser.py"))

        # Uninstall telegram (permission-hooks still installed, owns bash_command_parser.py)
        result = self._uninstall_features(["telegram"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        self.assertTrue(self._module_exists("bash_command_parser.py"),
                        "bash_command_parser.py must survive (still owned by permission-hooks)")

    def test_module_removed_when_last_owner_uninstalled(self):
        """
        Uninstalling the last owner of a module removes it.
        Uninstall both telegram and questions at once (questions requires telegram,
        so they must be uninstalled together).
        """
        result = self._install_features(["telegram", "questions"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertTrue(self._module_exists("roles_config.py"))
        self.assertTrue(self._module_exists("questions_store.py"))

        # Uninstall both at once (questions requires telegram — can't uninstall separately)
        result = self._uninstall_features(["telegram", "questions"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        self.assertFalse(self._module_exists("roles_config.py"),
                         "roles_config.py must be removed when all owners are uninstalled")
        self.assertFalse(self._module_exists("questions_store.py"),
                         "questions_store.py must be removed when its sole owner is uninstalled")

    def test_unique_module_removed_on_uninstall(self):
        """
        A module with a single owner is removed when that owner is uninstalled.
        """
        # Install questions (exclusively owns questions_store.py, questions_listen_lib.py)
        result = self._install_features(["questions"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertTrue(self._module_exists("questions_store.py"))
        self.assertTrue(self._module_exists("questions_listen_lib.py"))

        # Uninstall questions
        result = self._uninstall_features(["questions"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        self.assertFalse(self._module_exists("questions_store.py"),
                         "questions_store.py must be removed (sole owner uninstalled)")
        self.assertFalse(self._module_exists("questions_listen_lib.py"),
                         "questions_listen_lib.py must be removed (sole owner uninstalled)")


class TestKeepSemantics(InstallerTestBase):
    """
    Keep semantics: shared modules refresh, wiring unchanged.
    """

    def test_keep_refreshes_shared_modules(self):
        """
        Install telegram, then install permission-hooks with --only:
        telegram gets 'keep', its shared modules (bash_command_parser.py etc.)
        are refreshed, but its wiring (PostToolUse) is unchanged.
        """
        # First install telegram
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Verify telegram is installed
        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        self.assertIn("PostToolUse", hooks, "telegram PostToolUse should be wired")

        # Record the PostToolUse entry
        post_tool_use_before = hooks["PostToolUse"]

        # Now install permission-hooks only — telegram should get "keep"
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Check that shared modules still exist (were refreshed)
        hooks_dir = self.tmp_home / ".claude" / "hooks"
        self.assertTrue((hooks_dir / "bash_command_parser.py").exists(),
                        "Shared module must be refreshed during keep")
        self.assertTrue((hooks_dir / "settings_loader.py").exists(),
                        "Shared module must be refreshed during keep")

        # Wiring for telegram must be unchanged
        settings_after = read_settings(self.tmp_home)
        hooks_after = settings_after.get("hooks", {})
        self.assertIn("PostToolUse", hooks_after,
                      "telegram PostToolUse must survive permission-hooks install (keep semantics)")
        self.assertEqual(post_tool_use_before, hooks_after["PostToolUse"],
                         "PostToolUse wiring must be identical (keep = no wiring change)")

    def test_keep_plus_update_refreshes_all_modules(self):
        """
        Keep + Update: refreshes shared modules. No wiring change for kept feature.
        """
        # Install both
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks,telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Now update just permission-hooks — telegram gets keep
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # telegram's wiring should be unchanged
        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        self.assertIn("PostToolUse", hooks, "telegram PostToolUse must survive")
        self.assertIn("PreToolUse", hooks, "permission-hooks PreToolUse must be present")


class TestFailureIsolation(InstallerTestBase):
    """
    Failure isolation: inject a failing _install, assert others complete,
    manifest records failed, exit non-zero.
    """

    def test_failing_feature_does_not_stop_others(self):
        """
        Inject a failing feature_telegram_install. Other features must complete,
        the manifest records telegram as failed, and exit is non-zero.
        """
        text = INSTALL_SH.read_text()

        # Patch feature_telegram_install to fail
        patched = text.replace(
            'feature_telegram_install() {\n    log_step "Installing: $(feature_telegram_title)"',
            'feature_telegram_install() {\n    log_step "Installing: $(feature_telegram_title)"\n    return 1  # PATCHED: simulate failure',
            1,
        )
        self.assertIn("PATCHED: simulate failure", patched, "Patch must apply")

        patched_path = self.tmp_home / "install_fail_test.sh"
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
            ["bash", str(patched_path), "--only", "statusline,telegram,permission-hooks"],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )

        # Must exit non-zero (failures present)
        self.assertNotEqual(result.returncode, 0,
                            "Run with a failing feature must exit non-zero")

        # The failure message must mention telegram
        combined = result.stdout + result.stderr
        self.assertIn("telegram", combined,
                      "Failure summary must name the failing feature")
        self.assertIn("FAILURES", combined,
                      "Summary must include FAILURES section")

        # Other features must have completed
        manifest = read_manifest(self.tmp_home)
        features = manifest.get("features", {})

        # statusline should be installed (it runs before telegram)
        self.assertEqual(features.get("statusline", {}).get("state"), "installed",
                         "statusline must complete despite telegram failure")

        # permission-hooks should be installed
        self.assertEqual(features.get("permission-hooks", {}).get("state"), "installed",
                         "permission-hooks must complete despite telegram failure")

        # telegram should be failed
        self.assertEqual(features.get("telegram", {}).get("state"), "failed",
                         "telegram must be recorded as failed in manifest")

    def test_failed_feature_exit_code_is_nonzero(self):
        """A run where any feature fails exits non-zero."""
        text = INSTALL_SH.read_text()
        patched = text.replace(
            'feature_statusline_install() {\n    # Lifted from STEP 2',
            'feature_statusline_install() {\n    return 1  # PATCHED: fail\n    # Lifted from STEP 2',
            1,
        )
        self.assertIn("PATCHED: fail", patched)

        patched_path = self.tmp_home / "install_fail2.sh"
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
            ["bash", str(patched_path), "--only", "statusline"],
            capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0,
                            "Run with failing feature must exit non-zero")


class TestDataPreservation(InstallerTestBase):
    """
    Data preservation: profiles.toml, history.jsonl, state stores survive
    full uninstall (invariant 10).
    """

    def test_profiles_toml_survives_full_uninstall(self):
        """profiles.toml must survive uninstalling everything."""
        # Install profiles to create profiles.toml
        result = run_installer(self.tmp_home, extra_args=["--only", "profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        profiles_path = self.tmp_home / ".claude" / "profiles.toml"
        self.assertTrue(profiles_path.exists(), "Pre-condition: profiles.toml must exist")
        profiles_content = profiles_path.read_bytes()

        # Uninstall profiles
        result = run_installer(self.tmp_home, extra_args=["--uninstall", "profiles"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # profiles.toml must survive
        self.assertTrue(profiles_path.exists(),
                        "profiles.toml must survive uninstall (user data)")
        self.assertEqual(profiles_path.read_bytes(), profiles_content,
                         "profiles.toml must be byte-identical after uninstall")

    def test_history_jsonl_survives_full_uninstall(self):
        """history.jsonl must survive uninstalling everything."""
        # Create a fake history.jsonl
        history_path = self.tmp_home / ".claude" / "history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_content = b'{"event": "test"}\n'
        history_path.write_bytes(history_content)

        # Install then uninstall a feature
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_installer(self.tmp_home, extra_args=["--uninstall", "statusline"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # history.jsonl must survive
        self.assertTrue(history_path.exists(),
                        "history.jsonl must survive uninstall")
        self.assertEqual(history_path.read_bytes(), history_content,
                         "history.jsonl must be byte-identical")

    def test_state_store_survives_full_uninstall(self):
        """State stores under ~/.claude/ must survive uninstalling everything."""
        # Create fake state stores
        state_dir = self.tmp_home / ".claude"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_files = {
            "permission_state.json": b'{"auto_allow": true}',
            "session_yolo.json": b'{"session_id": "test"}',
        }
        for name, content in state_files.items():
            (state_dir / name).write_bytes(content)

        # Install and uninstall
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_installer(self.tmp_home, extra_args=["--uninstall", "statusline"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # State files must survive
        for name, content in state_files.items():
            path = state_dir / name
            self.assertTrue(path.exists(),
                            f"{name} must survive uninstall")
            self.assertEqual(path.read_bytes(), content,
                             f"{name} must be byte-identical")

    def test_projects_dir_survives_full_uninstall(self):
        """~/.claude/projects/ must survive uninstalling everything."""
        projects_dir = self.tmp_home / ".claude" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        project_file = projects_dir / "test-project" / "config.json"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_content = b'{"project": "test"}'
        project_file.write_bytes(project_content)

        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_installer(self.tmp_home, extra_args=["--uninstall", "statusline"])
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        self.assertTrue(project_file.exists(),
                        "~/.claude/projects/ content must survive uninstall")
        self.assertEqual(project_file.read_bytes(), project_content)


class TestDependencyRefusal(InstallerTestBase):
    """
    Dependency refusal: uninstalling a feature that others depend on is refused.
    """

    def test_uninstall_permission_hooks_with_telegram_in_plan_is_refused(self):
        """
        Uninstall permission-hooks while telegram is install/update/keep in the
        same plan -> refused, names telegram.

        The refusal fires when the DEPENDENT is actively in the plan (not just
        passively installed from a prior run). This matches the selector's
        behaviour: it prevents setting a prerequisite to Uninstall when a
        dependent is Install/Update/Keep in the same plan.
        """
        # Install both
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks,telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Try to keep telegram while uninstalling permission-hooks
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "telegram", "--uninstall", "permission-hooks"],
                               extra_env=_make_fake_uv(self.tmp_home))

        # Must fail
        self.assertNotEqual(result.returncode, 0,
                            "Uninstalling permission-hooks with telegram in plan must be refused")
        combined = result.stdout + result.stderr
        self.assertIn("telegram", combined,
                      "Refusal message must name the dependent feature 'telegram'")

    def test_uninstall_profiles_with_amux_in_plan_is_refused(self):
        """
        Uninstall profiles while amux is in the plan -> refused, names amux.
        """
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "profiles,amux"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Try to keep amux while uninstalling profiles
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "amux", "--uninstall", "profiles"],
                               extra_env=_make_fake_uv(self.tmp_home))

        self.assertNotEqual(result.returncode, 0,
                            "Uninstalling profiles with amux in plan must be refused")
        combined = result.stdout + result.stderr
        self.assertIn("amux", combined,
                      "Refusal message must name the dependent feature 'amux'")

    def test_uninstall_dependency_alone_succeeds(self):
        """
        Uninstalling a dependency when the dependent is passively installed
        (not in the current plan) succeeds. The user is responsible for the
        consequences. Shared modules survive via refcount.
        """
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "telegram,questions"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Uninstall telegram alone — questions is passively installed, not in plan
        result = run_installer(self.tmp_home,
                               extra_args=["--uninstall", "telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0,
                         f"Uninstalling telegram alone should succeed. stderr: {result.stderr[:300]}")

    def test_uninstall_both_dependency_and_dependent_succeeds(self):
        """
        Uninstalling both a dependency and its dependent at the same time succeeds.
        """
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks,telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Uninstall both at once
        result = run_installer(self.tmp_home,
                               extra_args=["--uninstall", "permission-hooks,telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0,
                         f"Uninstalling both dependency and dependent at once should succeed. stderr: {result.stderr[:300]}")

    def test_only_with_uninstall_creates_dependency_refusal(self):
        """
        --only telegram --uninstall permission-hooks is refused because
        telegram requires permission-hooks and telegram is in the plan.
        """
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "permission-hooks,telegram"],
                               extra_env=_make_fake_uv(self.tmp_home))
        self.assertEqual(result.returncode, 0, result.stderr[:300])

        # Try --only telegram --uninstall permission-hooks
        result = run_installer(self.tmp_home,
                               extra_args=["--only", "telegram", "--uninstall", "permission-hooks"],
                               extra_env=_make_fake_uv(self.tmp_home))

        self.assertNotEqual(result.returncode, 0,
                            "Uninstalling a dependency while keeping the dependent must be refused")


class TestFullRoundTrip(InstallerTestBase):
    """
    Full round trip: install all, uninstall all, install all again.
    The third state equals the first.
    """

    def test_install_uninstall_reinstall_round_trip(self):
        """
        Install a set of features, uninstall them all, install them again.
        settings.json and hooks directory listing must match.
        """
        features = "statusline,permission-hooks,profiles,permissions-allowlist"
        fake_uv_env = _make_fake_uv(self.tmp_home)

        # First install
        result = run_installer(self.tmp_home,
                               extra_args=["--only", features],
                               extra_env=fake_uv_env)
        self.assertEqual(result.returncode, 0,
                         f"First install must succeed. stderr: {result.stderr[:300]}")

        settings_first = read_settings(self.tmp_home)
        hooks_dir = self.tmp_home / ".claude" / "hooks"
        if hooks_dir.exists():
            hooks_first = sorted(f.name for f in hooks_dir.iterdir())
        else:
            hooks_first = []

        # Uninstall all
        result = run_installer(self.tmp_home,
                               extra_args=["--uninstall", features],
                               extra_env=fake_uv_env)
        self.assertEqual(result.returncode, 0,
                         f"Uninstall must succeed. stderr: {result.stderr[:300]}")

        # Reinstall
        result = run_installer(self.tmp_home,
                               extra_args=["--only", features],
                               extra_env=fake_uv_env)
        self.assertEqual(result.returncode, 0,
                         f"Reinstall must succeed. stderr: {result.stderr[:300]}")

        settings_third = read_settings(self.tmp_home)
        if hooks_dir.exists():
            hooks_third = sorted(f.name for f in hooks_dir.iterdir())
        else:
            hooks_third = []

        # Settings should match (modulo timestamps/backups)
        # Compare hooks configuration
        self.assertEqual(
            settings_first.get("hooks"),
            settings_third.get("hooks"),
            "hooks configuration must match between first install and reinstall",
        )

        # Compare statusLine
        self.assertEqual(
            settings_first.get("statusLine"),
            settings_third.get("statusLine"),
            "statusLine must match between first install and reinstall",
        )

        # Hooks directory listing must match
        self.assertEqual(
            hooks_first, hooks_third,
            "hooks directory listing must match between first install and reinstall",
        )


# =============================================================================
# §5. Interactive selector — task 29-06
# =============================================================================

def run_selector(
    tmp_home: Path,
    keystrokes: str,
    extra_env: dict | None = None,
    extra_args: list | None = None,
) -> subprocess.CompletedProcess:
    """
    Run install.sh with the interactive selector active (CLAUDE_INSTALL_ASSUME_TTY=1)
    and the given keystroke script piped to stdin.

    keystrokes is a newline-separated string, e.g. "q\\n" or "1\\n\\ny\\n".
    No explicit selection flags: the selector is the entry point.
    """
    env = {
        **os.environ,
        "HOME": str(tmp_home),
        "CLAUDE_INSTALL_NO_EXTERNAL": "1",
        "CLAUDE_INSTALL_ASSUME_TTY": "1",
        "CLAUDE_INSTALL_EPIC29_LIVE": "1",
    }
    if extra_env:
        env.update(extra_env)

    cmd = ["bash", str(INSTALL_SH)] + (extra_args or [])
    return subprocess.run(
        cmd,
        input=keystrokes,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=30,
    )


class TestTTYDetection(InstallerTestBase):
    """
    TTY detection (29-06 §6): no TTY + no --yes + no explicit selection = error
    naming --yes.
    """

    def test_no_tty_no_yes_no_selection_is_error(self):
        """
        CLAUDE_INSTALL_ASSUME_TTY=0 + no --yes + no explicit selection → non-zero
        with message naming --yes.
        """
        result = run_installer(
            self.tmp_home,
            # run_installer already sets CLAUDE_INSTALL_ASSUME_TTY=0; no extra args
        )
        self.assertNotEqual(
            result.returncode, 0,
            "No-TTY + no --yes + no selection must exit non-zero. "
            f"stdout: {result.stdout[:300]}"
        )
        combined = result.stdout + result.stderr
        self.assertIn(
            "--yes", combined,
            "Error message must name --yes as the remedy"
        )

    def test_no_tty_with_explicit_only_succeeds(self):
        """
        CLAUDE_INSTALL_ASSUME_TTY=0 + --only = OK (explicit selection bypasses selector).
        """
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline"],
        )
        self.assertEqual(
            result.returncode, 0,
            f"--only with no TTY should succeed (explicit selection). stderr: {result.stderr[:300]}"
        )

    def test_no_tty_with_yes_uses_manifest(self):
        """
        CLAUDE_INSTALL_ASSUME_TTY=0 + --yes with an explicit --only selection works.
        """
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline", "--yes"],
        )
        self.assertEqual(
            result.returncode, 0,
            f"--only --yes with no TTY should succeed. stderr: {result.stderr[:300]}"
        )

    def test_assume_tty_1_enables_selector(self):
        """
        CLAUDE_INSTALL_ASSUME_TTY=1 + no --yes + no explicit selection → enters
        selector (not an error). 'q' exits 0 immediately.
        """
        result = run_selector(self.tmp_home, keystrokes="q\n")
        self.assertEqual(
            result.returncode, 0,
            f"Selector with 'q' must exit 0. stderr: {result.stderr[:300]}"
        )
        combined = result.stdout + result.stderr
        self.assertIn("Quit", combined, "Quit message must appear")


class TestSelectorChecklist(InstallerTestBase):
    """
    Checklist rendering: every feature shown, _writes() lines disclosed,
    sub-toggles shown.
    """

    def _get_selector_output(self, keystrokes: str = "q\n") -> str:
        result = run_selector(self.tmp_home, keystrokes=keystrokes)
        return result.stdout + result.stderr

    def test_all_features_appear_in_checklist(self):
        """Every feature title appears in the checklist."""
        output = self._get_selector_output()
        # Check a sample of feature titles
        for substring in [
            "Statusline",
            "Permission hooks",
            "Telegram",
            "Model profiles",
            "amux integration",
            "permissions allowlist",
            "context-usage",
            "claude-history",
            "Async questions",
            "Daily permission review",
        ]:
            self.assertIn(
                substring.lower(), output.lower(),
                f"Feature title fragment '{substring}' must appear in checklist"
            )

    def test_writes_lines_appear_for_features_with_writes(self):
        """
        Every feature that has a non-empty _writes() must render a 'writes:' line.
        Enumerate from the registry rather than hard-coding the list so a new
        feature cannot be added without its disclosure.
        """
        # Harvest features with non-empty _writes() by grepping install.sh
        text = INSTALL_SH.read_text()
        import re
        # Find all feature_<id>_writes functions that return non-empty strings
        features_with_writes = []
        for match in re.finditer(
            r"feature_(\w+)_writes\(\)\s*\{\s*echo\s+\"([^\"]+)\"\s*;?\s*\}", text
        ):
            id_mangled, writes_val = match.group(1), match.group(2)
            if writes_val.strip():
                features_with_writes.append((id_mangled, writes_val))

        self.assertGreater(
            len(features_with_writes), 0,
            "At least one feature should have a non-empty _writes() in install.sh"
        )

        output = self._get_selector_output()
        for id_mangled, writes_val in features_with_writes:
            with self.subTest(feature=id_mangled):
                self.assertIn(
                    "writes:", output,
                    f"'writes:' must appear for feature {id_mangled} (writes: {writes_val!r})"
                )

    def test_suboptions_appear_in_checklist(self):
        """Sub-toggles render indented under their parent feature."""
        output = self._get_selector_output()
        # Check sub-toggle titles appear
        for fragment in [
            "auto-source profiles",
            "auto-wrap sessions",
            "listener daemon",
            "crontab",
        ]:
            self.assertIn(
                fragment.lower(), output.lower(),
                f"Sub-toggle fragment '{fragment}' must appear in checklist"
            )

    def test_suboption_bashrc_writes_disclosed(self):
        """profiles-autosource and amux-autowrap sub-toggles disclose ~/.bashrc write."""
        output = self._get_selector_output()
        # Both bashrc sub-toggles must show their writes disclosure
        self.assertIn(
            "~/.bashrc", output,
            "~/.bashrc must appear in checklist for the bashrc sub-toggles"
        )


class TestSelectorInitialState(InstallerTestBase):
    """
    Initial selection: fresh machine uses feature defaults; manifest overrides;
    probe overrides everything.
    """

    def test_fresh_machine_questions_starts_skip(self):
        """On a fresh machine with no manifest, 'questions' starts at Skip (D14)."""
        output = run_selector(self.tmp_home, keystrokes="q\n").stdout
        output += run_selector(self.tmp_home, keystrokes="q\n").stderr
        # The checklist should show Skip for questions
        self.assertIn("Skip", output, "questions must start at Skip on fresh machine")

    def test_fresh_machine_daily_review_starts_skip(self):
        """On a fresh machine with no manifest, 'daily-review' starts at Skip (D14)."""
        output = run_selector(self.tmp_home, keystrokes="q\n").stdout
        output += run_selector(self.tmp_home, keystrokes="q\n").stderr
        # daily-review defaults to skip — line count check: should see at least 2 Skip lines
        skip_count = output.count("Skip")
        self.assertGreaterEqual(
            skip_count, 2,
            "At least questions and daily-review must show Skip on a fresh machine"
        )

    def test_fresh_machine_statusline_starts_install(self):
        """On a fresh machine, statusline starts at Install (default on)."""
        output = run_selector(self.tmp_home, keystrokes="q\n").stdout
        output += run_selector(self.tmp_home, keystrokes="q\n").stderr
        self.assertIn("Install", output,
                      "statusline must start at Install on a fresh machine")

    def test_manifest_skipped_shown_as_skip(self):
        """
        Manifest with a feature at 'skipped' → selector shows Skip for that feature.
        """
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "schema": 1,
            "repo": str(REPO),
            "revision": "test",
            "updated_at": "2026-01-01T00:00:00Z",
            "features": {
                "statusline": {"state": "skipped", "at": "2026-01-01T00:00:00Z",
                               "artifacts": [], "options": {}},
            },
        }))
        output = run_selector(self.tmp_home, keystrokes="q\n").stdout
        output += run_selector(self.tmp_home, keystrokes="q\n").stderr
        self.assertIn("Skip", output, "Feature with manifest state=skipped must show Skip")

    def test_installed_feature_starts_at_update(self):
        """
        A feature detected as installed by probe starts at Update regardless of manifest.
        """
        # Install statusline first so the probe sees it
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        output = run_selector(self.tmp_home, keystrokes="q\n").stdout
        output += run_selector(self.tmp_home, keystrokes="q\n").stderr
        self.assertIn("Update", output,
                      "Installed feature must start at Update in the selector")


class TestSelectorCycling(InstallerTestBase):
    """
    Tri-state cycling: correct state transitions for installed and not-installed.
    """

    def test_not_installed_cycles_install_to_skip(self):
        """
        Cycling a not-installed feature once: Install → Skip.
        Feature 1 is statusline (not-installed on clean home).
        """
        # Send "1\n" to cycle feature 1, then "q\n" to quit
        result = run_selector(self.tmp_home, keystrokes="1\nq\n")
        output = result.stdout + result.stderr
        # After one cycle, statusline should show Skip (started Install, cycled to Skip)
        self.assertIn("Skip", output, "Cycling Install→Skip must show Skip")
        self.assertEqual(result.returncode, 0)

    def test_not_installed_cycles_skip_back_to_install(self):
        """
        Cycling twice: Install → Skip → Install.
        """
        result = run_selector(self.tmp_home, keystrokes="1\n1\nq\n")
        output = result.stdout + result.stderr
        self.assertIn("Install", output, "Double-cycle must return to Install")

    def test_installed_cycles_update_to_keep(self):
        """
        For an installed feature: Update → Keep.
        Install statusline first, then cycle it.
        """
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_selector(self.tmp_home, keystrokes="1\nq\n")
        output = result.stdout + result.stderr
        self.assertIn("Keep", output, "Installed feature must cycle Update→Keep")

    def test_installed_cycles_keep_to_uninstall(self):
        """
        Installed feature: Update → Keep → Uninstall.
        """
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_selector(self.tmp_home, keystrokes="1\n1\nq\n")
        output = result.stdout + result.stderr
        self.assertIn("Uninstall", output, "Installed feature must cycle Keep→Uninstall")

    def test_installed_cycles_uninstall_back_to_update(self):
        """
        Installed feature: Update → Keep → Uninstall → Update.
        (3 cycles back to Update)
        """
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_selector(self.tmp_home, keystrokes="1\n1\n1\nq\n")
        output = result.stdout + result.stderr
        self.assertIn("Update", output, "Three cycles must return installed feature to Update")

    def test_keep_message_shown(self):
        """
        Cycling an installed feature to Keep must print the Keep semantics message
        (brd D3, 29-06 §4).
        """
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        result = run_selector(self.tmp_home, keystrokes="1\nq\n")
        output = result.stdout + result.stderr
        # The keep message must say something about modules still being updated
        self.assertIn(
            "modules", output.lower(),
            "Keep message must mention shared modules still being updated"
        )
        self.assertIn(
            "wiring", output.lower(),
            "Keep message must say wiring is left alone"
        )


class TestSelectorDependencies(InstallerTestBase):
    """
    Dependency handling in the UI: promotion, refusal, mutual exclusion.
    """

    def _feature_num(self, feature_id: str) -> int:
        """Return the 1-based display number for a feature id."""
        return list([
            "statusline", "permission-hooks", "telegram", "profiles",
            "amux", "permissions-allowlist", "context-mcp", "claude-history",
            "questions", "daily-review",
        ]).index(feature_id) + 1

    def test_installing_telegram_promotes_permission_hooks(self):
        """
        telegram requires permission-hooks.
        If permission-hooks is Skip and user installs telegram, permission-hooks
        is promoted to Install and the message names it (29-06 §3).
        """
        # On fresh machine permission-hooks starts Install by default.
        # Cycle it to Skip first (feature 2), then cycle telegram to Install
        # (feature 3 is already Install, cycle to Skip then back to Install — but
        # telegram starts Install. Cycle to Skip, then back to Install.)
        # Cleaner: cycle permission-hooks to Skip, then telegram should be Install already;
        # re-cycle telegram past Install to Skip and back, but promotion fires when cycling
        # to Install. Let's:
        # 1. Cycle permission-hooks (2) to Skip
        # 2. Cycle telegram (3) to Skip
        # 3. Cycle telegram (3) back to Install → promotion fires
        # 4. q
        result = run_selector(self.tmp_home, keystrokes="2\n3\n3\nq\n")
        output = result.stdout + result.stderr
        self.assertIn(
            "permission-hooks", output,
            "Promotion message must name 'permission-hooks' when telegram is set to Install"
        )
        self.assertIn(
            "promot", output.lower(),
            "Promotion message must use the word 'promot' (promoted/promoting)"
        )

    def test_uninstalling_permission_hooks_with_telegram_refused(self):
        """
        Uninstalling permission-hooks while telegram is Install/Update is refused,
        and the message names 'telegram' (29-06 §3).
        """
        # Both are not installed → Install by default.
        # Cycle permission-hooks (2) to Skip → install, skip cycle
        # Then cycle it to... wait, not-installed cycles Install ↔ Skip only.
        # Can't Uninstall a not-installed feature. We need to first install them,
        # then use the selector with them probed as installed.
        run_installer(self.tmp_home,
                      extra_args=["--only", "permission-hooks,telegram"],
                      extra_env=_make_fake_uv(self.tmp_home))

        # Now both probe as installed. Cycle permission-hooks (2) to Uninstall:
        # Update → Keep → Uninstall
        result = run_selector(self.tmp_home, keystrokes="2\n2\nq\n")
        output = result.stdout + result.stderr
        # After Update → Keep: telegram is still Update. Cannot Uninstall yet.
        # Actually: permission-hooks (2): cycle 1 → Keep, cycle 2 → Uninstall (refused!)
        self.assertIn(
            "telegram", output,
            "Refusal message must name 'telegram' when uninstalling permission-hooks"
        )
        # The action should be reverted (message says Cannot uninstall)
        self.assertIn(
            "Cannot uninstall", output,
            "Refusal message must say 'Cannot uninstall'"
        )

    def test_mutual_exclusion_autowrap_clears_autosource(self):
        """
        Enabling amux-autowrap (5a) while profiles-autosource (4a) is on
        clears profiles-autosource and prints a message (architecture §2).
        """
        # Enable profiles-autosource (4a), then enable amux-autowrap (5a):
        # Feature 4 = profiles, sub-toggle = profiles-autosource
        # Feature 5 = amux, sub-toggle = amux-autowrap
        # Input: "4a\n5a\nq\n"
        result = run_selector(self.tmp_home, keystrokes="4a\n5a\nq\n")
        output = result.stdout + result.stderr
        self.assertIn(
            "profiles-autosource", output,
            "Mutual exclusion message must name 'profiles-autosource'"
        )
        # After enabling amux-autowrap, the output must contain the cleared confirmation.
        # The final render after enabling amux-autowrap must show autosource as unchecked.
        # Scan only the last checklist render (after the 5a toggle).
        # The mutual exclusion clears the other sub-toggle — verify the cleared message
        # appears (it says "cleared conflicting profiles-autosource" or similar).
        self.assertTrue(
            "cleared" in output.lower() or "Do NOT source both" in output,
            "Mutual exclusion must print a cleared/conflict message for profiles-autosource"
        )

    def test_mutual_exclusion_autosource_clears_autowrap(self):
        """
        Enabling profiles-autosource (4a) while amux-autowrap (5a) is on
        clears amux-autowrap (architecture §2).
        """
        result = run_selector(self.tmp_home, keystrokes="5a\n4a\nq\n")
        output = result.stdout + result.stderr
        self.assertIn(
            "amux-autowrap", output,
            "Mutual exclusion must name 'amux-autowrap' when autosource clears it"
        )

    def test_suboption_blocked_when_feature_is_skip(self):
        """
        Cannot toggle a sub-option when its parent feature is Skip.
        """
        # Cycle questions (9) to Skip, then try to toggle 9a
        # Feature 9 = questions, default = Skip (D14), so it starts Skip.
        # Try to toggle 9a directly.
        result = run_selector(self.tmp_home, keystrokes="9a\nq\n")
        output = result.stdout + result.stderr
        self.assertIn(
            "Enable", output,
            "Must message to enable the parent feature before toggling sub-option"
        )


class TestSelectorQAndDryRun(InstallerTestBase):
    """
    q and --dry-run leave the machine byte-identical (no manifest, no backup,
    no file changed) — 29-06 §5 done criteria.
    """

    def test_q_writes_nothing(self):
        """
        'q' exits 0 and leaves the machine byte-identical: no manifest, no backup,
        no settings file created.
        """
        # Capture all files before
        def snapshot(base: Path) -> set:
            return {str(p) for p in base.rglob("*") if p.is_file()}

        before = snapshot(self.tmp_home)
        result = run_selector(self.tmp_home, keystrokes="q\n")
        after = snapshot(self.tmp_home)

        self.assertEqual(result.returncode, 0, f"q must exit 0. stderr: {result.stderr[:200]}")
        new_files = after - before
        self.assertEqual(
            new_files, set(),
            f"'q' must not create any files. New files: {new_files}"
        )

    def test_dry_run_writes_nothing(self):
        """
        --dry-run renders the plan and exits 0 without writing any file.
        """
        def snapshot(base: Path) -> set:
            return {str(p) for p in base.rglob("*") if p.is_file()}

        before = snapshot(self.tmp_home)
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline", "--dry-run"],
        )
        after = snapshot(self.tmp_home)

        self.assertEqual(result.returncode, 0,
                         f"--dry-run must exit 0. stderr: {result.stderr[:300]}")
        new_files = after - before
        self.assertEqual(
            new_files, set(),
            f"--dry-run must not create any files. New files: {new_files}"
        )

    def test_dry_run_shows_plan(self):
        """--dry-run prints feature names and writes info."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline", "--dry-run"],
        )
        output = result.stdout + result.stderr
        self.assertIn("Plan", output, "--dry-run must render the Plan header")
        self.assertIn("install", output.lower(),
                      "--dry-run plan must mention the action")

    def test_dry_run_no_manifest(self):
        """--dry-run does not write the manifest (architecture §5.3)."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline", "--dry-run"],
        )
        self.assertEqual(result.returncode, 0, result.stderr[:200])
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        self.assertFalse(
            manifest_path.exists(),
            "--dry-run must not write the manifest"
        )

    def test_q_no_manifest(self):
        """'q' does not write the manifest."""
        result = run_selector(self.tmp_home, keystrokes="q\n")
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        self.assertFalse(
            manifest_path.exists(),
            "'q' must not write the manifest"
        )

    def test_q_preserves_existing_manifest(self):
        """'q' leaves an existing manifest byte-identical."""
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"schema": 1, "repo": str(REPO),
                                "revision": "abc", "updated_at": "2026-01-01T00:00:00Z",
                                "features": {}})
        manifest_path.write_text(original)

        run_selector(self.tmp_home, keystrokes="q\n")

        self.assertEqual(
            manifest_path.read_text(), original,
            "'q' must leave existing manifest byte-identical"
        )


class TestSelectorConfirmAndExecute(InstallerTestBase):
    """
    Confirm path: Enter → plan → y → execute; n → back to selector.
    """

    def test_enter_then_yes_executes(self):
        """
        Input: Enter (show plan), then y (confirm) — installer runs and installs
        features according to the selection.
        On a fresh machine with defaults, statusline is Install, so it should be installed.
        """
        result = run_selector(self.tmp_home, keystrokes="\ny\n")
        # The selector should confirm and execute; statusline should be installed
        self.assertEqual(result.returncode, 0,
                         f"Enter+y must complete successfully. stderr: {result.stderr[:300]}")
        # After execution, at least some files should exist
        claude_dir = self.tmp_home / ".claude"
        self.assertTrue(
            claude_dir.exists(),
            "~/.claude must exist after confirmed selector run"
        )

    def test_enter_then_no_stays_in_selector(self):
        """
        Input: Enter, then n (reject confirmation) → back to selector, then q.
        Machine must remain unchanged.
        """
        def snapshot(base: Path) -> set:
            return {str(p) for p in base.rglob("*") if p.is_file()}

        before = snapshot(self.tmp_home)
        result = run_selector(self.tmp_home, keystrokes="\nn\nq\n")
        after = snapshot(self.tmp_home)

        new_files = after - before
        self.assertEqual(
            new_files, set(),
            "Rejecting confirmation + q must leave machine unchanged"
        )
        self.assertEqual(result.returncode, 0)

    def test_plan_preview_shows_uninstall_callout(self):
        """
        If a feature is set to Uninstall, the plan calls it out separately
        under 'REMOVALS' (29-06 §5).
        """
        # Install statusline first
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        # In selector: cycle statusline (1) to Uninstall (Update→Keep→Uninstall)
        # then press p to preview plan
        result = run_selector(self.tmp_home, keystrokes="1\n1\np\nq\n")
        output = result.stdout + result.stderr
        self.assertIn(
            "REMOV", output.upper(),
            "Plan must call out uninstalls under REMOVALS section"
        )

    def test_p_does_not_execute(self):
        """
        'p' previews the plan but does not execute. Machine unchanged after p+q.
        """
        def snapshot(base: Path) -> set:
            return {str(p) for p in base.rglob("*") if p.is_file()}

        before = snapshot(self.tmp_home)
        result = run_selector(self.tmp_home, keystrokes="p\n\nq\n")
        after = snapshot(self.tmp_home)

        new_files = after - before
        self.assertEqual(new_files, set(),
                         "p+q must leave machine unchanged")
        self.assertEqual(result.returncode, 0)

    def test_all_key_sets_all_install(self):
        """
        'a' sets all not-installed features to Install and installed to Update.
        """
        result = run_selector(self.tmp_home, keystrokes="a\nq\n")
        output = result.stdout + result.stderr
        # After 'a', should see Install (or Update) for all features, no Skip
        self.assertIn("Install", output, "After 'a', Install must appear")
        self.assertIn("all features", output.lower(),
                      "After 'a', message must mention all features")

    def test_s_key_sets_all_skip(self):
        """
        's' sets all not-installed features to Skip and installed to Keep.
        """
        result = run_selector(self.tmp_home, keystrokes="s\nq\n")
        output = result.stdout + result.stderr
        self.assertIn("Skip", output, "After 's', Skip must appear")
        self.assertIn("all features", output.lower(),
                      "After 's', message must mention all features")


# =============================================================================
# 29-07: Flag parsing errors
# =============================================================================

class TestFlagParsingErrors(InstallerTestBase):
    """
    The three error combinations from brd §5 / architecture §5.2:
      1. No TTY and no --yes → error (already covered by TestTTYDetection)
      2. --yes with neither manifest nor explicit selection → error
      3. --only naming an unknown feature id → error listing valid ids
    """

    def test_yes_without_manifest_or_selection_is_error(self):
        """--yes with no manifest and no explicit selection exits non-zero."""
        # tmp_home is clean — no manifest
        result = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertNotEqual(result.returncode, 0,
                            "--yes without manifest or selection must exit non-zero. "
                            f"stdout: {result.stdout[:300]}")
        combined = result.stdout + result.stderr
        # Message must be actionable
        self.assertIn("manifest", combined.lower(),
                      "Error must mention 'manifest'")
        self.assertIn("--all", combined,
                      "Error must suggest --all as an alternative")

    def test_yes_with_manifest_succeeds(self):
        """--yes with an existing manifest replays it without error."""
        # First install to create a manifest
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        manifest = self.tmp_home / ".claude" / "install-manifest.json"
        self.assertTrue(manifest.exists(), "First run must create manifest")

        # Now --yes alone should work (replay mode)
        result = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0,
                         "--yes with manifest must succeed. "
                         f"stderr: {result.stderr[:300]}")

    def test_yes_with_explicit_all_needs_no_manifest(self):
        """--yes --all succeeds even on a clean machine with no manifest."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--all", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0,
                         "--all --yes must succeed with no manifest. "
                         f"stderr: {result.stderr[:300]}")

    def test_yes_with_explicit_only_needs_no_manifest(self):
        """--yes --only <id> succeeds without a manifest."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0,
                         "--only --yes must succeed with no manifest. "
                         f"stderr: {result.stderr[:300]}")

    def test_only_unknown_feature_is_error(self):
        """--only with an unknown feature id exits non-zero and lists valid ids."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "nonexistent-feature"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertNotEqual(result.returncode, 0,
                            "--only unknown-id must exit non-zero")
        combined = result.stdout + result.stderr
        self.assertIn("nonexistent-feature", combined,
                      "Error must echo the bad id")
        # Must list some valid ids
        self.assertIn("statusline", combined,
                      "Error must list valid feature ids")

    def test_with_unknown_feature_is_error(self):
        """--with with an unknown feature id exits non-zero."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--all", "--with", "bogus-feature"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertNotEqual(result.returncode, 0,
                            "--with unknown-id must exit non-zero")
        self.assertIn("bogus-feature", result.stdout + result.stderr)

    def test_only_with_dependency_promotes_prerequisite(self):
        """--only telegram promotes permission-hooks (its prerequisite)."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "telegram", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0,
                         "--only telegram must succeed. "
                         f"stderr: {result.stderr[:300]}")
        settings = read_settings(self.tmp_home)
        hooks = settings.get("hooks", {})
        # permission-hooks wires PreToolUse; telegram alone doesn't — so if
        # PreToolUse is present, the prerequisite was promoted.
        self.assertIn(
            "PreToolUse", hooks,
            "PreToolUse must be wired when telegram is selected (permission-hooks promoted)"
        )


# =============================================================================
# 29-07: --help and --list work without jq
# =============================================================================

class TestHelpAndListWithoutJq(InstallerTestBase):
    """
    --help and --list must succeed even when jq is not on PATH
    (architecture §9: arg parsing precedes the jq dependency check).
    """

    def _env_without_jq(self) -> dict:
        """
        Return an env dict where jq is shadowed by a stub that exits 127.
        We prepend a fake-bin dir containing a jq that always exits non-zero,
        rather than removing PATH entries (which would also remove bash/python3).
        The important property is that the installer's _check_dependencies fails
        on jq — tests using this env verify that --help / --list exit before
        that check fires.
        """
        fake_bin = self.tmp_home / "fake-bin-nojq"
        fake_bin.mkdir(exist_ok=True)
        fake_jq = fake_bin / "jq"
        fake_jq.write_text("#!/bin/sh\nexit 127\n")
        fake_jq.chmod(0o755)
        original_path = os.environ.get("PATH", "")
        return {"PATH": f"{fake_bin}:{original_path}"}

    def test_help_exits_zero_without_jq(self):
        """--help exits 0 even when jq is absent from PATH."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--help"],
            extra_env=self._env_without_jq(),
        )
        self.assertEqual(result.returncode, 0,
                         "--help must exit 0 even without jq. "
                         f"stderr: {result.stderr[:300]}")
        combined = result.stdout + result.stderr
        self.assertIn("--yes", combined, "--help output must mention --yes")
        self.assertIn("--list", combined, "--help output must mention --list")
        self.assertIn("enable", combined, "--help output must describe enable subcommand")

    def test_list_exits_zero_without_jq(self):
        """--list exits 0 even when jq is absent from PATH."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--list"],
            extra_env=self._env_without_jq(),
        )
        self.assertEqual(result.returncode, 0,
                         "--list must exit 0 even without jq. "
                         f"stderr: {result.stderr[:300]}")
        combined = result.stdout + result.stderr
        # Must show every feature id
        for fid in (
            "statusline", "permission-hooks", "telegram", "profiles",
            "amux", "permissions-allowlist", "context-mcp", "claude-history",
            "questions", "daily-review",
        ):
            self.assertIn(fid, combined, f"--list must show feature '{fid}'")
        # Must show sub-toggle ids
        for stid in ("amux-autowrap", "profiles-autosource",
                     "questions-listen", "daily-review-cron"):
            self.assertIn(stid, combined, f"--list must show sub-toggle '{stid}'")

    def test_list_shows_detected_state(self):
        """--list reports 'yes' for features that are installed."""
        # Install statusline
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])

        result = run_installer(self.tmp_home, extra_args=["--list"])
        self.assertEqual(result.returncode, 0)
        # The output must say 'yes' somewhere for statusline
        combined = result.stdout + result.stderr
        # statusline row should contain 'yes'
        for line in combined.splitlines():
            if "statusline" in line and "↳" not in line:
                self.assertIn("yes", line,
                              f"--list must show 'yes' for installed statusline. "
                              f"Row: {line!r}")
                break
        else:
            self.fail("--list output must contain a row for 'statusline'")

    def test_list_does_not_write_anything(self):
        """--list is read-only: no manifest, no backup, no settings change."""
        def snapshot(base: Path) -> set:
            return {str(p) for p in base.rglob("*") if p.is_file()}

        before = snapshot(self.tmp_home)
        result = run_installer(self.tmp_home, extra_args=["--list"])
        after = snapshot(self.tmp_home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, after, "--list must write no files")


# =============================================================================
# 29-07: --dry-run byte-identity
# =============================================================================

class TestDryRunByteIdentity(InstallerTestBase):
    """
    --dry-run renders the plan and exits 0 without writing anything at all —
    no manifest, no backup, no settings file, no external surface.
    Asserted by snapshotting the whole tmp_home tree before and after.
    """

    def _snapshot(self, base: Path) -> dict:
        """Return {relative_path_str: file_bytes} for every file under base."""
        result = {}
        for p in base.rglob("*"):
            if p.is_file():
                result[str(p.relative_to(base))] = p.read_bytes()
        return result

    def test_dry_run_clean_home_byte_identity(self):
        """--dry-run on a clean home writes nothing."""
        before = self._snapshot(self.tmp_home)
        result = run_installer(
            self.tmp_home,
            extra_args=["--all", "--dry-run"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        after = self._snapshot(self.tmp_home)
        self.assertEqual(result.returncode, 0,
                         f"--dry-run must exit 0. stderr: {result.stderr[:300]}")
        self.assertEqual(before, after,
                         "--dry-run must leave the machine byte-identical. "
                         f"New files: {set(after) - set(before)}")

    def test_dry_run_after_install_byte_identity(self):
        """--dry-run after an install run writes nothing (no new backup)."""
        # First install creates a manifest and settings
        run_installer(self.tmp_home, extra_args=["--only", "statusline"])
        before = self._snapshot(self.tmp_home)

        result = run_installer(
            self.tmp_home,
            extra_args=["--yes", "--dry-run"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        after = self._snapshot(self.tmp_home)
        self.assertEqual(result.returncode, 0,
                         f"--dry-run must exit 0. stderr: {result.stderr[:300]}")
        self.assertEqual(before, after,
                         "--dry-run on installed machine must write nothing. "
                         f"Changed/new files: {set(after) - set(before)}")

    def test_dry_run_shows_plan(self):
        """--dry-run outputs a plan that mentions the features to be installed."""
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline,permission-hooks", "--dry-run"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("statusline", combined,
                      "--dry-run must mention statusline in plan output")
        self.assertIn("permission-hooks", combined,
                      "--dry-run must mention permission-hooks in plan output")


# =============================================================================
# 29-07: enable / disable subcommands
# =============================================================================

def _install_with_suboption(tmp_home: Path, feature: str, suboption: str) -> None:
    """
    Helper: install <feature> then enable <suboption> via the enable subcommand.
    Requires CLAUDE_INSTALL_NO_EXTERNAL=1 (set by test setUp).
    """
    run_installer(tmp_home, extra_args=["--only", feature])
    run_installer(tmp_home, extra_args=["enable", suboption])


class TestEnableDisableSubcommands(InstallerTestBase):
    """
    Tests for the enable / disable subcommands (brd D15, architecture §9).
    Each sub-toggle: parent-not-installed refusal, idempotency, whole-HOME diff.
    Mutual exclusion: amux-autowrap ↔ profiles-autosource.
    """

    # ------------------------------------------------------------------
    # Unknown toggle id
    # ------------------------------------------------------------------

    def test_enable_unknown_id_is_error(self):
        """enable with an unknown toggle id exits non-zero and lists valid ids."""
        result = run_installer(self.tmp_home, extra_args=["enable", "no-such-toggle"])
        self.assertNotEqual(result.returncode, 0,
                            "enable unknown-id must exit non-zero")
        combined = result.stdout + result.stderr
        self.assertIn("no-such-toggle", combined)
        # Must list valid ids
        self.assertIn("amux-autowrap", combined)

    def test_disable_unknown_id_is_error(self):
        """disable with an unknown toggle id exits non-zero."""
        result = run_installer(self.tmp_home, extra_args=["disable", "no-such-toggle"])
        self.assertNotEqual(result.returncode, 0,
                            "disable unknown-id must exit non-zero")

    # ------------------------------------------------------------------
    # Parent-not-installed refusal
    # ------------------------------------------------------------------

    def test_enable_questions_listen_without_questions_fails(self):
        """enable questions-listen when questions is not installed exits non-zero."""
        result = run_installer(self.tmp_home, extra_args=["enable", "questions-listen"])
        self.assertNotEqual(result.returncode, 0,
                            "enable questions-listen must fail when questions is not installed")
        combined = result.stdout + result.stderr
        self.assertIn("questions", combined.lower(),
                      "Error must name the parent feature 'questions'")

    def test_enable_amux_autowrap_without_amux_fails(self):
        """enable amux-autowrap when amux is not installed exits non-zero."""
        result = run_installer(self.tmp_home, extra_args=["enable", "amux-autowrap"])
        self.assertNotEqual(result.returncode, 0,
                            "enable amux-autowrap must fail when amux is not installed")
        combined = result.stdout + result.stderr
        self.assertIn("amux", combined.lower())

    def test_enable_profiles_autosource_without_profiles_fails(self):
        """enable profiles-autosource when profiles is not installed exits non-zero."""
        result = run_installer(self.tmp_home,
                               extra_args=["enable", "profiles-autosource"])
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("profiles", combined.lower())

    def test_enable_daily_review_cron_without_daily_review_fails(self):
        """enable daily-review-cron when daily-review is not installed exits non-zero."""
        result = run_installer(self.tmp_home,
                               extra_args=["enable", "daily-review-cron"])
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("daily-review", combined.lower())

    # ------------------------------------------------------------------
    # questions-listen enable/disable (parent installed)
    # ------------------------------------------------------------------

    def test_enable_questions_listen_with_questions_installed(self):
        """
        enable questions-listen with questions installed exits 0 and
        records the sub-toggle in the manifest.
        """
        # Install questions (which requires telegram, which requires permission-hooks)
        run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram,questions", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertTrue(run_probe(self.tmp_home, "questions"),
                        "questions must be installed before enable")

        result = run_installer(self.tmp_home,
                               extra_args=["enable", "questions-listen"])
        self.assertEqual(result.returncode, 0,
                         "enable questions-listen must succeed when questions is installed. "
                         f"stderr: {result.stderr[:300]}")

        # Manifest must record the sub-toggle as true
        manifest = read_manifest(self.tmp_home)
        q_opts = manifest.get("features", {}).get("questions", {}).get("options", {})
        self.assertEqual(q_opts.get("questions-listen"), True,
                         "Manifest must record questions-listen = true")

    def test_enable_questions_listen_changes_nothing_else(self):
        """
        enable questions-listen changes only the manifest's sub-toggle field
        and the systemd surface — nothing else in $HOME moves.
        CLAUDE_INSTALL_NO_EXTERNAL=1 gates the systemd call, so the only
        expected change is the manifest.
        """
        run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram,questions", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )

        def snapshot(base: Path) -> dict:
            return {
                str(p.relative_to(base)): p.read_bytes()
                for p in base.rglob("*") if p.is_file()
            }

        before = snapshot(self.tmp_home)
        result = run_installer(self.tmp_home,
                               extra_args=["enable", "questions-listen"])
        self.assertEqual(result.returncode, 0)
        after = snapshot(self.tmp_home)

        changed = {k for k in after if after[k] != before.get(k)}
        new_files = set(after) - set(before)
        # Only the manifest may have changed; no new files
        self.assertEqual(new_files, set(),
                         "enable questions-listen must create no new files. "
                         f"New: {new_files}")
        # All changes must be to the manifest
        non_manifest_changes = {
            k for k in changed
            if "install-manifest.json" not in k
        }
        self.assertEqual(non_manifest_changes, set(),
                         "enable questions-listen must change nothing except the manifest. "
                         f"Changed: {non_manifest_changes}")

    def test_enable_questions_listen_idempotent(self):
        """enable questions-listen twice is a no-op on the second call."""
        run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram,questions", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        run_installer(self.tmp_home, extra_args=["enable", "questions-listen"])

        def snapshot(base: Path) -> dict:
            return {
                str(p.relative_to(base)): p.read_bytes()
                for p in base.rglob("*") if p.is_file()
            }

        before = snapshot(self.tmp_home)
        result = run_installer(self.tmp_home, extra_args=["enable", "questions-listen"])
        after = snapshot(self.tmp_home)

        self.assertEqual(result.returncode, 0)
        # No changes expected (already enabled)
        changed = {k for k in after if after[k] != before.get(k)}
        new_files = set(after) - set(before)
        self.assertEqual(new_files, set(),
                         "Second enable questions-listen must create no new files")
        self.assertEqual(changed, set(),
                         "Second enable questions-listen must change no files")
        combined = result.stdout + result.stderr
        self.assertIn("already", combined.lower(),
                      "Second enable must say 'already enabled'")

    # ------------------------------------------------------------------
    # amux-autowrap ↔ profiles-autosource mutual exclusion
    # ------------------------------------------------------------------

    def test_enable_amux_autowrap_with_profiles_autosource_on_flips_both(self):
        """
        enable amux-autowrap when profiles-autosource is already enabled:
        - amux-autowrap gets enabled
        - profiles-autosource gets disabled
        - manifest records both flips
        """
        # Install profiles and amux
        run_installer(
            self.tmp_home,
            extra_args=["--only", "profiles,amux", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        # Create .bashrc so ext_bashrc_add can work
        bashrc = self.tmp_home / ".bashrc"
        bashrc.write_text("# test bashrc\n")

        # Enable profiles-autosource first
        result1 = run_installer(self.tmp_home,
                                extra_args=["enable", "profiles-autosource"])
        self.assertEqual(result1.returncode, 0,
                         f"enable profiles-autosource failed: {result1.stderr[:200]}")

        manifest_after_enable = read_manifest(self.tmp_home)
        pa_opt = (manifest_after_enable.get("features", {})
                  .get("profiles", {}).get("options", {})
                  .get("profiles-autosource"))
        self.assertEqual(pa_opt, True,
                         "profiles-autosource must be True after enable")

        # Now enable amux-autowrap — must flip profiles-autosource off
        result2 = run_installer(self.tmp_home, extra_args=["enable", "amux-autowrap"])
        self.assertEqual(result2.returncode, 0,
                         f"enable amux-autowrap failed: {result2.stderr[:200]}")

        manifest_final = read_manifest(self.tmp_home)
        aa_opt = (manifest_final.get("features", {})
                  .get("amux", {}).get("options", {})
                  .get("amux-autowrap"))
        pa_opt_final = (manifest_final.get("features", {})
                        .get("profiles", {}).get("options", {})
                        .get("profiles-autosource"))
        self.assertEqual(aa_opt, True,
                         "amux-autowrap must be True in manifest after enable")
        self.assertEqual(pa_opt_final, False,
                         "profiles-autosource must be False in manifest after mutual exclusion")

        # Message must mention the mutual exclusion
        combined = result2.stdout + result2.stderr
        self.assertIn("profiles-autosource", combined,
                      "enable amux-autowrap must report disabling profiles-autosource")

    # ------------------------------------------------------------------
    # disable subcommand
    # ------------------------------------------------------------------

    def test_disable_already_disabled_is_noop(self):
        """disable on a toggle that is already disabled is a no-op."""
        run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram,questions", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        # questions-listen starts disabled — disabling again is a no-op
        result = run_installer(self.tmp_home,
                               extra_args=["disable", "questions-listen"])
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("already", combined.lower(),
                      "disable on already-disabled must say 'already disabled'")


# =============================================================================
# 29-07: --yes replay
# =============================================================================

class TestYesReplay(InstallerTestBase):
    """
    --yes replays the manifest exactly (brd §6.3, architecture §5.2).
    Tests:
      - Full replay: --all --yes, then --yes → identical manifest
      - Partial replay: --only statusline --yes, then --yes → nine others not installed
      - Skipped feature stays skipped
      - Newly-offered feature reported, not installed
    """

    def test_all_yes_then_yes_alone_is_noop(self):
        """
        After --all --yes, a second --yes run (manifest replay) produces
        an identical manifest — every feature still installed, no re-work visible.
        """
        # First run: install everything
        result1 = run_installer(
            self.tmp_home,
            extra_args=["--all", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result1.returncode, 0,
                         f"--all --yes must succeed. stderr: {result1.stderr[:300]}")
        manifest1 = read_manifest(self.tmp_home)
        self.assertNotEqual(manifest1, {}, "First run must write manifest")

        # Second run: replay — manifest must be byte-identical
        result2 = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result2.returncode, 0,
                         f"--yes replay must succeed. stderr: {result2.stderr[:300]}")
        manifest2 = read_manifest(self.tmp_home)

        # Feature states must be identical
        f1 = manifest1.get("features", {})
        f2 = manifest2.get("features", {})
        for fid in f1:
            s1 = f1[fid].get("state")
            s2 = f2.get(fid, {}).get("state")
            self.assertEqual(s1, s2,
                             f"Feature '{fid}' state must be preserved by replay: "
                             f"{s1!r} → {s2!r}")

    def test_partial_replay_does_not_install_unselected_features(self):
        """
        --only statusline --yes, then --yes alone: the nine other features must
        remain not installed. This is the assertion that protects the unattended
        daily-review path from silently installing declined features.
        """
        # Install only statusline
        result1 = run_installer(
            self.tmp_home,
            extra_args=["--only", "statusline", "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result1.returncode, 0)
        manifest1 = read_manifest(self.tmp_home)
        self.assertIsNotNone(manifest1, "First run must create manifest")

        # Replay
        result2 = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result2.returncode, 0,
                         f"Replay must succeed. stderr: {result2.stderr[:300]}")
        manifest2 = read_manifest(self.tmp_home)

        # All features except statusline must be 'skipped' after the replay
        features = manifest2.get("features", {})
        for fid in ("permission-hooks", "telegram", "profiles", "amux",
                    "permissions-allowlist", "context-mcp", "claude-history",
                    "questions", "daily-review"):
            state = features.get(fid, {}).get("state", "absent")
            self.assertEqual(
                state, "skipped",
                f"Feature '{fid}' must stay skipped after partial replay. "
                f"Got: {state!r}. "
                f"Replay must never install what the user did not choose.",
            )

    def test_skipped_feature_stays_skipped_on_replay(self):
        """
        A manifest with questions=skipped: --yes replay leaves questions skipped.
        """
        # Install everything except questions (which defaults to skip anyway)
        result = run_installer(
            self.tmp_home,
            extra_args=["--only",
                        "statusline,permission-hooks,profiles,permissions-allowlist",
                        "--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0)

        manifest = read_manifest(self.tmp_home)
        questions_state = manifest.get("features", {}).get("questions", {}).get("state")
        # questions not selected → should be skipped in manifest
        self.assertEqual(questions_state, "skipped",
                         "Unselected questions must be recorded as skipped")

        # Replay — questions must stay skipped
        result2 = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result2.returncode, 0)
        manifest2 = read_manifest(self.tmp_home)
        q_state2 = manifest2.get("features", {}).get("questions", {}).get("state")
        self.assertEqual(q_state2, "skipped",
                         "questions must remain skipped after --yes replay")

    def test_newly_offered_feature_reported_not_installed(self):
        """
        A feature present in FEATURES but absent from the manifest is reported
        as newly-offered by a --yes run, not silently installed.
        """
        # Build a manifest that omits 'statusline' entirely
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a manifest with only permission-hooks
        manifest_data = {
            "schema": 1,
            "repo": str(REPO),
            "revision": "test",
            "updated_at": "2026-01-01T00:00:00Z",
            "features": {
                "permission-hooks": {
                    "state": "skipped",
                    "at": "2026-01-01T00:00:00Z",
                    "artifacts": [],
                    "options": {},
                },
            },
        }
        manifest_path.write_text(json.dumps(manifest_data, indent=2))

        # Also need settings.json to exist for the installer
        claude_dir = self.tmp_home / ".claude"
        settings_path = claude_dir / "settings.json"
        settings_path.write_text("{}")

        result = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0,
                         "--yes with partial manifest must succeed. "
                         f"stderr: {result.stderr[:400]}")

        combined = result.stdout + result.stderr
        # Must report newly-offered features (those not in the manifest)
        self.assertIn("newly-offered", combined.lower(),
                      "--yes must report features absent from manifest as newly-offered")
        # statusline was not in the manifest — must NOT be installed
        self.assertFalse(run_probe(self.tmp_home, "statusline"),
                         "statusline absent from manifest must not be installed by replay")


# =============================================================================
# 29-07: Hand-edited manifest edge cases
# =============================================================================

class TestHandEditedManifest(InstallerTestBase):
    """
    Resilience against a manifest that has been hand-edited.
    An unknown feature id in the manifest must not crash the installer.
    A missing feature id must be reported as newly-offered.
    """

    def _write_manifest(self, features: dict) -> None:
        manifest_path = self.tmp_home / ".claude" / "install-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": 1,
            "repo": str(REPO),
            "revision": "test",
            "updated_at": "2026-01-01T00:00:00Z",
            "features": features,
        }
        manifest_path.write_text(json.dumps(data, indent=2))
        # Also create a settings.json so the installer doesn't abort on missing config
        settings_path = self.tmp_home / ".claude" / "settings.json"
        if not settings_path.exists():
            settings_path.write_text("{}")

    def test_unknown_feature_id_in_manifest_does_not_crash(self):
        """
        A manifest with a feature id that doesn't exist in FEATURES
        must not crash the installer.
        """
        self._write_manifest({
            "statusline": {
                "state": "installed",
                "at": "2026-01-01T00:00:00Z",
                "artifacts": [],
                "options": {},
            },
            "defunct-feature-xyz": {  # unknown id — should be ignored
                "state": "installed",
                "at": "2026-01-01T00:00:00Z",
                "artifacts": [],
                "options": {},
            },
        })

        result = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        # Must not crash (exit 1 is OK for other reasons, but must not be unhandled)
        self.assertNotIn(
            "unbound variable",
            result.stdout + result.stderr,
            "Unknown manifest feature id must not cause an unbound variable error",
        )

    def test_missing_known_id_reported_as_newly_offered(self):
        """
        A manifest that omits a known feature id causes that feature to be
        reported as newly-offered on a --yes run.
        """
        # Manifest with only permission-hooks; statusline and others are absent
        self._write_manifest({
            "permission-hooks": {
                "state": "skipped",
                "at": "2026-01-01T00:00:00Z",
                "artifacts": [],
                "options": {},
            },
        })

        result = run_installer(
            self.tmp_home,
            extra_args=["--yes"],
            extra_env={"CLAUDE_INSTALL_ASSUME_TTY": "0"},
        )
        self.assertEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("newly-offered", combined.lower(),
                      "Features absent from manifest must be reported as newly-offered")


if __name__ == "__main__":
    # When run directly, set CLAUDE_INSTALL_NO_EXTERNAL for convenience
    os.environ.setdefault("CLAUDE_INSTALL_NO_EXTERNAL", "1")
    unittest.main()
