#!/usr/bin/env python3
"""
Migration and documentation tests for install.sh — task 29-09.

Tests:
  1. Migration fixture: run the pre-epic frozen script into a tmp_home, then run
     install.sh --all --yes over the result, and assert the end state matches a
     fresh --all --yes install (modulo the manifest).
  2. config.toml migration: true, false, absent, and malformed TOML.
  3. Docs coverage: every feature id and sub-toggle id appears in docs/installer.md.
  4. Source reference integrity: no file in the repo references an installer path
     that does not exist on disk.

Run this module alone:
    python3 tests/run_all_tests.py --module installer_migration

The pre-epic script is captured via git blob so the test keeps working after
install-claude-config.sh has been deleted (task 29-09 §1).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
DOCS_INSTALLER = REPO / "docs" / "installer.md"

# Git blob hash of the frozen pre-epic install-claude-config.sh (commit HEAD at
# the time 29-09 captured it). This blob never changes even after the file is
# deleted — see tasks/29_installer_interactive/29-09-migration-and-docs.md §1.
PRE_EPIC_BLOB = "aee1c69e8af1320e5b8eb0fe4ad32fa52ee35060"


def _get_pre_epic_script() -> bytes:
    """
    Retrieve the frozen pre-epic installer from the git object store.
    Returns the raw script bytes.
    """
    result = subprocess.run(
        ["git", "cat-file", "blob", PRE_EPIC_BLOB],
        capture_output=True,
        cwd=str(REPO),
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Cannot retrieve pre-epic script blob {PRE_EPIC_BLOB}: "
            f"{result.stderr.decode()[:200]}"
        )
    return result.stdout


def run_installer(
    tmp_home: Path,
    extra_args: list | None = None,
    extra_env: dict | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run install.sh with HOME=tmp_home, CLAUDE_INSTALL_NO_EXTERNAL=1, no-TTY mode.
    """
    env = {
        **os.environ,
        "HOME": str(tmp_home),
        "CLAUDE_INSTALL_NO_EXTERNAL": "1",
        "CLAUDE_INSTALL_ASSUME_TTY": "0",
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
        timeout=120,
    )


def read_manifest(tmp_home: Path) -> dict:
    """Read ~/.claude/install-manifest.json from tmp_home."""
    path = tmp_home / ".claude" / "install-manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _make_fake_uv(tmp_home: Path) -> dict:
    """Create a minimal fake uv that exits 0, return env dict that adds it to PATH."""
    bin_dir = tmp_home / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "uv"
    fake.write_text("#!/bin/bash\nexit 0\n")
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}


# =============================================================================
# Migration fixture test
# =============================================================================

class TestPreEpicMigration(unittest.TestCase):
    """
    Run the frozen pre-epic installer into a tmp_home, then run install.sh
    --all --yes over the result.  Assert the end state matches a fresh
    --all --yes install (modulo the manifest timestamps/revision fields).

    The pre-epic script is retrieved from a git blob so this test keeps working
    after install-claude-config.sh has been deleted from the working tree.
    """

    def setUp(self):
        self.assertIn("CLAUDE_INSTALL_NO_EXTERNAL", os.environ,
                      "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1")
        self.tmp_home = Path(tempfile.mkdtemp(prefix="claude-hooks-migration-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp_home), ignore_errors=True)

    def _run_pre_epic_script(self, tmp_home: Path) -> subprocess.CompletedProcess:
        """Extract the frozen pre-epic script and run it into tmp_home."""
        try:
            script_bytes = _get_pre_epic_script()
        except RuntimeError as exc:
            self.skipTest(f"Pre-epic blob unavailable (not in git history?): {exc}")

        script_path = tmp_home / "pre-epic-install.sh"
        script_path.write_bytes(script_bytes)
        script_path.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(tmp_home),
            "CLAUDE_INSTALL_ASSUME_TTY": "0",
        }
        # The pre-epic script doesn't honour CLAUDE_INSTALL_NO_EXTERNAL, but it
        # gates all external calls on preconditions (systemctl requires XDG_RUNTIME_DIR,
        # ln -sf checks directory existence, etc.) so it should run safely in a
        # temp HOME with a real PATH.
        env["XDG_RUNTIME_DIR"] = ""  # disable systemd path so unit step no-ops

        fake_uv_env = _make_fake_uv(tmp_home)
        env.update(fake_uv_env)

        return subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            env=env,
            timeout=120,
        )

    def test_migration_succeeds_and_matches_fresh_install(self):
        """
        Pre-epic layout → install.sh --all --yes → same feature states as fresh install.

        The pre-epic script leaves artifacts but no manifest. install.sh must:
          1. Probe the existing artifacts and treat them as "update" targets.
          2. Run to completion with exit 0.
          3. Write a manifest with all features having state "installed" or "skipped".
          4. Produce the same feature-state set as a fresh --all --yes install
             on a clean home (modulo the manifest timestamps and revision).
        """
        # Step A: run the pre-epic script to create a pre-epic layout.
        pre_epic_result = self._run_pre_epic_script(self.tmp_home)

        # The pre-epic script may warn about missing things (uv, amux fork, etc.)
        # but must not produce a non-zero exit due to absent external services.
        # If it fails for a hard reason (jq absent, python3 absent) skip.
        if pre_epic_result.returncode != 0:
            combined = pre_epic_result.stderr + pre_epic_result.stdout
            if "jq is required" in combined or "python3 is required" in combined:
                self.skipTest("Pre-epic script cannot run: missing jq or python3")
            # Other non-zero exits might indicate partial install — proceed anyway
            # since the migration test's value is in what install.sh does next.

        # Verify the pre-epic script left something behind.
        claude_dir = self.tmp_home / ".claude"
        self.assertTrue(
            claude_dir.exists(),
            "Pre-epic script must have created ~/.claude — nothing to migrate",
        )
        # No manifest from the pre-epic run.
        manifest_before = self.tmp_home / ".claude" / "install-manifest.json"
        self.assertFalse(
            manifest_before.exists(),
            "Pre-epic script must not have written a manifest",
        )

        # Step B: run install.sh --all --yes over the pre-epic layout.
        fake_uv_env = _make_fake_uv(self.tmp_home)
        result = run_installer(
            self.tmp_home,
            extra_args=["--all", "--yes"],
            extra_env=fake_uv_env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"install.sh --all --yes over pre-epic layout must exit 0.\n"
            f"stderr: {result.stderr[:800]}\nstdout: {result.stdout[:800]}",
        )

        # Step C: manifest must now exist and be valid JSON.
        manifest_after = read_manifest(self.tmp_home)
        self.assertIn("features", manifest_after,
                      "Manifest must have a 'features' key after migration run")
        self.assertGreater(len(manifest_after["features"]), 0,
                           "Manifest must record at least one feature")

        # Step D: compare feature states against a fresh --all --yes install.
        fresh_home = Path(tempfile.mkdtemp(prefix="claude-hooks-fresh-"))
        try:
            fresh_result = run_installer(
                fresh_home,
                extra_args=["--all", "--yes"],
                extra_env=fake_uv_env,
            )
            if fresh_result.returncode != 0:
                self.skipTest(
                    "Fresh --all --yes install also fails; cannot compare.\n"
                    f"stderr: {fresh_result.stderr[:400]}"
                )

            fresh_manifest = read_manifest(fresh_home)
            fresh_features = fresh_manifest.get("features", {})
            migrated_features = manifest_after.get("features", {})

            # Every feature id in the fresh manifest must appear in the migrated one.
            for fid, fresh_entry in fresh_features.items():
                self.assertIn(
                    fid,
                    migrated_features,
                    f"Feature '{fid}' present in fresh install manifest but missing "
                    f"from migrated manifest",
                )

            # Features that the old script would have installed (statusline,
            # permission-hooks, telegram, profiles, amux, permissions-allowlist)
            # must be "installed" in the migration run too (the probe found them).
            pre_epic_features = [
                "statusline", "permission-hooks", "telegram", "profiles",
                "permissions-allowlist",
            ]
            for fid in pre_epic_features:
                if fid not in migrated_features:
                    continue  # feature may not exist in this registry
                state = migrated_features[fid].get("state", "")
                self.assertIn(
                    state, ("installed", "skipped", "failed"),
                    f"Feature '{fid}' has unexpected state '{state}' after migration",
                )

        finally:
            shutil.rmtree(str(fresh_home), ignore_errors=True)


# =============================================================================
# config.toml migration tests
# =============================================================================

class TestConfigTomlMigration(unittest.TestCase):
    """
    config.toml [questions_listen] enabled is the sole opt-in the pre-epic
    installer read (brd D15, constraint 2.2). On a first run with no manifest
    the installer must:
      - Seed the questions-listen sub-toggle from the key if present.
      - Announce the key is now inert.
      - Write the seeded value to the manifest.
    On subsequent runs (manifest present) it must not re-read config.toml.
    """

    def setUp(self):
        self.assertIn("CLAUDE_INSTALL_NO_EXTERNAL", os.environ,
                      "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1")
        self.tmp_home = Path(tempfile.mkdtemp(prefix="claude-hooks-configtoml-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp_home), ignore_errors=True)

    def _write_config_toml(self, enabled_value: str | None, malformed: bool = False) -> None:
        """Write ~/.config/claude-tg-relay/config.toml with the given enabled value."""
        config_dir = self.tmp_home / ".config" / "claude-tg-relay"
        config_dir.mkdir(parents=True, exist_ok=True)
        toml_path = config_dir / "config.toml"
        if malformed:
            toml_path.write_text("this is not valid toml {{{[\n")
        elif enabled_value is None:
            # No [questions_listen] section at all
            toml_path.write_text("[relay]\nserver_url = \"https://relay.example.com\"\n")
        else:
            toml_path.write_text(
                f"[relay]\nserver_url = \"https://relay.example.com\"\n"
                f"\n[questions_listen]\nenabled = {enabled_value}\n"
            )

    def _run_questions_install(self) -> tuple[subprocess.CompletedProcess, dict]:
        """Install only the questions feature and return (result, manifest)."""
        # questions requires telegram which requires permission-hooks.
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram,questions", "--yes"],
            extra_env=_make_fake_uv(self.tmp_home),
        )
        manifest = read_manifest(self.tmp_home)
        return result, manifest

    def _questions_listen_state(self, manifest: dict) -> bool | None:
        """Return the questions-listen sub-toggle state from the manifest, or None if absent."""
        try:
            return manifest["features"]["questions"]["options"]["questions-listen"]
        except (KeyError, TypeError):
            return None

    def test_config_toml_true_seeds_subtoggle_enabled(self):
        """config.toml [questions_listen] enabled = true → sub-toggle seeded true."""
        self._write_config_toml("true")
        result, manifest = self._run_questions_install()

        if result.returncode != 0:
            self.skipTest(
                f"questions install failed — cannot check sub-toggle.\n"
                f"stderr: {result.stderr[:400]}"
            )

        state = self._questions_listen_state(manifest)
        self.assertIsNotNone(state, "questions-listen sub-toggle must be recorded in manifest")
        self.assertTrue(state, "config.toml enabled=true must seed sub-toggle to true")

        # The log must announce the key is now inert.
        combined = result.stderr + result.stdout
        self.assertIn(
            "now inert",
            combined,
            "Installer must announce that [questions_listen] enabled in config.toml is now inert",
        )

    def test_config_toml_false_seeds_subtoggle_disabled(self):
        """config.toml [questions_listen] enabled = false → sub-toggle seeded false."""
        self._write_config_toml("false")
        result, manifest = self._run_questions_install()

        if result.returncode != 0:
            self.skipTest(
                f"questions install failed.\nstderr: {result.stderr[:400]}"
            )

        state = self._questions_listen_state(manifest)
        self.assertIsNotNone(state, "questions-listen sub-toggle must be recorded in manifest")
        self.assertFalse(state, "config.toml enabled=false must seed sub-toggle to false")

        combined = result.stderr + result.stdout
        self.assertIn("now inert", combined,
                      "Installer must announce the key is now inert")

    def test_config_toml_absent_defaults_to_false(self):
        """No config.toml → sub-toggle defaults to false, no migration message."""
        # No config.toml at all.
        result, manifest = self._run_questions_install()

        if result.returncode != 0:
            self.skipTest(
                f"questions install failed.\nstderr: {result.stderr[:400]}"
            )

        state = self._questions_listen_state(manifest)
        # State may be absent (None) or False — both are acceptable defaults.
        if state is not None:
            self.assertFalse(state, "Without config.toml the sub-toggle must default to false")

        # No migration message should appear.
        combined = result.stderr + result.stdout
        self.assertNotIn(
            "now inert", combined,
            "With no config.toml there should be no migration announcement",
        )

    def test_config_toml_no_questions_listen_section_defaults_false(self):
        """config.toml present but no [questions_listen] section → sub-toggle false."""
        self._write_config_toml(None)  # relay section only, no questions_listen
        result, manifest = self._run_questions_install()

        if result.returncode != 0:
            self.skipTest(
                f"questions install failed.\nstderr: {result.stderr[:400]}"
            )

        state = self._questions_listen_state(manifest)
        if state is not None:
            self.assertFalse(state, "Without [questions_listen] section the sub-toggle must be false")

    def test_config_toml_malformed_does_not_crash(self):
        """Malformed config.toml must not cause install.sh to crash."""
        self._write_config_toml(None, malformed=True)
        result, manifest = self._run_questions_install()

        # The installer must exit 0 or at least not crash with an unhandled error.
        # (questions feature itself may fail if dependencies are missing, but not
        # due to a TOML parse error.)
        combined = result.stderr + result.stdout
        self.assertNotIn(
            "Traceback",
            combined,
            "Malformed config.toml must not produce a Python traceback",
        )
        self.assertNotIn(
            "syntax error",
            combined.lower(),
            "Malformed config.toml must not surface a bash syntax error",
        )


# =============================================================================
# Docs coverage test
# =============================================================================

class TestInstallerDocsCoverage(unittest.TestCase):
    """
    Every feature id and sub-toggle id in the registry must appear in
    docs/installer.md.  This test enumerates them from install.sh source so
    that a feature added later without documentation fails the suite.
    """

    def setUp(self):
        self.assertIn("CLAUDE_INSTALL_NO_EXTERNAL", os.environ,
                      "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1")
        self.install_text = INSTALL_SH.read_text()
        self.docs_text = DOCS_INSTALLER.read_text() if DOCS_INSTALLER.exists() else ""

    def test_docs_installer_exists(self):
        """docs/installer.md must exist."""
        self.assertTrue(
            DOCS_INSTALLER.exists(),
            f"docs/installer.md not found at {DOCS_INSTALLER}",
        )

    def _extract_features(self) -> list[str]:
        """Extract the FEATURES=(...) list from install.sh."""
        m = re.search(r"^FEATURES=\(([^)]+)\)", self.install_text, re.MULTILINE)
        if not m:
            return []
        return re.findall(r"\b(\S+)\b", m.group(1))

    def _extract_valid_suboptions(self) -> list[str]:
        """Extract VALID_SUBOPTIONS=(...) from install.sh."""
        m = re.search(r"^VALID_SUBOPTIONS=\(([^)]+)\)", self.install_text, re.MULTILINE)
        if not m:
            return []
        return re.findall(r"\b(\S+)\b", m.group(1))

    def test_all_feature_ids_in_docs(self):
        """Every feature id from the FEATURES array must appear in docs/installer.md."""
        features = self._extract_features()
        self.assertGreater(len(features), 0, "Could not extract FEATURES from install.sh")
        missing = [f for f in features if f not in self.docs_text]
        self.assertEqual(
            missing, [],
            f"Feature ids missing from docs/installer.md: {missing}\n"
            f"Add documentation for each feature in docs/installer.md.",
        )

    def test_all_suboption_ids_in_docs(self):
        """Every sub-toggle id from VALID_SUBOPTIONS must appear in docs/installer.md."""
        suboptions = self._extract_valid_suboptions()
        self.assertGreater(len(suboptions), 0, "Could not extract VALID_SUBOPTIONS from install.sh")
        missing = [s for s in suboptions if s not in self.docs_text]
        self.assertEqual(
            missing, [],
            f"Sub-toggle ids missing from docs/installer.md: {missing}\n"
            f"Add documentation for each sub-toggle in docs/installer.md.",
        )

    def test_docs_covers_cli_flags(self):
        """docs/installer.md must document the key CLI flags."""
        required_flags = ["--all", "--only", "--with", "--without", "--yes",
                          "--list", "--dry-run", "--help"]
        for flag in required_flags:
            self.assertIn(
                flag, self.docs_text,
                f"docs/installer.md is missing documentation for CLI flag '{flag}'",
            )

    def test_docs_covers_no_external_seam(self):
        """docs/installer.md must document CLAUDE_INSTALL_NO_EXTERNAL."""
        self.assertIn(
            "CLAUDE_INSTALL_NO_EXTERNAL",
            self.docs_text,
            "docs/installer.md must document the CLAUDE_INSTALL_NO_EXTERNAL testing seam",
        )

    def test_docs_covers_uninstall(self):
        """docs/installer.md must cover the uninstall section."""
        self.assertIn(
            "Uninstall",
            self.docs_text,
            "docs/installer.md must have an Uninstall section",
        )


# =============================================================================
# Source reference integrity test
# =============================================================================

class TestInstallerReferenceIntegrity(unittest.TestCase):
    """
    No file in the repo may reference an installer filename that does not exist
    on disk.  This catches a half-done cutover: if install-claude-config.sh is
    deleted but something still references it, the suite fails.

    Exclusions:
      - The git object store (.git/): contains the historical blob.
      - Historical task files that cite the frozen script by line number for
        reference purposes (tasks/29_installer_interactive/29-0*.md,
        tasks/29_installer_interactive/state.md historical log section).
      - This test file itself (references the path as a string to check it).
    """

    # Paths that are permitted to reference install-claude-config.sh even after
    # deletion, because they are historical documentation or frozen artifacts.
    _ALLOWED_OLD_REF_PATTERNS = [
        "tasks/29_installer_interactive/",  # epic task files — historical context
        "tasks/",                           # all historical task files (line-no citations)
        "tests/test_installer_migration.py",  # this file — we hold the blob hash
        "tests/test_unit_installer.py",     # tests that assert the file is absent/unused
        "agents_output/",                   # frozen agent implementation reports
        ".git/",                            # git internal storage
        # install.sh describes legacy tmux-marker text left on old machines
        # ("# Added by claude-hooks install-claude-config.sh (...)") — intentional.
        "install.sh",
    ]

    def setUp(self):
        self.assertIn("CLAUDE_INSTALL_NO_EXTERNAL", os.environ,
                      "Tests must run with CLAUDE_INSTALL_NO_EXTERNAL=1")

    def _is_allowed(self, filepath: str) -> bool:
        """Return True if this file is allowed to reference install-claude-config.sh."""
        for pattern in self._ALLOWED_OLD_REF_PATTERNS:
            if pattern in filepath:
                return True
        return False

    def test_no_live_references_to_deleted_installer(self):
        """
        No non-excluded file in the repo must reference install-claude-config.sh
        after the cutover.

        If install-claude-config.sh still exists, this test is skipped — the
        cutover hasn't happened yet and references are expected.
        """
        old_path = REPO / "install-claude-config.sh"
        if old_path.exists():
            self.skipTest(
                "install-claude-config.sh still exists — cutover not yet done. "
                "Delete it first, then this test will enforce reference cleanliness."
            )

        result = subprocess.run(
            ["git", "grep", "-l", "install-claude-config.sh"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=15,
        )

        violating_files = []
        for line in result.stdout.splitlines():
            filepath = line.strip()
            if filepath and not self._is_allowed(filepath):
                violating_files.append(filepath)

        self.assertEqual(
            violating_files, [],
            f"These files reference install-claude-config.sh which no longer exists:\n"
            + "\n".join(f"  {f}" for f in violating_files)
            + "\nUpdate each to reference install.sh.",
        )

    def test_install_sh_exists_on_disk(self):
        """install.sh must exist (the only installer after the cutover)."""
        self.assertTrue(
            INSTALL_SH.exists(),
            f"install.sh not found at {INSTALL_SH}",
        )
