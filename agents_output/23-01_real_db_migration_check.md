# 23-01 — Real-DB forward-migration check (blocker resolution)

**Run by:** implementation manager · **Date:** 2026-08-26
**Authorization:** user chose "Copy the real DB down" when asked how to close the
23-01 blocker.

## Method

The production DB is not on the host filesystem — it lives in the Docker named
volume `relay-data` mounted at `/var/lib/relay` inside `relay-server-relay-1`
(`h02.activecdn.net`). It is a live WAL database (13 MB + a 4 MB WAL), so a plain
`cp` would have produced a torn copy.

A consistent snapshot was taken with the sqlite backup API from inside the
container (read-only source), copied out with `docker compose cp`, pulled down
with `scp`, and **both server-side copies were deleted immediately**. The relay
was never stopped and nothing was written to the production DB.

The snapshot lives only in this session's scratchpad, never in the repo:
`…/scratchpad/relay-real-v3.db` (pristine v3) and `relay-migrate-test.db`
(the migrated working copy).

## Production baseline

| | |
|---|---|
| schema_version | 3 |
| messages | 5006 |
| installations | 5 |
| answered messages | 745 |

## Result — `init_schema()` v3 → v4 against the real copy

```
version before         : 3
version after          : 4
migration wall time    : 0.005s
messages before/after  : 5006 / 5006
new indexes            : ['messages_answer_feed']
lost indexes           : []
integrity_check        : ok
foreign_key_check rows : 0
expires_at NOT NULL    : True
null expires_at rows   : 0
re-run version         : 4      (init_schema is idempotent)
re-run integrity       : ok
```

Indexes only, no table rebuild, no row loss, no constraint change — invariant 12
holds against the real schema.

## Index use on real data distribution

```
EXPLAIN QUERY PLAN →
  SEARCH messages USING INDEX messages_answer_feed
    (telegram_chat_id=? AND state=? AND id>?)
```

No table scan against 5006 real rows.

## Installation scoping — the shared-chat case is real in production

The task asked for a test with "two installations on one chat". That is not a
hypothetical: chat `108296207` genuinely carries **two installations** across
4808 messages.

| chat | installation | answered |
|---|---|---|
| 108296207 | 1 | 616 |
| 108296207 | 2 | 21 |
| 368002897 | 4 | 94 |
| -5278209830 | 5 | 10 |
| 95265605 | 3 | 4 |

Running the shipped feed query against both installations on that chat:

```
inst1 page1: 500 rows, inst2 page1: 21 rows
overlap: 0
inst1 total answered: 616
inst1 page2 rows: 116   (strictly after page1's max id)
```

500 + 116 = 616 — the page cap is honoured, paging by advancing `after` is
correct and complete, and the two installations' feeds are **disjoint on real
data**. Without the `installation_id` predicate installation 2 would have seen
616 answers belonging to installation 1.

## Verdict

**Blocker closed.** The 23-01 "Done when" clause "migration applies cleanly
forward on a copy of a real relay DB" is satisfied with evidence above.

## Follow-up

The snapshot is retained in the scratchpad for 23-02, which adds schema version 5
(three nullable columns + an index) to the same table and will want the same
check — that one *does* alter the table, so the real-DB run matters more there.
Delete the scratchpad copies once 23-02's migration is verified; they contain
real message text.
