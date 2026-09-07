# Task 37-02 — Claude lifecycle reducer and honest status derivation

**Status:** todo · **Depends on:** 37-01

## Goal

Make `status` **read** a tracked Claude worker's state instead of inferring it.
Remove every dependency on a transcript file, and make the reported state
distinguish situations that currently collapse into one word.

## Why

Today a single missing file takes the whole state machine down rather than
degrading it ([evidence.md](./evidence.md) §1). The reduction ORs three
heuristics together — `active = live_bg or open_turn or no_stop_yet`
(`.claude/bin/amux-spawn:1710`) — and one of the three is derived from a file
stat that carries two unrelated meanings. Three heuristics ORed together is what
"we lost the real signal" looks like architecturally.

Operationally the cost is that `stuck` means all of: *running CI for 40 minutes*,
*finished eight hours ago*, *blocked on a permission prompt*, and *genuinely
wedged*. Those demand different action, and an orchestrator cannot tell them
apart.

## Read first

- [brd.md](./brd.md) §§4.2, 4.5 · [architecture.md](./architecture.md) §§3, 6, 7
- [evidence.md](./evidence.md) §§1, 2
- `.claude/bin/amux-spawn` — `_derive_status` and `_derive_codex_status`;
  read them side by side, the Codex path is the shape to converge on
- `.claude/hooks/codex_event_reducer.py` — the purity contract for a reducer in
  this codebase (stdlib only; never launches a process; refuses to read
  unstable internal formats)
- [`../10_spawn_sessions/architecture.md`](../10_spawn_sessions/architecture.md)
  §6.0 — the binding handle schema and the derive-at-read-time rule
- `.claude/hooks/amux_spawn_lib.py` — handle schema, strict-schema and
  atomic-write rules
- `tests/test_unit_amux_supervise.py`, `tests/test_unit_amux_reads.py`,
  `tests/test_unit_codex_reducer.py`

## Work

1. Reduce Claude worker state from the recorded events of 37-01 plus one honest
   liveness check. The result must be derivable with the worker's transcript
   absent, unreadable, or never created.
2. Keep the external vocabulary — `spawning`, `running`, `idle`, `stuck`,
   `terminated` — and keep `status`/`status --json` compatible for existing
   callers.
3. Separate the questions that `stuck` currently conflates. A consumer must be
   able to distinguish, without reading a transcript or a pane:
   - finished, nothing outstanding;
   - working, with a turn open;
   - working, with background work outstanding;
   - waiting on a human decision;
   - exceeded its activity threshold;
   - gone.
4. Make the activity threshold a **watchdog observation reported alongside the
   reduced state**, not a state that overwrites a known one
   ([brd.md](./brd.md) §4.2). It is **derived at read time and appends
   nothing** — a wedged worker fires no hooks, so no producer can record its own
   expiry, and a reader that appended it would be writing into a log the
   producer owns with no dedup against other concurrent readers.
   `_derive_codex_status` already reports `stale_activity` exactly this way
   (`.claude/bin/amux-spawn:1530-1543`).
5. Promote permission-pending from buried reason-context to something a fleet
   consumer can act on directly.
6. Define and test the degradation path for every handle shape that exists in
   the wild: pre-37-01 Claude handles with no event log, legacy handles with no
   provider key, Codex handles, and handles whose log was removed. Rollout must
   not be a flag day.
7. Decide what happens to `_reason_context`'s transcript-derived
   **`foreground_tool`** signal (`.claude/bin/amux-spawn:1247`, epic 10 §7 —
   *which* tool is in flight and for how long). Invariant 1 binds state, wait
   outcomes and digest entries; it does not by itself require deleting an
   additive diagnostic that is simply absent when there is no transcript.
   Either keep it on those terms — never affecting a verdict — or delete it and
   say so. What must not happen is that a useful signal disappears unremarked
   because a test asserted "nothing reads the transcript".
8. Converge `ls` with `status` for Claude handles. `cmd_ls` derives state for
   Codex rows but reports the *stored* `state` for Claude ones
   (`.claude/bin/amux-spawn:1942`), so after this task the fleet-wide view is the
   only surface still reporting un-reduced state — and a fleet consumer reaches
   for `ls` first.

## Implementation hints — suggestions, not instructions

- The natural end state is a pure reducer that is the sibling of
  `codex_event_reducer.py`, with `_derive_status` collapsing into "reduce the
  log, then apply liveness". If that lands, `open_turn` and its transcript-tail
  parsing can be deleted outright rather than fixed.
- For liveness, `_derive_codex_status` already uses tmux-session-alive. That is
  the direct answer to the only legitimate question the mtime clock was groping
  toward — *"have this worker's hooks stopped firing?"* — and two providers
  answering the same question two different ways is the smell worth removing.
- The operator has confirmed there is **no requirement behind the transcript
  mtime clock**; it was an implementation choice. Treat it as free to remove
  rather than as a constraint to preserve.
- `_reason_context()` already collects `pending_permission` and then buries it.
  Promoting it may be mostly a matter of where it surfaces, not new plumbing.
- Watch for the failure mode where a worker finishes its *turn* but has live
  background subagents: `SubagentStop` deliberately never sets idle, and the
  next `Stop` is what settles it. That existing design is correct — preserve it,
  and make sure the reduction expresses "working" rather than "finished" in that
  window.
- **`open_turn` is deletable only because 37-01 records a turn *starting*.** If
  that event is missing or not being reduced, deleting the transcript-tail
  parsing silently reintroduces the stale-`idle`-after-`amux send` bug that epic
  10 §6.0 calls it load-bearing for. Check the event exists before you delete
  anything, and cover the follow-up case in fixtures.
- The `--wait` false-idle guard (`seen_non_idle`,
  `.claude/bin/amux-spawn:1097`, consumed at `:1145`) currently leans on `no_stop_yet` mapping a
  fresh spawn to `running`. Removing the mtime clock removes that mechanism; the
  reduction has to keep a pre-first-`Stop` worker reading non-idle by itself, or
  `spawn --wait` accepts idle before the seeded turn has begun.

## Done when

- A worker with no transcript reports `idle` on completion, `running` while
  background work is outstanding, and is distinguishable from wedged.
- No status path reads, stats, or parses a transcript file. A test asserts this
  directly rather than by proxy.
- A worker blocked on a permission prompt is identifiable from `status --json`
  alone.
- Threshold expiry produces an event and does not erase a known state.
- A worker handed a follow-up turn after settling reads as working, from the
  event log alone, with no transcript present.
- Fixtures cover: no event log, empty log, truncated final line during a live
  write, unknown event types, legacy handle, Codex handle, missing transcript,
  present transcript, and a follow-up turn opened after a settle.
- `status`, `last`, `--wait`, `ls`, `rm` behave identically for every case that
  was **correct** before; Claude and Codex suites stay green. Two behaviors
  change on purpose and should be called out in the report rather than
  smuggled: `ls` starts reporting derived state for Claude rows (item 8), and a
  finished-but-live worker that used to read `stuck` now reads `idle`, so
  `cmd_rm` reaps it without demanding `--force`.
