# Epic 19 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, done criteria and tests. This file owns **cross-task invariants**
and **ordering**.

**No Phase 0.** Every cross-cutting decision is locked below. The one genuine
external unknown (hashtag search scope per client) was closed by the operator as
*don't care* — brd §2.4.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 19-01 | [Schema + availability engine](./19-01-availability-engine.md) | done | — | **The epic's whole migration** (`recipients` + the `messages` nudge columns + `render_dirty`), tz/windows parsing, `advance_active`. Touches no Telegram code. |
| 19-02 | [Preference commands](./19-02-preference-commands.md) | done | 19-01 | `/tz`, `/hours`, `/nudge`, `/me` in the webhook; `/installations/me` fields; `relay-admin`; every chat-visible string |
| 19-03 | [Render layer + `#unanswered`](./19-03-render-layer-tag.md) | done | — | `render_body`, payload canonicalization, PATCH write-back, cancel + expiry text edits. **Independently shippable.** |
| 19-04 | [Nudge engine](./19-04-nudge-engine.md) | done | 19-01, 19-03 | backend reply-send, reaper pass, coalescing, ladder, cleanup, config knobs. **No migration — 19-01 owns it.** |
| 19-05 | [Reply-to-nudge routing](./19-05-reply-routing.md) | todo | 19-04 | nudge ids resolve to their target; ambiguity counter ignores nudges |
| 19-06 | [Diagnostics, docs, installer](./19-06-diagnostics-docs.md) | todo | 19-02, 19-05 | `claude-roles` column, `architecture.md`, `docs/availability.md`, task-05 reversal note |
| 19-07 | [Live verification](./19-07-live-verification_human.md) | todo | 19-06 | **human** — needs a real chat, real clients and a window that actually closes overnight |

## Dependency graph

```
19-01 ─┬─► 19-02 ──────────────────┐
       └─► 19-04 ─► 19-05 ─────────┼─► 19-06 ─► 19-07
19-03 ────► 19-04                  ┘        (19-07 is human)
```

## Recommended order

1. **Start 19-01 and 19-03 together** — two independent roots, no shared files
   (19-01 is `db.py` + a new pure module; 19-03 is `app.py` + `reaper.py`).
2. **Ship 19-03 on its own.** The hashtag is the larger half of the operator's
   problem (brd §1 failure mode 2), costs no new messages, and changes nothing
   about notification behaviour. It should reach the live relay and be lived
   with **before** 19-04 adds anything that interrupts.
3. **19-02 and 19-04 in parallel** once 19-01 lands. They share only the
   `recipients` row shape; fix that in 19-01 and they cannot drift.
4. **19-05** immediately after 19-04 — never leave a build where a nudge exists
   and replying to it does nothing (brd §5.6). If they must be split across
   sessions, ship them together.
5. **19-06**, then **19-07** live.

The first end-to-end moment is the end of 19-03: open a prompt, see the tag, tap
it, answer, watch the tag vanish. That validates the render invariant with no
nudge machinery in the picture at all.

## Implementer model

19-03 and 19-04 carry the real risk and want the stronger implementer model;
19-01 is exacting but self-contained; 19-02 and 19-06 are mechanical.

- **19-03** — one invariant applied across five call sites and two endpoints,
  where the failure mode is *silent data loss* (a wiped answer bake), not a
  crash. Highest-stakes task in the epic.
- **19-04** — scheduling arithmetic, coalescing across two keys, and a cleanup
  path that has to fire from four unrelated transitions.
- **19-01** — DST, week boundaries and empty-window edge cases. Mechanical only
  if the tests are written first.

## Invariants (do not relitigate without a brd revision)

1. **`payload_json.text` is canonical and untagged.** Every writer keeps it
   current; PATCH now writes back. The tag exists only in the render layer
   (brd §4.2). This is the epic's central invariant — 19-03 establishes it and
   everything downstream assumes it.
2. **One `render_body`, called by every send and every edit.** No call site
   decides tagging for itself.
3. **A person is a `telegram_chat_id`**, not an installation and not a role
   (brd §2.1). Nothing in this epic reads `roles.toml`.
4. **Unconfigured = today's behaviour, byte for byte.** Nudges off by default,
   hours absent means always available, `next_nudge_at` NULL means the reaper's
   new pass never touches the row.
5. **Nudges are measured in active time; TTL is wall-clock** (brd §3.4, §3.5).
6. **A nudge is owned by exactly one message row** — the one it replies to
   (brd §5.5). Rows folded into a `+N more` own nothing.
7. **Idle notifications are excluded from both the tag and the ladder**
   (brd §4.1, decided 2026-08-11). `#unanswered` means *an agent is blocked on
   you*. The predicate keys on `kind != 'notification'`, **not** on
   `reply_required` alone — idle messages carry `reply_required=True` whenever
   reply-from-Telegram is on, so the shorter form would sweep every idle session
   into the tag and nudge about it overnight. 19-03 defines `awaits_human`;
   19-04 reuses it rather than defining eligibility a second time.
8. **The client never learns any of this exists.** No hook change, no
   `RelayClient` method, no new field in `CreateMessageRequest`. If a task finds
   itself editing `.claude/hooks/`, something has gone wrong — the one exception
   is 19-06's read-only diagnostic column.
9. **Relay-local commands.** `/tz`, `/hours`, `/nudge`, `/me` are handled in the
   webhook beside `/bind` and have no dependency on epic 16's command queue.
10. **Terminal cleanup is swept, not trusted** (decided 2026-08-16). Of the five
    terminal paths, `_record_answer` reaches Telegram not at all — so neither the
    tag nor a nudge may depend on the client's PATCH arriving. The reaper's
    cleanup pass is the backstop for both; the existing renders remain the fast
    path. 19-01 defines `render_dirty`, 19-04 is the only task that reads or
    writes it.

## Cross-epic conflict — schema version

**RESOLVED (Anton, 2026-08-11): epic 19 takes version 3.** Verified against
`main` at decision time — `db.py:18` is `SCHEMA_VERSION = 2` and `MIGRATIONS`
contains only key `2`; every epic-16 task is still `todo` and no `commands`
table exists in the tree. So 19-01 bumps **2 → 3** and writes `MIGRATIONS[3]`.
Epic 16's 16-01 rebases to **4**.

Shipped `SCHEMA_VERSION` is **2**. Epic 16's task 16-01 also claims **3** (for
`commands`). Whichever epic lands first takes 3 and the other rebases to 4;
`MIGRATIONS` entries are ordered and must be reconciled at implementation time,
not assumed. **19-01 owns the entire schema change for this epic** — the
`recipients` table *and* the three `messages` nudge columns, in one migration.
No other task file adds a migration or names a version.

## Log

**2026-08-11 — 19-03 done.** Implemented, reviewed PASS (0 issues at any
severity), committed. 156 relay tests / 708 root hooks tests green; 20 net new
tests. The finalize-then-cancel regression test was written before the
implementation and observed failing against a payload-rendering cancel
(`assert '✍️ some answer' in 'original body'` — a silent wipe, no exception),
then verified by the reviewer. Awaiting live-relay soak before 19-04.

**2026-08-11 — a fifth terminal path exists; brd §2.2's table of four is
incomplete.** Found by 19-03's implementer, verified by its reviewer against the
code. `_record_answer` — reached from `_handle_callback_query` for **ungrouped**
button taps and from `_handle_text_reply` for **ungrouped** plain-text replies —
flips `state` to `answered` and performs **no Telegram edit at all**. The tag is
removed only when the hook's `finalize_message` (PATCH + cancel) subsequently
runs. If the machine sleeps or the hook dies in between, the row is `answered`
with a live `#unanswered` tag and nothing ever removes it: the reaper sweeps
`state = 'open'` only.

Pre-existing behaviour with a new symptom — hashtag search would list an item
that is already resolved. Correctly scoped out of 19-03. **Resolved 2026-08-16 —
see the entry below**, which also corrects this one: the remedy blocks 19-01,
not 19-04.

**2026-08-16 — both open decisions closed; the epic has none left.** Decided by
Anton after re-reading the paths in the shipped code.

1. **The `_record_answer` leak (above) is cleaned up by the reaper, not by an
   eager edit.** A `render_dirty` flag on `messages`, set in `_record_answer`'s
   existing `UPDATE` (value supplied by the caller via `awaits_human`, so
   untagged rows are never flagged) and cleared by the PATCH and cancel renders
   that already exist. A new reaper pass sweeps `render_dirty = 1 AND state !=
   'open'`, and the same pass deletes orphaned nudges via `nudge_tg_message_id
   IS NOT NULL AND state != 'open'`.

   *Rejected:* giving `_record_answer` its own render-after-flip edit. It costs
   an `editMessageText` on the hottest path in the system for **every** ungrouped
   answer, and the hook's PATCH lands milliseconds later with the baked `✅`
   text — two edits where there is one today, against brd §2.7's ~1 msg/s per
   chat, precisely when several sessions are answering at once. It buys nothing
   in the normal case (the PATCH already clears the tag within a second) and
   only differs in the failure case, where the reaper's answer is "≤ one tick"
   against "never". It would also drag brd §5.7's nudge deletion onto the same
   hot path, where the sweep gets it for free.

   **Scheduling correction: this blocks 19-01, not 19-04.** The flag is a column,
   19-01 owns the epic's entire migration, and no other task may add one — so
   deciding this after 19-01 lands would cost a second migration and another
   reconciliation against epic 16. 19-01 now defines `render_dirty`; **19-04
   owns every read and write of it.**

2. **brd §5.3's assumption is confirmed: both coalescing keys are kept** — group
   *and* chat-level. The chat-level rule was the planner's addition; without it
   four sessions going idle in one tick emit four nudges, which is the original
   complaint in new clothes. brd §5.3 no longer marks it optional.

**2026-08-16 — 19-01 done.** Implemented, reviewed PASS (0 BLOCKER/HIGH, 4 LOW
— dead inner imports, two wrong docstrings, an overcomplicated range — all
fixed), committed. Relay tests 156 → 200 (+44 new); root hooks 708 green,
unchanged.

**Schema version confirmed against `main` at implementation time, not assumed:**
`db.py` was `SCHEMA_VERSION = 2` with `MIGRATIONS` holding only key `2` and no
`commands` table in the tree, exactly as the cross-epic note predicted. **Epic 19
took 3**; `MIGRATIONS[3]` carries the `recipients` CREATE, the four `messages`
ALTERs (`nudge_count`, `next_nudge_at`, `nudge_tg_message_id`, `render_dirty`)
and both indexes. Epic 16's 16-01 must rebase to **4**. The epic's whole
migration is now spent — **any later task in epic 19 that finds itself writing
`ALTER TABLE` has misread its scope and must stop.**

`render_dirty` is defined and deliberately unread: a tree-wide grep confirms no
reader or writer exists yet, and no eager render-after-flip edit was added to
`_record_answer`. 19-04 remains the sole owner of both.

**Environment note for future sessions:** `relay-server/.venv` did not exist and
was rebuilt from `requirements.txt` + `pip install -e .` to establish the
baseline. It is gitignored. The relay suite is
`cd relay-server && ./.venv/bin/python -m pytest tests -q`.

**2026-08-17 — 19-02 done.** Implemented, reviewed PASS (1 MEDIUM + 3 LOW, all
fixed), re-reviewed clean (0 issues at any severity), committed. Relay tests
200 → 217; root hooks 708 unchanged.

**Two contracts 19-04 inherits from 19-02 — read these before touching the
reaper:**

1. **`windows_json` holds a canonical *spec string*, not JSON**, despite the
   column name. Every reader parses it with `availability.parse_windows`
   (`app.py`, `admin_cli.py`, `/v1/installations/me`). Follow that pattern; do
   not `json.loads` it.
2. **The nudge knobs live in exactly one place:** `nudge_default_schedule =
   "15m,45m,3h"` and `nudge_max = 3` on `RelayConfig` in `config.py`. 19-04 must
   read them from `app.state.config` — **not** by constructing a fresh
   `RelayConfig()`, and not by re-declaring the literals. (`admin_cli.py` was
   corrected during review to use `load_config()` for the same reason.)

19-02 also added duration/schedule helpers to `availability.py`, purely additive
after the 19-01 functions; 19-01's five core functions are semantically
untouched and the module is still clock-free and DB-free.

**A guard worth not breaking:** the preference-command branch sits *above* both
the `reply_to` and loose-reply answer paths in `_handle_update` and returns, so a
`/tz` sent as a reply to an open prompt is never recorded as its answer. Two
regression tests cover it. The `/nudge on` backfill is likewise guarded by an
off → on transition — the resolved time is computed unconditionally for the echo,
but the `next_nudge_at` write stays inside the transition check, so a repeat
`/nudge on` cannot postpone open rows' nudges.

**2026-08-17 — 19-04 done.** Implemented (opus, per the Implementer-model note
above rather than the suffix-less filename), reviewed PASS (0 BLOCKER/HIGH/MEDIUM,
2 LOW — both accepted, see below), committed. Relay tests 217 → 241; root hooks
708 unchanged. **The 19-03 answer-bake regression test
(`test_cancel_preserves_a_patched_finalized_body`, `tests/test_messages.py:248`)
is unmodified and still passes** — 19-04 added `render_dirty` clearing to both
renders that test depends on, and that failure mode is a silently wiped body, so
this was the first thing reviewed.

The `_record_answer` hole was demonstrated, not assumed: with the sweep alone
disabled on the finished tree, both hole tests fail with the row `answered`,
`render_dirty = 1`, the nudge id still set and **zero Telegram calls in the tick**.
The implementer wrote the tests after the code rather than before and said so;
the de-implementation check is what makes them credible.

**Two accepted LOW behaviours — deliberate trade-offs, recorded so nobody
"fixes" them blindly and so 19-07 knows to watch for them:**

1. **A failed nudge send burns a ladder rung** (`nudge_count` is bumped even when
   the send fails). The alternative — retry the same rung next tick — is
   unbounded for a chat failing persistently for any reason that is not
   `TelegramForbidden`, since only the cap bounds retries. Burning the rung keeps
   total attempts bounded by `nudge_max`. Consequence: a chat with flaky sends may
   receive fewer nudges than its ladder promises.
2. **The per-tick pacing ceiling visits chats in message-age order, not
   nudge-due-time order.** Not strictly FIFO under extreme load, but
   self-correcting: a serviced chat advances off the due list. Revisit only if a
   real chat is observed starving.

**Shape 19-05 must build against:** a nudge has **no `messages` row of its own** —
it exists only as `messages.nudge_tg_message_id` on the row it serves. That is
what 19-05's reply lookup resolves against, and it is why nudges stay out of the
ambiguity counter and the expiry sweep by construction.

**Disclosed and accepted:** seeding costs one indexed `recipients` SELECT per
created message that awaits a human. Unavoidable — only that table knows whether
the chat has nudges on. It is a DB read, not a behaviour change; the stored row
is byte-identical to today's when nudges are off, and an absent `recipients` row
is a valid state that cannot error.

**Still deferred, and correctly so:** brd §4.4's per-workspace companion tag. Not
decidable today for a structural reason — the relay does not know the workspace
(the session name is composed client-side into the body), so it would need a new
field before it is even possible.

## Relationship to epic 15

Epic 15 owns `escalate_after`: *who* to ask when the first person is silent.
This epic owns *when* a person is reachable. They compose — an escalation timer
that respected availability would be strictly better — but that composition is
explicitly **out of scope** (brd §8) and must not be smuggled in. 19-01's
`advance_active` is the function epic 15 would later call; keep it importable
and free of relay-specific state so that stays possible.
