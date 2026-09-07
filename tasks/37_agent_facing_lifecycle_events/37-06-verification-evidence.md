# Epic 37 — Live verification evidence (37-06)

**Date:** 2026-09-07 · **CC version:** 2.1.263 · **Verifier:** Anton + Claude Opus 4.6

Wave run-id: `94654931-86f0-4041-ba12-df1d6dcc292d` (items 1–9, 11)
Self-exclusion run-id: `955a16d7-32ea-4719-b965-9dc3f1292719` (item 7)
Watchdog run-id: `2f1971d4-be4c-4ba6-893f-8fd2f2b8b90f` (item 10)

Workspace: `/data/sync/work/leangeeks-ai/ai-playground-2/`

## 1. Idle is reachable — PASS

Three workers (worker-a, b, c) spawned with transcript persistence on (37-04
default). All three completed trivial prompts and read `idle` in a single
watch digest:

```json
{"seq":0, "settled":[
  {"name":"ai-playground-2-worker-a","state":"idle","activity_age_s":8.2},
  {"name":"ai-playground-2-worker-b","state":"idle","activity_age_s":5.3},
  {"name":"ai-playground-2-worker-c","state":"idle","activity_age_s":5.1}
], "watching":3, "pending":0}
```

## 2. Bulk supervision — PASS

One `amux-spawn watch --run-id <uuid>` subscription covered all workers.
No per-worker poll loops were used. `watching: 3` (later `watching: 4` after
late join).

## 3. Burst coalescing — PASS

Three workers settled within 3 seconds of each other. The watch emitted one
digest (`seq: 0`) containing all three, not three separate emissions.

## 4. Busy accumulation — PASS

Worker-c entered `permission_pending` while other workers were already settled.
The watch accumulated the state change and emitted it as part of a digest
(seq=4) containing all settled handles, not as an individual interrupt.
Total emissions for the wave: 5 digests over the full session (initial + 4
state changes), not 10+ individual events.

## 5. Failure visibility — PASS

Worker-b was killed mid-turn (`tmux kill-session`). Status immediately read
`terminated`. The watch digest included worker-b as `state: "terminated"` with
its last message preserved from the prior turn.

## 6. Follow-up turns — PASS

Worker-a was settled (idle). `amux send` delivered a follow-up prompt.
`status --json` showed `state: "running"` immediately after, then `state:
"idle"` after completion. The turn-start event from `UserPromptSubmit`
correctly marked the turn as open.

```
idle → running (after amux send) → running → idle (after completion)
```

## 7. No self-supervision — PASS

Orchestrator (`ai-playground-2-orch`) spawned as a tracked worker in the same
wave as test-w1 and test-w2. A watch command run inside the orchestrator's
tmux session (via `tmux split-window`) showed:

```
watching: 2  pending: 0
settled handles: ['ai-playground-2-test-w1', 'ai-playground-2-test-w2']
```

The orchestrator's own handle was excluded. `resolve_amux_session()` detected
the tmux session name and filtered it from the subscription.

## 8. Blocked visibility — PASS

Worker-c triggered a permission prompt (`Bash` tool). `status --json` showed:

```json
{"state":"running", "permission_pending":true,
 "reason_context":{"pending_permission":{"tool_name":"Bash","request_id":"f51b42d9be5c"}}}
```

The watch digest also showed `permission_pending: true` for worker-c.

## 9. Late join — PASS

Worker-d was spawned into the wave after the watch was already armed.
The watch showed `watching: 4` (up from 3) and worker-d appeared in the
settled digest once it completed.

The `--since <cursor>` flag on a fresh watch converged on current truth
without replaying old events.

## 10. Watchdog — PASS

Worker spawned with `--stuck-after 5`. Given a complex coding prompt that
took >8 seconds. Status showed:

```
state: stuck
stale_activity: true
activity_age_s: 8.0
stuck_after_s: 5
turn_open: true
```

`turn_open: true` alongside `stale_activity: true` lets a consumer distinguish
"legitimately busy past threshold" from "genuinely wedged" (which would have
`turn_open: false` or no events at all).

## 11. Post-mortem sufficiency — PASS

After all workers completed, `amux-spawn rm` reaped all four handles.
Handle `.json` files were deleted. **All four `.lifecycle.jsonl` files
survived** the reap (37-01's retention decision: `cmd_rm` does not know
about lifecycle logs).

Reconstruction from event logs alone (no handles, no transcripts):

| Worker | Events | Reduced state | Notes |
|--------|--------|---------------|-------|
| worker-a | 4 | idle | 2 turns (original + follow-up) |
| worker-b | 3 | running/turn_open | Killed mid-turn — correct |
| worker-c | 6 | idle | Had permission prompt, resolved |
| worker-d | 3 | idle | Includes post-idle subagent_stop |

## 12. MCP assumption confirmed — PASS

Claude Code 2.1.263: MCP server notifications are processed by the client
(e.g., `notifications/cancelled` dismisses pending elicitations, verified in
CC v2.1.198) but do **not** create a new agent turn. An idle orchestrator
cannot be woken by an MCP server notification.

This confirms the architecture's rejection of MCP as the event channel.
37-07 (remote event fan-out) remains correctly deferred.

## Bug found during verification

**`subagent_stop` after idle `stop` reverted state to running.** Worker-d's
event log was `turn_start → stop(idle, bg=0) → subagent_stop(bg=0)`. The
reducer's `last_bg_setter` logic unconditionally set `"subagent_stop"` on
any `subagent_stop` event, overriding the prior `"stop"` that had already
confirmed idle. The reducer then derived `state_hint = "running"` (because
"last bg-draining event was a subagent_stop → running"), and the watchdog
fired → `stuck`.

**Fix:** Guard `last_bg_setter` override: a `subagent_stop` does not override
when the prior `stop` already confirmed idle (`last_stop_state == "idle"`).
Committed as `03f7064`.

**Root cause:** The reducer assumed `subagent_stop` events only arrive between
a `stop(bg>0)` and the final `stop(bg=0)`. In practice, Claude Code fires
`SubagentStop` after the session has already settled, as a cleanup event.

## Pre-existing edge case observed (not epic 37)

A partially-failed spawn (hooks errored before the handle was registered)
left orphaned `~/.amux/sessions/<name>.env` files. Amux created the tmux
session and `.env` registration, but `amux-spawn` never wrote the `.json`
handle. When the user exited the Claude sessions, the tmux sessions died but
the `.env` files persisted — blocking re-spawn of the same name. Manual
removal (`rm ~/.amux/sessions/<name>.env`) was required.

This is a pre-existing gap in amux's session lifecycle: no automatic cleanup
of `.env` on tmux session death, and no `amux-spawn` path to clean up a
registration that has no handle. The trigger in this session was the missing
`REQUIRED_HOOKS` entries for `lifecycle_events.py` and
`claude_event_reducer.py` (fixed in commit `03f7064`).

## Comparison against baseline

| Metric | Driving run (evidence.md §4) | Verification run |
|--------|------------------------------|------------------|
| Wall-clock in gaps >4min | 85% (14.7h of 17.3h) | 0% (no poll loops) |
| Poll loops | 23 (17 single-handle) | 0 |
| Worst detection latency | 10.5 hours | ~8 seconds (debounce window) |
| Wake-ups per completion | 1 per poll iteration | 1 per digest |
| Consumer context growth | 83K → 432K (never compacted) | N/A (no orchestrator session) |

## Signoff

All 12 items confirmed. One bug found and fixed (reducer `subagent_stop`
ordering). The epic's success criteria from [brd.md](./brd.md) §6 are met.
