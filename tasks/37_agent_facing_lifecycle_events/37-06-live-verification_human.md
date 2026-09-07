# Task 37-06 — Live multi-worker verification

**Status:** todo · **Depends on:** 37-05 · **Human-in-the-loop**

## Goal

Prove the epic against a real fleet, under the conditions that broke the previous
one. Fixtures cannot establish these properties; the driving failure was a
composition failure that every unit test would have passed.

## Read first

- [brd.md](./brd.md) §6 · [evidence.md](./evidence.md) §§1, 4, 6
- [`../20_codex_background_workers/20-06-live-verification_human.md`](../20_codex_background_workers/20-06-live-verification_human.md)
  — the precedent for a live verification task and its evidence file

## Work

Run a real multi-worker wave with a real orchestrator and confirm each property
below with recorded evidence. Use disposable work; do not commit into the
verification target.

1. **Idle is reachable.** A worker with transcript persistence off finishes and
   reads `idle`. This is [evidence.md](./evidence.md) §6 run forward.
2. **Bulk supervision.** One subscription covers the whole wave. Confirm the
   orchestrator issues no per-worker poll loops for the run's duration.
3. **Burst coalescing.** Several workers settle at once; the consumer is woken
   once.
4. **Busy accumulation.** Workers settle while the consumer is mid-turn.
   Confirm nothing is lost, nothing interrupts, and delivery is a digest rather
   than a backlog. Record how many wake-ups actually occurred.
5. **Failure visibility.** Kill a worker without letting it complete. Confirm the
   stream says so rather than going quiet.
6. **Follow-up turns.** Send a settled worker a follow-up with `amux send` and
   confirm it reads as working, not `idle`, before its next `Stop` — the
   regression 37-02 is most exposed to, and one no fixture composition proves.
7. **No self-supervision.** If the orchestrator is itself a tracked worker,
   confirm its own turn-ends do not appear in its own subscription and it never
   wakes itself.
8. **Blocked visibility.** Drive a worker into a permission prompt and confirm it
   is identifiable as blocked, not as running or stuck.
9. **Late join, and late workers.** Arm a subscription after workers have already
   settled; confirm convergence on current truth. Then spawn a further worker
   into the *same* wave and confirm the already-armed subscription picks it up.
10. **Watchdog.** Let a worker exceed its threshold while legitimately busy, e.g.
   a long CI run, and confirm the reported state still says something true rather
   than being overwritten.
11. **Post-mortem sufficiency.** After the wave, reconstruct what happened from the
   event streams **alone**, with no session transcripts. This is the criterion
   the driving run could not meet. Do this **after tearing the workers down with
   `amux-spawn rm`**, not before: teardown is when a real post-mortem starts, the
   driving run reaped every worker, and 37-01's retention decision is exactly
   what this step tests.
12. **Confirm the MCP assumption.** [architecture.md](./architecture.md) §6
    rejects MCP as the event channel on the premise that the Claude Code MCP
    client cannot turn a server notification into a new agent turn. That premise
    is decisive and was inherited, not tested. Verify it; if it is wrong, say so
    and reopen [37-07](./37-07-remote-event-fanout_deferred.md).

## Comparison baseline

The driving run's numbers ([evidence.md](./evidence.md) §4) are the baseline
worth beating, and the honest ones to report against: 85% of wall-clock in
gaps over four minutes, 23 poll loops of which 17 were single-handle, workers
undetected as finished for up to 10.5 hours, and consumer context growing
83 K → 432 K across the run.

Measure at least: fraction of wall-clock spent waiting, number of consumer
wake-ups per completion, and worst-case detection latency from a worker settling
to its spawner knowing. Record the wave's run id handling too — whether the
`--run-id` discipline held across every spawn, or whether some worker fell out
of the subscription because it minted its own.

## Done when

- Each property above is confirmed with recorded evidence in a signoff file
  alongside this task.
- Detection latency and wake-ups-per-completion are reported as numbers.
- Any property that fails is written up as a follow-up task rather than
  explained away.
