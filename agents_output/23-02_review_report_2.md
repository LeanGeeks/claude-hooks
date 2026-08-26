# Review Report — 23-02: Relay per-message nudges + escalation (Pass 2)

## Verdict: PASS

All four issues from pass 1 are resolved. No new problems were introduced.

---

## Compile / Import

```
python3 -m py_compile relay_server/availability.py relay_server/reaper.py \
  relay_server/app.py relay_server/db.py relay_server/models.py \
  tests/test_relay_nudge_escalation.py
→ ALL COMPILE OK
```

Both `reaper` and `app` import cleanly.

---

## Test counts

| Suite | Claimed | Actual run 1 | Actual run 2 |
|---|---|---|---|
| `pytest relay-server/tests/ -q` | 315 passed | 315 passed (7.00 s) | 315 passed (7.13 s) |
| `python3 tests/run_all_tests.py` | 1030 passed, 1 skipped | 1030 passed, 1 skipped | — |

No flakiness observed.

---

## Schema / Migration

**Unchanged by the fix pass.** The diff from `f83fc3e` to the current uncommitted tree shows the same four `ALTER TABLE ADD COLUMN` statements and one `CREATE INDEX` under migration slot 5 that were verified in pass 1. The fixer added no SQL.

---

## Per-issue rulings

### Issue 1 (HIGH) — `parse_duration` day support → **Resolved**

- **File:** `relay-server/relay_server/availability.py`
- `_DURATION_RE` is now `r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$"` — correct group
  order (d, h, m), all optional, the all-None / all-zero guard updated.
- `parse_duration('1d')`, `'2d12h'`, `'1d6h30m'` all return the correct timedelta.
  `parse_duration('0d')` returns `None` (zero-length guard: `if days == 0 and hours == 0
  and minutes == 0: return None`). Verified by direct execution.
- `parse_nudge_schedule_with_repeat('4h,1d,3d,7d*', 10)` parses without error; returns
  `([4h, 1d, 3d, 7d], True)`.
- Tests updated to use spec literals `1d`/`3d`/`7d` (not `24h`/`72h`/`168h`). The
  Done-when clause ("still nudges in week three") is now demonstrated by
  `test_week_three_nudge_still_fires`: calls `_compute_next_nudge_due` with
  `nudge_count=15` on a `7d*` schedule and asserts `result == now + timedelta(days=7)`.
- Previously valid forms (`15m`, `3h`, `2h30m`) still parse correctly (confirmed).
- The epic-19 path sharing `parse_nudge_schedule` / `parse_nudge_schedule_with_repeat`
  inherits day support additively — no breakage possible, only wider acceptance.
- Seven new tests in `TestParseDurationDaySupport` cover all the above.

---

### Issue 2 (Architecture deviation — accepted) → **Unchanged, no action required**

`parent_message_id` column accepted in pass 1. Fixer left it in place as requested.

---

### Issue 3A (MEDIUM) — Idempotent escalation → **Resolved**

- **File:** `relay-server/relay_server/reaper.py` (`_escalation_pass`)

**Transaction atomicity.** `_commit_success` wraps both `UPDATE` statements in a single
`with conn:` block. Python's sqlite3 context manager issues `BEGIN … COMMIT` (or
`ROLLBACK`), so both the child's `telegram_message_id` update and the parent's
`escalate_at = NULL` are atomic. The connection uses `isolation_level` default (not
`None`), so implicit transaction management is in effect. The claim holds.

**Crash-safety analysis verified:**

| Crash point | Outcome | Duplicate child? |
|---|---|---|
| Before `_insert_child` | `tg_msg_id=0` absent, `escalate_at` set → retry inserts child | No |
| After `_insert_child`, before send | Child exists with `tg_msg_id=0`, `escalate_at` set → next tick retries via existing child row | No |
| After send, before `_commit_success` commit | `tg_msg_id` stays 0, `escalate_at` set → next tick finds child with `tg_msg_id=0`, retries send | No (one extra TG message possible) |
| After `_commit_success` commit | `tg_msg_id != 0`, `escalate_at = NULL` → existence check suppresses all further sends | No |
| Normal failure (network error) | `escalate_at` not cleared, child exists with `tg_msg_id=0` → retry on next tick | No |

The fixer's residual-window admission (one extra Telegram message on crash-after-send)
is correct and is the only such window. No crash point can produce a second child row.

**Could a cancelled child trigger a spurious new escalation?** The existence check
queries `state = 'open'` children. A child is cancelled only when the parent becomes
terminal (answered or expired). Since `_fetch_due` requires `parent.state = 'open'`,
a parent with a cancelled child is no longer selected. The spurious-escalation scenario
cannot occur in normal code paths.

**`_fetch_due` filters `state = 'open'` on the parent.** Confirmed:
`WHERE state = 'open' AND escalate_at IS NOT NULL AND escalate_at < ?`.

**Tests:** Three new tests cover send failure, retry-on-second-tick (exactly one child
row, exactly one Telegram message), and crash simulation (existing child with real
tg_msg_id, no second send). All are meaningful — state starts as 'open' and assertions
verify state transitions, not initial values.

---

### Issue 3B (MEDIUM) — Expiry cancels escalated children → **Resolved**

- **File:** `relay-server/relay_server/reaper.py` (`reaper_tick`, expiry pass)
- `cancel_escalated_children(conn, backend, message_id)` is called after `_mark_expired`
  succeeds (`updated != 0`). It is wrapped in `try/except` so a failure does not block
  the expiry path or the subsequent Telegram edits. Placement confirmed at line 513.
- The `tg_msg_id > 0` guard inside `cancel_escalated_children` prevents an
  `edit_message` call for a child whose send never completed — confirmed by reading
  the function body (lines 334–413).
- The `try/except` on `cancel_escalated_children` at line 509–518 is a broad `Exception`
  catch that logs and continues. It does not swallow errors that should stop the expiry
  — the expiry state transition (`_mark_expired`) has already committed before this
  call. There is no error from the cancellation step that should have stopped the expiry.
- **Tests are meaningful:** `test_expiry_cancels_escalated_child` inserts the child via
  `_insert_message` with default `state='open'`, then asserts `state == 'cancelled'`
  after the tick. The parent is not selected by the escalation pass (no `escalate_at`
  set on the parent), so the only mechanism that could cancel the child is the expiry
  pass calling `cancel_escalated_children`. The assertion is therefore non-vacuous.
- `test_expiry_cancels_unsent_child` additionally asserts no `edit_message` call with
  `telegram_message_id=0` is made. Both assertions are correct and sufficient.

---

### Issue 4 (LOW) — Dead `answer_waiters` parameter removed → **Resolved**

- `_escalation_pass`: `answer_waiters` parameter absent from signature. Confirmed.
- `reaper_tick`: `answer_waiters: ConditionWaiterRegistry | None = None` absent from
  signature. Confirmed. `grep -n "answer_waiters\|ConditionWaiterRegistry" reaper.py`
  returns no output.
- `reaper_loop`: call to `reaper_tick` no longer passes `answer_waiters`. Confirmed at
  line 1317 area.
- **All `reaper_tick` callers updated:** `test_relay_nudge_escalation.py`,
  `test_reaper.py`, `test_webhook.py`, `test_long_poll.py`, `test_answer_feed.py`
  — none pass `answer_waiters`. Verified by grep.
- `ConditionWaiterRegistry` remains in `app.py` where it is legitimately needed (the
  GET /v1/answers long-polling path and `_record_answer`). Confirmed.

**Note for 23-05 (not an issue):** The escalation answer flow is: escalated copy
tapped → `_handle_callback_query` in `app.py` → routes to `_record_answer` on the
original's `installation_id` → `answer_waiters.notify(original_installation_id)`. This
means the answer-feed wake already happens via `app.py`'s webhook path, not the reaper.
If a future task requires the reaper to independently wake the feed (e.g., a
reaper-side answer recording path), `answer_waiters` would need to be re-threaded into
`reaper_tick`. For 23-02 alone, removal is correct.

---

## Cross-Module Move — Additional Analysis (Section 1 of Scope)

The fixer moved `_cancel_escalated_children` from `app.py` (implementer's placement)
into `reaper.py` as the exported `cancel_escalated_children`, then re-imported it into
`app.py` under the old private name.

**Import direction:** `app.py` imports from `reaper.py`. `reaper.py` does not import
from `app.py`. The direction is unchanged from the rest of the codebase — no cycle.
Confirmed: `grep "from .app import\|import app" relay_server/reaper.py` returns nothing.

**Behavioural equivalence:** The function in `reaper.py` (lines 334–413) is equivalent
to the implementer's version, with the deliberate addition of the `tg_msg_id > 0`
guard. No `except` branches were dropped, no argument order changed, no transaction
boundary altered. The `delete_nudge` call is inside the `tg_msg_id > 0` block, which
is correct: a child that was never sent has no live nudge to delete.

**Call sites in `app.py`:** Four call sites — lines 2692, 2718, 2823, 2859 — all use
`_cancel_escalated_children` (the alias). All are correct.

**Stale code in `app.py`:** No stale helper, no unused import, no dangling docstring
reference left behind. `ConditionWaiterRegistry` is still used in `app.py`'s own paths.
`NEVER_EXPIRES` was correctly added to `reaper.py`'s imports (needed for the child
row insert) and is exported from `models.py`.

---

## Regression Sweep

- **Invariant 9:** All four new columns are nullable with no `NOT NULL` or default;
  existing rows carry `NULL`. Migration via `ALTER TABLE ADD COLUMN` (no rebuild).
  Confirmed unchanged from pass 1 verification.
- **Invariant 4 (no role concept server-side):** `_escalation_pass` resolves token
  hash → installation only. No role information read or stored. Unchanged.
- **Nudge pass:** The `nudge_enabled = 0` precedence logic was reviewed in pass 1 and
  the fixer did not modify it. The fix-pass diff touches only the four areas above.
- **Existing tests:** 303-test relay baseline extended to 315 (+12). Top-level
  1030-passed baseline unchanged.

---

## Code Quality Notes

- **`_row_is_capped` double-call:** The nudge pass calls `_row_is_capped` twice per
  row (once for the `capped` filter, once for the `chat_rows` filter), which calls
  `parse_nudge_schedule_with_repeat` twice. No correctness issue at current volumes;
  pre-existing from the implementer's work, not introduced by the fixer.
- **`_load_open_message_any` naming:** The name implies state='open' but the function
  loads any state. The docstring clarifies "without an installation filter" (not a
  state filter). Pre-existing; callers correctly check `parent["state"] == "open"`.

---

## Questions for User

None.
