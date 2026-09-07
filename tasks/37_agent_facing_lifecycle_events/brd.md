# Epic 37 — Agent-facing lifecycle events for tracked workers

**Status:** planned · **Owner:** Anton · **Created:** 2026-09-07 · **Rev:** 2
(pre-handover review, 2026-09-07 — see [state.md](./state.md) Phase 0 §§9–14)

> Tooling only. No workflow file in a consumer project changes as part of this
> epic. See [state.md](./state.md) for ordering, [architecture.md](./architecture.md)
> for the contract, and [evidence.md](./evidence.md) for the measured post-mortem
> that drives it.

## 1. Problem

`amux-spawn` can launch a fleet of tracked Claude workers, but it gives their
spawner no way to **learn that anything happened**. An orchestrator has exactly
one supervision primitive: ask, repeatedly, and hope the answer means something.

Two defects compound.

**A worker cannot speak to its spawner.** Every lifecycle event a tracked worker
produces is already computed. `spawn_producer_hook.py` fires on `Stop`,
`SubagentStop`, `SessionEnd`, and `Notification`, holding the full payload at the
instant of the event. `notification_hook.py` computes an idle event carrying the
worker's last message. For agent-spawned sessions that idle event is
deliberately silenced (`architecture.md` §Idle notification: *"yes (tracked
amux-spawn handle matches this session) → silent"*) — correct in intent, since a
machine's lifecycle should not page a human, but the branch has only one sink.
The spawner was never given an address, so the event is computed, gated, and
dropped.

**The state that remains is inferred, and the inference is fragile.** State is
derived at read time from a transcript file's mtime plus a three-way disjunction
(`.claude/bin/amux-spawn:1710`):

```python
active   = live_bg or open_turn or no_stop_yet
is_stuck = active and (now - activity_mtime) > stuck_after
```

`no_stop_yet` is `(stored_state == "spawning") or (mtime_at_stop is None)`
(`:1699`), and `mtime_at_stop` is a
**file stat** taken by the producer hook (`spawn_producer_hook.py:150`) rather
than a fact the hook owns. That single value carries two unrelated meanings —
*"did Stop ever fire"* (a boolean the hook knows outright) and *"when was there
last activity"* (a clock) — so when the transcript file is absent the hook
destroys a fact it already had.

The consequence is total, not graceful. With no transcript: `mtime_at_stop` is
`None` forever → `no_stop_yet` is permanently true → `active` is permanently
true → `idle` is **unreachable**, and every worker reads `stuck` at
`stuck_after` and stays there for the rest of its life. `open_turn`, which
parses the transcript tail, can never evaluate at all.

On the run in [evidence.md](./evidence.md) this happened to **every worker in
the fleet**, all run. Observed `activity_age_s` reached 28502 s and 37972 s —
workers that had been finished or blocked for eight and ten hours while the
orchestrator had no way to find out. The orchestrator compensated by hand-writing
23 blocking poll loops, spent 85% of its wall-clock inside them, and still missed
completions by hours.

**The asymmetry is the tell.** Epic 20 made `amux-spawn` provider-neutral and
recorded the split plainly (`codex_event_reducer.py`): *"The Claude side derives
state from Claude Code hooks plus the transcript mtime; the Codex side has no
hooks at all, so its lifecycle has to be reduced from three files."* The provider
with **no** lifecycle events was forced into a precise, ordered, replayable
append-only event log at `~/.amux/spawn/<name>.events.jsonl`
(`amux_spawn_lib.py:690`) with a pure reducer over it. The provider that fires a
hook at the exact instant of every event took a shortcut and got mtime inference.
That is backwards.

## 2. Thesis

Make the Claude provider **produce the artifact the Codex provider already
produces**, and give orchestrators a way to subscribe to it in bulk.

Lifecycle hooks append to a durable, append-only, per-session event log. State
derivation becomes a reducer over that log — the Claude sibling of
`codex_event_reducer.py` — so state is *read*, not inferred. Supervision becomes
event-driven: one persistent subscription per orchestrator, waking it when the
fleet changes, instead of N blocking polls that each hold a turn open.

Nothing here invents a new subsystem. It removes an asymmetry, promotes facts the
producer already holds, and adds one consumer-facing command.

## 3. User-visible contract

An orchestrator supervising a wave arms **one** subscription for the whole fleet
and keeps working:

```bash
amux-spawn watch --run-id <run-id> [--handle <name> ...] [--debounce 8s] [--since <cursor>]
```

driven from the agent side as a single persistent stream, e.g. Claude Code's
`Monitor` tool with `persistent: true`. Each emission is one digest of everything
that has settled and not yet been acknowledged. The orchestrator is not blocked
while it waits.

The existing surface keeps working and keeps its spelling:

```bash
amux-spawn status <handle>       # now reduced from the event log, not inferred
amux-spawn last   <handle>
amux-spawn ls | rm | spawn | resume
amux-spawn spawn  … --wait       # block until the seeded turn reaches idle
```

> **Spelling note, because a sibling epic gets this wrong.** There is no `wait`
> **subcommand** — `amux-spawn wait <handle>` exits with
> `unknown subcommand: wait`. The blocking-until-idle capability ships as the
> `--wait` / `--notify` **flags** on `spawn` and `resume`, single-handle, with
> the exit-code contract at `.claude/bin/amux-spawn:1016-1018` (`0` idle,
> `1` error, `3` timeout). [Epic 20's brd](../20_codex_background_workers/brd.md)
> §3 shows `amux-spawn wait review-123` in its *target* contract; that spelling
> was never implemented. Read the CLI, not that line.

## 4. Required behavior

### 4.1 Event production

- Every lifecycle transition a tracked Claude worker undergoes is appended to a
  durable, append-only, per-session event stream as it happens.
- Events preserve what the file-based projection loses: **which** event fired
  (turn start / `Stop` / `SubagentStop` / `SessionEnd` / permission prompt), its
  ordering relative to other events, and enough payload for a consumer to act
  without opening the worker's transcript.
- **A turn beginning is itself an event.** `Stop` fires only at turn *end*, so
  without a turn-start record a worker that was handed a follow-up via
  `amux send` is indistinguishable from one that is finished — the stale
  `state: idle` that epic 10's
  [`architecture.md`](../10_spawn_sessions/architecture.md) §6.0 calls
  open-turn detection **load-bearing** for. The stream must answer *"is a turn
  open right now"* on its own, because §4.2 forbids answering it from the
  transcript.
- **A record is bounded.** The payload requirement above and the fail-open
  requirement below pull against each other: an unbounded `last_message` makes a
  record that a concurrent append can interleave with. A record has a stated
  maximum serialized size, chosen so that one append stays atomic, with an
  oversized field truncated rather than the record dropped.
- Producing an event must never be able to disrupt, delay, or hang the session
  that produced it. The existing fail-open discipline of the hooks is a hard
  constraint, not a preference.
- Durable state is written **before** the event announcing it. A consumer that
  reacts instantly must never observe an event describing a state that is not
  yet on disk. What this requires is **ordering, not durability** — the consumer
  is a process on the same machine, so the existing atomic tmp+rename handle
  write already satisfies it. Epic 23 is the precedent for the *sequencing*, not
  a licence to put an `fsync` on the hot path of every turn-end of every tracked
  session. See [architecture.md](./architecture.md) §3.
- The idle transition that agent-spawned sessions currently drop is recorded in
  the stream — **as the producer's `Stop` with no outstanding work, not as
  `notification_hook`'s `idle_prompt`**. Those are two different events at two
  different moments, and the second cannot carry the payload
  ([architecture.md](./architecture.md) §2). Human-started sessions keep the
  Telegram idle path byte-for-byte; the `idle_prompt` branch for agent-spawned
  sessions stays silent.

### 4.2 State derivation

- The external state vocabulary is unchanged: `spawning`, `running`, `idle`,
  `stuck`, `terminated`.
- State is reduced from recorded events plus one honest liveness check. No status
  answer may depend on a transcript file's existence, mtime, or contents.
- That rule binds **state, wait outcomes and digest entries**. Transcript-derived
  *diagnostics* — notably `_reason_context`'s in-flight foreground tool — may
  remain as strictly additive context that is simply absent when there is no
  transcript. What is forbidden is a verdict that changes depending on whether
  the file exists.
- A worker that finished a turn with no outstanding background work reads
  `idle` — including when its transcript was never written.
- `stuck` stops overwriting the truth. Exceeding the activity threshold is a
  **watchdog observation reported alongside the reduced state**; it does not
  erase a known state. Distinguishing "running CI for 40 minutes", "finished
  8 hours ago", and "wedged" is required, because they demand different operator
  action.
- The watchdog is **derived at read time and appends nothing.** A wedged worker
  fires no hooks by definition, so no producer can record its own expiry; and a
  consumer that appended it would make every reader a writer of a log the
  producer owns, with no dedup between concurrent readers. This is already how
  `_derive_codex_status` reports `stale_activity`, and the two providers should
  not answer this one differently either.
- A worker blocked on a permission prompt is reportable as such. This is the
  highest-value event in a fleet and is currently collected only as buried
  reason-context.
- Legacy handles, Codex handles, and handles written by an older producer remain
  readable, with a documented degradation for each.

### 4.3 Subscription and delivery

- **Bulk by default.** One subscription covers an arbitrary number of workers.
  N workers must never require N subscriptions, N processes, or N wake-ups.
- **Two delivery regimes**, because a consuming agent is either idle or busy and
  the right behavior differs (see [architecture.md](./architecture.md) §4):
  - *consumer idle* — deliver promptly; a short window is enough to group
    simultaneous settles;
  - *consumer busy* — accumulate; do not interrupt; deliver at the next boundary
    as one digest rather than a backlog of individual events.
- **Digest, level-triggered.** Each emission states everything currently settled
  and unacknowledged, so successive emissions are supersets and a consumer that
  reads only the newest is never wrong. Duplicate or missed deliveries are
  self-healing rather than requiring a replay protocol.
- **Emit on every terminal state**, not only the happy path — settled, blocked,
  permission-pending, terminated, watchdog-expired. A subscription that reports
  only success is silent through a crashloop, and silence is indistinguishable
  from still-running. This is precisely how a worker went unnoticed for 10.5
  hours.
- **Late join and resume.** A consumer arming a subscription after workers have
  already settled must see current truth, and must be able to resume from a
  cursor without replaying the whole history or missing the gap.
- Subscription is keyed on identity that already exists at spawn time; a new
  parent-pointer field should not be necessary. But the key has to be genuinely
  *shared across the wave* and must not include the subscriber — today `run_id`
  is minted **per spawn** unless the caller is itself a tracked handle or passes
  `--run-id`, and when it is inherited the consumer's own handle carries it too.
  Closing both holes belongs to the subscription; see
  [architecture.md](./architecture.md) §5.
- **A subscription never reports the subscriber to itself.** A consumer woken by
  its own turn-end wakes itself again by reacting, and a stream that does that
  is stopped by the harness's rate limiter — switching the supervision channel
  off at exactly the moment it is in use.
- **A worker that joins after the subscription is armed is covered by it.** Waves
  are spawned in batches over hours, not all at once; the driving run's wave
  spanned 17 hours.
- The existing blocking-until-idle capability (`--wait` / `--notify`) is
  retained as the degenerate synchronous case — "the consumer has nothing else
  to do". Whatever it is spelled, it should become multi-handle and
  edge-triggered so one blocked turn can cover a whole fleet instead of one
  worker, and existing single-handle callers must keep their current exit-code
  contract.

### 4.4 Worker identity and artifacts

- A missing recorded artifact must fail **visibly**. On the driving run a stat
  against a never-created transcript returned nothing and the reduction carried
  on to a confidently wrong answer; no path in the system reported that the
  artifact it depended on was absent.
- Transcript persistence for spawned workers must be a deliberate, documented
  choice rather than an inherited accident. `CLAUDE_CODE_CHILD_SESSION` is
  ambient and inherited down the process tree; on the driving run it silently
  disabled persistence for 26 worker sessions.
- Correctness of state must not depend on that choice either way. Persistence is
  an **observability** decision after this epic, not a **correctness** one.
- Referring to a worker should not require knowing how `amux-spawn` composes
  handle names. A caller that spawned `unit-028-pre` and then asked about
  `unit-028-pre` got `unknown (no tracked handle found)`; the registry key was
  `leads-platform-unit-028-pre`.

### 4.5 Compatibility

- Existing `amux-spawn` callers need no new flags.
- Claude and Codex launch, handles, reads, waits, resume, and their test suites
  remain green.
- Sessions with no tracked handle — plain human sessions, other repos — remain
  entirely unaffected. The hooks stay handle-gated and provider-gated.
- Absent any new configuration, behavior is unchanged.

## 5. Explicit non-goals

- Replacing `amux send` for **parent→child** messaging. That direction is
  semantically correct as a user turn (the parent genuinely is the child's
  operator) and stays as it is.
- Using `amux send` for **child→parent** events. See
  [architecture.md](./architecture.md) §6 for why this is rejected rather than
  merely unscheduled.
- An MCP server as the event channel. MCP is request/response; it cannot wake an
  idle consumer. Its value on the query side is real but separable — see
  §6 of the architecture and task
  [37-07](./37-07-remote-event-fanout_deferred.md).
- Cross-machine event delivery, a daemon, or a broker. Deferred with recorded
  reasoning in [37-07](./37-07-remote-event-fanout_deferred.md).
- Changing Telegram permission, question, or notification flows for
  human-started sessions.
- Fleet policy: how many workers to run, how to schedule them, what to do with an
  event. That belongs to consumer projects.
- Fixing the consumer-side orchestration defects the same post-mortem found
  (prompt-boilerplate duplication, context growth, host RAM contention). Recorded
  in [evidence.md](./evidence.md) §4 so they are not lost; they are not this
  epic's work.

## 6. Success criteria

- [ ] A tracked Claude worker whose transcript was never written reports `idle`
      when it finishes, and is distinguishable from one that is wedged.
- [ ] An orchestrator supervising N workers arms one subscription, is not blocked
      while waiting, and is woken with a digest when any subset settles.
- [ ] A burst of simultaneous completions produces one delivery, not N.
- [ ] Completions occurring while the consumer is busy are neither lost nor
      delivered as a backlog of individual interrupts.
- [ ] A worker blocked on a permission prompt is visible as such without reading
      its transcript or its pane.
- [ ] A worker handed a follow-up turn reads as working, not as finished, with no
      transcript present.
- [ ] An orchestrator's own lifecycle never appears in its own subscription, and
      a worker spawned mid-wave is picked up by a subscription already armed.
- [ ] A consumer that arms late, or resumes from a cursor, converges on the same
      truth as one that watched from the start.
- [ ] No status answer anywhere depends on a transcript file.
- [ ] The event stream is sufficient to reconstruct a fleet run afterwards
      without any session transcript — including after its workers were reaped.
- [ ] Existing Claude and Codex suites remain green; a real multi-worker spawn
      chain remains green.

## 7. Dependencies and related work

- Epic 20 [`20_codex_background_workers`](../20_codex_background_workers/) —
  owns the provider-neutral handle, the Codex event artifact contract, and
  `codex_event_reducer.py`. This epic deliberately mirrors its shape rather than
  inventing a second event model. Read its `architecture.md` §§3–6 before
  designing anything here.
- Epic 23 [`23_async_questions`](../23_async_questions/) — the precedent for
  durable-file-before-notification, watermarks that advance only on terminal
  outcomes, and a single module owning a file format. The invariants transfer.
- Epic 24 [`24_idle_notification_origin.md`](../24_idle_notification_origin.md)
  — owns the origin gate whose silent branch this epic gives a second sink.
- `amux` repo — unchanged by this epic. Any fan-out work is deferred to
  [37-07](./37-07-remote-event-fanout_deferred.md).
