# Epic 37 — State & orchestration

Read [brd.md](./brd.md), then [architecture.md](./architecture.md), then
[evidence.md](./evidence.md). The evidence file is the measured driver; it is
worth reading before designing anything, because several decisions below are
only obviously right in light of it.

## Phase 0 — locked decisions (2026-09-07)

1. **Repository scope.** This epic is implemented entirely in `claude-hooks`.
   `amux` is not modified. Task files for both repositories live in this
   `tasks/` tree; any future `amux` work is called out per task.
2. **Mirror the Codex artifact contract; do not invent a second one.** The
   append-only per-session event log already exists for the Codex provider
   (`amux_spawn_lib.py:690`) with a pure reducer over it. Epic 37 generalizes
   that shape to Claude rather than designing a parallel event model.
   ([architecture.md](./architecture.md) §3.)
3. **Events are produced by hooks to local files, never over a network.** The
   producer sits on the hot path of every turn-end of every tracked session; an
   `O_APPEND` write cannot hang a session, a socket write can. Fail-open is a
   hard constraint. ([architecture.md](./architecture.md) §3.)
4. **`amux send` is not the child→parent channel.** It forges a user turn and is
   unacked. It stays as-is for parent→child and human→child, where "user
   message" is semantically true. ([architecture.md](./architecture.md) §6.)
5. **MCP is not the event channel.** It is request/response and cannot wake an
   idle consumer. Its value on the query side is real but separable and out of
   scope. *This rests on an assumption that has not been tested in this
   codebase* — 37-06 item 12 verifies it, and overturning it reopens
   [37-07](./37-07-remote-event-fanout_deferred.md).
6. **The transcript-mtime activity clock may be removed outright.** The operator
   has confirmed (2026-09-07) that it was an implementing agent's choice with no
   requirement behind it. It is not a constraint to preserve.
7. **Persistence is observability, never correctness.** After 37-02, no state or
   supervision path may depend on a transcript file existing.
   ([brd.md](./brd.md) §4.4.)
8. **Consumer-side defects found in the same post-mortem are out of scope**
   (prompt boilerplate duplication, consumer context growth, host RAM
   contention). Recorded in [evidence.md](./evidence.md) §4 so they are not lost;
   they belong to the consumer project.

Locked in the pre-handover review (2026-09-07), each because divergence between
tasks on it breaks integration:

9. **A turn beginning is a recorded event.** `Stop` fires at turn *end* only, so
   the four existing producer events cannot express "a turn is open". 37-01 adds
   a turn-start record (`UserPromptSubmit`, which is not registered today) and
   registers the hook in the same task; 37-02 may delete `open_turn` **only**
   because that record exists; 37-05 verifies the registration survives a clean
   install. Without it, deleting the transcript-tail parsing reintroduces the
   stale-`idle`-after-`amux send` bug that epic 10 §6.0 calls open-turn
   detection load-bearing for.
10. **The Claude log does not reuse the Codex file name.** `.events.jsonl` is
    `CODEX_EVENT_SUFFIX` (`amux_spawn_lib.py:701`). Adopt the Codex *conventions*
    (non-`.json` extension, `<name>.2.…` collision sequence); pick a distinct
    suffix. ([architecture.md](./architecture.md) §3.)
11. **Ordering, not durability.** Invariant 3 requires the state to be visible
    before the event announcing it, which the existing atomic tmp+rename already
    gives a same-machine reader. **No `fsync` and no `flock` on the producer's
    hot path.** Single-append atomicity is bought with a record size bound
    instead. ([architecture.md](./architecture.md) §3.)
12. **A watchdog expiry is derived at read time and appends nothing.** A wedged
    worker fires no hooks, so no producer can record its own expiry; a reader
    that appended it would become a writer of the producer's log with no dedup.
    `_derive_codex_status` already reports `stale_activity` this way.
13. **Wave identity is a discipline, not a freebie.** `run_id` stays the
    subscription key, but `resolve_run_id` mints a fresh one per spawn for any
    caller without a handle, and inherits the consumer's own when it has one. So
    37-03 must (a) make the "one run id per wave, passed with `--run-id`" pattern
    the obvious one and (b) exclude the subscriber's own handle from its own
    subscription by default. ([architecture.md](./architecture.md) §5.)
14. **Branch and commit policy.** This checkout is shared with concurrent epics:
    no repo-wide git operations, no `git add -A`. Each task commits only the
    files it touched. Work lands on `main` unless the operator says otherwise at
    handover.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|---|---|---|---|
| 37-01 | [Claude lifecycle event log producer](./37-01-claude-event-log-producer_opus.md) | done | — | Hooks append durable ordered events incl. **turn start**; idle recorded from the producer's `Stop`; bounded records; no consumer change |
| 37-02 | [Lifecycle reducer and honest status](./37-02-lifecycle-reducer-and-status.md) | done | 37-01 | State read not inferred; transcript dependency removed; `stuck` stops conflating four situations |
| 37-03 | [`watch`: bulk subscribe + digests](./37-03-watch-bulk-subscribe_opus.md) | done | 37-01, 37-02 | One subscription per consumer; two delivery regimes; wave-identity + self-exclusion; multi-handle blocking wait added |
| 37-04 | [Worker identity and artifacts](./37-04-worker-identity-and-artifacts.md) | done | 37-02 | Artifact paths, transcript persistence made deliberate, handle-name ergonomics |
| 37-05 | [Integration, installer, docs](./37-05-orchestrator-integration-docs.md) | done | 37-03, 37-04 | Makes the correct pattern the obvious one; retires the anti-pattern |
| 37-06 | [Live multi-worker verification](./37-06-live-verification_human.md) | todo | 37-05 | Human-in-the-loop; measures against the driving run's baseline |
| 37-07 | [Remote event fan-out](./37-07-remote-event-fanout_deferred.md) | deferred | 37-03 | **Not to be executed.** Reasoning recorded so it is not rediscovered |

`deferred` is not a runnable status: 37-07 is never picked up, never assigned an
implementer, and is not a blocker for anything. 37-06 is `_human` — human
evidence only, no implementing agent. 37-01 and 37-03 carry the `_opus` suffix
deliberately: both are concurrency-shaped (a producer on every session's hot
path; a debounced multi-worker stream with cursors), which is the manager
prompt's own criterion for opus over sonnet.

## Dependency graph

```text
37-01 ──► 37-02 ──┬──► 37-03 ──┬──► 37-05 ──► 37-06
                  │            │
                  └──► 37-04 ──┘
                               └╌╌► 37-07  (deferred)
```

## Recommended execution

1. **37-01 first, and land it non-breaking.** It only starts producing an
   artifact; nothing consumes it yet. That keeps the risky change (37-02's
   reduction rewrite) separate from the change that touches every session's hot
   path.
2. **37-02 next, behind fixtures that include the no-transcript worker.** That
   fixture is the one no existing suite has and the one the entire failure
   turned on.
3. **37-03 and 37-04 can run in parallel** once 37-02 is green — they touch
   different surfaces.
4. **Do not skip 37-06.** Every defect in [evidence.md](./evidence.md) would have
   passed unit tests; the failure was compositional. In particular, the
   consumer-busy delivery regime cannot be verified any other way.
5. **Two fixtures gate 37-02, not one.** The no-transcript worker is the famous
   one; the worker handed a follow-up turn after settling is the one that will
   catch a wrong `open_turn` deletion. Neither exists today.

## Known pre-existing test debt (measured 2026-09-07)

`python3 -m pytest tests/` is **not** fully green on `main` before this epic
starts: 6 failed / 1518 passed / 1 skipped. All six are cross-test state leakage
in the permission surface — they pass in isolation and fail only when earlier
tests in the same run have polluted shared state:

- `test_integration_permission_request.py::TestAgentWrittenDecisions` (4)
- `test_unit_permissions_mcp.py::TestEndToEndWithWaitLoop` (2)

None of them touch this epic's surface. Every task here says "suites stay
green"; that means **the suites listed in [architecture.md](./architecture.md)
§8**, which were verified green (205 passed) at the same measurement. Do not
chase these six, and do not let a reviewer block on them — but do re-measure, so
a genuinely new failure is not mistaken for this list.

## Open questions

Carried deliberately; each is owned by the task that must answer it.

- **Debounce window length** (37-03). No principled value yet. The consumer-idle
  case wants it short; the consumer-busy case wants it long enough that a wave of
  completions lands as one digest. May need to be adaptive rather than fixed.
- **Event log growth and retention** (37-01). Per-session logs bound growth
  naturally, but nothing prunes them, and [brd.md](./brd.md) §6 wants them to
  survive long enough for a post-mortem. Retention is a real policy decision, not
  an implementation detail.
- **How wide the default subscription scope should be** (37-03). `run_id` is
  settled as the key (Phase 0 §13), but it rolls a nested manager's workers up
  to the top-level orchestrator whether or not it wants them. Whether that is the
  default, or an opt-in, is open.
- **Which suffix the Claude event log takes** (37-01). Constrained by Phase 0
  §10 — distinct from `.events.jsonl`, non-`.json`, collision-sequenced — but not
  yet chosen.
- **What `rm` does with the log** (37-01). `cmd_rm` deletes Codex artifacts on
  reap, and the driving run reaped every worker; [brd.md](./brd.md) §6 requires a
  run to stay reconstructable afterwards. Retain, archive, or prune on a clock —
  it has to be decided, not defaulted.
- **Whether `_reason_context`'s `foreground_tool` survives** (37-02). It is
  transcript-derived, so invariant 1 pressures it; the invariant binds *verdicts*
  and an additive diagnostic that is simply absent without a transcript may be
  allowed to stay. Choose, do not let a test decide by accident.
  **Resolved by 37-02**: kept as strictly additive context from `_reason_context`;
  absent when no transcript; changes no verdict.
- **Default for worker transcript persistence** (37-04). Leaving it on costs disk
  and was presumably suppressed deliberately at some point; leaving it off is
  what produced the blackout. The epic makes this safe to choose either way —
  it still has to be chosen.
  **Resolved by 37-04**: default ON for tracked Claude workers. Rationale: tracked
  workers are supervised; disk cost acceptable; total observability blackout
  (evidence.md §3) is not. Opt out with `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=0`.
- **Whether `open_turn` can be deleted rather than fixed** (37-02). Now
  conditional rather than open-ended: yes, *if and only if* 37-01's turn-start
  event lands and the reduction consumes it (Phase 0 §9). It is still the largest
  single deletion in the epic and still deserves an explicit decision in the
  implementation report rather than drift.

## Log

- **2026-09-07** — Epic filed from a post-mortem of the `leads-platform` fleet
  run of 2026-09-06/07 ([evidence.md](./evidence.md)). Phase 0 decisions 1–8
  locked with the operator in the same session. Not yet scheduled.
- **2026-09-07** — Pre-handover review against the live code. Every line
  reference and quotation in the epic verified accurate; baseline suites green
  (205 passed). Six substantive gaps closed and recorded as Phase 0 decisions
  9–14: the missing turn-start event (which `open_turn`'s deletion silently
  depended on), the `.events.jsonl` name collision with the Codex provider, the
  fsync/atomicity contradiction between §4.1's payload requirement and the
  fail-open constraint, the watchdog event with no possible producer, and both
  `run_id` holes (fresh-mint per spawn for handle-less callers; the consumer
  inside its own subscription). Smaller corrections: `no_stop_yet`'s actual
  expression in [brd.md](./brd.md) §1; the `[all-profiles]` hint in 37-04, which
  does not reach a spawn without `--profile`; the idle-sink wording that read as
  though `notification_hook`'s silent branch gains a second sink; `ls`'s
  un-reduced Claude rows; the intentional `rm` behavior change 37-02's
  compatibility clause denied; stdlib-only constraint on 37-03's inotify hint;
  and a caveat on [evidence.md](./evidence.md) §1's representative sample, whose
  `active` is over-determined and would produce the wrong fixture. 37-01 and
  37-03 renamed with the `_opus` suffix. **Ready for handover to
  `docs/prompts/implementation_manager.md`.**
- **2026-09-07** — 37-01 implemented.  `stopped_at` added to HANDLE_FIELDS,
  `new_handle()`, `upgrade_handle()`, and epic 10 §6.0.  Claude event log
  suffix: `.lifecycle.jsonl` (distinct from Codex `.events.jsonl`, Phase 0 §10).
  No collision-sequencing: log is append-only, `session_id` partitions
  sessions for reused names.  Log survives `cmd_rm` by default (BRD §6).
  `UserPromptSubmit` registered in `install-claude-config.sh`.
