# 50-04 Fix Report (second pass)

## Change
`.claude/hooks/epic_driver_lib.py`, `_construct_unique_mapping` (the `SafeLoader` subclass mapping constructor): keys whose tag is `tag:yaml.org,2002:merge` are skipped in the duplicate check, so only explicitly written keys are compared. Construction then goes to `yaml.SafeLoader.construct_mapping`, whose `flatten_mapping` does the standard merge: explicit keys override merged ones, and earlier sources in `<<: [..]` win. Real explicit duplicates still raise with the key and line.

## Tests (tests/test_unit_epic_driver.py, TestLedgerValidation)
- test_schema_merge_keys_are_supported: subtests for a merge from an anchor, a merge list, and an explicit key overriding a merged one.
- test_schema_merge_list_earlier_source_wins: `<<: [*b, *a]` takes b's values.
- test_schema_explicit_duplicate_beside_a_merge_is_still_an_error: a `<<: *base` plus two explicit `effort:` keys gives one error naming 'effort' and the second key's line.
- test_schema_ledger_with_merged_role_defaults_validates: a ledger whose `reviewer` role merges `driver` and overrides `effort` validates with no errors and loads with merged values.

## Mutation (cp backup /tmp/edl.bak, restored, `diff` clean both times)
1. Removed the merge-tag skip (`if False:`): the 4 new tests failed (2 failures, 4 errors across subtests). Restored, diff clean.
2. Disabled the duplicate raise (`if False:`): the duplicate tests failed (4 failures, 1 error). Restored, diff clean.

## Counts
- `python3 -m py_compile .claude/hooks/epic_driver_lib.py`: OK.
- Epic driver module (`tests/test_unit_epic_driver.py`, foreground): `Ran 144 tests` / `OK`.
- Full suite (`python3 tests/run_all_tests.py`, foreground): `Ran 2685 tests in 460.746s` / `OK (skipped=2)` (baseline 2681 + 4 new). Exit code 0.

## git status --short
```
 M .claude/hooks/amux_spawn_lib.py
 M context-mcp/context_mcp_lib.py
 M tasks/50_epic_driver/architecture.md
 M tasks/50_epic_driver/state.md
 M tests/conftest.py
 M tests/run_all_tests.py
 M tests/test_unit_python_compat.py
?? .claude/hooks/epic_driver_lib.py
?? tests/test_unit_epic_driver.py
```
(plus this report, in the gitignored agents_output/)
