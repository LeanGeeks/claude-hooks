# Epic 23 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md)
and [architecture.md](./architecture.md). Each task file is written for a
fresh-context agent and carries its own "read first" refs, done criteria and
tests. This file owns **cross-task invariants** and **ordering**.

**No Phase 0.** Every cross-cutting decision is locked in brd §3 (D1–D10). Two
defaults were chosen by recommendation rather than hard requirement and are
one-line changes if they prove wrong:

1. the default nudge ladder `4h,1d,3d,7d*` and default `escalate_after = 24h`
   (architecture §7) — numbers, not design;
2. the pending-apply retry count before the sidecar fallback (§5.1).

**The one genuine unknown is now settled.** 23-03's Phase 0 scan has been run
(read-only, by the manager) and the parse contract is rewritten in
architecture §3.2 as six rules. Findings:
`agents_output/23-03_phase0_anchor_scan.md`. 23-03 implements the contract and
must not re-scan or open the reference workspace.

**Scope clarification (2026-08-26, from the user).** This epic ships a
*contract*, not a retrofit. Adoption happens per-workspace, individually, and
the reference workspace (`hyppie-flow`, a different repo) is **evidence only and
is never modified by this epic**. Compatibility is still a goal, but the bar is
*migratable* compatibility: an existing queue must be editable into conformance
**without information loss**. Measured: the reference queues adopt with **2
lossless edits across 290 entries**. Because adopters do this themselves,
23-06 also ships a conformance checker and an adopter-facing contract doc.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 23-01 | [Relay: answer feed + non-expiring messages](./23-01-relay-answer-feed.md) | done | — | `never_expires` sentinel, `GET /v1/answers`, waiter registry keyed by installation, migration. **Touches no Telegram rendering.** |
| 23-02 | [Relay: per-message nudges + escalation](./23-02-relay-nudge-escalation.md) | done | — | Repeating-tail ladder, three nullable columns, the escalation reaper pass. Independent root; same files as 23-01, so coordinate the migration number. |
| 23-03 | [Queue-file engine](./23-03-questions-store.md) | done | — | `questions_store.py`: anchor resolution, config, id allocation under lock, compose, `apply_answer`. The heart of the epic. |
| 23-04 | [Questions MCP server](./23-04-questions-mcp.md) | done | 23-01, 23-02, 23-03 | `ask` + `notify`, role resolution, escalation-token resolution, write-then-send ordering. |
| 23-05 | [Answer listener runtime](./23-05-listener-runtime.md) | done | 23-01, 23-03 | `questions-listen`: loop, index, watermark, apply, PATCH, pending retry, lock, `--status`. |
| 23-06 | [Installer, diagnostics, docs](./23-06-installer-diagnostics-docs.md) | done | 23-04, 23-05 | MCP registration, systemd unit, `shell/claude-questions`, `docs/async-questions.md`, top-level `architecture.md`. **Grew a conformance checker (`--check`) and `docs/questions-contract.md`** — see the task file. |
| 23-07 | [Live verification](./23-07-live-verification_human.md) | blocked | 23-06 | **human** — needs a real relay, a real answer given days later, and a machine that sleeps. |

## Dependency graph

```
23-01 ──┬────────────────────────► 23-05 ──┐
        │                            ▲      │
        └──────────────► 23-04 ──────┼──────┼─► 23-06 ─► 23-07
                            ▲        │      │              (human)
23-02 ──────────────────────┘        │      │
23-03 ──────────────────────┴────────┘──────┘
```

Read as: **23-04** needs 23-01 + 23-02 + 23-03; **23-05** needs 23-01 + 23-03
(not 23-02); **23-06** needs both. Three independent roots: 23-01, 23-02, 23-03.

## Recommended order

1. **Start 23-01, 23-02 and 23-03 together.** 23-03 is the critical path and the
   most valuable to get right; the two relay tasks are smaller and share a file,
   so agree the migration number between them before either starts.
2. **23-04 and 23-05 in parallel** once their roots land. They are opposite ends
   of the same wire and share only the index shape (architecture §5) — fix that
   shape before either starts and they cannot drift.
3. The first end-to-end moment is the end of 23-05: an answer given in Telegram
   reaches a file with no session running. Demonstrate that before 23-06.
4. **23-06**, then **23-07** live.

## Implementer model

23-03 and 23-05 carry the real risk and should go to the stronger implementer
model; 23-01, 23-02 and 23-04 are moderate; 23-06 is mechanical. Reviewer stays
consistent throughout.

- **23-03** — file mutation under concurrency against files a human also edits.
  A bug here corrupts a 300 KB hand-curated artifact with real history.
- **23-05** — crash-safety around the watermark. A mistake loses a human
  decision silently, which is the one outcome the epic must never produce.

## Cross-task invariants

1. **The file write precedes the first relay call, and is fsynced.** No crash
   may leave a Telegram message referring to an entry that does not exist. The
   reverse is the accepted failure and must be reported, never hidden (brd D4).
2. **The watermark advances only after an apply reaches a terminal outcome** —
   applied, or moved to `pending`. Never on a bare read (architecture §5.1).
3. **Applies are idempotent on relay message id.** Every writer and every retry
   path checks for an existing answer block citing that id before inserting.
4. **The relay never learns a role, a path or a workspace.** Escalation is passed
   as a token; the queue location exists only on the machine (brd D6).
5. **`questions_store.py` is the only module that parses or writes a queue
   file.** The MCP server and the listener both import it; neither reimplements
   any part of the format.
6. **Frame vs body** (brd D3): the tool composes only heading, id, status, tags,
   routing line, rendered options and the dispatch marker. `body` is opaque.
   A task that adds a field to the frame is out of scope by default.
7. **Anchor resolution runs at both ends** and is keyed on `workspace_id`, not on
   a captured absolute path (architecture §3.1).
8. **No component spawns anything** (brd D9). A task that wants to "just kick the
   loop" is violating the epic.
9. **Absent `[questions]`, nothing changes.** Every existing flow must produce
   byte-identical relay calls with the listener stopped and no config present —
   this is the regression floor of every task's test section.
10. **Epic 19's invariant 7 holds**: idle-session notifications keep no keyboard
    and are never tagged `#unanswered`. Ack-notifications get their tag by being
    `kind=question`, not by widening `awaits_human` (brd D7).

11. **No answer text can change how a file parses.** Every insertion of
    human-supplied text is escaped so it cannot forge a heading, a status token
    or an answer-block field (architecture §3.4, brd §8 Security). This is a correctness
    invariant first and a security bound second; a hostile answer is a required
    test input, not an edge case.
12. **`never_expires` is a sentinel, not NULL** (brd D11). No task may drop the
    `NOT NULL` on `messages.expires_at` or add None-handling to its readers.

## Migration numbering (locked before 23-01/23-02)

`relay-server/relay_server/db.py` is at `SCHEMA_VERSION = 3` as of the start of
this epic. The two relay tasks take the next two slots, in this order:

- **23-01 → schema version 4** — the `messages_answer_feed` index (indexes only,
  no table rebuild; the `never_expires` sentinel needs no schema change).
- **23-02 → schema version 5** — the three nullable reminder-policy columns plus
  their index.

Neither task may renumber the other's slot. If 23-02 lands first for any reason,
swap the numbers here first rather than in the code.

## Live-repo hazard: epic 22 (resolved)

Epic 22's engineering is complete — 22-01–22-05 are committed as of 2026-08-26
(`7014481`); only 22-06 (human live gate) remains open and it touches no code.
The overlap files — `install-claude-config.sh`, `REQUIRED_HOOKS` and
`tests/run_all_tests.py` — are settled, so 23-04 and 23-06 extend a landed base
rather than racing one. `permissions-mcp/server.py` + `permissions_mcp_lib.py`
is now a shipped, readable precedent for 23-04.

The checkout is still shared by concurrent sessions: never run repo-wide git
operations (`stash`/`checkout`/`reset` without a pathspec), and copy a file aside
rather than reverting it to test against HEAD.

## Relationship to other epics

- **Epic 16 (Telegram spawn)** — deliberately *not* a dependency (brd D5). If 16
  later builds its `commands` table, this epic's feed stays as it is; they solve
  different problems and share only the idea of a resident process.
- **Epic 19 (unanswered reminders)** — this epic extends its nudge machinery with
  a repeating tail and adds an escalation pass beside it. Invariants 2 and 7 of
  epic 19 bind here unchanged.
- **Epic 22 (agent permission flows)** — draws the same boundary from the other
  side: its MCP `decide` refuses `AskUserQuestion` rows because "questions have
  reply semantics, not allow/deny". Copy its server registration pattern.
- **Blocking `AskUserQuestion`** — untouched. The 60 s-cap migration is brd §9.

## Log

- **2026-08-26 — 23-01 done.** Implemented, reviewed (FAIL: 1 HIGH, 1 MEDIUM,
  1 LOW), fixed, re-reviewed (PASS). Relay suite 272 passed, top-level 1030
  passed / 1 skipped.
  - The HIGH was real and would have crippled 23-05: `WaiterRegistry` latches
    its `asyncio.Event`, so after an installation's first answer every
    `GET /v1/answers?wait=N` returned in ~0 ms and the listener would have
    tight-polled. Fixed with a new `ConditionWaiterRegistry` (multi-fire,
    `notify_all()`) added *beside* the untouched `WaiterRegistry` — the
    blocking answer path still depends on the latching semantics, so it was
    deliberately not changed. Re-review confirmed no lock-ordering inversion
    between the condition lock and `db.py`'s `_conn_lock`.
  - Grouped-message finalization now wakes the feed too (reviewer's MEDIUM;
    the implementer had called it out of scope, overridden by the manager —
    a silent up-to-25 s latency was not worth leaving under 23-05).
  - Unbounded `_conditions` keys accepted as-is: safe cleanup needs reference
    counting, and any cleanup racing a parked waiter would drop a wake.
    Correctness over tidiness; revisit only if installation counts explode.
  - **Real-DB migration blocker closed by the manager**, not by an agent —
    see `agents_output/23-01_real_db_migration_check.md`. v3→v4 against a
    production snapshot (5006 messages): clean, 5 ms, integrity ok, no lost
    indexes, `expires_at` still NOT NULL with zero NULL rows, idempotent on
    re-run. Note the DB is **inside the Docker volume `relay-data`**, not at
    `/var/lib/relay/relay.db` on the host, and it is live WAL — snapshot it
    with the sqlite backup API, never `cp`.
  - Worth knowing for 23-05: the shared-chat case is **real in production** —
    chat `108296207` carries two installations (616 and 21 answered rows).
    The feed's `installation_id` predicate is load-bearing, not defensive.

- **2026-08-26 — 23-02 done.** Implemented, reviewed (FAIL: 1 HIGH, 2 MEDIUM,
  1 LOW), fixed, re-reviewed (PASS, 0 new). Relay suite 315 passed (stable over
  two runs), top-level 1030 / 1 skipped.
  - **The HIGH was a spec-vs-code gap the implementer had filed as a decision:**
    `parse_duration` accepted only `h`/`m`, so the epic's own default ladder
    `4h,1d,3d,7d*` — documented in brd §5, architecture §7, this file, and this
    task's Done-when — raised `ValueError`. Tests had been rewritten to
    `24h,72h,168h` to fit the parser. `d` support added; tests restored to the
    spec literals so the Done-when clause is actually demonstrated. **23-04
    would have taken a 422 on the first send from a human-written roles.toml.**
  - **`parent_message_id` is an accepted architecture deviation** — see the note
    added to architecture.md §2.2. Three columns cannot link a child back to its
    parent, which answer attribution and sibling cancellation both require.
  - **Escalation is now idempotent rather than fire-once-or-lose.** It was
    clearing `escalate_at` before the send, so a transient Telegram failure
    dropped the escalation permanently and silently. Now: existence-check the
    child by `parent_message_id`; `telegram_message_id == 0` means the prior
    send failed → reuse that row and retry; non-zero means delivered → just
    clear. Failure leaves `escalate_at` set (WARNING logged) and the next tick
    retries. **Residual, accepted and documented:** a crash between the Telegram
    send and the DB commit can deliver a second message — it cannot create a
    second child row, which would have been the worse failure.
  - The expiry pass now cancels escalated children. Before the fix, a child from
    a failed send (`tg_msg_id = 0`, never nudged, never expired because it
    carries `NEVER_EXPIRES`) leaked as `state='open'` forever.
  - `answer_waiters` removed from `reaper_tick` — nothing in the reaper records
    answers. **If 23-05 needs the reaper to wake the feed, this comes back**;
    the feed wake lives in `app.py`'s `_record_answer` and group finalization.
  - Migration v5 verified against the production snapshot on both paths
    (v3→v5 one-hop, which is what the live relay at v3 will do, and v4→v5).
    All four columns NULL across 5006 real rows; `expires_at` kept NOT NULL
    through the ALTERs. See `agents_output/23-02_real_db_migration_check.md`.

- **2026-08-26 — 23-03 done.** Phase 0 scan (manager, read-only) → contract
  rewritten as architecture §3.2 rules 1–6 → implemented (opus) → reviewed
  (PASS, 0 blocker / 0 high / 3 low) → fixed → verified. Suite 1120 passed /
  1 skipped (baseline was 1030); relay untouched at 315.
  - **The escaping was attacked, not just read.** 18 adversarial inputs —
    forged `<!-- answer:N -->` markers, fence closure, 1000-backtick runs,
    CRLF and lone-CR, zero-width characters before `#`, setext headings, and
    answers that are themselves complete well-formed entries. The falsifiable
    property (re-parse yields the same entries, ids and order, no new entry)
    held for all of them. Invariant 11 is evidenced, not asserted.
  - Two design choices worth remembering: the idempotency marker is emitted by
    the store and only recognised **anchored at line start**, so a hostile
    answer containing `<!-- answer:999 -->` cannot make a later genuine answer
    999 look already-applied; and the idempotency check runs **before** the
    ambiguity check, so a retry of an already-applied answer is not permanently
    stuck on a duplicate that appeared afterwards (invariant 3 outranks rule 5
    when the work is provably done).
  - The per-line escaping guard was narrowed to the **bracketed** status shape.
    Rule 3 parses a bracketed token, so a bare leading word was never a forgery
    risk; guarding it turned "Open the file by clicking…" into `\Open the
    file…` in a file humans read in git diffs.
  - Anchor resolution reads `.git` pointers directly rather than shelling out to
    `git rev-parse`, which is what makes it provably pure (creates nothing,
    spawns nothing, cannot touch git state) and testable without a git binary.
  - **Obligation passed to 23-05, already written into its task file:** the
    store raises a typed `QuestionsStoreError` on a corrupt/unreadable queue
    file. The listener MUST catch it and route the answer to `pending` like a
    `not_found`. Uncaught, one unreadable file kills the resident loop — and a
    file is unreadable for ordinary reasons (a `git checkout` mid-flight, a
    partially-synced tree, a permissions change).
  - `questions_store.py` is registered in `REQUIRED_HOOKS` but
    `install-claude-config.sh` was deliberately **not** re-run — 23-06 owns
    installation, and re-running it in a shared checkout would clobber another
    session's live config.

### Index ownership — resolved 2026-08-26 (23-04 runs before 23-05)

architecture §5 assigns the index shape to 23-05, but sequential execution puts
**23-04 first** and 23-04 must write to that index. Rather than let two agents
invent two shapes:

- **23-04 creates `.claude/hooks/questions_listen_lib.py`** containing *only* the
  index concern — the on-disk shape (architecture §5), the `flock` + atomic
  replace protocol, and the reader/writer helpers, including `pending` and
  `watermark` fields it does not itself use.
- **23-05 extends that same module** with the loop, watermark advance, apply and
  retry logic. It does not create a second module and does not restate the shape.

Invariant 5 applies to the index exactly as it does to a queue file: one module
parses it, everyone else imports. Neither task hand-parses the JSON.

- **2026-08-26 — 23-04 done.** Implemented → reviewed (PASS, 0 blocker / 0 high /
  2 medium / 2 low) → fixed → verified. Suite `Ran 1156, skipped=1` (the only
  skip is the pre-existing `test_headless_spawn`); relay untouched at 315.
  - **Index ownership was a real conflict in the epic docs, resolved here.**
    architecture §5 gives the index to 23-05, but sequential execution puts
    23-04 first and `ask` must write to it. 23-04 created
    `.claude/hooks/questions_listen_lib.py` carrying the *whole* architecture §5
    shape — `watermark` and `pending` included, though it uses neither — with
    `flock` + tmp + `os.replace`. **23-05 extends this module; it must not
    create a second one or restate the shape** (invariant 5 applies to the index
    exactly as to a queue file).
  - **`index_routing_failed: True` is a new result field 23-05 should know
    about.** If the index write fails after retries, the entry and the Telegram
    message both exist but no routing record does — and the listener resolves
    answers by `message_id → index`, so that answer would be permanently
    unapplied, the one outcome brd D10 forbids. It now retries with backoff,
    then returns the flag and logs WARNING with message id, qid and path.
    Recovery is manual: read the queue file.
  - A test had silently disabled itself — `test_min_interval_allows_after_cooldown`
    called `skipTest` when its fixture failed to load a config, so the
    *recovery* half of brd D12's rate limit was never exercised (the refusal
    half was). The logic turned out to be correct; the fixture was calling
    `load_questions_config` without patching `CLAUDE_PROJECT_DIR`. The
    `skipTest` escape was **replaced with an assertion**, so a broken fixture
    now fails instead of vanishing.
  - **Third instance of the same anti-pattern this epic** — 23-01 tested only
    the first waiter wake, 23-02 substituted `24h,72h,168h` for the unparseable
    `1d,3d,7d`, 23-04 skipped itself. In each case the suite was green and the
    requirement was unverified. An audit of 23-04's new tests for `skipTest`,
    bare `except` and no-exception-only assertions came back clean otherwise.
  - `min_interval_s` state is in-memory per workspace (resets on server
    restart); only `max_open` was required to be durable, and it counts open
    entries from the queue file.
  - The `questions-mcp` installer block is deliberately **not** written — 23-06
    owns installation. The server mirrors `permissions-mcp` exactly so 23-06 can
    copy that block and substitute the name.

- **2026-08-27 — 23-05 done.** Implemented (opus) → reviewed (PASS, 0 blocker /
  0 high / 1 medium / 3 low) → fixed → verified. Suite `Ran 1222, skipped=1`
  (still only the pre-existing `test_headless_spawn`); relay untouched at 315.
  - **The epic's design missed something and the implementation caught it: the
    answer feed is installation-scoped, and a machine can hold several tokens.**
    `[roles] hpl = "rly_…"` binds a role to its own installation, so an `ask`
    routed there is sent with *that* token and its answer appears only on *that*
    feed. Polling the default token alone would have silently stranded every
    answer given to a role-bound human — invisible to every test in the epic's
    plan, and visible in production only as "they answered and nothing
    happened". The reviewer confirmed the premise against the relay's own SQL
    (`GET /v1/answers` filters by `installation_id`).
    **Deviation from architecture §5, accepted:** `Index.watermarks`
    (fingerprint → position) is the truth per feed; the top-level `watermark`
    mirrors the primary so the documented shape and `--status` stay honest.
    The loop stays single-threaded and splits the 25 s budget `max(1, 25 // N)`.
  - The six-step crash walk was independently verified. Steps 1–5 write nothing;
    step 6 is one `flock`-ed read-modify-write recording the terminal outcome
    *and* advancing the watermark — so there is no window where the watermark
    has moved past an answer nothing is tracking (invariant 2). Crash tests
    inject `BaseException` so the loop's own `except Exception` cannot swallow
    them, then run a **fresh** listener and assert exactly-once application.
  - **A missing index record is skipped and the watermark advances** — correct,
    not a leak: the feed carries *every* answered message of the installation,
    including permission approvals and blocking answers, so pending them would
    fill the backlog with rows that can never apply and make `pending_answers`
    meaningless.
  - **The one hole in "never lose a decision", and why it is closeable.** If
    23-04's index write fails after retries, that answer is skipped forever.
    But `mark_dispatched` runs *unconditionally*, so the entry carries
    `**Dispatched:** … relay #<message_id>` — the mapping is durable **in the
    queue file**, and the index is reconstructible. `--reindex` is specified in
    23-06 to recover it; replay is safe because applies are idempotent.
  - Existing installations no longer replay from zero: the primary fingerprint
    is seeded once from the legacy top-level `watermark`. Role-bound
    fingerprints correctly stay at 0 — they have genuinely never been polled,
    and seeding them would skip answers.
  - **23-06 must install `questions_listen_lib.py` and `questions-mcp` in the
    same pass.** Until it does, the installed 23-04 library drops `watermarks`
    and `token_fp` on write; nothing is lost, but the listener replays and skips
    the Telegram tick. Recorded in 23-06's task file.

- **2026-08-27 — 23-06 done. Engineering for epic 23 is complete.**
  Implemented → reviewed (PASS, 0 blocker / 0 high / 2 medium / 6 low) → fixed →
  verified. Suite `Ran 1259, skipped=1`; relay 315. The count fell by one
  because a redundant test was deleted, not because coverage was lost.
  - Ships `shell/claude-questions` (four flags), `docs/async-questions.md`,
    `docs/questions-contract.md`, `docs/questions-prompt-example.md`,
    `docs/questions.example.toml`, the installer block and the systemd unit.
  - **`--check` and `--check-contract` are deliberately two flags.** The first
    probes the relay per token (mirrors `claude-roles --check`); the second
    reports queue-file conformance. The task file originally used one name for
    both — resolved before implementation.
  - **`--reindex` closes the epic's last data-loss hole** and its marker regex
    was verified to match exactly what `mark_dispatched` writes. A mismatch
    would have made the recovery silently find nothing, which is worse than not
    shipping it.
  - `--check-contract` now **names the unknown status token**, not just the
    configured set. Declaring an unknown token is the lossless fix; an adopter
    cannot declare what the tool will not name.

### Test-quality note for whoever picks this epic up

Every one of the five engineering tasks shipped a **green suite that hid an
unverified requirement**, each in a different disguise:

| Task | The suite said | The requirement actually was |
|---|---|---|
| 23-01 | 270 passed | only the *first* waiter wake was tested; every later long-poll returned in 0 ms |
| 23-02 | 303 passed | tests used `24h,72h,168h` because the documented ladder `1d,3d,7d` did not parse |
| 23-04 | green | the test `skipTest`-ed itself when its fixture broke, leaving half the rate limit unexercised |
| 23-06 | green | 11 tests asserted string presence/absence in a file; one had *shaped the code* to satisfy it |

None were caught by running the tests. All were caught by reading each
implementation report's **Decisions** section against the epic documents, and by
treating an unexplained count change as worth chasing. Keep doing that.

The epic-23 test files now contain **zero** `skipTest` calls and **zero** bare
`except` clauses, and the two most load-bearing assertions (systemd never
enabling silently; `--reindex` never writing a queue file) were **mutation-proved
able to fail** before being accepted.
