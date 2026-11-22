# 50-03 — Measurement helpers

**Status:** todo · **Depends on:** Phase 0
**Read first:** [architecture.md](./architecture.md) §11.1, §11.6, §12.4, §12.5, §13.2 · [state.md](./state.md)
P0-9 · review m3 · `context-mcp/context_mcp_lib.py` (`parse_session`, `_measure`, `resolve_context_window`) ·
`.claude/statusline/statusline.py` (`read_cache_ttl`, `_read_transcript_tail`, `cache_state`,
`last_activity_epoch`) · `.claude/hooks/amux_spawn_lib.py` (`read_transcript_tail`) · `tests/test_unit_context_mcp.py`

## Goal

Give the tick numeric, transcript-based readings of a session's context, cache lifetime, compactions and last
assistant entry, from the modules that already parse transcripts, without changing what those modules show today.

## Scope

1. **`context_mcp_lib.measure_transcript(path) -> dict | None`**: the public form of `_measure(parse_session(...))`
   for an explicit transcript path (the `_measure` dict shape). It reads a bounded tail first (the latest usage record
   and model are near the end) and falls back to the full parse only when the tail holds no usage record;
   `cumulative_output_tokens` and `turns_count` are then marked approximate in the result
   (`"approximate": true`) when only the tail was read. Never raises on a missing or malformed file (returns None).
2. **`statusline.cache_seconds_left(path, now) -> tuple[float | None, int | None]`**: `(seconds left, ttl)` where ttl
   comes from `read_cache_ttl` (5m/1h bucket, so overage is seen) and the clock is the **last assistant entry's
   `timestamp`**, not the file mtime. Negative when cold. `(None, None)` when the transcript has no assistant entry
   with a timestamp. The existing `cache_state`/`last_activity_epoch` behaviour of the status line is unchanged.
3. **`amux_spawn_lib.last_assistant_entry(transcript_path, since=None) -> dict | None`** (architecture §13.2), if 50-01
   has not already added it: `{timestamp, model, error, is_api_error, text}`; with `since`, the first assistant entry
   at or after that time.
4. **`amux_spawn_lib.compaction_boundaries(transcript_path, since=None) -> list[dict]`**: the entries with
   `"type": "system", "subtype": "compact_boundary"` (fact: real transcripts carry `compactMetadata.trigger`,
   `preTokens`, `postTokens`), as `{timestamp, trigger, pre_tokens, post_tokens}`, optionally only after `since`.
5. All four are fail-soft and bounded in what they read (reuse the existing tail readers' limits).

## Tests

Fixture transcripts under `tests/fixtures/` (small, synthetic, shaped like real entries):

- `measure_transcript`: a normal transcript; a duplicated streamed message id (counted once); usage only beyond the
  tail limit (full-parse fallback); empty and malformed files.
- `cache_seconds_left`: warm 1 h, warm 5 m (overage), cold, an mtime much newer than the last assistant timestamp
  (the timestamp wins), no assistant entry.
- `last_assistant_entry`: a rate-limit entry (`error: "rate_limit"`, `isApiErrorMessage: true`), `since` selection,
  model field read.
- `compaction_boundaries`: none, one auto, one manual, `since` filtering.
- The context-mcp and statusline suites pass unchanged.

## Done when

- The tick can get `effective_context_tokens`, `(seconds_left, ttl)`, the newest assistant entry and compactions from
  one call each, with no import of `mcp`.
- No status-line or context-mcp output changes.
