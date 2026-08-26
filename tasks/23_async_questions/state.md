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

One genuine unknown is scoped inside 23-03 and blocks nothing before it: whether
the three parse anchors hold against **every** real entry in the reference
workspace's queues, or whether a subset needs `heading` overrides. Answer it with
a read-only scan over the real files before writing the writer.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 23-01 | [Relay: answer feed + non-expiring messages](./23-01-relay-answer-feed.md) | todo | — | `never_expires` sentinel, `GET /v1/answers`, waiter registry keyed by installation, migration. **Touches no Telegram rendering.** |
| 23-02 | [Relay: per-message nudges + escalation](./23-02-relay-nudge-escalation.md) | todo | — | Repeating-tail ladder, three nullable columns, the escalation reaper pass. Independent root; same files as 23-01, so coordinate the migration number. |
| 23-03 | [Queue-file engine](./23-03-questions-store.md) | todo | — | `questions_store.py`: anchor resolution, config, id allocation under lock, compose, `apply_answer`. The heart of the epic. |
| 23-04 | [Questions MCP server](./23-04-questions-mcp.md) | todo | 23-01, 23-02, 23-03 | `ask` + `notify`, role resolution, escalation-token resolution, write-then-send ordering. |
| 23-05 | [Answer listener runtime](./23-05-listener-runtime.md) | todo | 23-01, 23-03 | `questions-listen`: loop, index, watermark, apply, PATCH, pending retry, lock, `--status`. |
| 23-06 | [Installer, diagnostics, docs](./23-06-installer-diagnostics-docs.md) | todo | 23-04, 23-05 | MCP registration, systemd unit, `shell/claude-questions`, `docs/async-questions.md`, top-level `architecture.md`. |
| 23-07 | [Live verification](./23-07-live-verification_human.md) | todo | 23-06 | **human** — needs a real relay, a real answer given days later, and a machine that sleeps. |

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

## Live-repo hazard: epic 22 is in flight

Epic 22 is landing in the same files as this epic. As of 2026-08-26, 22-01–22-03
are committed (`ac221c5`, `77b3449`, `bb86c5b`) and **22-04 is in progress**, with
22-05 still to come — all three touch `install-claude-config.sh`, `REQUIRED_HOOKS`
and `tests/run_all_tests.py`, which are exactly the files 23-04 and 23-06 extend.

Check epic 22's `state.md` before starting either, rebase onto whatever has
landed rather than racing it, and treat a dirty tree in those files as expected
rather than as a surprise to "clean up" — see the cp-backup-and-git-status
discipline in the reviewer briefs. `permissions-mcp/server.py` +
`permissions_mcp_lib.py` is now a shipped, readable precedent for 23-04.

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
