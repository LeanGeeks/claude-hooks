# Epic 37 — Architecture: event-driven supervision of tracked workers

Companion to [brd.md](./brd.md). This document owns the contract between the
event **producer** (lifecycle hooks), the **durable artifacts**, and the
**consumers** (`status`/`last`/`watch`, and the orchestrating agent above them).

Implementation shape below is a **recommendation with recorded reasoning**, not
a specification. An implementer who finds a better arrangement should take it and
record why in the epic's `state.md`. What is binding is [brd.md](./brd.md) §4 and
the invariants in §7 here.

## 1. Ownership boundary

```text
orchestrating agent
   │  arms one persistent subscription; is woken, not blocked
   ▼
amux-spawn watch / status / last          (this repository)
   │  reduction, debounce, digest, cursor
   ▼
~/.amux/spawn/<name>.json                 handle  — durable STATE
~/.amux/spawn/<name>.<log-suffix>         log     — durable HISTORY  (§3)
   ▲
   │  appended at the instant of the event, fail-open
spawn_producer_hook.py / notification_hook.py   (this repository)
   ▲
   │  turn start · Stop · SubagentStop · SessionEnd · Notification
claude TUI (the tracked worker)
```

`amux` is not in this path. It owns process launch and tmux; it does not learn
about lifecycle events and does not gain a broker role in this epic.

## 2. Why the hook is the source, not the handle file

The handle is a lossy projection — `spawn_producer_hook.py` "writes only the
fields defined in architecture §6.0". Recovering events by watching the handle
file (inotify, mtime diffing, polling) reconstructs less than the producer
already had:

| Lost by watching the file | Held by the hook |
|---|---|
| **Which** event fired — turn start vs `Stop` vs `SubagentStop` vs `SessionEnd` vs permission prompt all land as "the file changed" | dispatched by `--event`; knows exactly |
| **Transitions.** Two rapid writes coalesce into fewer inotify events; a reader can catch a mid-rename | one append per event, no coalescing |
| **Ordering**, and any notion of "have I seen this" | append order is total; byte offset *is* a watermark |
| **Payload** beyond the §6.0 field set | the whole hook payload |

So the log is written by the hook, at the event, with the event's identity — not
derived afterwards from a cache of it.

The same argument rules out the *other* tempting source. `notification_hook.py`
computes an idle event, but both of its inputs are read back out of the
transcript (`extract_last_agent_message()` at `:440-452`,
`has_active_background_agents()` at `:231-233`, each returning empty when the
file is absent). `spawn_producer_hook` receives the identical facts directly in
the hook payload. Any event built from the transcript is empty exactly when a
worker has no transcript — the case this epic exists to fix — so the producer
path is the only viable source.

## 3. Two artifacts, two jobs

Both are already-established patterns in this repository; neither is new.

**Handle (`<name>.json`) — durable STATE.** The answer to *"what is true now"*.
Survives consumer restarts, serves late joins, and is what recovery reads. Keeps
its current role and, as far as possible, its current shape.

**Event log — durable HISTORY.** The answer to *"what happened, in what
order"*. Append-only. The naming and collision-sequence *conventions* already
exist for the Codex provider (`amux_spawn_lib.py:690`, `:698`, `:701` — a
non-`.json` extension cannot collide with the `*.json` handle glob;
`<name>.2.…` handles stem reuse). The Claude side should adopt those
conventions rather than invent a parallel scheme.

**It must not adopt the Codex file *name*.** `.events.jsonl` is
`CODEX_EVENT_SUFFIX` (`amux_spawn_lib.py:701`) — the `codex exec --json` stream
that `codex_event_reducer.py` parses and that `cmd_rm` deletes through
`remove_codex_artifacts`. A handle carries exactly one provider, so the two can
never co-exist for a single name; the cost of reusing it is subtler. It puts two
unrelated schemas behind one path, so every provider-agnostic reader becomes a
latent mis-parse, and `allocate_codex_artifacts` — which stats that name before
any pane exists — starts colliding with an artifact it does not own. Give the
Claude log a distinct suffix. An implementer who converges the two names anyway
must put a provider/schema discriminator on every record of both, document the
reducer dispatch, and record the reasoning in `state.md`.

**Write order is an invariant: state first, then the event.** Nobody is told
about a transition before the state that transition describes is on disk, so a
consumer that reacts in microseconds always finds a consistent handle. This is
epic 23's write-before-notify rule with the roles correctly assigned — but what
it requires here is **ordering, not durability**.

The consumer is a process on the same machine reading through the same page
cache, so `write_handle`'s existing atomic tmp+rename (`amux_spawn_lib.py:601`,
which does **not** fsync today) already makes the new state visible the instant
it returns. Epic 23 fsyncs because a crash between its file write and a *remote*
relay call would strand a question with a human; nothing here spans that gap, and
an `fsync` on the hot path of every turn-end of every tracked session buys
nothing back for it. **Do not add one.**

**The record is bounded, and that is what keeps the append safe.** The fail-open
argument above is an argument about *one* append, and it holds only while a
record fits inside a single atomic append — on Linux, under `PIPE_BUF` (4096
bytes). [brd.md](./brd.md) §4.1 pulls the other way by requiring enough payload
to act without opening the transcript, and `last_message` is arbitrary agent
prose. So: cap the serialized record, truncate the offending field with a marker
rather than dropping the record, and keep the producer lock-free. A `flock`
around the append would put contention on the hot path to buy back exactly what
a size bound gives for free.

**Retention is a contract, not an implementation detail.** `cmd_rm` deletes a
Codex worker's artifacts as part of the reap — and teardown is precisely when a
post-mortem needs them; the driving run reaped every worker in the fleet, each
one logged as `rm: <unit> (stuck) killed and removed`. [brd.md](./brd.md) §6
requires a run to be reconstructable from the streams alone *after* that. That
criterion is what 37-01's `rm` decision has to satisfy, and what
[37-06](./37-06-live-verification_human.md) item 11 verifies.

**Both are local file writes.** This is what keeps the hook genuinely fail-open.
An `O_APPEND` write of one line has no failure mode that can hang a session; a
socket or HTTP write does. This is the decisive reason the producer does not talk
to a daemon, and it is not negotiable — this is the hot path of every turn-end of
every tracked session on the machine.

## 4. The two delivery regimes

A consuming agent is either idle or busy, and the correct behavior differs. The
harness arbitrates part of this; the emitter must handle the rest.

**Consumer idle.** An event should wake it promptly. Simultaneous settles want
only a short grouping window. Claude Code's `Monitor` already batches stdout
lines arriving within 200 ms into one notification, so near-simultaneous events
group for free.

**Consumer busy.** Events accumulate. They must not interrupt, and they must not
arrive later as a backlog of individual wake-ups — each wake-up costs a full
request at whatever the consumer's context has grown to (432 K tokens by the end
of the driving run; see [evidence.md](./evidence.md)). The harness's 200 ms
window does nothing here, because the events are minutes apart. **This is the gap
the emitter must close**, and it is the main reason `watch` is a command rather
than a one-line shell recipe left to each orchestrator.

The recommended resolution is **level-triggered digests with a debounce window**:

- hold a window (order of seconds) after the first event before emitting;
- emit everything currently settled-and-unacknowledged, not one line per event;
- because each digest is a superset of the last, a consumer woken three times
  during a busy stretch can act on the newest and ignore the rest.

This makes duplicate and missed deliveries self-healing, and it closes the
re-arm gap without a separate replay protocol. It is the same watermark
discipline as epic 23, expressed as a cursor into an append-only file.

An alternative considered and **not** chosen as the primary path: draining
accumulated events through a `Stop` hook that blocks the consumer's turn-end.
Claude Code does support this (`decision: "block"` with a reason; the strings
`Stop hook denied continuation` and `Stop hook blocking error from command` are
in the 2.1.263 bundle) and the turn boundary is a naturally perfect batch point.
It is rejected as the primary mechanism because the same bundle contains
`Stop hook block discarded (turn ended by …)` — the block is not guaranteed to
land, and a supervision channel that silently drops under a condition we do not
control is worse than one that occasionally wakes twice. Worth revisiting as an
optimization once the digest path is proven.

## 5. Subscription identity

`run_id` already exists, is minted or inherited at spawn, and is already
persisted on the handle (`amux_spawn_lib.py:462`, `:662`; `--run-id` is an
existing flag, and `ls --run-id` already filters on it). It is still the right
key: it needs no new schema, it survives re-parenting, and a nested manager's
workers roll up to the top-level orchestrator for free.

It is not, however, free the way an earlier reading of this section assumed.
`resolve_run_id` (`.claude/bin/amux-spawn:183`) is *`--run-id` overrides; else
inherit from the parent **handle**; else mint a fresh one*. That leaves two
holes, and [37-03](./37-03-watch-bulk-subscribe_opus.md) owns closing both.

**A consumer that is not itself a tracked spawn has no run_id to filter on.**
A human-started orchestrator has no handle, so every `amux-spawn spawn` it issues
mints a *fresh* run_id — twenty workers, twenty run ids, and no single
subscription that covers them. The fix is consumer discipline, not schema: mint
or capture one run id up front (`spawn` already prints it at
`.claude/bin/amux-spawn:482-494`) and pass `--run-id` on every spawn of the wave.
[37-05](./37-05-orchestrator-integration-docs.md) documents that as part of the
pattern; a `watch` that cannot express "the wave" is worse than no `watch`.

**A consumer that *is* a tracked spawn is inside its own subscription.** Its
children inherit its run_id and its own handle carries it, so it is woken by its
own turn-end — and reacting ends another turn, which wakes it again. Because the
harness stops a monitor that emits too fast, the failure mode is not noise: it is
the supervision channel switching itself off mid-wave. **A subscription excludes
the subscriber's own handle by default.**

If narrower scoping is wanted later, an immediate-spawner field can be added and
filtered on additionally — but the wave identity is the useful default, not the
exception.

## 6. Rejected alternatives, with reasons

**`amux send` for child→parent events.** `amux send <name> <text>` injects
text+Enter as a **new user turn** (repo `architecture.md:393`). Rejected on two
grounds. Semantically, it forges a user turn: an orchestrator's role prompt
treats user messages as operator instruction, so a machine event wearing that
costume invites the consumer to read `029-a idle` as a directive. Mechanically,
`send-keys` into a live pane is unacked and racy. Claude Code's own event
notifications are explicitly typed as *not* replies from the user — that
distinction is exactly what we want, and reimplementing it worse is not
progress. `amux send` stays for parent→child and human→child, where "user
message" is *true*.

**MCP as the event channel.** MCP is request/response, and the Claude Code MCP
client does not turn a server notification into a new agent turn. An MCP server
therefore cannot wake an idle orchestrator; it would still need a blocking
`wait` tool, which is the poll we are removing, now with a tool-call timeout
ceiling. *This assumption is decisive and should be confirmed rather than
inherited* — see [37-06](./37-06-live-verification_human.md). MCP's value on the
**query** side is real and separable: typed `fleet_status()` output would have
prevented the hand-rolled `sed` parsing that produced several of the driving
run's defects. Deliberately left out of scope here, because a well-shaped
`watch` removes most of the querying that would justify it.

**Inotify over the handle directory.** Considered as the transport before the
Codex precedent was found. Rejected: it reconstructs, lossily, what the producer
already knows (§2). It remains a legitimate *implementation detail inside*
`watch` for noticing that a log has grown.

**A websocket / daemon fan-out.** `amux-server.py` already serves REST on
`:8822`, so a `/events` socket is achievable — but it would fan out over the same
files, buying cross-machine reach and nothing else. The local log is the whole
mechanism, not a stepping stone toward the socket. Deferred with reasoning in
[37-07](./37-07-remote-event-fanout_deferred.md).

**Keeping the transcript mtime as the activity clock.** No recorded rationale
existed for it; it was an implementation choice, confirmed by the operator as
carrying no requirement behind it. The one legitimate question it was groping
toward — *"have this worker's hooks stopped firing?"* — is answered directly and
honestly by tmux liveness, which `_derive_codex_status` already uses for the
Codex provider. Two providers should not answer the same question two ways.

## 7. Invariants

1. **No status answer depends on a transcript file** — its existence, mtime, or
   contents. This binds state, wait outcomes and digest entries.
   Transcript-derived *diagnostics* may remain as strictly additive context that
   is absent without a transcript and changes no verdict.
2. **The producer never blocks, delays, or fails a session.** Fail-open is
   preserved end to end; an event that cannot be written is dropped, never
   retried into the session's critical path.
3. **State is on disk and visible before the event announcing it** — ordering,
   not durability (§3). No `fsync` on the producer's hot path.
4. **One consumer, one subscription, any number of workers.**
5. **Every terminal outcome emits.** Silence means "nothing happened", never
   "something happened that the filter did not match".
6. **A digest is a superset of its predecessors**, so acting on the newest is
   always safe.
7. **Handles with no event log still resolve** to a documented, sane state.
   Rollout is not a flag day.
8. **One module owns the log format.** Producer and every reader import it;
   neither reimplements parsing. (Epic 23's rule for `questions_store.py`.)
9. **Untracked sessions are untouched.** Handle-gating and provider-gating stay.
10. **The subscriber is never inside its own subscription**, and a worker that
    joins the wave late is inside it.
11. **The stream answers "is a turn open" by itself.** A turn beginning is a
    recorded event, never an inference from a file.
12. **A watchdog expiry is derived, never appended.** Readers stay readers.

## 8. Test surfaces

Existing suites that constrain this work: `tests/test_unit_spawn_producer.py`,
`tests/test_unit_amux_supervise.py`, `tests/test_unit_amux_reads.py`,
`tests/test_unit_amux_wait_print.py`, `tests/test_unit_notification_hook.py`,
`tests/test_unit_codex_reducer.py`, `tests/test_unit_amux_ergonomics.py`,
`tests/fake_amux_env.py`.

Two fixture shapes matter most, and no existing fixture covers either.

**A tracked Claude worker with no transcript file at all** — the shape the
driving run exposed. Every reduction path should be exercised against it.

**A worker handed a follow-up turn after it settled** (`amux send`, then a turn
that has not ended yet). This is the case `open_turn` exists for, and the one a
naive event reduction reads as `idle` because the last recorded event is still
the previous `Stop`. It is the regression 37-02 is most likely to ship.
