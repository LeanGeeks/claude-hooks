# Review Report - 50-03 Measurement helpers

## Verdict: PASS

Compile and import are clean for all changed files. The full suite (`python3 tests/run_all_tests.py`, foreground) ends with `Ran 2515 tests in 455.443s` / `OK (skipped=2)`. That run includes the unchanged context-mcp and statusline suites and the new 26-test `unit_measurement_helpers` module. It also passes under `TZ=Asia/Tokyo`.

## Completeness Check

- **1. `measure_transcript`: met** (`context-mcp/context_mcp_lib.py`).
  - It reads the last 512 KiB, drops the leading partial line, and folds that tail. It sets `approximate: true` only when the tail held a usage record and the file is larger than the tail.
  - It falls back to a full `parse_session` when the tail has no usage record. That result carries no `approximate` key.
  - Dedup of streamed message ids is shared with `parse_session` through `_fold_lines`.
  - It returns None for a missing file, a directory, `None`, empty or malformed input. The result has the `_measure` shape.
- **2. `cache_seconds_left`: met** (`statusline.py`).
  - The clock is the newest assistant entry's `timestamp`, not the mtime. The ttl comes from `read_cache_ttl`, so 5m (overage) and 1h are both seen.
  - The result is negative when cold, and `(None, None)` with no timestamped assistant entry.
  - Timestamps parse correctly: `Z` becomes `+00:00`, naive values are treated as UTC, and `+02:00` offsets convert right. I checked this and ran the tests under a non-UTC `TZ`.
  - It also returns `(None, None)` when no cache bucket was ever recorded. That is a reasonable, documented reading of the spec.
  - `cache_state` and `last_activity_epoch` are untouched.
- **3. `last_assistant_entry`: met.** 50-01 had not added it; 50-03 adds it to `amux_spawn_lib.py`.
  - It returns `{timestamp, model, error, is_api_error, text}`.
  - Without `since` it returns the newest assistant entry. With `since` it returns the FIRST assistant entry at or after that time, inclusive.
  - `since` accepts an ISO string, an epoch or a datetime. An unparseable `since` gives None.
- **4. `compaction_boundaries`: met.** It reads `compact_boundary` entries and maps `compactMetadata.trigger`, `preTokens` and `postTokens` to `{timestamp, trigger, pre_tokens, post_tokens}`, with an inclusive `since` filter.
- **5. Bounded reads and fail-soft: met.**
  - `amux_spawn_lib` reads the last 1 MiB, `measure_transcript` reads the last 512 KiB, and `cache_seconds_left` reuses the existing 64K/512K limits.
  - Missing files, bad paths and malformed lines are handled without raising.
  - Importing `context_mcp_lib` in a subprocess leaves no `mcp` or `mcp.*` in `sys.modules`. A test checks this too.
- **No output changes: met.**
  - Statusline diff: pure additions (`_parse_iso_epoch`, `last_assistant_epoch`, `cache_seconds_left`). No existing function is touched.
  - context-mcp diff: `parse_session`'s body moved into `_fold_lines`, and `parse_session` is a thin wrapper that still opens the file and iterates lines.
  - The fold logic is identical, with one difference. It adds `isinstance(dict)` guards on the entry, message and usage. Inputs that used to raise `AttributeError` are now skipped. That is a fail-soft change and not a behaviour change for any valid transcript.
- **Fixtures: met.** There are 11 small synthetic files (10 KB total), none with real paths, personal data or content. A grep for `/home`, `anton` and `@` found nothing.
- **Test registration: met.** `tests/run_all_tests.py` registers the new module. `state.md` moves 50-03 to `in_progress`.

## Mutation checks

I ran these on a temporary copy of the repo, not the shared tree, so there was nothing to restore. The unmutated copy passed 26 of 26.

| Mutation | Result |
|---|---|
| `since` takes the last match instead of the first | 2 failures |
| `cache_seconds_left` uses the mtime | 4 failures |
| `approximate` flag removed | 1 error |
| `since` filter removed from `compaction_boundaries` | 1 failure |
| streamed-id dedup removed | 1 failure |

The tests bite.

## Issues Found

No BLOCKER or HIGH issues.

### Issue 1: [severity: LOW]
- **File:** `.claude/hooks/amux_spawn_lib.py` (`_epoch_of`) and `.claude/statusline/statusline.py` (`_parse_iso_epoch`)
- **Problem:** The ISO-to-epoch parsing exists in two copies. That is likely deliberate, because the status line is installed separately and avoids cross-imports. The tests do not run the timezone paths with a non-UTC local `TZ`. The tests' own `epoch()` helper forces UTC.
- **Fix:** Optional. Add one test with an offset timestamp (`+02:00`) and no `Z`.

### Issue 2: [severity: LOW]
- **File:** `context-mcp/context_mcp_lib.py` (`measure_transcript`)
- **Problem:** When the tail holds no usage record, it does a full unbounded `parse_session`. That is what the spec asks for, but a very large usage-less transcript is read in full.
- **Fix:** None needed. Informational.

## Code Quality Notes

- The structure is clean, with docstrings and consistent fail-soft handling.
- The suite's last lines are subprocess output (questions-index rebuild), so the `OK` summary is in the middle of the log. That is a runner quirk, not related to this change.

## Questions for User

None.

## Tree state
`git status` and `git diff --stat` match what the implementer left: 5 modified files plus the untracked fixtures and test. I made no edits to repo files.
