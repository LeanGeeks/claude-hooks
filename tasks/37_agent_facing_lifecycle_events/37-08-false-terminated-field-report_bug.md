# Task 37-08 — Field bug report: a live session reads `terminated`, and its digest stream goes silent for good

**Status:** todo · **Depends on:** 37-02, 37-03 · **Reported by:** the consumer project
(`leads-platform` Stage-C fleet, controller generation 11), 2026-09-09 · **Human-in-the-loop
report — not a code review, no fix proposed.**

Observations against real state, in the shape of [state.md](./state.md) §Field findings. I am a
consumer of `amux-spawn`, not a contributor to it; everything below is what I ran, what came
back, and what the ground truth was.

## Summary

A tracked worker session that was alive and working for 22 minutes was reported by
`amux-spawn status` as `state: terminated`, `active: false`. That terminal class was emitted
once into my `watch` subscription and then **nothing was ever emitted for that handle again —
including its actual completion**. Digests are level-triggered, the level had already latched at
a terminal value, so the worker finishing produced no wake-up. I waited; the human operator
noticed the session sitting idle and told me roughly 15–20 minutes after it had finished. The
mis-classification is **still true as I write this**, 1 h 39 min after the session started, with
its tmux pane alive and its `claude` process running.

## Environment

- `claude-hooks` at `579ac28`; `amux-spawn` at `~/.local/bin/amux-spawn`
- Provider `claude`, profile `claude-lg`, `--model=opus --effort=high --yolo --detach`
- Handle `leads-platform--006-13-unit-006-13`, run-id `b4bb3639-da8d-4e33-aa73-b821b225f971`
- Subscription: one `amux-spawn watch --run-id <wave> --since <cursor> --stuck-after 30m
  --timeout 4h` under a persistent background monitor, armed **before** the spawn

## What I did

All times UTC (local is +04).

1. **08:41** — took over the wave from a predecessor controller and removed it. The worker for
   unit 006-13 had terminated earlier while parked, so its uncommitted work sat in a worktree
   and I needed a replacement session for the same unit.
2. **08:42:33** — armed the subscription from a cursor 7 hours old. Digest `seq 0` replayed the
   old, genuinely terminated session. Correct.
3. **08:43:0x** — `amux-spawn rm leads-platform--006-13-unit-006-13` → *"(terminated) removed"*.
   Digest `seq 1` (08:43:31) showed `watching: 0`. Correct.
4. **08:43:37** — re-spawned **under the same handle name**, same run-id.
   `amux-spawn spawn … --run-id … --stuck-after 30m unit-006-13 -- "<seed>"` → exit 0, printed a
   new `session_id` `d9de0a8a-…`.
5. **08:43:46** — digest `seq 2` arrived: `{"settled":[{"name":"leads-platform--006-13-unit-006-13",
   "state":"terminated"}],"watching":1}`.
6. I checked that by hand, because the session had just started. `amux-spawn status --json` said
   `state: terminated`, `stored_state: running`, `active: false`, `artifacts: null`. `tmux
   list-panes` for the same session said `dead=0 pid=96924 cmd=claude`, and `capture-pane` showed
   the agent reading its prompt file and thinking. **I judged it a start-up race that would
   self-correct, and went on with other work. It never self-corrected — that call was mine and it
   was wrong, but the signal I based it on was the bug.**
7. **09:05:31** — the worker finished its chain and ended its turn with its completion line.
   **No digest.** I received nothing after `seq 2`.
8. **~09:2x** — the human operator asked me why I had not acted on an idle session. I ran
   `amux-spawn last <handle>` and found the completed final report waiting there.

## What I observed

**Derived state versus ground truth, at the same moment (now, 1 h 39 min in, pane still alive):**

| Source | Says |
|---|---|
| `amux-spawn status --json` | `state: terminated`, `active: false` |
| the same JSON | `stored_state: idle`, `updated_at 09:05:31.582Z` |
| the same JSON | `artifacts.transcript.status: present`, `activity: present`, `lifecycle_log: present` |
| `tmux list-panes` | `dead=0`, `pid=96924`, `cmd=claude` |
| `ps -o etime` on that pid | `01:39:07`, the spawn command line intact |
| registry file `…/leads-platform--006-13-unit-006-13.json` | exists, `state: idle`, correct `run_id` |

**The silent window was not quiet.** The lifecycle log recorded **25 events** between the
`terminated` digest (08:43:46) and the settle (09:05:31) — `turn_start` / `stop` /
`subagent_stop`, `state: running` throughout, ending `stop` with `state: idle`. So the event log
was healthy and being written the entire time the subscription treated the handle as finished.

**`artifacts` was `null` immediately after the re-spawn and is populated now.** At 08:43 the
registry entry carried `artifacts: null`; today's `status` output resolves all three artifact
paths, against the **new** `session_id`, all `present`. `active: false` did not change when the
artifacts appeared.

**The handle is missing from `amux-spawn ls` entirely.** `ls` currently prints three rows
(`leads-platform-adr013-review`, `leads-platform-judgment-006-13`, `leads-platform-resume-11`)
and does not list `leads-platform--006-13-unit-006-13` at all — while that handle's registry
JSON, lifecycle log and tmux session all exist and `status`/`last` on it answer fine. I do not
know whether this is the same defect or a separate one; recording it because a consumer that
rebuilt its watch set from `ls` would silently drop a live worker.

**`amux-spawn last` was correct all along.** The completion text was there whenever I chose to
ask. Nothing about the worker's own output was lost — only the notification.

## Why this mattered to me

The doctrine I work under says *silence must mean nothing happened*, and I am explicitly
forbidden from per-worker polling: one subscription over the wave, never a poll loop. That trade
only holds if a terminal class is true. Here a false terminal class was indistinguishable from a
correct one, and it **absorbed** the real completion rather than merely delaying it, so no later
event could rescue me — not `quiet` (the documented 30-minute fallback), because the handle was
already classified terminal and never entered the quiet path. The recovery procedures written for
`quiet | terminated | gone` all assume the *worker* is in trouble; none covers "the classifier is
wrong and will stay wrong".

Cost in this instance: ~15–20 minutes of unnoticed-worker-time on a finished unit, an escalation
that had to come from the human, and a measurement row in my own wave that now reads
"operator-detected" instead of "subscription-detected".

## What I ruled out

**The subscription cursor.** My operator's first hypothesis was that I had armed `--since` too
late, i.e. after the predecessor controller was shut down. The numbers say otherwise: I armed at
08:42:33 with `--since 1788918059.533` = **05:40:59 local**, which is **7 hours before** the
predecessor was removed (12:41 local) and **3 hours before** the re-spawn (12:43 local). A cursor
placed "just before the predecessor's shutdown" would have been *later* than mine and replayed
*less*. I also did receive three digests for this very handle. The miss was not a cursor gap.

**A dead pane.** Checked directly, three times across the incident, including now: `dead=0` and
the `claude` process alive with its original argv.

**A crashed or wedged worker.** It completed its work correctly and committed it; the final
report was in `last` all along.

## Reproduction shape

I have **not** re-run this in isolation — it happened once, inside a live wave, and I did not
want to spend a second real worker on it. The shape that produced it:

1. Spawn a tracked session with `--run-id`, let it terminate.
2. Arm `amux-spawn watch --run-id <same>` under a persistent monitor.
3. `amux-spawn rm <handle>` (the terminated one), then re-spawn **under the same handle name**
   with the same run-id.
4. Read `amux-spawn status <handle> --json` within seconds of the spawn, and compare with
   `tmux list-panes` for the same session.
5. Let the new session run to completion and watch whether any digest arrives for its settle.

Whether the reused handle name matters, or whether any fresh spawn is classified this way for its
first seconds, I cannot say from one occurrence — I only ever saw it on the re-used name.

## What I would find most useful as a consumer

Not a fix — a rule I can trust. Specifically: whether a terminal class is ever allowed to be
emitted for a handle whose tmux session is alive, and whether a handle that has been reported
terminal can re-enter the watch set if it starts producing events again. My supervision doctrine
is built on the first being "no"; I have adjusted my own workflow on the assumption that, for
now, it is "yes".

## Later observation — a fresh handle name spawns clean (2026-09-09, same session)

About an hour after the incident I had to spawn another tracked session in the same environment, this time under a handle name **never used before** (`leads-platform-judgment-006-13-merge`, `--profile claude-lg --model=opus --effort=high --yolo --detach`, no `--run-id`). Checked ~6 seconds after spawn:

- `amux-spawn status --json` → `state: running`, `stored_state: running`, **`active: true`**, and all three artifact paths already resolved and `present` against the new `session_id`.
- `tmux list-panes` → `dead=0`, `cmd=claude`. Derived state and ground truth agreed.

So a fresh spawn under an unused name did **not** reproduce it, in the same shell, minutes apart, on the same box. The only difference I can point at is that the failing handle's name had been used by an earlier session which I removed with `amux-spawn rm` immediately before re-spawning. That is one occurrence against one counter-example, not a proof — but if you are looking for where to start, the reused-name path is the difference I actually observed.
