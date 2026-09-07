# Task 37-03 — `watch`: bulk subscription with debounced digests

**Status:** todo · **Depends on:** 37-01, 37-02

## Goal

Give an orchestrating agent a way to supervise an arbitrary number of tracked
workers with **one** subscription, without blocking, and without paying a wake-up
per event. This is the consumer-facing half of the epic and the reason the event
log exists.

## Why

An agent cannot be idle and get woken by anything it controls itself, so on the
driving run the orchestrator hand-wrote 23 blocking poll loops — 17 of them
watching a *single* handle — and spent 85% of its wall-clock inside them
([evidence.md](./evidence.md) §4). It also used the wrong tool shape: Claude
Code's `Monitor` is a **non-blocking event stream** ("*each stdout line is an
event — you keep working*"), not a place to put an `until` loop.

The mechanism was there. What was missing was anything worth streaming, and any
command that could produce it. A blocking wait alone does not fix this — it is
polling with a better signature, and it still stops the agent.

Note the surface as it actually exists: there is **no `wait` subcommand**.
`--wait` / `--notify` are single-handle flags on `spawn` and `resume`
([brd.md](./brd.md) §3 spelling note). There is no multi-handle blocking
primitive to extend — item 7 below is new surface, not a modification.

Cost model that shapes the whole design: **each wake-up is a full request at the
consumer's current context size** — 432 K tokens by the end of the driving run.
Eight uncoalesced completions late in a run cost eight of those. Coalescing is a
correctness-of-cost requirement, not polish.

## Read first

- [brd.md](./brd.md) §§3, 4.3 · [architecture.md](./architecture.md) §§4, 5, 6, 7
- [evidence.md](./evidence.md) §4
- `.claude/bin/amux-spawn:1013-1180` — the existing `--wait` / `--notify`
  supervise path, its exit-code contract (`:1016-1018` — `0` idle, `1` error,
  `3` timeout, documented as *"caller's patience, NOT a stuck signal"*), and the
  `seen_non_idle` guard around accepting idle; plus `ls` handle enumeration
- `.claude/hooks/amux_spawn_lib.py` — `run_id` on the handle (`:462`, `:662`)
- `.claude/bin/amux-spawn:183` — `resolve_run_id`, and `cmd_ls:1914-1925` — the
  existing `--run-id` filter. Read both before item 6; they are why the
  subscription key needs work rather than just adoption
- [`../23_async_questions/architecture.md`](../23_async_questions/architecture.md)
  — watermark discipline: advance only on terminal outcomes, so a crash replays
  and never drops
- `tests/test_unit_amux_wait_print.py`, `tests/test_unit_amux_supervise.py`

## Work

1. Provide a streaming subscription over the events of 37-01, covering many
   workers at once. One process, one stream, N workers — never N of anything.
2. Handle the two delivery regimes of [architecture.md](./architecture.md) §4.
   The consumer-busy case is the one that needs real work: events must
   accumulate without interrupting, and arrive as one digest rather than a
   backlog of individual wake-ups.
3. Emit **level-triggered digests**: each emission states everything currently
   settled and unacknowledged, so successive emissions are supersets and reading
   only the newest is always safe.
4. Emit on **every** terminal outcome — settled, blocked, permission-pending,
   terminated, threshold-expired. A stream that reports only success is silent
   through a crashloop, and silence is indistinguishable from still-running.
   This is exactly how a worker went unnoticed for 10.5 hours. Threshold expiry
   is *derived* here, not read out of the log — nothing appends it
   ([brd.md](./brd.md) §4.2).
5. Support late join and resume: a consumer arming after workers have settled
   must converge on current truth, and one resuming from a cursor must not
   replay everything or miss the gap.
6. Cover **workers that join after the subscription is armed.** A wave is
   spawned in batches — the driving run's spanned 17 hours — so a `watch` that
   enumerates the registry once at startup supervises only the first batch. The
   set of handles matching the subscription is re-resolved, not frozen.
7. Close the two holes in the subscription key. `run_id` is the right key
   ([architecture.md](./architecture.md) §5) but is not usable as-is:
   - `resolve_run_id` (`.claude/bin/amux-spawn:183`) mints a **fresh** run_id per
     spawn whenever the caller has no handle of its own, so a human-started
     orchestrator's twenty workers carry twenty run ids and no single
     subscription covers them. The consumer-side discipline — mint or capture
     one run id, pass `--run-id` on every spawn of the wave — has to be made
     obvious by the command's own shape and documented in
     [37-05](./37-05-orchestrator-integration-docs.md). Verify it end to end
     rather than assuming it.
   - When the run_id *is* inherited, the consumer's own handle carries it too, so
     an unfiltered subscription reports the subscriber to itself: it is woken by
     its own turn-end, reacting ends another turn, and that wakes it again. The
     harness stops a monitor that emits too fast, so this does not merely make
     noise — it turns the supervision channel off mid-wave. **Exclude the
     subscriber's own handle by default**, and make the exclusion testable.
8. Provide the degenerate synchronous case — "the consumer has nothing else to
   do" — as a multi-handle, edge-triggered block that returns handles as they
   settle, so one blocked turn covers a whole fleet. Existing single-handle
   `--wait` / `--notify` callers keep their current behavior and exit codes.
   Whether this is a new subcommand, a flag on the subscription, or an extension
   of the existing flags is open.
9. Make the output something an agent consumes correctly on first reading, and
   something a human can read in a terminal. The driving run's `sed`-parsing of
   `status --json` is the failure mode to design out.

## Implementation hints — suggestions, not instructions

- Intended usage is a single `Monitor` with `persistent: true` running this
  command for the life of a wave — armed once, never re-armed, stopped with
  `TaskStop`. Worth writing the command so that shape is the obvious one.
- `Monitor` batches stdout lines arriving within 200 ms into one notification,
  and groups multiline output from a single event naturally. That covers the
  consumer-idle burst case for free; it does nothing for the consumer-busy case,
  which is why the debounce belongs here.
- Inside `watch`, noticing that a log has grown is a legitimate use of inotify
  over `~/.amux/spawn/` — atomic tmp+rename means every write is a real event, so
  the command can be genuinely poll-free. What was rejected
  ([architecture.md](./architecture.md) §6) is inotify as the *event source*, not
  as an implementation detail here. Note the constraint though: this repository
  has **no dependency manifest at all** and every hook and CLI is stdlib-only, so
  inotify means a `ctypes` binding, not a package. Plain stat-polling inside
  `watch` is an equally acceptable answer and does not compromise the epic —
  one poll loop in one long-lived process costs the *consumer* zero wake-ups,
  which is the entire quantity this epic is minimizing. Pick either; record why.
- A byte offset into an append-only log is already a watermark; a separate
  sequence protocol may be unnecessary.
- Consider what happens when a debounce window is open and the consumer dies. The
  digest is recoverable from the log, which is the point of level-triggering —
  but make that explicit in a test rather than incidental.
- Rate limiting is real: monitors that produce too many events are stopped
  automatically by the harness. A chatty stream is not just noisy, it
  self-terminates.

## Done when

- One subscription supervises N workers, including workers spawned after it was
  armed; a burst of simultaneous completions produces one delivery.
- A consumer that is itself a tracked worker is not present in its own
  subscription, demonstrated by a test rather than by inspection.
- Completions occurring while the consumer is busy are neither lost nor delivered
  as N separate wake-ups.
- Every terminal outcome appears, demonstrated by a test in which a worker dies
  without completing and the stream says so.
- A consumer arming late, and one resuming from a cursor, both converge on the
  same truth as one that watched from the start.
- A blocking multi-handle wait exists and returns handles as they settle;
  existing single-handle `--wait` / `--notify` behavior and exit codes are
  unchanged.
- Nothing in the emitted output requires ad-hoc parsing to act on.
