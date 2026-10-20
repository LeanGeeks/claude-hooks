# Review Report 2 - 41-03 An unresolvable write target is an unknown, not a risk

## Verdict: PASS

0 BLOCKER, 0 HIGH, 0 MEDIUM, 0 LOW. Pass-1 MEDIUM (all-construct directory segments) is fixed.
py_compile OK. Full suite: Ran 2151, OK (skipped=2) (/tmp/41-03-review2-suite.log). `git status --short` identical before/after everything; real ~/.claude/bash_manual_confirm.log size+mtime unchanged.

## Completeness Check

Orchestrator rule: met. Condition at pretool_hook.py:1660 (`in_directory and not any(kind in (LITERAL, GLOB))` -> names = []; the word then returns `held` = R1 -> unresolved). Segment with a literal char or glob is still matched against the directory entries. Verified by running (HEAD vs change, cwd = root holding `.claude/` and root/sub):

| shape | root | sub |
|---|---|---|
| `./"$dir"/x`, `./$V/x`, `./$V/settings.json`, `./${a}${b}/x`, `out/$name/x.log`, `"$dir"/x` | unknown | unknown |
| `.cl${V:-aude}/x`, `.$V/x`, `./.*/settings.json` | risk | unknown |
| `./""$V/x`, `./''$V/x`, `./$V""/x` | unknown | unknown |
| `./\$V/x` (escaped, literal dir `$V`) | allow (as HEAD) | allow |
| `./${V}.d/x`, `./"$V"d/x` (literal char, `.*\.d`/`.*d` does not match `.claude`) | unknown | unknown |
| `./~$V/x`, `./$(echo $V)/x`, `./$((1+1))/x`, `` ./`date`/x ``, `./$V/$V/x` | unknown | unknown |
| `./$V*/x`, `./*$V/x`, `./$V?/x`, `./[.]$V/x` (glob: matched, `.claude` matches) | risk | unknown |
| `./$V/../x`, `./$V/.claude/x` (visible evidence) | risk | risk |

Quoted-empty decision (`./""$V/x`, `./''$V/x`): quote removal leaves no literal character, the parser drops the empty piece (shown as `./$V/x`), so it is a construct-only segment -> unknown. Defensible: the rule is about characters that survive to the runtime name, and an empty quote names nothing. Same for `./$V""/x`.

## Invariant 2 (HEAD copy via `git show HEAD:` vs working copy)

Same fake loader, real workspace (`.claude/hooks`, `.claude/settings.json`, symlinks to /etc and into `.claude`), cwd root and root/sub. Corpus: pass-1 shapes + all edge shapes above + ~90k random fragment targets (extra `./` and `$V/` fragments) = 109,532 comparisons.
- same 60,592; changed 48,940, all ('ask','risk') -> ('ask','unknown'). 0 flips to allow, 0 allow changes, 0 other changes (BAD 0).
- Modes via build_pretool_output over all 109,532 results: default 0 changes; auto and bypassPermissions: 48,940 ask -> None each, nothing to allow.
- Visible-evidence filter on the flips (literal `.claude`, `..` path segment in the construct skeleton, absolute /etc-style anchor, `claude`/`..` inside a construct source): 0 real hits. 16 regex hits per cwd were all `\${V:-..}` (escaped dollar = literal text, my filter's artifact).

## Re-read of the 41-03 diff

No regressions found. `_judge_redirect_word` fully replaced by `_redirect_target_outcome` (no remaining callers; trap handler site updated). Both result keys on every result. Precedence comment and reason strings agree with code. `_unresolved_target_refusal` docstring accurately describes the in-directory branch (matching entries count, missing dir matches nothing, construct-only segment not matched, `.$V/x` asks beside `.claude`). The "Accepted residual" paragraph is still correct. No dead code: `has_construct` is still used in the final-segment branches. Tests: new `test_41_construct_only_directory_segment_is_unknown` (6 shapes x root/sub) and `..._with_literal_or_glob_stays_risk_from_the_root` assert real decisions and targets; isolation env unchanged.

## Issues Found

None.

## Code Quality Notes

- `./~$V/x` is unknown from root: tilde not at word start is a literal `~` that cannot match `.claude` anyway; no hole.
- Test comment "Behaviour change (fix 4)" above `test_41_construct_in_a_directory_position_asks` is slightly historical but accurate.

## Questions for User

None.

## Mutation proofs

Protocol: `git status --short` saved before and compared after (identical); `cp` backup at /tmp/41-03-review2-mut/pretool_hook.py.bak; restored with `cp`, `diff` identical each time; no git write ops. Tests run as `cd tests && python3 -m unittest test_integration_pretool`.

| # | Mutation | Result |
|---|---|---|
| 1 | Remove the all-construct-segment condition (`if False:`) | FAIL x12: construct_only_directory_segment_is_unknown (`./"$dir"/x`, `./$V/x`, `./$V/settings.json`, `./${a}${b}/x`, root and sub), construct_in_a_directory_position_asks, refused_by_rule `./$V/settings.json`, rule_to_outcome_unknown_rows `./$V/x` |
| 2 | Invert it (`in_directory and any(LITERAL/GLOB)`) | FAIL x15: same construct-only tests, plus directory_segment_with_literal_or_glob_stays_risk_from_the_root (`.$V/x`, `.cl${V:-aude}/settings.json`, `./.*/settings.json`) and refused_by_rule R1 rows |
| 3 | Outside no longer wins: `elif outside_targets and not unresolved_targets:` | FAIL x5: test_41_03_outside_wins_over_unresolved, task_table `"$f" ; echo b > /etc/x`, inner_redirect_of_a_target_substitution_is_scanned x3 |
