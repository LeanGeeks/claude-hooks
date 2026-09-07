# Epic 37 — Evidence: the 2026-09-06/07 fleet run post-mortem

Measured driver for [brd.md](./brd.md).

**Orientation, so nothing here is mistaken for this repository.** The figures
below come from a *consumer* of `amux-spawn` — an unrelated codebase called
`leads-platform` that used the fleet tooling for a large parallel build. It is a
different repository with its own epic numbering (its `029`, `005`, … are not
`claude-hooks` epics), it is **not required reading**, and nothing in it needs to
be opened to implement this epic. Every claim below is restated here in
`claude-hooks` terms precisely so that no task depends on access to it.

The run: one orchestrator session, 2026-09-06 12:23Z → 2026-09-07 05:43Z,
supervising ~20 tracked workers spawned through `amux-spawn` across six of that
project's epics.

Recorded here because the raw material is perishable: the workers' own
transcripts do not exist (§3), and the orchestrator's will eventually rotate.
Where a figure is about the consumer's own behavior rather than the tooling's,
it is marked as such in §4.

## 1. Supervision collapsed to a single state

**Every worker in the fleet read `stuck`, all run.** Every teardown in the
orchestrator's transcript reads `amux-spawn rm: <unit> (stuck) killed and
removed` — including workers that had cleanly finished.

Representative `status --json`:

```json
{"name": "leads-platform-unit-028-pre", "state": "stuck", "stored_state": "running",
 "active": true, "stuck_after_s": 1800, "activity_age_s": 1822.6,
 "signals": {"live_background_tasks": true, "open_turn": false, "no_stop_yet": true}}
```

**Read that sample precisely, because a fixture built from it would test the
wrong thing.** This worker's `active` is over-determined: `live_background_tasks`
is `true`, so it would have read `stuck` at the threshold even with a correct
`no_stop_yet`. What it demonstrates is the *activity clock* failing — 1822 s is
wall-clock since spawn, not time since activity, because there was no transcript
to stat. The `no_stop_yet` collapse below is the general mechanism and the one
the whole fleet shared; the worker it applies to most cleanly is one whose
handle carries `"state": "idle"` with `"mtime_at_stop": null`. Build the fixture
from *that* shape.

Observed `activity_age_s` values, which under the broken path equal wall-clock
since spawn rather than time since activity:

| Worker | activity_age_s | = |
|---|---|---|
| `leads-platform-unit-028-pre` | 1 822 → 5 486 | 30 min → 1.5 h |
| `leads-platform--029-c2-unit-029-c2` | 3 614 | 1.0 h |
| `leads-platform--006-b-unit-006-b` | 28 502 | **7.9 h** |
| `leads-platform--006-c-unit-006-c` | 37 972 | **10.5 h** |

The last two are workers that were finished or blocked for most of a working day
with no way to say so.

**Confirmed cause**, from the live registry (`~/.amux/spawn/*.json`) — every
fleet handle carried `"mtime_at_stop": null`. Tracing the reduction:

```
transcript file absent
  → _current_mtime() → None
  → handle["mtime_at_stop"] = None            (spawn_producer_hook.py:150)
  → no_stop_yet = True, permanently           (.claude/bin/amux-spawn:1699)
  → active = live_bg or open_turn or no_stop_yet = True, permanently   (:1710)
  → idle unreachable; stuck at stuck_after, forever                    (:1720)
  → open_turn requires current_mtime > mtime_at_stop; both None → never evaluates
```

The producer hook had written `state: "idle"` correctly. The reader discarded it.

## 2. A second, independent defect

- **Handle-name resolution.** Spawning suffix `unit-028-pre` registers
  `leads-platform-unit-028-pre`. The orchestrator's first query,
  `amux-spawn status unit-028-pre`, returned
  `unit-028-pre: unknown (no tracked handle found)` and it spent several turns
  rediscovering the prefix rule.

**Checked and ruled out**, recorded so it is not re-investigated: the recorded
`transcript_path` is *correct*. `transcript_path_for()`
(`amux_spawn_lib.py:82-89`) builds `.jsonl`, and the live handles hold `.jsonl`.
An earlier pass of this analysis misread it as `.json` — a truncation artifact in
the inspection script, not a defect. The path is right; the file is absent,
for the reason in §3.

What that leaves is a subtler and more interesting problem than a bad path:
**nothing failed loudly when the artifact was missing.** A stat on a
never-created file returned `None` and the reduction carried on to a confidently
wrong answer. That silent-false-negative path, not the path construction, is what
37-04 should defend.

## 3. Total observability blackout

Child sessions warned:

```
⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker
  · restart with CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 to keep future transcripts
```

`CLAUDE_CODE_CHILD_SESSION=1` is **ambient and inherited down the process tree**
— it is present in unrelated sessions on this machine, not set per spawn.

Result: **26 fleet worktree project directories under `~/.claude/projects/`, zero
`.jsonl` transcripts.** Only 124 subagent `.meta.json` stubs survive. The fleet's
own behavior is unreconstructable; everything in this document was recovered from
the orchestrator's transcript and the handle registry.

This is why [brd.md](./brd.md) §4.4 requires that persistence become an
observability choice and never a correctness dependency — and why §6 requires the
event stream alone to be sufficient for a post-mortem.

## 4. Consequences measured on the consumer side

These are what the missing signal *cost*. They are recorded for sizing, and are
**not** this epic's work.

**Wall-clock.** Over a 17.3 h span, time inside gaps > 4 min totals **14.7 h
(85%)**. Individual gaps reached 139, 128, 60 and 37 minutes.

**Supervision was serialized.** 23 `Monitor` calls, each a hand-written
`while true` loop parsing `amux-spawn status --json` with `sed`. **17 of 23
watched a single handle** — while waiting on one worker, seven others got no
attention. Every one used the tool as a blocking poll that holds the turn open,
which is the documented anti-pattern; `Monitor` is a non-blocking event stream
("*each stdout line is an event — you keep working*"). The orchestrator was not
missing a mechanism. It misused the one it had, because the CLI offered only a
state to poll for.

**Tokens.** 660 assistant requests: 171 M cache-read, 2.5 M input, 627 K output.
Context grew monotonically **83 K → 432 K and never compacted**; per-hour average
context by the final hour was 432 K. Output split: **369 KB thinking** (179
blocks) / 189 KB prose (149 blocks) / 327 KB tool inputs — the largest single
category being extended reasoning, spent on a role that is mostly *poll →
dispatch → update a table*.

Note the cost model this implies for §4.3 of the BRD: **every wake-up is a full
request at current context size**. Eight uncoalesced completions late in a run
cost eight × 432 K. Coalescing is not a nicety.

**Out-of-scope consumer defects found in the same pass**, recorded so they are
not lost — they belong to the consumer project, not here:

- 30 hand-written worker prompts totalling 185.6 KB, of which **134.7 KB (73%)
  is 18 near-identical boilerplate lines**; unique per-worker content averages
  1.7 KB. No assembly step existed.
- 45 commits to a 16 KB fleet-status document in 17 h, repeatedly rewritten to
  stay under its own size cap.
- Host contention: 30 GB RAM, 24 cores, **11 GB swap in use**; two OOM sweeps
  killed CI wrapper processes, one *after* its CI had already passed. A 3-slot CI
  semaphore counts slots but not memory.

## 5. The asymmetry that suggested the fix

`codex_event_reducer.py`, verbatim:

> Epic 20 turns `amux-spawn` into a provider-neutral tracked-worker surface. The
> Claude side derives state from Claude Code hooks **plus the transcript mtime**;
> the Codex side has no hooks at all, so its lifecycle has to be *reduced* from
> three files.

The provider with **no** lifecycle events was forced into an append-only event
log with a pure reducer over it, and it works. The provider that fires a hook at
the instant of every event got mtime inference, and it failed totally the first
time a file was missing.

## 6. Reproduction

The failure needs no fleet:

1. Spawn one tracked Claude worker with transcript persistence off (the default
   in any session inheriting `CLAUDE_CODE_CHILD_SESSION`).
2. Let it finish a turn with no background work. Its handle will read
   `state: "idle"`; the hook did its job.
3. Ask `amux-spawn status <handle>`. After `stuck_after`, it reads `stuck`,
   and continues to for as long as the session exists.
