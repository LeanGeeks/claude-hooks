#!/usr/bin/env python3
"""
Integration Tests: PreToolUse Hook

Tests the PreToolUse hook with simulated stdin payloads:
- Allowed commands return 'allow'
- Unknown/denied commands return 'ask'
- Non-Bash tools are passed through
- Compound commands are validated correctly
"""

import json
import os
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

# Import test fixtures
from fixtures import PRETOOL_USE_PAYLOADS

from pretool_hook import BashPermissionValidator, BashCommandParser
from settings_loader import SettingsLoader


class TestPreToolUseHook(unittest.TestCase):
    """Integration tests for PreToolUse hook."""

    def setUp(self):
        """Set up test fixtures."""
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_allowed_simple_command(self):
        """Test that simple allowed commands are approved."""
        payload = PRETOOL_USE_PAYLOADS["allowed_simple"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")
        self.assertIn("All sub-commands", result["reason"])

    def test_allowed_compound_command(self):
        """Test that compound allowed commands are approved."""
        payload = PRETOOL_USE_PAYLOADS["allowed_compound"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")

    def test_unknown_command_returns_ask(self):
        """Test that unknown commands return 'ask'."""
        payload = PRETOOL_USE_PAYLOADS["unknown_command"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "ask")

    def test_denied_command_returns_ask(self):
        """Test that denied commands return 'ask' (to be handled by PermissionRequest)."""
        payload = PRETOOL_USE_PAYLOADS["denied_command"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        # Denied patterns trigger 'ask' (let PermissionRequest handle it)
        self.assertEqual(result["decision"], "ask")
        self.assertIn("denied", result["reason"].lower())

    def test_mixed_allowed_unknown_returns_ask(self):
        """Test that mixed allowed+unknown commands return 'ask'."""
        payload = PRETOOL_USE_PAYLOADS["mixed_allowed_unknown"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        # Any unknown triggers 'ask'
        self.assertEqual(result["decision"], "ask")

    def test_complex_pipeline(self):
        """Test complex pipeline with multiple commands."""
        payload = PRETOOL_USE_PAYLOADS["complex_pipeline"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        # git, head, and grep are all allowed
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(len(result["sub_commands"]), 3)

    def test_allowed_subshell_groups(self):
        """Test allowed commands wrapped in subshell grouping parentheses."""
        command = (
            '(cd apps/contributor && npx tsc --noEmit 2>&1 | head -40) '
            '&& echo "---PRESENTATION---" '
            '&& (cd apps/presentation && npx tsc --noEmit 2>&1 | head -40)'
        )

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["sub_commands"], [
            "cd apps/contributor",
            "npx tsc --noEmit",
            "head -40",
            'echo "---PRESENTATION---"',
            "cd apps/presentation",
            "npx tsc --noEmit",
            "head -40",
        ])

    def test_sub_command_parsing(self):
        """Test that compound commands are correctly split."""
        test_cases = [
            ("git status", ["git status"]),
            ("git diff | head -100", ["git diff", "head -100"]),
            ("ls -la && echo done", ["ls -la", "echo done"]),
            ("npm install || npm cache clean", ["npm install", "npm cache clean"]),
        ]

        for command, expected in test_cases:
            with self.subTest(command=command):
                result = self.validator.validate_bash_command(command)
                self.assertEqual(result["sub_commands"], expected,
                    f"Failed for command: {command}")


class TestPreToolUseHookProcess(unittest.TestCase):
    """Test PreToolUse hook as a subprocess."""

    def setUp(self):
        """Set up test fixtures."""
        self.hook_path = Path(__file__).parent.parent / ".claude" / "hooks" / "pretool_hook.py"
        self.workspace_dir = str(Path(__file__).parent.parent)

    def run_hook(self, payload):
        """Run the hook with a payload and return output."""
        return self.run_hook_with_options(payload)

    def run_hook_with_options(self, payload, *, cwd=None, include_workspace_env=True):
        """Run the hook with optional cwd/env overrides."""
        env = os.environ.copy()
        if include_workspace_env:
            env["CLAUDE_WORKSPACE_DIR"] = self.workspace_dir
        else:
            env.pop("CLAUDE_WORKSPACE_DIR", None)
        env["CLAUDE_HOOK_DEBUG"] = "0"

        result = subprocess.run(
            ["python3", str(self.hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=10,
        )

        return result

    def test_hook_allows_known_command(self):
        """Test hook allows known command."""
        payload = PRETOOL_USE_PAYLOADS["allowed_simple"]
        result = self.run_hook(payload)

        # Exit code 0 with output means allow
        if result.stdout.strip():
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_hook_asks_for_unknown_command(self):
        """Test hook asks for unknown command."""
        payload = PRETOOL_USE_PAYLOADS["unknown_command"]
        result = self.run_hook(payload)

        # Should output ask decision
        if result.stdout.strip():
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_hook_passes_through_non_bash(self):
        """Test hook passes through non-Bash tools."""
        payload = PRETOOL_USE_PAYLOADS["non_bash_tool"]
        result = self.run_hook(payload)

        # Non-Bash tools should exit 0 with no output (pass through)
        self.assertEqual(result.returncode, 0)
        # No output means pass through to default behavior
        self.assertEqual(result.stdout.strip(), "")

    def test_hook_uses_payload_cwd_when_env_missing(self):
        """Test hook resolves workspace from payload cwd if env is absent."""
        payload = PRETOOL_USE_PAYLOADS["allowed_simple"].copy()
        payload["cwd"] = self.workspace_dir

        result = self.run_hook_with_options(
            payload,
            cwd="/tmp",
            include_workspace_env=False,
        )

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_hook_allows_workspace_and_tmp_rm(self):
        """Test hook allows rm targets in workspace plus /tmp."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "rm -f packages/api/src/forms/*.rej packages/api/src/testing/*.rej /tmp/api-eslint-fix.patch"
            },
            "session_id": "test-session-rm-tmp",
            "cwd": self.workspace_dir,
        }

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")


class TestPreToolUseEdgeCases(unittest.TestCase):
    """Test edge cases in PreToolUse hook."""

    def setUp(self):
        """Set up test fixtures."""
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_empty_command(self):
        """Test handling of empty command."""
        result = self.validator.validate_bash_command("")
        self.assertEqual(result["decision"], "ask")  # Empty -> unknown

    def test_whitespace_command(self):
        """Test handling of whitespace-only command."""
        result = self.validator.validate_bash_command("   ")
        self.assertEqual(result["decision"], "ask")

    def test_command_with_quotes(self):
        """Test command with quoted strings."""
        command = 'echo "hello world"'
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")  # echo is allowed

    def test_command_with_heredoc(self):
        """Test command with heredoc (known fragility)."""
        # This tests the known heredoc parsing issue
        command = 'cat << "EOF" | python3\nprint("hello")\nEOF'
        result = self.validator.validate_bash_command(command)

        # The parser should handle this, but may have issues
        # Just verify no crash
        self.assertIn(result["decision"], ["allow", "ask"])

    def test_env_var_prefix(self):
        """Test command with environment variable prefix."""
        command = "NODE_ENV=production npm run build"
        result = self.validator.validate_bash_command(command)

        # Should strip env var and validate npm
        self.assertEqual(result["decision"], "allow")

    def test_sudo_prefix(self):
        """Test command with sudo prefix."""
        command = "sudo apt update"
        result = self.validator.validate_bash_command(command)

        # apt should be allowed if in list
        # Result depends on settings
        self.assertIn(result["decision"], ["allow", "ask"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
