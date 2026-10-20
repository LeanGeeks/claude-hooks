# 41-01 Fix Report 2

## Issue 1 (HIGH): construct glued before `.claude` in one segment
Change: `.claude/hooks/pretool_hook.py` `_literal_prefix_inside_allowed_roots` (segment loop) plus new `_mask_constructs` / `_CONSTRUCT_MARK` just above it; docstring updated. I adopted the reviewer's regex idea with two adjustments:
- Constructs (`$V`, `${..}`, `$(..)` with balanced parens, backticks, globs) are masked first, then the target is split on `/`. This stops a `/` inside `$(echo a/b)` from splitting a segment and hiding `.claude`.
- Rule per segment holding a construct: the literal pieces joined with `.*` must not fullmatch `.claude` (covers `$V.claude`, `${V}.claude`, `"$V".claude`, `$(echo).claude`, `$V$W.claude`, `.claude$V`, `.cl$V`, `.c*laude`). The old head heuristics are gone. The separate "only dots" rule stays for `..$V` and `.$V.`.
Tests (TestRedirectTargets): `test_41_construct_glued_before_dot_claude_is_refused` (15 forms, including `./$V.claude/settings.json`, `hooks/x.py`, `settings.local.json`, `~/$V.claude/x`).

## Issue 2 (LOW): friction on dotfiles
Change: same function. A segment whose joined literal is under 2 characters is not judged (`.$V`), and `.$i.log` no longer completes to `.claude`. I did not use a bare `.*` regex, since `.$V` would then match `.claude` and stay refused. The user asked for `.$V` to be allowed, so that case is handled by the length floor. `..$V` and `.$V.` are still refused by the dots rule.
Test: `test_41_dotfile_names_with_a_construct_stay_allowed` (`/tmp/.$i.log`, `.$V`, `./.$V.log`, `./$V.claude.bak`, `./my$V.claude_notes`; none refused).

## Mutation proof
Protocol: `git status --short` saved before and compared after (identical), `cp` backup, `diff` after each restore.
- M1 disable the regex refusal: 16 failures (the new refusal test, all subtests).
- M2 length floor `< 2` changed to `< 0`: the LOW test and `test_41_anchored_variable_leaf_still_allowed` fail.
- M3 pattern reduced to first piece + `.*`: the dotfile test and `..._other_unresolvable_names_..._stay_allowed` fail (over-refusal caught).
- M4 mask without the `$(`/`${` branch: 5 failures (`$(echo a/b).claude`, `${V}.claude`, `.claude$(echo)`, `..$(echo)`).

## Suite
`python3 tests/run_all_tests.py`: Ran 2041 tests, OK (skipped=2). Log: /tmp/41-01-fix2-suite.log

## Questions for User
None.
