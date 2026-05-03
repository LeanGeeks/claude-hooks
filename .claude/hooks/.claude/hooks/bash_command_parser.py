#!/usr/bin/env python3
"""
Bash Command Parser for Claude Code PreToolUse Hook

Parses compound bash commands, splitting on operators while respecting:
- Quotes (don't split on operators inside quotes)
- Escapes (handle backslash escaping)
- Environment variables (strip leading KEY=VALUE assignments)
- Redirections (ignore >, >>, 2>&1, etc.)
"""

import re
from typing import List, Tuple


class BashCommandParser:
    """Parse and split compound bash commands"""

    # Operators that separate commands
    OPERATORS = ['&&', '||', '|', ';', '\n']

    # Redirection operators (NOT command separators)
    REDIRECTIONS = ['>', '>>', '<', '<<', '2>&1', '2>', '&>', '&>>', '1>&2', '2>>', '1>', '<&', '>&']

    # Redirections that take an argument (file/fd)
    REDIRECTIONS_WITH_ARG = ['>', '>>', '<', '<<', '2>', '&>', '&>>', '2>>', '1>']

    # Redirections that don't take an argument
    REDIRECTIONS_NO_ARG = ['2>&1', '1>&2', '<&', '>&']

    def __init__(self):
        """Initialize parser"""
        pass

    def parse_compound_command(self, command: str) -> List[str]:
        """
        Split compound command into individual sub-commands

        Args:
            command: Full bash command (may contain pipes, &&, etc.)

        Returns:
            List of normalized sub-commands

        Examples:
            "git status" → ["git status"]
            "git diff | head -100" → ["git diff", "head -100"]
            "GIT_PAGER=cat git diff" → ["git diff"]
            "npm install && npm test" → ["npm install", "npm test"]
        """
        if not command or not command.strip():
            return []

        # Tokenize the command
        tokens = self._tokenize_with_quotes(command)

        # Split on operators
        command_groups = self._split_on_operators(tokens)

        # Normalize each group (strip env vars, clean whitespace)
        result = []
        for group in command_groups:
            normalized = self._normalize_command(group)
            if normalized:
                result.append(normalized)

        return result

    def _tokenize_with_quotes(self, command: str) -> List[Tuple[str, str]]:
        """
        Tokenize command into (type, value) pairs

        Token types:
        - ENV: Environment variable assignment (KEY=VALUE)
        - WORD: Regular word/argument
        - OP: Operator (|, &&, ||, ;)
        - REDIRECT: Redirection operator
        - QUOTED: Quoted string

        Args:
            command: Command string

        Returns:
            List of (type, value) tuples
        """
        tokens = []
        current = []
        in_quote = None  # None, "'", or '"'
        escaped = False
        i = 0

        def flush_current():
            """Flush current token buffer"""
            if current:
                token_str = ''.join(current)
                tokens.append(self._classify_token(token_str))
                current.clear()

        while i < len(command):
            char = command[i]

            # Handle escape
            if escaped:
                current.append(char)
                escaped = False
                i += 1
                continue

            if char == '\\' and in_quote != "'":  # Backslash doesn't escape in single quotes
                escaped = True
                current.append(char)
                i += 1
                continue

            # Handle quotes
            if char in ('"', "'"):
                if in_quote == char:
                    # Closing quote
                    current.append(char)
                    in_quote = None
                    i += 1
                    continue
                elif in_quote is None:
                    # Opening quote
                    in_quote = char
                    current.append(char)
                    i += 1
                    continue
                else:
                    # Different quote inside quoted string
                    current.append(char)
                    i += 1
                    continue

            # If in quote, add everything literally
            if in_quote:
                current.append(char)
                i += 1
                continue

            # Check for operators (only when not quoted)
            op = self._check_operator(command, i)
            if op:
                flush_current()
                # Classify operator as OP or REDIRECT
                op_type = 'REDIRECT' if self._is_redirect(op) else 'OP'
                tokens.append((op_type, op))
                i += len(op)
                continue

            # Whitespace separates tokens
            if char in (' ', '\t'):
                flush_current()
                i += 1
                continue

            # Regular character
            current.append(char)
            i += 1

        # Flush final token
        flush_current()

        return tokens

    def _check_operator(self, command: str, pos: int) -> str:
        """
        Check if position starts with an operator

        Args:
            command: Full command string
            pos: Current position

        Returns:
            Operator string if found, empty string otherwise
        """
        # Check multi-character operators first (longest match)
        for op in ['&&', '||', '2>&1', '>>', '&>>', '2>>', '<<', '1>&2', '>&', '<&', '1>']:
            if command[pos:pos+len(op)] == op:
                return op

        # Check single-character operators
        if command[pos] in ('|', ';', '>', '<', '\n'):
            return command[pos]

        return ''

    def _is_redirect(self, op: str) -> bool:
        """Check if operator is a redirection"""
        return op in self.REDIRECTIONS

    def _classify_token(self, token: str) -> Tuple[str, str]:
        """
        Classify token as ENV or WORD

        Args:
            token: Token string

        Returns:
            (type, value) tuple
        """
        # Check if it's an environment variable (KEY=VALUE format)
        if '=' in token and not token.startswith('-'):
            parts = token.split('=', 1)
            key = parts[0]
            # Valid env var: starts with letter or underscore, followed by alnum/underscore
            if key and (key[0].isalpha() or key[0] == '_'):
                if all(c.isalnum() or c == '_' for c in key):
                    return ('ENV', token)

        return ('WORD', token)

    def _split_on_operators(self, tokens: List[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
        """
        Split token list on operator boundaries

        Args:
            tokens: List of (type, value) tuples

        Returns:
            List of token groups (one per sub-command)
        """
        groups = []
        current_group = []
        skip_next = False  # Skip next token (redirect argument)

        for token_type, token_value in tokens:
            if skip_next:
                # Skip this token (it's the argument to a redirect)
                skip_next = False
                continue

            if token_type == 'OP':
                # Operator splits commands
                if current_group:
                    groups.append(current_group)
                    current_group = []
            elif token_type == 'REDIRECT':
                # Redirections are stripped
                # Only skip next token if this redirect takes an argument
                if token_value in self.REDIRECTIONS_WITH_ARG:
                    skip_next = True
            else:
                # Regular token or ENV var
                current_group.append((token_type, token_value))

        # Flush final group
        if current_group:
            groups.append(current_group)

        return groups

    def _normalize_command(self, tokens: List[Tuple[str, str]]) -> str:
        """
        Convert tokens back to normalized command string

        - Strips environment variables
        - Joins remaining tokens with spaces
        - Normalizes whitespace

        Args:
            tokens: List of (type, value) tuples for a single command

        Returns:
            Normalized command string
        """
        # Strip leading environment variables
        words = []
        skip_env = True  # Skip env vars at the beginning

        for token_type, token_value in tokens:
            if skip_env and token_type == 'ENV':
                # Skip leading environment variables
                continue
            else:
                # Once we hit a non-ENV token, stop skipping
                skip_env = False
                if token_type in ('WORD', 'QUOTED'):
                    words.append(token_value)

        # Join and normalize whitespace
        result = ' '.join(words)
        return result.strip()


# For testing
if __name__ == '__main__':
    import sys

    parser = BashCommandParser()

    # Test cases
    test_cases = [
        ("git status", ["git status"]),
        ("git diff | head -100", ["git diff", "head -100"]),
        ("GIT_PAGER=cat git diff", ["git diff"]),
        ("A=1 B=2 ./script.sh", ["./script.sh"]),
        ("npm install && npm test", ["npm install", "npm test"]),
        ("git diff > out.txt 2>&1", ["git diff"]),
        ('echo "foo | bar"', ['echo "foo | bar"']),
        ("cmd1 || cmd2", ["cmd1", "cmd2"]),
        ("ls -la | grep foo | wc -l", ["ls -la", "grep foo", "wc -l"]),
        ("PATH=\"\" ./script.sh", ["./script.sh"]),
        ("git diff activecdn-module/handler.go 2>&1 | head -100", ["git diff activecdn-module/handler.go", "head -100"]),
        ("GIT_PAGER=cat git diff activecdn-module/handler.go 2>&1 | head -100", ["git diff activecdn-module/handler.go", "head -100"]),
    ]

    print("=== Bash Command Parser Tests ===\n")

    passed = 0
    failed = 0

    for command, expected in test_cases:
        result = parser.parse_compound_command(command)
        success = result == expected

        if success:
            passed += 1
            status = "✓ PASS"
        else:
            failed += 1
            status = "✗ FAIL"

        print(f"{status}: {command!r}")
        print(f"  Expected: {expected}")
        print(f"  Got:      {result}")
        print()

    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
