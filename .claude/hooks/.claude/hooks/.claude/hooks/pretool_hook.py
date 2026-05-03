#!/usr/bin/env python3
"""
PreToolUse Hook: Compound Bash Command Validator

Validates compound bash commands (with |, &&, ||, ;) by:
1. Splitting into sub-commands
2. Checking each against allowed/denied patterns
3. Allowing if ALL sub-commands are allowed
"""

import json
import sys
import os
from datetime import datetime
from typing import Dict, List, Any

# Import our modules
try:
    from bash_command_parser import BashCommandParser
    from settings_loader import SettingsLoader
except ImportError:
    # If running from different directory, try adding hooks dir to path
    import pathlib
    hooks_dir = pathlib.Path(__file__).parent.absolute()
    sys.path.insert(0, str(hooks_dir))
    from bash_command_parser import BashCommandParser
    from settings_loader import SettingsLoader


# Debug logging
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = os.path.expanduser('~/.claude/bash_hook_debug.log')


def debug_log(message: str):
    """Log debug message if debug mode is enabled"""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now().isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            print(f"Debug log error: {e}", file=sys.stderr)


class BashPermissionValidator:
    """Validate bash commands against Claude settings"""

    def __init__(self, settings_loader: SettingsLoader, command_parser: BashCommandParser):
        """
        Initialize validator

        Args:
            settings_loader: Settings loader instance
            command_parser: Command parser instance
        """
        self.parser = command_parser
        self.settings = settings_loader.load_all_settings()
        self.allowed_patterns = self.settings.get('permissions', {}).get('allow', [])
        self.denied_patterns = self.settings.get('permissions', {}).get('deny', [])

        debug_log(f"Loaded {len(self.allowed_patterns)} allow patterns, {len(self.denied_patterns)} deny patterns")

    def validate_bash_command(self, command: str) -> Dict[str, Any]:
        """
        Validate compound command

        Args:
            command: Full bash command to validate

        Returns:
            Dictionary with:
            - decision: 'allow' | 'deny' | 'defer'
            - reason: Explanation string
            - sub_commands: List of parsed sub-commands
            - validation_results: List of validation results for each sub-command
        """
        debug_log(f"Validating command: {command!r}")

        # Parse compound command
        sub_commands = self.parser.parse_compound_command(command)
        debug_log(f"Parsed into {len(sub_commands)} sub-commands: {sub_commands}")

        # Validate each sub-command
        results = []
        for cmd in sub_commands:
            result = self._check_single_command(cmd)
            results.append(result)
            debug_log(f"  Sub-command {cmd!r}: allowed={result['allowed']}, denied={result['denied']}")

        # Make decision
        any_denied = any(r['denied'] for r in results)
        all_allowed = all(r['allowed'] for r in results)

        if any_denied:
            # ANY denied → defer (let normal system block it)
            decision = 'defer'
            reason = f"Contains denied command: {[r['command'] for r in results if r['denied']]}"
        elif all_allowed and len(sub_commands) > 0:
            # ALL allowed → explicitly allow
            decision = 'allow'
            reason = "All sub-commands are allowed"
        else:
            # Some unknown or empty → defer
            decision = 'defer'
            reason = "Contains unknown or empty commands"

        debug_log(f"Decision: {decision} - {reason}")

        return {
            'decision': decision,
            'reason': reason,
            'sub_commands': sub_commands,
            'validation_results': results
        }

    def _check_single_command(self, cmd: str) -> Dict[str, Any]:
        """
        Check if single command matches any pattern

        Args:
            cmd: Normalized command string

        Returns:
            Dictionary with:
            - command: The command checked
            - allowed: Boolean - matches an allow pattern
            - denied: Boolean - matches a deny pattern
            - matched_patterns: List of patterns that matched
        """
        matched_allow = []
        matched_deny = []

        # Check deny patterns first (deny takes precedence)
        for pattern in self.denied_patterns:
            if self._matches_pattern(cmd, pattern):
                matched_deny.append(pattern)

        # Check allow patterns
        for pattern in self.allowed_patterns:
            if self._matches_pattern(cmd, pattern):
                matched_allow.append(pattern)

        return {
            'command': cmd,
            'allowed': len(matched_allow) > 0,
            'denied': len(matched_deny) > 0,
            'matched_allow_patterns': matched_allow,
            'matched_deny_patterns': matched_deny
        }

    def _matches_pattern(self, command: str, pattern: str) -> bool:
        """
        Check if command matches a Bash(...) pattern

        Args:
            command: Command string (e.g. "git diff file.txt")
            pattern: Pattern from settings (e.g. "Bash(git diff:*)")

        Returns:
            True if matches, False otherwise

        Examples:
            command='git diff file.txt', pattern='Bash(git diff:*)' → True
            command='git status', pattern='Bash(git diff:*)' → False
            command='pwd', pattern='Bash(pwd)' → True
        """
        # Only match Bash patterns
        if not pattern.startswith('Bash('):
            return False

        # Extract inner pattern from Bash(...)
        if not pattern.endswith(')'):
            return False

        inner = pattern[5:-1]  # Remove 'Bash(' and ')'

        # Check for wildcard suffix
        if inner.endswith(':*'):
            prefix = inner[:-2]
            matches = command.startswith(prefix)
            debug_log(f"    Pattern {pattern!r}: prefix match {prefix!r} → {matches}")
            return matches
        else:
            # Exact match
            matches = command == inner
            debug_log(f"    Pattern {pattern!r}: exact match → {matches}")
            return matches


def main():
    """Main hook entry point"""
    try:
        # Read hook input from stdin
        raw_input = sys.stdin.read()
        debug_log(f"=== Hook called ===")
        debug_log(f"Raw input: {raw_input[:500]}...")  # First 500 chars

        input_data = json.loads(raw_input)
        debug_log(f"Parsed input: {json.dumps(input_data, indent=2)}")

        # Extract tool info
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})

        # Only process Bash tool
        if tool_name != 'Bash':
            debug_log(f"Not a Bash tool (got {tool_name!r}), allowing")
            sys.exit(0)

        # Get command
        command = tool_input.get('command', '')
        if not command:
            debug_log("No command found, allowing")
            sys.exit(0)

        # Get workspace directory
        workspace_dir = os.environ.get('CLAUDE_WORKSPACE_DIR', os.getcwd())
        debug_log(f"Workspace: {workspace_dir}")

        # Initialize components
        settings_loader = SettingsLoader(workspace_dir)
        parser = BashCommandParser()
        validator = BashPermissionValidator(settings_loader, parser)

        # Validate command
        result = validator.validate_bash_command(command)

        debug_log(f"Validation result: {json.dumps(result, indent=2)}")

        # Make decision
        if result['decision'] == 'allow':
            # Explicitly allow - bypass normal permission system
            output = {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'allow'
                }
            }
            print(json.dumps(output))
            debug_log(f"ALLOWING command (bypassing normal permissions)")
            sys.exit(0)
        elif result['decision'] == 'deny':
            # Explicitly deny (though we usually defer to normal system)
            output = {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'deny',
                    'permissionDecisionReason': result['reason']
                }
            }
            print(json.dumps(output))
            debug_log(f"DENYING command: {result['reason']}")
            sys.exit(0)
        else:
            # Defer to normal permission system
            debug_log(f"DEFERRING to normal permission system: {result['reason']}")
            sys.exit(0)

    except Exception as e:
        # On error, allow (fail open to avoid breaking things)
        debug_log(f"ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        debug_log(f"Traceback:\n{traceback.format_exc()}")
        sys.exit(0)


if __name__ == '__main__':
    main()
