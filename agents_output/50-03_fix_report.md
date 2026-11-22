# Fix Report - 50-03 (Issue 1: ISO parsing across offsets and local TZ)

## Tests added
New class `TestTimestampOffsetsAndLocalTz` in `tests/test_unit_measurement_helpers.py` (5 tests; module now 31, was 26; added `import time`). `setUp` saves `TZ`, `tearDown` restores it (or removes it) and calls `time.tzset()`. Each test runs under `America/Los_Angeles` and `Asia/Kolkata`.
- `test_parsers_agree_across_offsets`: `amux_spawn_lib._epoch_of` and `statusline._parse_iso_epoch` give the same epoch for `Z`, `+02:00` and `-08:00` spellings of one instant.
- `test_naive_timestamp_is_utc_not_local`: a naive timestamp parses as UTC in both parsers under a non-UTC TZ.
- `test_cache_seconds_left_offset_equals_z`: the transcript's assistant timestamp written as `Z`, `+02:00` or naive gives the same `cache_seconds_left`.
- `test_last_assistant_entry_since_offset_equals_z`: `since` as `Z`, `+02:00` or `-08:00`, and entry timestamps written with an offset, select the same entry.
- `test_compaction_boundaries_since_offset_equals_z`: the same for `compaction_boundaries`, including inclusive and exclusive boundaries.

## Production change
None. The parsers were already correct; no bug found.

## Naive-timestamp behaviour
Both parsers do `dt.replace(tzinfo=timezone.utc)` for a naive value, so it is read as UTC, not local time. The docstrings do not state this rule; the code does, and the tests pin it. No production change made.

## Mutation result
Backed up both parsers with `cp`, then made both ignore the offset (`value.replace("Z", "+00:00")[:19]`, which drops any offset). The module went from OK to `FAILED (failures=16, errors=2)`. That count also includes pre-existing tests that use offsets or millis and are touched by the truncation, so it is not all attributable to the new tests. Restored both files from the backups; `diff` showed them identical, and the module passed again (31 tests, OK).

## Counts and results
- Measurement module: `Ran 31 tests`, `OK`.
- Full suite `python3 tests/run_all_tests.py` (foreground): `Ran 2520 tests in 427.233s`, `OK (skipped=2)`, exit code 0. The review's baseline was 2515, so the +5 are the new tests.

## git status --short
```
 M .claude/hooks/amux_spawn_lib.py
 M .claude/statusline/statusline.py
 M context-mcp/context_mcp_lib.py
 M tasks/50_epic_driver/state.md
 M tests/run_all_tests.py
?? tests/fixtures/measurement/
?? tests/test_unit_measurement_helpers.py
```
