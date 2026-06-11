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
    # Note: an unquoted newline IS a command separator (treated like ';').
    # Escaped/continuation newlines, quoted newlines, heredoc bodies, and
    # command-substitution newlines are handled specially and do NOT split.
    OPERATORS = ['&&', '||', '|', ';']

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
        return [cmd for cmd, _offset in self.parse_with_offsets(command)]

    def parse_with_offsets(self, command: str) -> List[Tuple[str, int]]:
        """
        Like parse_compound_command, but pairs each sub-command with the source
        offset at which its execution anchors. Callers that reason about
        execution order (e.g. "is this function defined before it is called?")
        need this; the bare string list discards it.

        The anchor offset is:
        - for a top-level sub-command: the source offset of its first token;
        - for a command extracted from a $()/`...` substitution: the source
          offset of the enclosing substitution. All commands inside a
          substitution share that anchor because they execute together, when
          the enclosing statement runs.

        Note the returned ORDER is top-level sub-commands first (in source
        order), then substitution-extracted commands appended — the same shape
        parse_compound_command has always produced. The offsets are what convey
        true source order; do not infer it from list position.
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
                result.append((normalized, group[0][2]))

        # Also extract commands from command substitutions recursively. Each
        # extracted command anchors at the substitution's own source offset, so
        # a function defined AFTER the substitution cannot appear to precede a
        # call made INSIDE it.
        for token_type, token_value, token_offset in tokens:
            if token_type == 'CMD_SUBST' and token_value.strip():
                # Recursively parse the content of command substitutions
                for sub_cmd, _rel_offset in self.parse_with_offsets(token_value):
                    result.append((sub_cmd, token_offset))

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
        - HEREDOC_CONTENT: Content inside a heredoc (not parsed as commands)
        - CMD_SUBST: Command substitution $(...) or `...`

        Args:
            command: Command string

        Returns:
            List of (type, value) tuples
        """
        tokens = []
        current = []
        current_start = 0  # Source offset where the current token buffer began
        in_quote = None  # None, "'", or '"'
        escaped = False
        i = 0

        # Heredoc handling
        heredoc_delimiter = None  # The delimiter we're looking for
        heredoc_seen_first_nl = False  # Track if we've passed first newline after <<

        # Command substitution handling
        cmd_subst_depth = 0  # Paren depth inside $(...)
        in_backtick = False  # Inside `...`

        def flush_current():
            """Flush current token buffer"""
            if current:
                token_str = ''.join(current)
                tokens.append(self._classify_token(token_str, current_start))
                current.clear()

        while i < len(command):
            # While the buffer is empty, keep the start offset pinned to the
            # current position; once we append a char it freezes until the next
            # flush, marking where this token began in the source.
            if not current:
                current_start = i
            char = command[i]

            # Handle heredoc mode (looking for delimiter)
            # Only activate heredoc mode after we've seen the first newline
            if heredoc_delimiter is not None and heredoc_seen_first_nl:
                # We're inside heredoc content, look for delimiter at start of line
                if char == '\n':
                    # End of line - check if next line starts with delimiter
                    i += 1
                    # Check if the delimiter appears at start of next line
                    if command[i:i+len(heredoc_delimiter)] == heredoc_delimiter:
                        # Found the delimiter! Consume it, but leave the newline
                        # that follows so the newline handler can emit a command
                        # separator — otherwise a command after the heredoc (e.g.
                        # `cat <<EOF\n...\nEOF\necho done`) would merge into the
                        # heredoc command.
                        i += len(heredoc_delimiter)
                        heredoc_delimiter = None
                        continue
                    # Not the delimiter, continue in heredoc mode
                    continue
                # Skip all heredoc content (don't tokenize it)
                i += 1
                continue

            # Handle escape (outside of single quotes)
            if escaped:
                current.append(char)
                escaped = False
                i += 1
                continue

            if char == '\\' and in_quote != "'":  # Backslash doesn't escape in single quotes
                # Check for line continuation (backslash followed by newline)
                if i + 1 < len(command) and command[i+1] == '\n':
                    # Line continuation - skip both the backslash and newline
                    i += 2
                    continue
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

            # Handle command substitution $(
            if command[i:i+2] == '$(' and cmd_subst_depth == 0:
                current_str = ''.join(current)
                env_prefix = self._is_env_prefix(current_str)

                if not env_prefix:
                    flush_current()

                # Start capturing command substitution
                cmd_subst_depth = 1
                subst_start = i
                i += 2
                # Scan until matching closing paren
                while i < len(command) and cmd_subst_depth > 0:
                    c = command[i]
                    if c == '\\' and i + 1 < len(command):
                        # Skip escaped char
                        i += 2
                        continue
                    if c == '"':
                        # Toggle double quote (simple handling)
                        pass  # We're not tracking quotes inside subst for simplicity
                    if c == '(':
                        cmd_subst_depth += 1
                    elif c == ')':
                        cmd_subst_depth -= 1
                    i += 1
                # Extract the command substitution content (excluding $( and ))
                subst_content = command[subst_start+2:i-1]  # Skip $( and )
                tokens.append(('CMD_SUBST', subst_content, subst_start))

                if env_prefix:
                    # Part of env var value — keep in current token
                    current.append('$')
                    current.append('(')
                    current.extend(subst_content)
                    current.append(')')

                continue

            # Handle backtick command substitution
            if char == '`' and not in_backtick and cmd_subst_depth == 0:
                current_str = ''.join(current)
                env_prefix = self._is_env_prefix(current_str)

                if not env_prefix:
                    flush_current()

                in_backtick = True
                subst_start = i
                i += 1
                # Scan until closing backtick
                while i < len(command):
                    c = command[i]
                    if c == '\\' and i + 1 < len(command):
                        # Skip escaped char
                        i += 2
                        continue
                    if c == '`':
                        in_backtick = False
                        i += 1
                        break
                    i += 1
                # Extract content (excluding backticks)
                subst_content = command[subst_start+1:i-1]
                tokens.append(('CMD_SUBST', subst_content, subst_start))

                if env_prefix:
                    # Part of env var value — keep in current token
                    current.append('`')
                    current.extend(subst_content)
                    current.append('`')

                continue

            # If inside command substitution, treat most chars literally
            if cmd_subst_depth > 0 or in_backtick:
                current.append(char)
                i += 1
                continue

            # Check for heredoc operator (<<)
            if command[i:i+2] == '<<':
                flush_current()
                # Check if it's quoted or unquoted heredoc
                j = i + 2
                # Skip whitespace after <<
                while j < len(command) and command[j] in (' ', '\t'):
                    j += 1

                # Check for quoted delimiter
                quote_char = None
                if j < len(command) and command[j] in ('"', "'"):
                    quote_char = command[j]
                    j += 1

                # Extract delimiter (alphanumerics and underscore)
                delim_start = j
                while j < len(command) and (command[j].isalnum() or command[j] == '_'):
                    j += 1
                delimiter = command[delim_start:j]

                # Skip closing quote if we had one
                if quote_char is not None and j < len(command) and command[j] == quote_char:
                    j += 1

                if delimiter:
                    heredoc_delimiter = delimiter
                    heredoc_seen_first_nl = False  # Will look for delimiter after first newline
                    tokens.append(('REDIRECT', '<<', i))
                    # Skip to after the delimiter (and closing quote if any)
                    i = j
                    continue

            # Check for operators (only when not quoted)
            op = self._check_operator(command, i)
            if op:
                flush_current()
                # Classify operator as OP or REDIRECT
                op_type = 'REDIRECT' if self._is_redirect(op) else 'OP'
                tokens.append((op_type, op, i))
                i += len(op)
                continue

            # Handle comments - skip from # to end of line (when not in quotes)
            if char == '#' and in_quote is None:
                # Skip everything until end of line
                while i < len(command) and command[i] != '\n':
                    i += 1
                # Don't skip the newline itself - let the newline handler process it
                continue

            # Whitespace separates tokens (including newlines)
            if char in (' ', '\t'):
                flush_current()
                i += 1
                continue

            # Newline handling
            if char == '\n':
                flush_current()
                # The newline that immediately follows a heredoc operator begins
                # the heredoc body — it transitions us into content mode and is
                # NOT a command separator.
                if heredoc_delimiter is not None and not heredoc_seen_first_nl:
                    heredoc_seen_first_nl = True
                else:
                    # An unquoted newline separates commands, just like ';'.
                    # (Quoted, escaped/continuation, heredoc, and command-subst
                    # newlines are handled before reaching this point.)
                    tokens.append(('OP', ';', i))
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

        # Check single-character operators (not newline - newlines are handled separately)
        if command[pos] in ('|', ';', '>', '<'):
            return command[pos]

        return ''

    def _is_redirect(self, op: str) -> bool:
        """Check if operator is a redirection"""
        return op in self.REDIRECTIONS

    @staticmethod
    def _is_env_prefix(token: str) -> bool:
        """Check if token is an env var assignment prefix (e.g., 'KEY=' or 'KEY=partial')"""
        if not token or '=' not in token or token.startswith('-'):
            return False
        key = token.split('=', 1)[0]
        if not key or not (key[0].isalpha() or key[0] == '_'):
            return False
        return all(c.isalnum() or c == '_' for c in key)

    def _classify_token(self, token: str, offset: int = 0) -> Tuple[str, str, int]:
        """
        Classify token as ENV or WORD

        Args:
            token: Token string
            offset: Source offset where the token began

        Returns:
            (type, value, offset) tuple
        """
        # Check if it's an environment variable (KEY=VALUE format)
        if '=' in token and not token.startswith('-'):
            parts = token.split('=', 1)
            key = parts[0]
            # Valid env var: starts with letter or underscore, followed by alnum/underscore
            if key and (key[0].isalpha() or key[0] == '_'):
                if all(c.isalnum() or c == '_' for c in key):
                    return ('ENV', token, offset)

        return ('WORD', token, offset)

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

        for token_type, token_value, token_offset in tokens:
            if skip_next:
                # Don't skip operator tokens - they should always split commands
                # (This handles the case where heredoc delimiter is already consumed)
                if token_type != 'OP':
                    # Skip this token (it's the argument to a redirect)
                    skip_next = False
                    continue
                else:
                    # Don't skip the operator, but clear the skip flag
                    skip_next = False

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
                current_group.append((token_type, token_value, token_offset))

        # Flush final group
        if current_group:
            groups.append(current_group)

        return groups

    def _normalize_command(self, tokens: List[Tuple[str, str]]) -> str:
        """
        Convert tokens back to normalized command string

        - Strips environment variables
        - Strips grouping parentheses left behind by subshell syntax
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

        for token_type, token_value, _token_offset in tokens:
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
        return self._strip_grouping_tokens(result.strip())

    def _strip_grouping_tokens(self, command: str) -> str:
        """
        Remove shell grouping tokens that can remain at command boundaries.

        The parser splits on operators like && and |, so a grouped command such
        as `(cd app && npm test)` or `{ git log; git status; }` can leave
        fragments like `(cd app`, `{ git log`, `npm test)`, or `}`. Subshell
        `()` and brace-group `{}` tokens do not change the command being
        validated, so strip them before matching permission patterns.

        Subshell '(' / ')' and brace-group '{' / '}' are handled differently
        because bash treats them differently:

        - '(' and ')' are metacharacters that self-delimit, so they glue to
          adjacent words ("(cd app", "head -40)"). We strip them even when glued.
        - '{' and '}' are reserved words recognized as a group ONLY when they
          are standalone tokens ("{ cmd; }"). A '{' or '}' glued to a word is
          brace/parameter expansion ("{a,b}", "${HOME}", "${arr[@]}") and must
          be left intact. So we strip braces only when they are whole tokens.

        Parens remain imperfect for glued non-grouping uses (arithmetic
        "$((1+2))", extglob "@(a|b)", case patterns "foo)"); fully resolving
        those needs paren-depth tracking in the tokenizer, not string stripping.
        """
        if not command:
            return command

        words = command.split()
        if not words:
            return command

        while words:
            first = words[0]
            if first[0] == '(':
                words[0] = first[1:]
                if not words[0]:
                    words.pop(0)
            elif first == '{':
                words.pop(0)
            else:
                break

        while words:
            last = words[-1]
            if last[-1] == ')':
                words[-1] = last[:-1]
                if not words[-1]:
                    words.pop()
            elif last == '}':
                words.pop()
            else:
                break

        return ' '.join(words).strip()


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
        # Heredoc tests
        ('cat << "EOF" | python3\nimport json\nprint("hello")\nEOF', ["cat", "python3"]),
        ('cat << \'EOF\' | python3\nimport json\nprint("hello")\nEOF', ["cat", "python3"]),
        ('cat <<EOF | grep foo\nbar\nbaz\nEOF', ["cat", "grep foo"]),
        # Multi-line pipeline
        ('git diff |\nhead -10', ["git diff", "head -10"]),
        ('ls -la |\ngrep foo', ["ls -la", "grep foo"]),
        # Line continuation
        ('ls -la \\\n| grep foo', ["ls -la", "grep foo"]),
        ('echo "hello" \\\n| wc -l', ['echo "hello"', 'wc -l']),  # Quotes preserved
        # Comments
        ('# this is a comment\ncurl -s http://example.com', ["curl -s http://example.com"]),
        ('# comment\necho foo # inline comment', ["echo foo"]),  # Inline comment stripped
        ('echo "# not a comment"', ['echo "# not a comment"']),  # # in quotes is not a comment
        # Command substitution
        ('VAR=$(curl -s http://example.com)', ["curl -s http://example.com"]),
        ('VAR=$(curl -s url | jq .)', ["curl -s url", "jq ."]),
        ('echo $(cat file.txt)', ["echo", "cat file.txt"]),
        ('RESULT=`grep foo file.txt`', ["grep foo file.txt"]),  # Backtick syntax
        ('VAR=$(nested $(echo inner))', ["nested", "echo inner"]),  # Nested (outer found first)
        # Env var with command substitution and path continuation
        ('GOWORK=$(pwd)/go.work go build ./...', ["go build ./...", "pwd"]),
        ('GOWORK=$(pwd)/go.work go build ./activecdn-module/... ./caddy-apps/... 2>&1 | head -60', ["go build ./activecdn-module/... ./caddy-apps/...", "head -60", "pwd"]),
        ('RESULT=`grep foo file.txt` echo bar', ["echo bar", "grep foo file.txt"]),
        ('PATH=$(dirname $0)/bin:$PATH python app.py', ["python app.py", "dirname $0"]),
        ('(cd apps/contributor && npx tsc --noEmit 2>&1 | head -40) && echo "---PRESENTATION---" && (cd apps/presentation && npx tsc --noEmit 2>&1 | head -40)', ["cd apps/contributor", "npx tsc --noEmit", "head -40", 'echo "---PRESENTATION---"', "cd apps/presentation", "npx tsc --noEmit", "head -40"]),
        ('(git status; git diff | head -20)', ["git status", "git diff", "head -20"]),
        # Brace group with redirection (strip standalone { and } grouping tokens)
        ('cd /tmp; { git log --oneline -3; echo "===STATUS==="; git status -s; } 2>&1', ["cd /tmp", "git log --oneline -3", 'echo "===STATUS==="', "git status -s"]),
        ('{ git status; git diff | head -20; }', ["git status", "git diff", "head -20"]),
        # Brace expansion / parameter expansion must NOT be stripped (not standalone)
        ('cp file.{txt,bak} /tmp', ["cp file.{txt,bak} /tmp"]),
        # Unquoted newlines split commands (like ';')
        ('echo foo\necho bar', ["echo foo", "echo bar"]),
        ('cd /tmp\nls -la\ngit status', ["cd /tmp", "ls -la", "git status"]),
        ('echo foo\n\n\necho bar', ["echo foo", "echo bar"]),  # Blank lines collapse
        ('\necho foo\n', ["echo foo"]),  # Leading/trailing newlines ignored
        # Newlines inside quotes are NOT separators (stays one sub-command;
        # internal whitespace is collapsed by normalization, as always)
        ('echo "line1\nline2"', ['echo "line1 line2"']),
        # if/then/else/fi one-liner spread across lines
        ('if git diff --quiet; then echo clean; else echo dirty; fi',
         ["if git diff --quiet", "then echo clean", "else echo dirty", "fi"]),
        ('if git diff --quiet\nthen echo clean\nfi',
         ["if git diff --quiet", "then echo clean", "fi"]),
        # Multi-line diagnostic script (the real-world failing case)
        ('echo "A:"\nls src/ | grep foo\necho "B:"\ngit ls-files | grep bar || echo none',
         ['echo "A:"', "ls src/", "grep foo", 'echo "B:"', "git ls-files", "grep bar", "echo none"]),
        # A command after a heredoc must not merge into the heredoc command
        ('cat <<EOF\nline\nEOF\necho done', ["cat", "echo done"]),
        ('cat <<EOF\nbody\nEOF\nls -la | grep foo', ["cat", "ls -la", "grep foo"]),
        # Brace GROUPS (standalone { } tokens) are stripped
        ('{ git log; git status; }', ["git log", "git status"]),
        ('{ echo hi; }', ["echo hi"]),
        # Brace/parameter EXPANSION (glued braces) is NOT stripped
        ('echo {a,b}', ["echo {a,b}"]),
        ('echo ${HOME}', ["echo ${HOME}"]),
        ('echo "${arr[@]}"', ['echo "${arr[@]}"']),
        ('ls foo{1,2}.txt', ["ls foo{1,2}.txt"]),
        # Subshell parens still strip even when glued
        ('(cd app && npm test)', ["cd app", "npm test"]),
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
