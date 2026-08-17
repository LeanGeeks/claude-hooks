# Epic 19 — State & orchestration

> **Status 2026-08-17: all six engineering tasks are done, committed and
> DEPLOYED** (`3e19dda`, `69212cb`, `5a6f609`, `447865a`, `6f45a04`, `cac1d30`).
> Schema v3 is live and migrated — see the deployment entry at the end of the Log.
> 19-07 (human) is the remaining gate: it needs a real chat, real clients and an
> availability window that actually closes overnight. Test baseline at handoff:
> **716 root hooks / 249 relay**, both green.

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
| 19-05 | [Reply-to-nudge routing](./19-05-reply-routing.md) | done | 19-04 | nudge ids resolve to their target; ambiguity counter ignores nudges |
| 19-06 | [Diagnostics, docs, installer](./19-06-diagnostics-docs.md) | done | 19-02, 19-05 | `claude-roles` column, `architecture.md`, `docs/availability.md`, task-05 reversal note |
| 19-07 | [Live verification](./19-07-live-verification_human.md) | blocked | 19-06 | **human** — needs a real chat, real clients and a window that actually closes overnight |

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

**2026-08-17 — 19-05 done, in the same session as 19-04 as brd §5.6 requires.**
Reviewed PASS with **zero issues at any severity**; committed. Relay tests
241 → 247 (+6); root hooks 708 unchanged. There is a one-commit window
(`447865a` alone) where nudges exist and a reply to one is swallowed — it never
outlived the session and was never deployed, but do not deploy `447865a` without
the 19-05 commit on top of it.

**The ambiguity counter is provably unaffected, by construction rather than by
luck:** `_distinct_open_targets` iterates the result of `SELECT … WHERE
state='open'` on `messages`, and a nudge is a *column value on a row*, not a row —
so it cannot increase that count. This is the payoff of invariant 6's "nudges are
not `messages` rows": had 19-04 made them rows to simplify anything, one pending
prompt plus its own nudge would now read as two targets and the relay would refuse
plain replies with "multiple sessions are waiting" when only one is.

Reply-target precedence is explicit: message rows by `telegram_message_id` first,
then `nudge_tg_message_id` scoped to the same chat. The nudge lookup deliberately
carries **no state filter**, so a nudge whose target already resolved is found and
answered with "That one's already been handled." rather than silently ignored —
and no path in the `reply_to` block falls through to the recency heuristic, which
is what caused the historical mis-routing.

`via="nudge_reply"` needs no client change: `relay_answer_to_decision` branches on
`via` for `button_multi` and `button` only and falls through to free-text for
anything else. Confirmed against the current `router.py`, not taken from the
planning note. The free-text fallthrough test in
`tests/test_unit_decision_mapper.py` (root suite) is the standing guard on that
and must stay green.

**2026-08-17 — 19-06 done. All engineering tasks in the epic are complete;
19-07 is the only one left and it is human.** Reviewed PASS (0 BLOCKER/HIGH,
3 MEDIUM + 2 LOW), all five fixed, re-reviewed clean (0 issues), committed.
Root hooks 708 → 716; relay 247 → 249.

**Its own task file was out of date and was overridden deliberately:** it said
"three new `messages` columns" (there are **four** — `render_dirty` postdates that
sentence) and "the reaper's **second** pass" (there are **two** new passes — the
nudge pass and the cleanup sweep). The docs describe the four and the two.

**All three MEDIUMs were documentation that misstated the shipped code**, which is
the failure mode this task exists to prevent, so they are recorded rather than
just fixed:

- The 18:50 worked example claimed "30 active minutes" remained to a 19:00 close.
  It is 10. The 09:20 conclusion was right all along.
- The 22:00 TTL example claimed it fires at 09:20 "plus 5-minute debt
  carry-over". **The true value is 09:15** — no active time elapses before the
  window opens, so there is no carry-over debt to credit. Three agents disagreed
  about this instant; it was settled by calling the real `advance_active` twice,
  independently, not by argument. If a future doc or comment claims carry-over
  across a closed window, it is wrong.
- `architecture.md` cited the PATCH endpoint at `app.py:695`, which is inside
  `_do_create`'s rollback. Now `app.py:735` **with the symbol name**, because
  every bare line number in this epic's planning docs went stale — `app.py` moved
  under 19-02, 19-04 and 19-05. Prefer symbols over line numbers in docs.

Both LOW findings were tests that could not have caught the bug they existed for:
the timezone test never varied `TZ` (now runs under UTC and Pacific/Kiritimati,
14 h apart, asserting the printed `active=` is identical in both — note its
bug-catching power is inherently time-of-day dependent, roughly 19 h in 24), and
the degradation test asserted an internal `any_problem` flag rather than the real
exit code (now calls `main()` and asserts 0).

**Invariant 8 held for the whole epic.** The only client-side change is
`shell/claude-roles`' read-only availability column, which prints the server's
`active_now` verbatim — no local clock, no timezone conversion, confirmed by
review. `install-claude-config.sh` needs **no** change, confirmed in writing as
the task required.

**2026-08-17 — deployed by Anton; schema v3 migration verified on live data.**
`cd relay-server && docker compose up -d --build`. **The relay runs on this
machine** (`h02-activecdn`) — no `ssh` needed, contrary to the blocker note in
`docs/prompts/implementation_manager.md`; `docker compose` at `~/.bin/claude-hooks/relay-server`
reaches it directly. Container `relay-server-relay-1` came up healthy, clean
startup, **zero** errors/tracebacks in the logs.

Verified read-only against `/var/lib/relay/relay.db` after the migration ran over
a real database of **4,295 message rows**:

| check | result |
|---|---|
| `schema_version` | **3** |
| `recipients` table | present, exactly the 6 planned columns |
| new `messages` columns | all four present |
| indexes | `messages_nudge_due`, `messages_render_dirty`, and `messages_state_expiry` **preserved** |
| rows with `nudge_count != 0` | 0 |
| rows with `render_dirty != 0` | 0 |
| rows with `next_nudge_at NOT NULL` | 0 |
| rows with `nudge_tg_message_id NOT NULL` | 0 |
| `recipients` rows | 0 |

So 19-01's migration done-criteria are now confirmed on production data, not just
on test fixtures, and **invariant 4 holds live**: with no `recipients` row anywhere,
nudges are off, no row is seeded, and the new reaper passes have nothing to select.
There were 11 `state='open'` rows at deploy time; none is a nudge candidate.

This ticks one box of 19-07's regression sweep (relay container logs clean at
deploy). Everything else in 19-07 needs real clients and a night.

**2026-08-17 — LIVE DEFECT found after deploy and fixed: `/nudge on`'s backfill
ignored `awaits_human`.** Found while checking the deployed DB before the operator
ran `/nudge on`; reviewed PASS (1 LOW, fixed); committed and redeployed.
Relay 249 → 252, root hooks 716.

The backfill was `UPDATE messages SET next_nudge_at = ? WHERE telegram_chat_id = ?
AND state = 'open' AND next_nudge_at IS NULL` — **no eligibility filter**. So
`/nudge on` seeded every open row including `kind='notification'` idle-session
rows, and the reaper's nudge pass trusted the seed without re-checking. Directly
violates brd §4.1 / invariant 7: *an idle session left overnight must never
produce a 09:00 ping.* Live and reachable — the deployed chat held 11 open
notification rows. Demonstrated, not theorised: before the fix the new reaper test
emitted a real send, `'text': '⏳ still waiting — Allow the tool call?',
'reply_to_message_id': 8200`, for a notification row.

**Why every review missed it — the lesson worth keeping.** The bug lived in the
*seam between two tasks*. 19-02 owned the backfill and was implemented before
19-04 existed; 19-04 owned the eligibility rule and correctly applied it to the
create path it wrote. Each task's tests covered its own half — 19-04 tested
create-time seeding against a notification row, 19-02 tested that the backfill
seeded the right chat — and **nobody tested backfill × notification**, because
neither task's author owned both sides. Invariant 7 said "19-04 reuses
`awaits_human` rather than defining eligibility a second time", which the
implementers honoured; what no one wrote down was that *every writer of
`next_nudge_at`* must consult it, including one that already existed. When an
invariant constrains a predicate, state it as a rule about **all writers of the
column**, not about the task introducing the predicate.

Fix, in two parts:
1. **The defect** (`app.py`, `_backfill`): SELECT candidates → filter in Python
   with the existing `awaits_human` → UPDATE only eligible ids. The UPDATE keeps
   `state = 'open' AND next_nudge_at IS NULL` so a row answered between the SELECT
   and the UPDATE cannot be given a stale due time (review LOW).
2. **A backstop** (`reaper.py`, `_nudge_pass`): re-check `awaits_human` before
   emitting — the pass already SELECTs `payload_json`, so it is nearly free. An
   ineligible row is logged at WARNING with its id, has `next_nudge_at` cleared,
   and is dropped **before** coalescing so it can neither become the target nor
   inflate a `+N more`. Reuse, not a second predicate.

**The class of bug is now closed, verified by grepping every writer of the
column:** only two ever set it non-NULL — the create path (`_seed_next_nudge_at`,
guarded since 19-04) and the backfill (guarded now). Everything else sets NULL or
re-arms rows that already passed the reaper's check. A future third writer that
bypassed both guards would be caught by the backstop.

No bad data needed repair: nudges had never been enabled on any chat, so every
`next_nudge_at` in the live DB was still NULL when the defect was found.

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
