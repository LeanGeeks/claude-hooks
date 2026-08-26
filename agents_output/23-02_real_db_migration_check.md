# 23-02 — Real-DB forward-migration check (schema v5)

**Run by:** implementation manager · **Date:** 2026-08-26

Same production snapshot as the 23-01 check (see
`23-01_real_db_migration_check.md` for how it was taken: sqlite backup API from
inside the `relay-server-relay-1` container, since the DB lives in the Docker
volume `relay-data`, not on the host filesystem). Baseline: schema v3, 5006
messages, 5 installations.

This check matters more than 23-01's: v5 **alters the `messages` table** (four
`ADD COLUMN`s), where v4 only added an index.

## Path 1 — v3 → v5 in one hop (the real production path; the relay is on v3)

```
version before         : 3
version after          : 5
wall time              : 0.008s
messages before/after   : 5006 / 5006
new indexes            : ['messages_answer_feed', 'messages_escalation_due']
lost indexes           : []
new columns            : nudge_schedule_override, escalate_at,
                         escalate_to_token_hash, parent_message_id
   every one           : notnull=0, default=None
   non-NULL rows on existing data: 0, 0, 0, 0
integrity_check        : ok
foreign_key_check rows : 0
expires_at NOT NULL    : True
re-run version         : 5   (idempotent)
re-run integrity       : ok
```

**Invariant 9 confirmed at the data level, not just in code:** all four new
columns are NULL for all 5006 real rows, so every existing message keeps
today's behaviour by construction.

**Invariant 12 confirmed:** `expires_at` still `NOT NULL` after the table was
altered — the `ADD COLUMN`s did not rebuild or relax it.

## Path 2 — v4 → v5 incrementally (if 23-01 deploys before 23-02)

```
version before : 4
version after  : 5
messages       : 5006
integrity      : ok
all 4 columns  : present
```

Both paths land identically. No table rebuild on either.

## Verdict

Migration is safe against real production data on both the one-hop and
incremental paths. This closes the migration half of 23-02; the rest of the
task is under review separately.

## Note

The snapshot copies can be deleted from the session scratchpad once 23-02 is
committed — they contain real message text. No further task in this epic adds a
migration.
