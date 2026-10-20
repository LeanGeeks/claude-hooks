# Fix Report - 41-03 (review Issue 1, MEDIUM)

## Change
- .claude/hooks/pretool_hook.py `_unresolved_target_refusal`, R5 block (~:1658): a directory-position (`in_directory`) segment whose parts are all constructs (no LITERAL, no GLOB part) now yields `names = []` (held R1 -> unknown). Segments with a literal character or an unquoted glob keep the filesystem match. Docstring R5 paragraph updated.

## Tests (tests/test_integration_pretool.py)
- New `test_41_construct_only_directory_segment_is_unknown`: `./"$dir"/x`, `./$V/x`, `./$V/settings.json`, `./${a}${b}/x`, `out/$name/x.log`, `"$dir"/x`, from root (holds `.claude/`) and `sub/` -> ask/unknown.
- New `test_41_directory_segment_with_literal_or_glob_stays_risk_from_the_root`: `.$V/x`, `.cl${V:-aude}/settings.json`, `./.*/settings.json` -> risk from root, ask from sub.
- Updated: `_UNKNOWN_SINCE_41_03` gains `./$V/settings.json` (root+sub); `test_41_construct_in_a_directory_position_asks` now asserts `./"$dir"/x` unknown (`./*/x` stays risk); `_RISK` row `./$V/x` replaced by `.$V/x` and `./$V/x` moved to `_UNKNOWN`.

## Mutation proof
Protocol followed (git status before/after identical, cp backup in /tmp/41-03-fix-mut, restore + diff).
1. Condition disabled (`if False and ...`): 12 failures (new test x many rows, `_UNKNOWN` row, refused_by_rule `./$V/settings.json`, directory_position_asks).
2. GLOB exemption dropped (`kind == R.LITERAL` only): `test_41_construct_in_a_directory_position_asks` fails (`./*/x`).
Restored; diff identical.

## Before/after comparison
Pre-change copy (/tmp/41-03-fix-mut/pretool_hook pre_change copy) vs working copy, script /tmp/41-03-fix-mut/cmp.py: 1632 comparisons (816 commands x cwd root and sub, real workspace with `.claude/`, symlinks). Result: 376 risk->risk, 1200 unknown->unknown, 56 risk->unknown (all from root, all-construct directory segments e.g. `./"$dir"/x`, `./$V/x`, `./$V/settings.json`). 0 moves to allow, 0 other changes.

## Suite
`python3 tests/run_all_tests.py`: Ran 2151, OK (skipped=2). git status unchanged by the mutation work. state.md untouched; install.sh not run.

## Questions for User
None.
