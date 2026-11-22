# 50-06 — Dispatch: session policy (D4) and delivery (D5)

**Status:** todo · **Depends on:** 50-05
**Read first:** [brd.md](./brd.md) §4 (OD-6, OD-7, OD-8) · [architecture.md](./architecture.md) §3, §9 (all), §11
(all), §13.3, §14.3 (event routing), §23 (commit points) · [state.md](./state.md) P0-1, P0-9, OQ-4, invariants 2,
5 · review M6, m2 · `amux send` in `/usr/local/bin/amux` (`cmd_send`)

## Goal

Decide, for each event, which session gets it — reuse, rotate, resume or spawn — and get it there exactly once,
never typed into text the operator is writing, with the state on disk before every side effect.

## Scope

In `epic_driver_lib.py`:

1. **Event files** (§9.1): building items, merging at most one driver event per pass and one per worker, numbering
   from `next_event_n`, writing `events/E-<n>.md` with the front matter of §9.1.
2. **Policy** (§11.2): the decision table, including the overage rule (m2: cold only because of the 5m bucket ⇒
   reuse in place; rotation only over the cap) and `compacted` ⇒ rotate.
3. **Rotation** (§11.3): the rotate event, the wait for `HANDOFF:` + idle + a newer handoff file, the gen + 1 spawn
   seeded with the handoff and the queued events, parking, reaping after the new session's first `turn_start` unless a
   client is attached, the 2-tick timeout with fallback to Reuse.
4. **Resume** (§11.4): the resume event (inflight markers, last event, ground-truth instruction), the call
   `amux-spawn resume <name> --json -- "RESUME E-<n>: …"`, the after-reboot selection (turn open or step in flight
   now; others lazily).
5. **Spawn**: the `amux-spawn spawn <suffix> --dir <dir> --yolo --model=<m> --effort=<e> [--profile <p>] --run-id
   <run_id> [--pin-env K=V …] --detach -- "EVENT E-<n>: read <path>"` command line from the role (P0-3 normalises the
   `=` form inside amux-spawn), with the role prompt (§19) named in the seed; `name_in_use` ⇒ `spawn_failed`.
6. **Keep-warm** (§11.5): the 40–50 min window, `k·B/C` hours against the role's next scheduled event (computed from
   50-04's schedule), the Opus/Fable price class from the expected model id, no ping under overage, a limit, or
   `keep_warm: false`.
7. **Delivery** (§9.2, §9.3): preconditions (idle, `pane_in_mode` 0, the input box empty **always**, a pane that cannot
   be parsed defers), deferral counting, the send through `amux send`, the single retry only when the first send
   provably did not land, then no further sends; the `VERDICT?` reminder once. Input-box parsing derived from captured
   panes of the installed Claude Code version and pinned with fixtures.
8. **Commit points** (§23 step 6): event file + outbox entry + inflight markers + session record (`spawning`/
   `resuming`) committed before the side effect, the result committed after. Expressed as a sequence of
   `(state mutation, effect)` pairs that 50-08's tick executes, so a fake `World` can kill the pass between any two.
9. The `feedback` event built from an `epic-driver queue` request (OQ-4) — the request is stored in the state by the
   CLI (50-08); this task turns it into an event under the same policy.

## Tests

- Policy: one test per §11.2 row with fixture readings (warm, cold, over cap, overage-cold, dead, dead with a
  handoff, busy, limited, compacted).
- Rotation: the full sequence; the timeout and fallback; an attached client delays reaping; queued events move to the
  new session.
- Resume after reboot: only the sessions that should resume at once do; the others on their next event.
- Delivery: copy-mode defers; typed text in the input box defers (attached and detached); a multi-line draft defers;
  an unparseable pane defers; 3 deferrals ⇒ condition; confirm on first send; retry once when absent; no third
  send; a line sitting in the input box is not re-sent.
- Keep-warm: inside and outside the 40–50 min window; next event inside and outside `k·B/C`; Fable vs Opus `k`;
  overage and limits suppress.
- Crash safety: killing the pass between each commit and its effect, then running the next pass, never sends an event
  twice, never spawns a second session for a name, and never loses an inflight marker.

## Done when

- Every effect the dispatcher wants is preceded by a committed state that records it (invariant 5).
- No path sends an event to a pane whose input box is not provably empty.
