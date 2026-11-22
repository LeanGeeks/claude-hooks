# Implementation Report - 50-03 Measurement helpers

## Summary

Four numeric, fail-soft readers for the epic-50 tick: `context_mcp_lib.measure_transcript`, `statusline.cache_seconds_left`, `amux_spawn_lib.last_assistant_entry` and `amux_spawn_lib.compaction_boundaries`. No status-line or context-mcp output changes. 26 new tests use small synthetic fixtures.

## Files Created

- `tests/test_unit_measurement_helpers.py` — 26 tests for all four helpers, the `mcp` import check, and the unchanged status-line readers.
- `tests/fixtures/measurement/*.jsonl` (11 synthetic transcripts: `measure_*`, `cache_*`, `assistant_entries`, `compaction_*`). All content is invented ("claude-test-model", fixed 2026-01-01 timestamps). No real transcript text was copied in.

## Files Modified

- `context-mcp/context_mcp_lib.py`:
  - The body of `parse_session` moved into `_fold_lines(lines)`. `parse_session` is now a thin wrapper around it.
  - `_fold_lines` now skips non-dict entries, messages and usage values instead of raising. Output on valid transcripts is identical.
  - New `MEASURE_TAIL_BYTES` (512 KiB) and `measure_transcript(path, tail_bytes=...)`.
- `.claude/statusline/statusline.py` — new `_parse_iso_epoch`, `last_assistant_epoch` and `cache_seconds_left(path, now=None)`. `read_cache_ttl`, `cache_state` and `last_activity_epoch` are untouched.
- `.claude/hooks/amux_spawn_lib.py` — new `MEASURE_TAIL_BYTES` (1 MiB), `_tail_entries`, `_epoch_of`, `last_assistant_entry` and `compaction_boundaries`.
- `tests/run_all_tests.py` — registers `unit_measurement_helpers`.

## Verification

- Compile/import: PASS. `py_compile` was run on all four changed modules and the new test file.
- `mcp` import check: PASS.
  - `python3 -I -c "import context_mcp_lib; [m for m in sys.modules if m.split('.')[0]=='mcp']"` prints `[]`.
  - `context_mcp_lib.py` has no `import mcp`.
  - The check is also a test: `test_import_does_not_pull_in_mcp` (subprocess, `-I`).
- Tests:
  - `python3 tests/run_all_tests.py --module unit_measurement_helpers`: 26 run, OK.
  - `python3 tests/test_unit_context_mcp.py`: 19 run, OK, unchanged.
  - `python3 -m unittest discover -s .claude/statusline -p "test_*.py"`: 205 run, OK, unchanged.
  - Full suite `python3 tests/run_all_tests.py`: 2515 run (baseline 2489 + 26 new), OK, 2 skipped, 450 s.
- Installed (`install.sh --yes` re-run): not applicable.

## Decisions

1. The context-mcp and statusline suites are not in `run_all_tests.py`: context-mcp runs standalone and the statusline tests live in `.claude/statusline/`. I ran both directly. The new test file imports all three modules and is registered.
2. `last_assistant_entry` did not exist (50-01 left it out), so it is implemented here. `since` is inclusive. It accepts an ISO string, an epoch number or a datetime. An unparseable `since` returns None / `[]`, never "newest".
3. Tail reads in `amux_spawn_lib` are byte-bounded (1 MiB), not line-bounded. The existing `read_transcript_tail` uses `readlines()`, which loads the whole file, and its 400-line window can miss an old compaction boundary. The status line's 64 KiB / 512 KiB byte limits were reused for `cache_seconds_left`.
4. `measure_transcript` marks `"approximate": true` only when the file is larger than the tail and the tail held a usage record. A whole-file read or the full-parse fallback has the exact `_measure` shape, with no extra key. The `tail_bytes` parameter exists so tests can use small fixtures.
5. `cache_seconds_left` returns `(None, None)` when no bucket is recorded (a provider such as GLM that reports none), as well as when there is no timestamped assistant entry. Seconds left is not computable without a TTL.
6. Compaction boundaries missing `compactMetadata` still come back, with None for the missing fields.

## Blockers

None
