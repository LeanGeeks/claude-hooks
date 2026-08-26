# Review Report - 23-01 Relay: Answer Feed + Non-Expiring Messages

## Verdict: FAIL

One HIGH issue: the `WaiterRegistry` event is permanently set after the first
`notify()`, so after any installation ever receives an answer, all subsequent
long-poll `wait()` calls for that installation return immediately (0 ms) instead
of blocking for the requested `wait` seconds. The long-poll degenerates to
tight polling.

---

## Completeness Check

### Scope 1: Non-expiring messages via sentinel

**Met.**

- `NEVER_EXPIRES = "9999-12-31T00:00:00Z"` added as module-level constant in
  `relay-server/relay_server/models.py:15`. Single site. ✓
- `never_expires: bool = False` added to `CreateMessageRequest` at
  `models.py:34`. ✓
- `ttl_sec` still has no default (`Field(gt=0, le=24*3600)`); existing senders
  are untouched. ✓
- `create_message` computes `expires_at_str = NEVER_EXPIRES if body.never_expires
  else (now + timedelta(seconds=body.ttl_sec)).isoformat()` at `app.py:629–651`. ✓
- `reaper.py:316` SQL `WHERE state='open' AND expires_at < ?` never matches
  `"9999-12-31T00:00:00Z"` given a real `now`. ✓
- Four `expires_at` readers verified unchanged: `app.py:519-527` and
  `app.py:2262-2268` read `binding_codes.expires_at` (not `messages`);
  `reaper.py:316` is a SQL predicate; `client_cli.py:163` reads the binding
  status API response. None parses `messages.expires_at` in Python and none
  gained `None` handling. ✓
- Test coverage: `test_never_expires_survives_reaper_ticks`, `test_ttl_sec_still_required`,
  `test_ttl_sec_still_capped_at_24h`, `test_never_expires_still_requires_ttl_sec`,
  `test_normal_message_does_not_use_sentinel`. ✓

### Scope 2: GET /v1/answers endpoint

**Partially met** — the endpoint is correct and installation-scoped, but
the long-poll behavior is broken after the first answer (see Issue 1).

- Endpoint exists at `app.py:987`, installation-authenticated. ✓
- Returns rows with `id > after`, `ORDER BY id ASC`, `LIMIT 500`. ✓
- `via` and `option_idx` extracted from existing `answer_json` format; `kind`
  included. Architecture §2.4 field set satisfied. ✓
- 204 on empty feed / timeout. ✓
- `after=0` returns full replay. ✓
- Installation scoping: SQL has `AND installation_id = ?` at `app.py:1003`.
  Test `test_installation_scoping_shared_chat` proves two installations sharing
  `chat_id=99` see only their own rows (two real IDs cross-checked, not vacuous).
  ✓
- Second `WaiterRegistry()` instance (`answer_waiters`) at `app.py:133`. Not
  modifying `waiters.py`. Satisfies "second registry rather than overloading
  the first". ✓
- **Broken**: after the first answer, `answer_waiters.wait(installation_id, N)`
  returns True in 0 ms (confirmed by script). See Issue 1.

Index `messages_answer_feed (telegram_chat_id, state, id)`:
- Added in `SCHEMA_SQL` at `db.py:63`. ✓
- Added in `MIGRATIONS[4]` at `db.py:141`. ✓
- `EXPLAIN QUERY PLAN` confirms `SEARCH messages USING INDEX messages_answer_feed
  (telegram_chat_id=? AND state=? AND id>?)`. ✓

### Scope 3: RelayClient.get_answers

**Met.**

- `client.py:277` — `get_answers(after=0, wait=0)`, same pattern as
  `wait_for_answer`: `timeout=httpx.Timeout(wait + 10.0, connect=5.0)`, 204
  returns `[]`, raises on 4xx. ✓

### Migration slot (state.md migration numbering)

**Met.** `SCHEMA_VERSION = 4` at `db.py:18`; adds indexes only, no table
rebuild. 23-02 retains slot 5. ✓

### Compile / import

All six changed/new files compile clean:
`python3 -m py_compile relay_server/models.py relay_server/db.py relay_server/app.py
relay_server/client.py tests/test_schema.py tests/test_answer_feed.py` — exit 0. ✓

### Tests

- `/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q`:
  **270 passed, 0 failed** (252 baseline + 18 new). ✓
- `python3 tests/run_all_tests.py`: **1030 passed, 0 failed, 1 skipped** (unchanged). ✓

---

## Issues Found

### Issue 1: HIGH — WaiterRegistry event stays set permanently; long-poll never parks after first answer

- **File:** `relay-server/relay_server/app.py:1033` (the `answer_waiters.wait()` call),
  `relay-server/relay_server/waiters.py:36–38`
- **Problem:** `WaiterRegistry.notify(installation_id)` calls
  `asyncio.Event.set()`, which is permanent. Subsequent
  `WaiterRegistry.wait(installation_id, timeout)` checks
  `event.is_set()` first and returns `True` in 0 ms without ever
  awaiting. Confirmed by a Python script against the installed venv:
  elapsed = 0.000 s with a 5-second timeout.

  Consequence: once any message from installation X is answered, every
  future `GET /v1/answers?after=<latest>&wait=25` for that installation
  immediately wakes from the waiter, re-fetches (finds nothing past the
  cursor), and returns `204` in < 1 ms. The endpoint never blocks for
  the requested `wait` seconds. The 23-05 listener, which relies on
  `wait=N` to throttle its poll rate, will tight-loop and hammer the
  server.

  The original message-keyed `waiters` has the same property but is not
  affected because message answers are acted on exactly once (no re-polling).
  The answer feed requires repeated long-polling with an advancing cursor,
  where the same-event must fire multiple independent times.

- **Fix:** Replace the `WaiterRegistry` for answer_waiters with a mechanism
  that supports multi-fire wake. The simplest correct approach is an
  `asyncio.Condition`: `notify()` does `async with cond: cond.notify_all()`;
  `wait()` does `async with cond: await asyncio.wait_for(cond.wait(), timeout)`.
  Each call to `wait()` blocks until the next `notify_all()` fires, regardless
  of past notifications. If reusing `WaiterRegistry`, the event must be
  cleared immediately before re-queuing a wait (but this requires caller-side
  protocol discipline and is race-prone under concurrent waiters).

### Issue 2: MEDIUM — `_finalize_group_if_complete` does not wake `answer_waiters`

- **File:** `relay-server/relay_server/app.py:1641`
- **Problem:** The group-finalization path calls `waiters.notify(int(m["id"]))` per
  member but never calls `answer_waiters.notify(installation_id)`. A grouped
  message whose all members are answered transitions to `state='answered'` and
  is therefore visible in the feed, but no feed long-poller is woken. The
  long-poller must wait for its full `wait` timeout before seeing the answer.
- **Severity justification:** The task says "wake it from the same place
  `_record_answer` already wakes the message waiter." For grouped messages that
  place is `_finalize_group_if_complete`, not `_record_answer`. However, the
  implementer's rationale is reasonable: async questions in this epic are
  non-grouped, so no in-scope path exercises the gap. The answer is not lost
  — it is visible after the next timeout expiry. Marking MEDIUM (not HIGH)
  because no data is lost and the primary use case is unaffected. However, any
  caller that creates a `never_expires=True` grouped message will experience
  delayed feed notification with no indication that grouping is the cause.
- **Fix:** Pass `answer_waiters` and `installation_id` through
  `_finalize_group_if_complete`, and call
  `answer_waiters.notify(installation_id)` alongside each
  `waiters.notify(m["id"])` in the per-member loop.

### Issue 3: LOW — `answer_waiters` events accumulate without bound within a process lifetime

- **File:** `relay-server/relay_server/waiters.py:19` (defaultdict),
  `relay-server/relay_server/app.py:1251`
- **Problem:** `WaiterRegistry._events` is a `defaultdict(asyncio.Event)`. Every
  `installation_id` ever passed to `notify()` or `wait()` adds a permanent entry.
  No `clear()` call exists for `answer_waiters`. The original `waiters` registry
  has the same issue, so this is not new behaviour. With bounded installations
  (hundreds at most) the growth is negligible, but it is a minor leak.
- **Fix:** Call `answer_waiters.clear(installation_id)` after the re-fetch in the
  endpoint (or after a `notify()` is consumed). This is the same fix that should
  eventually be applied to the message-keyed `waiters` as well.

---

## Code Quality Notes

**Test quality:** 18 tests exercise real code through the HTTP layer using
`FakeTelegramBackend`; no mocking of the units under test. The installation
scoping test uses two real installation IDs and cross-checks both directions —
it cannot pass vacuously. The `EXPLAIN QUERY PLAN` test asserts the index name
appears in the plan detail, which would fail if the index were not used (the
detail would be `SCAN messages`). The waiter wake test (`test_waiter_wakes_on_answer`)
correctly proves first-wake latency < 5 s but does not cover the degenerate
second-wake behavior — this is how Issue 1 went undetected.

**`_record_answer` blast radius:** Only two call sites exist (`_apply_text_answer`
at `app.py:2563` and `_handle_callback_query` at `app.py:2663`). Both were
updated in the diff. The `_finalize_group_if_complete` path does not call
`_record_answer` and was correctly left alone. No call site in reaper.py or
tests. The positional ordering of the new parameters (`answer_waiters`,
`installation_id` immediately after `waiters`) is consistent at both call sites. ✓

**Invariant 9 (regression floor):** `never_expires: bool = False` defaults to
False; every existing sender omits it and gets identical behavior. The test
`test_existing_message_flow_unaffected` confirms end-to-end equivalence. ✓

**`_format_answer_rows` helper:** Handles `answer_json = NULL` (returns `{}`) and
correctly extracts `label` (button) or `text` (free-text reply) as `answer_text`.
The helper lives as a module-level function rather than inside the closure, which
is testable (tests exercise it indirectly through the endpoint). ✓

**Open blocker (inherited, not a review finding):** The migration was not tested
against a copy of the production SQLite file at `ssh anton@h02.activecdn.net
/var/lib/relay/relay.db`. The locally-constructed v3→v4 migration test
(`test_v3_migrates_to_v4_cleanly`) is real and correct, and the migration only
adds an index so the risk of a production-DB failure is very low. Nonetheless
this remains an open blocker owned by the manager per the task's known-accepted
limitation.

---

## Questions for User

None that require user input. The two open points are:

1. Whether the tight-polling consequence of Issue 1 is acceptable for the 23-05
   listener's design (it is not — the `wait` parameter exists specifically to
   avoid tight-polling; the fix should land in 23-01 before 23-05 is built).

2. Whether Issue 2 (grouped-message feed latency) should be fixed in 23-01 or
   deferred to a follow-up. The implementer's "out of scope" call is defensible
   given the epic's stated use case, but the fix is small.
