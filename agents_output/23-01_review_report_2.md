# Review Report - 23-01 Relay: Answer Feed + Non-Expiring Messages (Pass 2)

## Verdict: PASS

No BLOCKER or HIGH issues remain. One new LOW finding (wrong type annotation
in `_handle_update`). All pass-1 issues are resolved or accepted-and-closed.

---

## Scope

This is a scoped second pass targeting the fix diff only. Pass-1 completeness
checks (sentinel, migration slot, endpoint shape, client method, installation
scoping, regression floor) are not repeated; they were verified in pass 1 and
are unaffected by the fix.

---

## Pass-1 Issues: Status

### Issue 1 (HIGH) — WaiterRegistry permanently-set event → RESOLVED

**What the fixer did:** Added `ConditionWaiterRegistry` in `waiters.py` using
`asyncio.Condition`, left `WaiterRegistry` untouched. Changed
`app.state.answer_waiters` to a `ConditionWaiterRegistry` instance. Restructured
the endpoint to fast-path DB check → acquire condition lock → second DB check →
`asyncio.wait_for(cond.wait(), timeout)` → final DB check.

**Concurrency reasoning — verified:**

_Can an answer between the fast-path and lock acquisition be missed?_ No. The
second DB check runs while the condition lock is held. `notify()` is async and
must acquire the same lock before calling `notify_all()`. There are exactly two
interleavings: (a) notify fires before the endpoint holds the lock — the second
DB check sees the committed row and returns without parking; (b) notify arrives
after the endpoint holds the lock — it blocks on `async with cond:` until
`cond.wait()` releases the lock and registers the caller's future, then fires
immediately. Neither misses the wake.

_Are all `notify()` call sites on `answer_waiters` properly awaited?_ Grep:
- `app.py:1264` `await answer_waiters.notify(installation_id)` in `_record_answer` ✓
- `app.py:1660` `await answer_waiters.notify(inst_id)` in
  `_finalize_group_if_complete` ✓

No un-awaited `notify(` call exists. A missing `await` would yield an unawaited
coroutine that silently does nothing; both sites are correct.

_`asyncio.wait_for(cond.wait(), timeout)` — is re-acquire after TimeoutError
safe?_ Verified against CPython 3.14.4 source (the venv Python version).
`Condition.wait()` releases the lock and then, in a `try/finally`, re-acquires
the lock before propagating any exception — including `CancelledError` injected
by `wait_for`. The `finally` block loops `await self.acquire()` catching each
`CancelledError` until the acquire succeeds, then re-raises the saved exception.
`wait_for` converts that `CancelledError` to `TimeoutError`. After the
`except asyncio.TimeoutError: pass` at `app.py:1048`, the condition lock is held
and `async with cond:` exits cleanly. The fixer's claim is correct.

_Lock-ordering inversion between condition lock and `_conn_lock`?_ No deadlock
is possible. `_conn_lock` is a `threading.Lock` (in `db.py:234`) acquired inside
threadpool workers dispatched via `asyncio.to_thread`. The asyncio condition lock
is an event-loop primitive held by a coroutine. While the endpoint holds the
condition lock and awaits `run_in_thread(_fetch_answers)`, the event loop is free
to schedule other coroutines — including `_record_answer` trying `await
answer_waiters.notify()`, which parks waiting for the condition lock (an asyncio
park, non-blocking for the event loop). The threadpool worker for `_fetch_answers`
acquires and releases `_conn_lock` independently. After that worker returns,
`_record_answer`'s write worker can acquire `_conn_lock`. When `_record_answer`
finally calls `await answer_waiters.notify()`, it either (a) acquires the
condition lock after the endpoint has released it (via `cond.wait()`), or (b)
the endpoint resumes first (Worker A already returned its result), sees the row
from the second check or transitions to `cond.wait()`. In no ordering do both
an asyncio Condition lock and `_conn_lock` need to be simultaneously held by the
same party for the other party to proceed — there is no circular dependency.

**Regression test `test_second_long_poll_still_parks` (Test 16):** Creates and
answers one message, consumes the response, then issues a second long-poll with
`after=<latest_id>&wait=2`. Asserts `status=204` and `elapsed >= 1.6 s`. Against
the old `WaiterRegistry`, elapsed is < 1 ms (latched event returns immediately);
against `ConditionWaiterRegistry`, elapsed is ≈ 2 s. The assertion is robust:
it can only return early if a new answer arrives (none is sent), and the upper
bound is unbounded, so a slow machine doesn't flip it. Not flaky.

---

### Issue 2 (MEDIUM) — Grouped finalization doesn't wake answer_waiters → RESOLVED

**What the fixer did:** Threaded `answer_waiters: ConditionWaiterRegistry` as a
new fourth parameter through six functions: `_finalize_group_if_complete`,
`_handle_multi_select_button`, `_handle_grouped_button`, `_handle_grouped_reply`,
`_apply_text_answer`, `_handle_callback_query`. After each per-member
`waiters.notify()` loop in `_finalize_group_if_complete`, it collects unique
`installation_id` values from the reloaded members and calls `await
answer_waiters.notify(inst_id)` for each.

**Call-site audit — every call site of every changed function verified:**

`_handle_callback_query(conn, backend, waiters, answer_waiters, cbq)`:
- Call site at `app.py:2107` in `_handle_update` — passes `answer_waiters` ✓

`_apply_text_answer(conn, backend, waiters, answer_waiters, row, text, via)`:
- `app.py:2144` (direct-reply path) ✓
- `app.py:2157` (nudge-reply path) ✓
- `app.py:2221` (fallback path) ✓

`_handle_grouped_reply(conn, backend, waiters, answer_waiters, row, text)`:
- `app.py:2581` (called from `_apply_text_answer`) ✓

`_handle_grouped_button(conn, backend, waiters, answer_waiters, row, ...)`:
- `app.py:2662` (called from `_handle_callback_query`) ✓

`_handle_multi_select_button(conn, backend, waiters, answer_waiters, row, ...)`:
- `app.py:2477` (called from `_handle_grouped_button`) ✓

`_finalize_group_if_complete(conn, backend, waiters, answer_waiters, chat_id, ...)`:
- `app.py:2395` (from `_handle_multi_select_button`) ✓
- `app.py:2502` (from `_handle_grouped_button`) ✓
- `app.py:2549` (from `_handle_grouped_reply`) ✓

No call sites exist in `reaper.py` (grep confirmed empty) or in tests (tests
use the endpoint/webhook interface, not these internals). No positional slot
misalignment found in any of the above.

Invariant 9 (existing flows byte-identical absent `[questions]`): the new
parameter adds a wake after a group finalizes. For grouped messages the behavior
change is: feed long-pollers wake earlier (on finalization) instead of waiting
for timeout. That is the intent of the fix, not an unintended regression.

**Regression test `test_grouped_message_wakes_feed_poller` (Test 17):** Creates a
two-member grouped question, answers member 1 (incomplete → no finalization),
starts a 10-second long-poll, waits 100 ms, answers member 2 (finalization fires
`notify()`). Asserts: status 200, at least one group member in rows, elapsed < 5 s.
The test is resilient: if finalization fires before the poller parks in `cond.wait()`,
the second DB check (under the lock) catches the committed rows and returns anyway.
Without the fix, elapsed ≈ 10 s and `asyncio.wait_for(poll_task, timeout=5.0)`
raises `TimeoutError` — the test fails. ✓

---

### Issue 3 (LOW) — `answer_waiters` entries accumulate without bound → ACCEPTED AND CLOSED

The fixer deliberately omits cleanup from `ConditionWaiterRegistry` and explains
why: the only safe cleanup window is while holding the condition lock with zero
parked waiters. Determining that there are zero parked waiters requires reference
counting. Without it, removing a condition between the endpoint's second DB check
and its `cond.wait()` call would leave the caller parked on a stale object that
`notify()` on the new object will never wake. The reasoning is correct. With a
bounded number of installations (hundreds at most) the steady-state size is
negligible. This is the same condition as the existing `WaiterRegistry`, which
the task explicitly scoped to leave untouched. Accepted and closed.

---

## Issues Found (this pass)

### Issue N+1: LOW — Wrong type annotation for `answer_waiters` in `_handle_update`

- **File:** `relay-server/relay_server/app.py:2103`
- **Problem:** `answer_waiters: WaiterRegistry = app.state.answer_waiters` —
  the annotation is `WaiterRegistry` but the actual runtime object is a
  `ConditionWaiterRegistry`. No runtime impact: `_handle_update` only passes the
  value down to other functions that have the correct `ConditionWaiterRegistry`
  annotation. No caller within `_handle_update` invokes `.wait()` (a method that
  exists only on `WaiterRegistry` and not on `ConditionWaiterRegistry`). A static
  type checker would accept calls to `.wait()` on this variable without complaint,
  which would fail at runtime.
- **Fix:** Change line 2103 to
  `answer_waiters: ConditionWaiterRegistry = app.state.answer_waiters`.

---

## Test Results

```
relay-server/tests/ (run 1):  272 passed, 0 failed (6.86s)
relay-server/tests/ (run 2):  272 passed, 0 failed (6.76s)
tests/run_all_tests.py:       1030 passed, 0 failed, 1 skipped (32.1s)
```

The two new timing-sensitive tests (`test_second_long_poll_still_parks`,
`test_grouped_message_wakes_feed_poller`) passed identically across both runs.
No flakiness observed.

---

## Code Quality Notes

- The `ConditionWaiterRegistry` docstring is accurate and its usage pattern
  example matches the actual endpoint code.
- De-duplicating installation IDs in `_finalize_group_if_complete` via a set
  comprehension is correct: all group members belong to the same installation in
  the current architecture, and the code is safe even if they don't.
- The `_format_answer_rows` helper (pass-1 verified, unchanged in this pass)
  handles `answer_json = NULL` correctly.

## Questions for User

None.
