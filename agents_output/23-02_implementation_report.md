# Implementation Report - 23-02: Relay — per-message nudges + escalation

## Summary

Added repeating-tail ladder syntax (`*` on last rung) to availability parsing, four new columns (migration slot 5) to the relay messages table, per-message nudge schedule override with correct `nudge_enabled=0` precedence, and a fourth escalation reaper pass that fires exactly once, routes answers from escalated copies back to the originating message, and cancels sibling copies when the original is answered. The API endpoint `POST /v1/messages` now accepts and validates `nudge_schedule`, `escalate_after_sec`, and `escalate_to_token`.

## Files Created

- `relay-server/tests/test_relay_nudge_escalation.py` — 31 tests covering ladder parsing, repeating-tail arithmetic, nudge override precedence (including `nudge_enabled=0`), escalation fire-once, unknown-token 422, answer attribution from the escalated copy, cancel-on-original-answer, two-installation feed isolation, full four-pass reaper tick, regression floor (invariant 9), and v4→v5 migration forward compatibility.

## Files Modified

- `relay-server/relay_server/availability.py` — Added `parse_nudge_schedule_with_repeat` function (parses `*` on last rung; raises `ValueError` with "last rung" when `*` appears elsewhere); updated `__all__`.
- `relay-server/relay_server/db.py` — `SCHEMA_VERSION` 4→5; added four columns to `SCHEMA_SQL` (`nudge_schedule_override`, `escalate_at`, `escalate_to_token_hash`, `parent_message_id`) plus `messages_escalation_due` partial index; added migration 5 block (ALTER TABLE ADD COLUMN ×4 + CREATE INDEX; no table rebuild).
- `relay-server/relay_server/models.py` — Added three optional fields to `CreateMessageRequest`: `nudge_schedule: str | None`, `escalate_after_sec: int | None` (gt=0), `escalate_to_token: str | None`.
- `relay-server/relay_server/reaper.py` — Added `_row_nudge_schedule` (returns per-row schedule + repeat flag), `_compute_next_nudge_due` (repeating-tail logic), `_escalation_pass` (fourth pass: queries `state='open' AND escalate_at < now`, clears `escalate_at` first, looks up target by token hash, inserts child row with `parent_message_id`, sends duplicate via `render_body`, updates child tg_message_id); updated `_nudge_pass` to split rows on `nudge_schedule_override` before the `nudge_enabled` check; updated `reaper_tick` signature to accept `answer_waiters`.
- `relay-server/relay_server/app.py` — Updated `_seed_next_nudge_at` to bypass `nudge_enabled` check when message has its own override; updated `create_message` to validate escalation field pairing (both or neither), resolve escalation token with 422 on unbound, compute `escalate_at` via `advance_active`, validate `nudge_schedule` syntax, INSERT new columns; added `_cancel_escalated_children`; updated `_apply_text_answer` and `_handle_callback_query` to route answers from child rows to parent and cancel siblings.
- `relay-server/tests/test_schema.py` — Updated version assertions to 5; added new-column and index assertions.
- `relay-server/tests/test_answer_feed.py` — Updated `test_v3_migrates_to_v4_cleanly` version assertion to 5.

## Verification

- Compile/import: PASS (`python3 -m py_compile` on all modified modules)
- Tests (relay suite): 303 passed, 0 failed (`/home/anton/.local/bin/pytest relay-server/tests/ -q`)
- Tests (top-level suite): 1030 passed, 1 skipped, 0 failed (`python3 tests/run_all_tests.py`)
- Installed (install-claude-config.sh re-run): not applicable (relay server only)

## Decisions

- **Child `installation_id = original's`**: The escalated copy shares `installation_id` with the original so it never surfaces in installation B's answer feed (which filters by `installation_id`). Invariant 4 respected: the relay never learns a role or path.
- **`escalate_at` cleared before send**: Ensures fire-once even if `send_message` raises; a failed escalation won't re-fire on the next tick.
- **`render_body` for the escalated duplicate**: Never re-renders from the original payload; uses the already-rendered body stored on the original row.
- **`nudge_enabled=0` split**: Rows with `nudge_schedule_override` are separated from the non-override batch before the `nudge_enabled` gate, so they continue to receive nudges even when the chat has nudges off; non-override rows in the same chat still get their `next_nudge_at` cleared.
- **Duration format**: `parse_duration` only supports `h`/`m` formats, not `d`; test schedules use `24h`, `72h`, `168h` in place of `1d`, `3d`, `7d`.

## Blockers

None.

## Questions for User

None.
