# Implementation Report - 23-01 Relay: Answer Feed + Non-Expiring Messages

## Summary

Added the `NEVER_EXPIRES` far-future sentinel (`9999-12-31T00:00:00Z`) to
`models.py` and wired it into message creation when `never_expires=True`; bumped
the schema to version 4 with the `messages_answer_feed` index; added the
`GET /v1/answers` endpoint with an installation-keyed waiter registry and a
matching `RelayClient.get_answers()` method; and added 18 new tests covering
every requirement in the task's Tests section.

## Files Created

- `relay-server/tests/test_answer_feed.py` — 18 tests: never_expires sentinel
  vs reaper, ttl_sec validation regression, feed ordering/paging/cap,
  installation scoping with shared chat, waiter-wake latency,
  EXPLAIN QUERY PLAN for `messages_answer_feed`, v3→v4 migration forward,
  response shape (button vs text-reply), 204 on empty feed, full replay,
  regression floor for existing flows, sentinel not leaked to normal messages.

## Files Modified

- `relay-server/relay_server/models.py` — Added `NEVER_EXPIRES: str =
  "9999-12-31T00:00:00Z"` module-level constant; added `never_expires: bool =
  False` field to `CreateMessageRequest` (existing `ttl_sec` is still required
  and capped at 24 h, no change).

- `relay-server/relay_server/db.py` — `SCHEMA_VERSION` bumped from 3 to 4;
  `SCHEMA_SQL` gets `CREATE INDEX IF NOT EXISTS messages_answer_feed ON
  messages(telegram_chat_id, state, id)`; `MIGRATIONS[4]` adds the same index as
  the only migration statement (indexes only, no table rebuild).

- `relay-server/relay_server/app.py` —
  - Import `NEVER_EXPIRES` from models.
  - `create_message`: when `body.never_expires`, set `expires_at_str =
    NEVER_EXPIRES`; otherwise compute TTL as before.
  - Lifespan: add `app.state.answer_waiters = WaiterRegistry()` (second
    registry, keyed by installation_id).
  - `_record_answer`: gained `answer_waiters: WaiterRegistry` and
    `installation_id: int` parameters; calls `answer_waiters.notify(installation_id)`
    immediately after the existing `waiters.notify(message_id)`.
  - `_apply_text_answer`: gained `answer_waiters: WaiterRegistry` parameter;
    passes it and `row["installation_id"]` to `_record_answer`.
  - `_handle_callback_query`: gained `answer_waiters: WaiterRegistry` parameter;
    passes it and `row["installation_id"]` to `_record_answer`.
  - `_handle_update` (webhook): reads `app.state.answer_waiters`; threads it
    through all three `_apply_text_answer` and `_handle_callback_query` call sites.
  - New `GET /v1/answers` endpoint with `_fetch_answers` closure and
    `_format_answer_rows` helper.
  - `_format_answer_rows` helper extracts `answer_text`, `via`, `option_idx`
    from `answer_json`.

- `relay-server/relay_server/client.py` — Added `RelayClient.get_answers(after,
  wait)` following `wait_for_answer`'s shape: 204 → empty list, raises on 4xx,
  carries the `wait + 10 s` read-timeout slack.

- `relay-server/tests/test_schema.py` — Updated two tests that hard-coded version
  3 to reflect version 4 (`test_v3_version_stamp`, `test_v2_migrates_to_v3_schema`);
  the latter also asserts `messages_answer_feed` is present after migration.

## Verification

- **Compile/import:** PASS — `python3 -m py_compile relay_server/models.py
  relay_server/db.py relay_server/app.py relay_server/client.py
  tests/test_schema.py tests/test_answer_feed.py` all exit 0.

- **Relay-server suite:** 270 passed, 0 failed —
  `/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q`
  (252 baseline + 18 new; the 2 schema-version tests that previously asserted
  version 3 were updated to assert version 4, which is a correct update, not a
  failure).

- **Top-level suite:** 1030 passed, 0 failed, 1 skipped —
  `python3 tests/run_all_tests.py` (unchanged from baseline; this suite does not
  cover relay-server code).

- **install-claude-config.sh re-run:** Not applicable. This task is entirely
  relay-server-side; no installed hook was modified.

## Decisions

**`waiters.py` not modified.** `WaiterRegistry` is already a generic int-keyed
registry. The task requires "a second registry keyed by `installation_id`…add a
second registry rather than overloading the first." I did exactly that: two
separate `WaiterRegistry()` instances — `app.state.waiters` (message_id-keyed,
unchanged) and `app.state.answer_waiters` (installation_id-keyed, new). No new
class was needed; the class is generic in its key type. The task's "do not
overload the first" constraint is met by using a completely separate instance.

**`answer_waiters` threaded through function signatures rather than read from
`app.state`.** `_record_answer` and friends are module-level async functions, not
methods, and receive the connection and message waiters as parameters (existing
pattern). Keeping `answer_waiters` in the same parameter position is consistent
with the established pattern and avoids coupling internal helpers to FastAPI app
state. The blast radius is small: `_record_answer` (+2 params), `_apply_text_answer`
(+1 param), `_handle_callback_query` (+1 param), and the webhook dispatcher.

**Grouped-message finalization (`_finalize_group_if_complete`) does not notify
`answer_waiters`.** The task says "wake it from the same place `_record_answer`
already wakes the message waiter." Group finalization bypasses `_record_answer`
and goes through a separate UPDATE path. Async questions (the consumer of the
feed) use non-grouped messages; the gap exists but is out of scope for 23-01.

**`NEVER_EXPIRES` lives in `models.py`** alongside `CreateMessageRequest` (the
only model that uses it at ingest time). This keeps the sentinel visible next to
the field that selects it and avoids a separate constants module.

**Feed query uses `telegram_chat_id` as the leading index key, not
`installation_id`.** Architecture §2.4 specifies `messages_answer_feed
(telegram_chat_id, state, id)`. For a given installation, `telegram_chat_id` is
known from the auth record, so we pass it explicitly. SQLite seeks efficiently on
`telegram_chat_id=X AND state='answered' AND id>N` and applies `installation_id`
as a residual filter; `EXPLAIN QUERY PLAN` in the test confirms no table scan.

## Blockers

**Real-DB forward-migration check not performed.** The task says: "Migration
(indexes only — no table rebuild) applies cleanly forward on a copy of a real
relay DB." The production SQLite file is at `ssh anton@h02.activecdn.net
/var/lib/relay/relay.db`. This environment has no SSH access to that host.
The equivalent test against a locally-constructed v3 database passes
(`test_v3_migrates_to_v4_cleanly`), but the real-DB run is a BLOCKER — it cannot
be substituted by a mocked unit test per the implementer rules.

**What is needed to resolve:** SSH access (or a copy of `/var/lib/relay/relay.db`
delivered here) so the migration can be run against an actual production-sized
database.

## Questions for User

None — all implementation choices are captured in Decisions above. The real-DB
migration check is recorded as a Blocker.
