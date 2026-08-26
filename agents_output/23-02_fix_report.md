# Fix Report — 23-02: Relay per-message nudges + escalation

## Summary

All four issues addressed. Schema/migration unchanged — no re-run needed.

---

## Issue 1 (HIGH) — `parse_duration` day support

**File:** `relay-server/relay_server/availability.py`

- `_DURATION_RE` changed from `r"^(?:(\d+)h)?(?:(\d+)m)?$"` to
  `r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$"` (line ~449).
- `parse_duration` now extracts group 1 as `days`, group 2 as `hours`, group 3 as
  `minutes` and returns `timedelta(days=days, hours=hours, minutes=minutes)`.
- Error messages in both `parse_nudge_schedule` and `parse_nudge_schedule_with_repeat`
  updated to: `"accepted forms: 15m, 3h, 2h30m, 1d, 2d12h"`.
- No existing test asserts on the old error-message text ("15m, 3h, 2h30m") — only
  `match="bad duration"` is used, which still matches.
- Change is additive only: every string valid before remains valid; the epic-19 nudge
  path using `parse_nudge_schedule` inherits day support automatically.

**Tests updated:**
- `TestLadderParsing::test_repeating_tail`: `"4h,24h,72h,168h*"` → `"4h,1d,3d,7d*"`;
  assertion `timedelta(hours=168)` → `timedelta(days=7)`.
- New class `TestParseDurationDaySupport` (7 tests):
  - Spec default ladder `"4h,1d,3d,7d*"` parses without error.
  - `1d`, `2d12h`, `1d6h30m` each parse to the correct timedelta.
  - Previously valid forms (`15m`, `3h`, `2h30m`) unchanged.
  - Invalid forms (`xyz`, `""`, `0d`) still return None.
  - `_compute_next_nudge_due` with `nudge_count=15` on a 4-rung `7d*` schedule returns
    `now + timedelta(days=7)` — the "week three" Done-when clause is now demonstrated.

---

## Issue 2 — Accepted (no change)

`parent_message_id` column accepted as justified architecture deviation.

---

## Issue 3B — Expiry pass cancels escalated children

**File:** `relay-server/relay_server/reaper.py`

Added `cancel_escalated_children` as a new exported async function (lines ~334–418),
extracted from the logic in app.py's `_cancel_escalated_children`.  The new function:
- Guards Telegram edits with `if tg_msg_id > 0` so a child whose send never completed
  (`telegram_message_id = 0`) does not produce an edit call for a non-existent message.

In `reaper_tick`, after `_mark_expired` succeeds and `updated != 0`, a call to
`cancel_escalated_children(conn, backend, message_id)` is inserted before the
Telegram body-edit (lines ~510–519).  It is wrapped in a try/except so a failure
does not block the expiry or the subsequent Telegram calls.

**app.py changes:**
- Removed the `_cancel_escalated_children` definition (was lines 1440–1514).
- Added `cancel_escalated_children as _cancel_escalated_children` to the
  `from .reaper import …` block so all existing call sites in app.py are unchanged.

**Tests added:**
- `test_expiry_cancels_escalated_child`: parent with real child (tg_msg_id=6101) — after
  expiry tick, child is `cancelled`.
- `test_expiry_cancels_unsent_child`: parent with child at `tg_msg_id=0` — after expiry
  tick, child is `cancelled`; no `edit_message` call is made with `telegram_message_id=0`.

---

## Issue 3A — Idempotent escalation (fire-once + retryable)

**File:** `relay-server/relay_server/reaper.py`, function `_escalation_pass`

### Ordering reasoning and crash-safety argument

The old implementation cleared `escalate_at` before the send to prevent duplicates,
but this silently lost escalations on transient failures.  The new implementation:

1. **Select due rows** (parent is open, escalate_at < now).
2. **Existence check**: query for a child row with `parent_message_id = message_id AND state = 'open'`.
   - Child with `telegram_message_id != 0`: send already delivered on a prior tick.
     Clear `escalate_at` only — no second send.  This handles the crash-between-send-
     and-clear scenario: if the process crashed after the Telegram send but before the
     DB commit, the child already has a real tg_msg_id, so the next tick detects it and
     skips the send.
   - Child with `telegram_message_id == 0`: prior send failed.  Reuse the existing child
     row (same relay id, same keyboard encoding) and retry the send.  No second child row
     is inserted.
   - No child: first attempt.  Insert child (tg_msg_id=0), attempt send.
3. **On send failure**: log WARNING with parent id, child id, target installation, and
   target chat.  `escalate_at` is NOT cleared, so the row stays eligible and the next
   tick retries (brd D10).
4. **On send success**: update `telegram_message_id` on the child AND clear `escalate_at`
   on the parent in a single SQLite transaction.  SQLite's BEGIN…COMMIT means either
   both updates land or neither does.

**Crash analysis:**
- Crash before send: tg_msg_id=0, escalate_at set → retry on next tick. ✓
- Crash after send but before commit: tg_msg_id stays 0, escalate_at stays set → next
  tick retries (one additional Telegram message possible, unavoidable without a WAL or
  distributed coordinator).
- Crash after commit: tg_msg_id != 0, escalate_at NULL → next tick skips via existence
  check. ✓
- Normal failure (network error): escalate_at stays set → retry. ✓
- Normal success: both columns committed, no retry. ✓

**Property achieved:** exactly one escalation message in all crash-free paths;
a transient failure is retried; no crash point inserts a second child row.

**Tests added:**
- `test_escalation_send_failure_leaves_escalate_at_set`: FailingBackend raises on every
  send — after tick, `escalate_at` is still set and child has `tg_msg_id=0`.
- `test_escalation_retry_on_second_tick_succeeds`: FirstFailBackend raises on the first
  send, succeeds on the second — after two ticks: exactly one child row, exactly one
  Telegram message, `escalate_at` cleared.
- `test_escalation_no_second_send_after_crash_simulation`: child pre-inserted with
  real `tg_msg_id=9999` but `escalate_at` still set (simulated crash) — after tick,
  `escalate_at` cleared, zero sends, still one child row.

---

## Issue 4 (LOW) — Dead `answer_waiters` parameter removed

**File:** `relay-server/relay_server/reaper.py`

- `_escalation_pass` signature: removed `answer_waiters: ConditionWaiterRegistry | None`.
  Updated docstring to explain the idempotency and retry design.
- `reaper_tick` signature: removed `answer_waiters: ConditionWaiterRegistry | None = None`
  (nothing else in the reaper used it after the escalation-pass pass was fixed).
- `reaper_loop`: removed `answer_waiters=getattr(app.state, "answer_waiters", None)` from
  the `reaper_tick` call.
- Import: removed `ConditionWaiterRegistry` from `from .waiters import …` in reaper.py
  (now unused there).

**Callers updated:**
- `test_relay_nudge_escalation.py`: removed `answer_waiters=ConditionWaiterRegistry()`
  from 3 `reaper_tick` calls (lines 540, 630, 719 before edit); `ConditionWaiterRegistry`
  removed from the import.

`answer_waiters` stays in `app.py` everywhere it is actually used (the GET /v1/answers
long-polling path and `_record_answer`).

---

## Test gap closures

- **`test_duplicate_not_in_target_installation_feed`**: enhanced to use the shared DB,
  force-answer the child, and call `GET /v1/answers` as installation B; asserts 204 or
  empty list.
- **Expiry → child cancellation**: two new tests (`test_expiry_cancels_escalated_child`,
  `test_expiry_cancels_unsent_child`).
- **Idempotent escalation**: three new tests covering send failure, retry, and crash
  simulation.
- **Day-format verification**: seven tests in `TestParseDurationDaySupport`.

---

## Schema / Migration

**No schema or migration changes.** All changes are in Python logic only.  The migration
from v3/v4 to v5 that was tested against a real snapshot does not need to be re-run.

---

## Verification

```
python3 -m py_compile relay_server/availability.py relay_server/reaper.py \
  relay_server/app.py relay_server/db.py relay_server/models.py \
  tests/test_relay_nudge_escalation.py
→ ALL COMPILE OK

/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
→ 315 passed in 7.19s  (run 1)
→ 315 passed in 6.94s  (run 2, no flakiness)

python3 tests/run_all_tests.py
→ Ran 1030 tests — OK (skipped=1)
```

Previous relay baseline: 303 passed. New count: 315 (+12 new tests).
Top-level baseline: 1030 passed, 1 skipped — unchanged.

---

## Blockers

None.
