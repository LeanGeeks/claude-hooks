# Fix Report — 23-01 Relay: Answer Feed + Non-Expiring Messages

## Issues Fixed

### Issue 1 (HIGH) — Multi-fire waiter: `ConditionWaiterRegistry`

**Files changed:**
- `relay-server/relay_server/waiters.py` — new `ConditionWaiterRegistry` class
  added below the unchanged `WaiterRegistry`.
- `relay-server/relay_server/app.py:71` — import updated to
  `from .waiters import ConditionWaiterRegistry, WaiterRegistry`.
- `relay-server/relay_server/app.py:130-135` — `app.state.answer_waiters`
  changed from `WaiterRegistry()` to `ConditionWaiterRegistry()`.
- `relay-server/relay_server/app.py:1009` — type annotation in `get_answers`
  changed from `WaiterRegistry` to `ConditionWaiterRegistry`.
- `relay-server/relay_server/app.py:1034-1055` — endpoint long-poll path
  restructured to use the race-free condition pattern (see Design below).
- `relay-server/relay_server/app.py:1209` — `answer_waiters` type annotation
  in `_record_answer` changed from `WaiterRegistry` to `ConditionWaiterRegistry`.
- `relay-server/relay_server/app.py:1264` — `answer_waiters.notify(installation_id)`
  changed to `await answer_waiters.notify(installation_id)` (now async).
- `relay-server/relay_server/app.py:2572,2600` — `answer_waiters` type
  annotations in `_apply_text_answer` and `_handle_callback_query` updated.

**`WaiterRegistry` is untouched** — its Event-based semantics, key lifecycle,
and all callers are unchanged.

#### Design: `ConditionWaiterRegistry`

The registry holds a `defaultdict(asyncio.Condition)` keyed by `installation_id`.

`notify(key)` is `async`: it does `async with cond: cond.notify_all()`.
Holding the lock while notifying is what closes the race (see below).

The endpoint no longer calls a `wait()` method on the registry. Instead it
calls `get_condition(installation_id)` and uses the condition directly:

```
# Fast path (no lock needed)
rows = await run_in_thread(_fetch_answers)
if rows: return rows

if wait <= 0: return 204

# Race-free long-poll
cond = answer_waiters.get_condition(installation_id)
async with cond:
    rows = await run_in_thread(_fetch_answers)   # second check under lock
    if rows: return rows
    try:
        await asyncio.wait_for(cond.wait(), timeout=float(wait))
    except asyncio.TimeoutError:
        pass
    rows = await run_in_thread(_fetch_answers)   # final check after wake
    if rows: return rows

return 204
```

**Why the race is closed:** `notify()` must acquire the condition lock before
calling `notify_all()`. Once the endpoint holds the condition lock (via
`async with cond:`), any concurrent `notify()` blocks until after `cond.wait()`
has registered the caller's future. There are only two interleavings:
- Notify fires *before* we acquire the lock → we see the row in the second
  DB check and return without parking at all.
- Notify fires *while we hold the lock* → it blocks until `cond.wait()`
  releases the lock and registers our future, then wakes us immediately.

Neither interleaving results in a missed wake or a full-timeout sleep.

**Why `asyncio.wait_for(cond.wait(), timeout)` is safe:** when `wait_for`
times out and cancels the inner coroutine, `asyncio.Condition.wait()`'s
`finally` block re-acquires the lock before propagating `CancelledError`
(which `wait_for` converts to `TimeoutError`). So after the `except
asyncio.TimeoutError`, the caller still holds the lock and the `async with
cond:` block exits cleanly.

#### Issue 3 (LOW) — Cleanup for the new registry

Cleanup is deliberately omitted from `ConditionWaiterRegistry`. The safe
window for removing a condition would be while holding the lock with no
parked waiters — but there is no cheap way to know that without reference
counting. Any cleanup that removes the condition between a caller's second
DB check and its `cond.wait()` call would create a new condition on the
next access, and a racing `notify()` on the old condition would not wake
the caller. With bounded installations (hundreds at most) the steady-state
size of `_conditions` is negligible. Correctness is preferred over tidiness.

The existing `WaiterRegistry` key lifecycle is untouched per the constraint.

---

### Issue 2 (MEDIUM) — Grouped-message finalization wakes `answer_waiters`

**Files changed:**
- `relay-server/relay_server/app.py:1587-1668` — `_finalize_group_if_complete`
  gained `answer_waiters: ConditionWaiterRegistry` as its fourth parameter.
  After the per-member `waiters.notify()` loop, it now collects the unique
  `installation_id` values from the re-loaded members and calls
  `await answer_waiters.notify(inst_id)` for each.
- `relay-server/relay_server/app.py:2357-2369` — `_handle_multi_select_button`
  gained `answer_waiters` param; passes it to `_finalize_group_if_complete`.
- `relay-server/relay_server/app.py:2462-2480` — `_handle_grouped_button`
  gained `answer_waiters` param; passes it to `_handle_multi_select_button`
  and `_finalize_group_if_complete`.
- `relay-server/relay_server/app.py:2529-2536` — `_handle_grouped_reply`
  gained `answer_waiters` param; passes it to `_finalize_group_if_complete`.
- `relay-server/relay_server/app.py:2568-2582` — `_apply_text_answer` now
  passes `answer_waiters` to `_handle_grouped_reply`.
- `relay-server/relay_server/app.py:2662-2673` — `_handle_callback_query`
  now passes `answer_waiters` to `_handle_grouped_button`.

The de-duplication (`{int(m["installation_id"]) for m in members}`) means
only one `notify()` per installation regardless of group size — correct
because all group members belong to the same installation in practice,
and harmless (one extra `notify_all()`) if they ever don't.

---

### Issue 3 (LOW)

Handled by design choice — see end of Issue 1 section above.

---

## New Tests Added

**`relay-server/tests/test_answer_feed.py`** — two new tests appended:

**Test 16 — `test_second_long_poll_still_parks`** (regression for Issue 1):
Creates and answers one message, consumes the feed response, then issues a
second long-poll with `after=<latest_id>&wait=2`. Asserts the response is
204 (nothing new) AND that elapsed time ≥ 1.6 s (80% of the 2 s wait).
With the old `WaiterRegistry`, elapsed was < 1 ms. With `ConditionWaiterRegistry`
it parks for the full duration.

**Test 17 — `test_grouped_message_wakes_feed_poller`** (regression for Issue 2):
Creates a two-member grouped question, answers the first member (group still
incomplete, no finalization), then starts a 10-second long-poll, then answers
the second member in a concurrent task. Asserts the feed wakes within 5 s and
returns both group members, and that elapsed < 5 s (well under the 10 s cap).

---

## Verification

```
python3 -m py_compile relay_server/waiters.py relay_server/app.py \
    relay_server/client.py relay_server/db.py relay_server/models.py \
    tests/test_schema.py tests/test_answer_feed.py
```
**PASS** — exit 0.

```
/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
```
**272 passed** (270 pre-fix baseline + 2 new regression tests).

```
python3 tests/run_all_tests.py
```
**1030 passed, 0 failed, 1 skipped** — unchanged from baseline.

---

## Blockers

None.
