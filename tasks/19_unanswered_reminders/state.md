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
| 19-01 | [Schema + availability engine](./19-01-availability-engine.md) | todo | — | **The epic's whole migration** (`recipients` + the `messages` nudge columns), tz/windows parsing, `advance_active`. Touches no Telegram code. |
| 19-02 | [Preference commands](./19-02-preference-commands.md) | todo | 19-01 | `/tz`, `/hours`, `/nudge`, `/me` in the webhook; `/installations/me` fields; `relay-admin`; every chat-visible string |
| 19-03 | [Render layer + `#unanswered`](./19-03-render-layer-tag.md) | done | — | `render_body`, payload canonicalization, PATCH write-back, cancel + expiry text edits. **Independently shippable.** |
| 19-04 | [Nudge engine](./19-04-nudge-engine.md) | todo | 19-01, 19-03 | backend reply-send, reaper pass, coalescing, ladder, cleanup, config knobs. **No migration — 19-01 owns it.** |
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
that is already resolved. Correctly scoped out of 19-03. **Open for decision
before 19-04**, which treats the tag as ground truth and whose nudge cleanup
(brd §5.7) hangs off the same chokepoint — a nudge orphaned this way would be
worse than a stale tag. Candidate remedies: give `_record_answer` the same
render-after-flip edit the cancel path now has, or widen the reaper sweep to
catch terminal rows still carrying a tag.

## Relationship to epic 15

Epic 15 owns `escalate_after`: *who* to ask when the first person is silent.
This epic owns *when* a person is reachable. They compose — an escalation timer
that respected availability would be strictly better — but that composition is
explicitly **out of scope** (brd §8) and must not be smuggled in. 19-01's
`advance_active` is the function epic 15 would later call; keep it importable
and free of relay-specific state so that stays possible.
