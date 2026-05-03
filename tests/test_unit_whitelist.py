#!/usr/bin/env python3
"""
Unit Tests: Whitelist Update Logic

Tests the whitelist update functionality:
- Pattern generation from requests
- Settings JSON update with atomic write
- Deduplication of existing rules
- Error handling for corrupted JSON
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

from telegram_permission_router import (
    generate_whitelist_pattern,
    update_settings_local_json,
)
from permission_state_store import PermissionRequest


class TestWhitelistPatternGeneration(unittest.TestCase):
    """Test whitelist pattern generation from requests."""

    def create_request(self, command, suggestions=None):
        """Helper to create a PermissionRequest."""
        return PermissionRequest(
            request_id="test-pattern",
            session_id="test-session",
            cwd="/test/path",
            tool_name="Bash",
            tool_input={"command": command},
            permission_suggestions=suggestions or [],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

    def test_pattern_with_suggestions(self):
        """Test pattern uses suggestion when available."""
        request = self.create_request(
            command="npm install",
            suggestions=["Bash(npm:*)"]
        )

        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Bash(npm:*)")

    def test_pattern_with_multiple_suggestions_uses_first(self):
        """Test pattern uses first suggestion when multiple available."""
        request = self.create_request(
            command="npm install && npm test",
            suggestions=["Bash(npm install:*)", "Bash(npm:*)"]
        )

        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Bash(npm install:*)")

    def test_pattern_generated_from_simple_command(self):
        """Test pattern generation from simple command without suggestions."""
        request = self.create_request(
            command="git status",
            suggestions=[]
        )

        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Bash(git:*)")

    def test_pattern_generated_with_sudo(self):
        """Test pattern generation strips sudo prefix."""
        request = self.create_request(
            command="sudo apt update",
            suggestions=[]
        )

        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Bash(apt:*)")

    def test_pattern_generated_with_env_vars(self):
        """Test pattern generation strips environment variables."""
        request = self.create_request(
            command="NODE_ENV=production npm run build",
            suggestions=[]
        )

        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Bash(npm:*)")

    def test_pattern_generated_with_path_command(self):
        """Test pattern generation with path-based command."""
        request = self.create_request(
            command="./scripts/build.sh",
            suggestions=[]
        )

        pattern = generate_whitelist_pattern(request)
        # Should extract the command after ./ or /
        self.assertIn("Bash(", pattern)

    def test_pattern_for_non_bash_tool(self):
        """Test pattern generation for non-Bash tools."""
        request = PermissionRequest(
            request_id="test-non-bash",
            session_id="test-session",
            cwd="/test/path",
            tool_name="Read",
            tool_input={"file_path": "/test/file.txt"},
            permission_suggestions=[],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Read")


class TestSettingsUpdate(unittest.TestCase):
    """Test settings.local.json update logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.settings_path = Path(self.temp_dir) / ".claude" / "settings.local.json"

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_new_settings_file(self):
        """Test creating new settings file when none exists."""
        success = update_settings_local_json(self.temp_dir, "Bash(ls:*)")

        self.assertTrue(success)
        self.assertTrue(self.settings_path.exists())

        with open(self.settings_path) as f:
            settings = json.load(f)

        self.assertIn("permissions", settings)
        self.assertIn("allow", settings["permissions"])
        self.assertIn("Bash(ls:*)", settings["permissions"]["allow"])

    def test_add_to_existing_settings(self):
        """Test adding pattern to existing settings."""
        # Create initial settings
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        initial_settings = {
            "permissions": {
                "allow": ["Bash(git:*)"]
            }
        }
        with open(self.settings_path, 'w') as f:
            json.dump(initial_settings, f)

        # Add new pattern
        success = update_settings_local_json(self.temp_dir, "Bash(ls:*)")

        self.assertTrue(success)

        with open(self.settings_path) as f:
            settings = json.load(f)

        self.assertIn("Bash(git:*)", settings["permissions"]["allow"])
        self.assertIn("Bash(ls:*)", settings["permissions"]["allow"])

    def test_deduplication(self):
        """Test that duplicate patterns are not added."""
        # Create initial settings with pattern
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        initial_settings = {
            "permissions": {
                "allow": ["Bash(ls:*)"]
            }
        }
        with open(self.settings_path, 'w') as f:
            json.dump(initial_settings, f)

        # Try to add same pattern
        success = update_settings_local_json(self.temp_dir, "Bash(ls:*)")

        self.assertTrue(success)

        with open(self.settings_path) as f:
            settings = json.load(f)

        # Should only appear once
        count = settings["permissions"]["allow"].count("Bash(ls:*)")
        self.assertEqual(count, 1)

    def test_preserve_other_settings(self):
        """Test that other settings are preserved."""
        # Create settings with other fields
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        initial_settings = {
            "description": "Test settings",
            "permissions": {
                "allow": ["Read"],
                "deny": ["Write"]
            },
            "custom_field": "preserved"
        }
        with open(self.settings_path, 'w') as f:
            json.dump(initial_settings, f)

        # Add new pattern
        update_settings_local_json(self.temp_dir, "Bash(ls:*)")

        with open(self.settings_path) as f:
            settings = json.load(f)

        # All other fields should be preserved
        self.assertEqual(settings["description"], "Test settings")
        self.assertEqual(settings["custom_field"], "preserved")
        self.assertIn("deny", settings["permissions"])
        self.assertEqual(settings["permissions"]["deny"], ["Write"])

    def test_handle_corrupted_json(self):
        """Test handling of corrupted JSON file."""
        # Create corrupted JSON file
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.settings_path, 'w') as f:
            f.write("{ invalid json }")

        # Should handle gracefully and create valid file
        success = update_settings_local_json(self.temp_dir, "Bash(ls:*)")

        self.assertTrue(success)

        # Should create valid JSON
        with open(self.settings_path) as f:
            settings = json.load(f)  # Should not raise

        self.assertIn("Bash(ls:*)", settings["permissions"]["allow"])

    def test_atomic_write(self):
        """Test that writes are atomic (temp file then rename)."""
        success = update_settings_local_json(self.temp_dir, "Bash(test:*)")

        self.assertTrue(success)

        # Temp file should not exist after write
        temp_path = self.settings_path.with_suffix('.json.tmp')
        self.assertFalse(temp_path.exists())

        # Main file should exist and be valid
        self.assertTrue(self.settings_path.exists())
        with open(self.settings_path) as f:
            json.load(f)  # Should not raise


class TestWhitelistIntegration(unittest.TestCase):
    """Integration tests for whitelist functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.settings_path = Path(self.temp_dir) / ".claude" / "settings.local.json"

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_whitelist_flow(self):
        """Test full whitelist flow from request to settings update."""
        # Create request
        request = PermissionRequest(
            request_id="test-full-flow",
            session_id="test-session",
            cwd=self.temp_dir,
            tool_name="Bash",
            tool_input={"command": "custom_cmd --flag"},
            permission_suggestions=["Bash(custom_cmd:*)"],
            state="pending",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            expires_at="2024-01-01T00:05:00Z",
        )

        # Generate pattern
        pattern = generate_whitelist_pattern(request)
        self.assertEqual(pattern, "Bash(custom_cmd:*)")

        # Update settings
        success = update_settings_local_json(self.temp_dir, pattern)
        self.assertTrue(success)

        # Verify
        with open(self.settings_path) as f:
            settings = json.load(f)

        self.assertIn("Bash(custom_cmd:*)", settings["permissions"]["allow"])

    def test_multiple_whitelist_updates(self):
        """Test multiple whitelist updates accumulate correctly."""
        patterns = ["Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)"]

        for pattern in patterns:
            success = update_settings_local_json(self.temp_dir, pattern)
            self.assertTrue(success)

        with open(self.settings_path) as f:
            settings = json.load(f)

        allow_list = settings["permissions"]["allow"]
        for pattern in patterns:
            self.assertIn(pattern, allow_list)

        # Verify no duplicates
        self.assertEqual(len(allow_list), len(patterns))


if __name__ == "__main__":
    unittest.main(verbosity=2)
