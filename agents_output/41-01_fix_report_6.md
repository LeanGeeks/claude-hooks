# Fix Report - 41-01 (pass 6)

Files: .claude/hooks/bash_command_parser.py, tests/test_integration_pretool.py (pretool_hook.py untouched this pass).
py_compile OK. Full suite (foreground): `Ran 2049 tests`, `OK (skipped=2)` (/tmp/41-01-fix6-suite.log). Was 2046.
`git status --short` identical before and after every mutation and at the end. No git state-changing command; install.sh not run.

## HIGH: `$[ ... ]` cut the target word - FIXED
- Change (bash_command_parser.py): `_dollar_expansion_end` now recognises `$[` (line 2512) and calls new `_scan_legacy_arith` (line 2531, `[`/`]` nesting).
  `_unproven_paren_end` (line 2198) has a `$[` branch: the end is proven only when the scan closed on a `]`, brackets balanced, parens balanced, and the
  body fullmatches `_ARITHMETIC_BODY_RE` (no quotes, braces, `;`, backslash). Otherwise the reader returns the unproven-end sentinel (fail closed). A proven one is an
  EXPANSION part, exactly as `$((...))`, so R0-R6 apply. The tokenizer is untouched (`_dollar_expansion_end` is only used by the redirect reader and `_scan_param_brace`).
  Shared helper `_balanced(text, pair)` now serves both arithmetic branches.
- Oracle: before the fix, `r$[0 + 0]/../.claude/settings.json`, `r$[ 0 ]/...`, `r$[ $[ 0 ] ]/...`, `r$[ ${V:-0} ]/...` were allow and bash overwrote ws/.claude/settings.json or outside/f. After: 0 hits.
- Tests (TestRedirectTargets): `test_41_blank_bearing_constructs_are_read_to_their_end` (target keeps the tail; `_WHOLE_WORD_41`), `test_41_blank_bearing_constructs_in_a_directory_position_ask` (both cwds, root and sub tails),
  `test_41_legacy_arithmetic_with_an_unproven_end_is_refused` (`_UNPROVEN_LEGACY_41` + `r$[ [ ]`, sentinel + ask, clean root and sub), rows added to R1 (`r$[ 0 + 0 ]/../...`), R5 (`es$[ 2 + 3 ]c`, `es$[ 0 ]c`, `./h$[ 1 + 1 ]`, both cwds), and `_MUST_ALLOW_41` (`out_$[ 1 + 1 ].txt`, `./out_$[ 2 * 3 ].txt`).
- Mutation proofs (backup /tmp/41-01-fix6-mut/parser.bak, restore by cp + diff each time; target = TestRedirectTargets, 59 tests):
  | Mutation | Result |
  |---|---|
  | drop the `$[` scan in `_dollar_expansion_end` | KILLED, 53 failures |
  | legacy branch: `== ']'` check -> True | KILLED (`r$[ 0`) |
  | legacy branch: `_balanced(body,'[]')` -> True | KILLED (`r$[ [ ]` at end of input); survived the first time, row added |
  | legacy branch: `_balanced(body,'()')` -> True | KILLED (`r$[ $(echo ]) ]`) |
  | legacy branch: arithmetic regex -> True | KILLED, 5 failures (quote, brace, `;`) |
  | `_scan_legacy_arith` nesting broken | KILLED (`r$[ $[ 0 ] ]`, `r$[ a[0] ]`) |
  A backslash skip I first put in `_scan_legacy_arith` survived mutation: it is redundant (a backslash in the body fails the regex whichever end the scan finds). Removed, no dead code.

## Construct enumeration (bash oracle under /dev/shm/f6, real redirects into scratch ws, ws/sub, HOME patched; gate decision vs. what bash wrote)
Each row is a word evaluating to `r0` (a real dir) plus `/../.claude/settings.json` or `/../../outside/f` (and the `../` variant from sub/). Hit = gate allow and bash wrote the protected/outside file.
| Construct | Variants run (blanks/operators inside) | Result |
|---|---|---|
| `$((...))` | `0 + 0`, `0 | 0`, ternary, `a=0, 0`, `<<` | read whole, 0 hits |
| `$((cmd) | (cmd))` (comsub fallback) | `(echo 0) | (cat)` | read whole; with a comment/`$(`-redirect body sentinel (see MEDIUM) |
| `$(...)` | pipes, `;`, `")"`, comment `)`, heredoc `)`, `case`, `${V:-)}` | read whole or sentinel, 0 hits |
| backticks | pipe, `;`, nested escaped, `")"` | 0 hits |
| `${...}` | `${V:-0 }`, `${V:-a b}`, quoted, nested, `${V:-)}`, `\}`, `"}"` | 0 hits |
| `$[...]` | blanks, `|`, `>`, nested, `$(..)`/`${..}` inside, `a[0]`, quoted `]`, `;`, `\]`, in double quotes | was HIT (4 shapes), now 0 hits (read whole or sentinel) |
| `<(...)`/`>(...)` | `<(echo 0 | cat)`, `)` in quotes/`${}`/comment | unresolvable/sentinel, ask, 0 hits |
| `$'...'` | `$'0 '`, `$' '0`, `\x30`, `\'` | 0 hits |
| `$"..."` | `$"0 "`, `$"\"0"` | 0 hits |
| extglob `@ ? * + !`(...) with `shopt -s extglob` on the line before | blanks, `|`, `)` escaped/quoted/in `[)]`/in `$(..)`/in `${..}`, comment, adjacent groups | read whole as GLOB, 0 hits |
Only `$[ ... ]` was wrong. Tests cover each construct (`_WHOLE_WORD_41`, extglob parser-level because `shopt` is not in the test allow list).
Oracle note: extglob needs a newline after `shopt -s extglob` (a `;` on the same line does not enable it at parse time).

## MEDIUM: `depth == 0` in the `$((` branch - REACHABLE, now pinned
It is needed. Bodies that dip below balance while the scan still ends on `))` are subshell pairs, `$((echo a > lnks) | (cat))`, which bash runs as a command substitution but the
tokenizer reads as arithmetic, so the inner redirect is never surfaced. The reader's refusal is then the only thing that catches it when the word has no other refusal
(final segment, no `/`, no `x.*` leaf on disk; `lnks` is a symlink into .claude). The review's end-of-word reasoning is right (the scan end equals bash's end, also for `))` inside a comment: bash errors "bad substitution"),
so the condition is about what the word hides, not where it ends.
- Change: condition kept, now `_balanced(body, '()')` (same semantics: never negative, ends at 0).
- Test: `test_41_arithmetic_shaped_subshell_pair_is_refused` (`echo k > x$((echo a > lnks) | (cat))` is the sentinel, ask in the real root and sub; plain `x$(((1+2)*3))` still read whole).
- Mutation: condition -> True: KILLED (that test).

## LOW
- Dead code: `_read_redirect_word` now returns the RedirectTarget (or None), no `end`; caller and docstring updated; three `, end` returns and the final `, i` removed.
- `UNREAD_REDIRECT_TARGET` guard kept (fail closed) and now reached: `test_41_an_unreadable_operand_is_refused_not_dropped` (patches `_redirect_operand_start` to None).
- Overlap: removed `test_41_anchored_variable_leaf_still_allowed` (`./$V` is in `_MUST_ALLOW_41`), `test_41_lenient_fallback_refuses_dotdot_anywhere`, `test_41_lenient_fallback_unexpandable_user_refused`; their rows now run in one `test_41_lenient_fallback_reports_the_real_target` with the tilde rows (target text asserted).
- `~+`/`~-`/`~N`: `test_41_directory_stack_tilde_is_unresolvable_in_the_parser` asserts `.unresolvable` and source-form `.text` via `extract_write_redirect_words` (plain `~/x` stays resolvable). Mutations `login[:1] in ('+','-')` -> `in ()` and `isdigit()` -> False: both KILLED.
- Stale names in docs: orchestrator's.

## For a follow-up task (measured on the repo copy, not fixed)
1. Pre-existing tokenizer hole at HEAD and now: `echo $((rm x) | (cat))` parses to one command (`echo $((rm x) | (cat`); bash runs `rm x`. Likewise
   `echo $((echo a > /dev/shm/f6/w/outside/g) | (cat))` is `allow` and bash wrote outside the roots (`extract_write_redirect_targets` returns []). Command matching and the redirect gate are both blind to a command inside a `$((subshell) | (subshell))` that bash falls back to a command substitution for.
2. Tokenizer and `$[ ... ]`: it splits at `|`, `;`, `&` and reads `>` as a redirect inside the brackets (`echo $[ 1 | 0 ]` -> commands `echo $[ 1` and `0 ]`; `echo $[ 1 > /etc/x ]` reports target /etc/x). Measured: only false asks, no command is hidden from matching (`$(...)` inside is still found). Not fixed.

## Questions for User
None.
