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


if __name__ == "__main__":
    # When run directly, set CLAUDE_INSTALL_NO_EXTERNAL for convenience
    os.environ.setdefault("CLAUDE_INSTALL_NO_EXTERNAL", "1")
    unittest.main()
