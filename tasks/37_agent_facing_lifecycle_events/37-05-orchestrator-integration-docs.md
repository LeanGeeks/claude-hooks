# Task 37-05 — Orchestrator integration, installer, and docs

**Status:** todo · **Depends on:** 37-03, 37-04

## Goal

Make the new supervision model the obvious one to use. Package it, document the
consumer-side pattern, and retire the anti-pattern that the previous surface
made natural.

## Why

The driving run's orchestrator was competent and still got this wrong 23 times
in a row, because the available surface only offered a state to poll for. A
correct mechanism that is not obvious will be misused the same way. The docs are
part of the fix, not an afterthought.

## Read first

- [brd.md](./brd.md) §3 · [architecture.md](./architecture.md) §§1, 4, 6
- [evidence.md](./evidence.md) §4 — the misuse to design out
- `docs/amux-spawn-codex-workers.md` — the operator doc this parallels
- `architecture.md` (repo root) §§"Client-side hook events",
  "Idle notification (current)", "Host environment: amux" — all three need
  updating once 37-01 lands
- `install-claude-config.sh`, `install-amux.sh`, `shell/amux-spawn.bash`,
  `shell/amux-spawn-completion.bash`
- `tests/test_amux_pin.py`

## Work

1. Document the supervision pattern an orchestrating agent should follow: arm
   one persistent subscription for the wave, keep working, act on digests. Show
   it end to end, with a real multi-worker example.
   **Include the wave-identity discipline**, because the subscription is useless
   without it: mint or capture one run id and pass `--run-id` on every spawn of
   the wave, since `resolve_run_id` otherwise mints a fresh one per spawn for any
   caller that has no handle of its own ([architecture.md](./architecture.md)
   §5). This is the one thing an orchestrator author must get right before
   anything else in the epic helps them.
2. Document the anti-pattern explicitly and say why it is wrong — a blocking
   poll loop inside a streaming tool, one watcher per worker, filtering for
   success only. Naming the failure is what stops it recurring.
3. Update the repository architecture document so the hook table, the idle-
   notification flow, and the tracked-worker lifecycle describe what is now true.
   Be precise about the idle branch: `session_started_by_agent → silent` at
   `architecture.md:367` is **unchanged** — the agent-facing idle fact is
   recorded by the producer's `Stop`, not by giving that branch a second sink
   ([architecture.md](./architecture.md) §2). Say that plainly, since the
   opposite reading is the natural one.
4. Package everything: installer, shell completions, any new artifact locations,
   and whatever pin or version coupling applies. **Verify the turn-start hook
   registration 37-01 added** survived into a clean install —
   `install-claude-config.sh:664-745` wired `PreToolUse`, `PermissionRequest`,
   `PostToolUse`, `Notification`, `Stop`, `SubagentStop` and `SessionEnd` before
   this epic, and an unregistered producer event is an event that never fires.
   Remember that installed hooks only take effect after
   `./install-claude-config.sh` re-runs.
5. Give operators a diagnostic path — how to inspect a worker's event stream by
   hand, and how to tell "no events" from "no worker".
6. State the guidance for consumer projects without writing their workflow for
   them: what an orchestrator role prompt should say about supervision, so
   projects like `leads-platform` can adopt it in their own words.

## Implementation hints — suggestions, not instructions

- The single most valuable sentence in the docs is probably the distinction
  between the two `Monitor` shapes: an unbounded stream armed once with
  `persistent: true` (this epic's pattern), versus a bounded
  `run_in_background` command that exits when a condition is met (the one-shot
  wait). The driving run conflated them and got the worst of both.
- Worth one line for orchestrators that are themselves tracked workers: they are
  excluded from their own subscription by default, and *why* — otherwise the
  first person to hit it will "fix" it by removing the exclusion.
- Worth stating the cost model in the docs — each wake-up is a full request at
  current context size — because it is what makes coalescing feel necessary
  rather than fussy to whoever tunes the debounce later.
- `docs/amux-spawn-codex-workers.md` is the closest existing precedent for tone
  and depth.

## Done when

- An orchestrator author can set up correct fleet supervision from the docs
  alone, without reading `amux-spawn` source.
- The anti-pattern is documented as such, with the reason.
- The repository architecture document matches the implemented behavior; no
  section still describes the idle event as unconditionally dropped for
  agent-spawned sessions.
- A clean install produces a working end-to-end supervision path.
- Operators have a documented way to inspect and diagnose the event stream.
