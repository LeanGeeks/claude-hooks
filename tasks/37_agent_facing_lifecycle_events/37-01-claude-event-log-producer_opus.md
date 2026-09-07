# Task 37-01 — Claude lifecycle event log producer

**Status:** todo · **Depends on:** — (first task of the epic)

## Goal

Give tracked Claude workers a durable, ordered record of what happened to them,
written at the instant it happens, so that supervision stops depending on
inference. No consumer behavior changes in this task — `status`, `last` and
`--wait` keep their current output. This task only starts producing the artifact
that 37-02 will reduce.

## Why

The information already exists and is thrown away. `spawn_producer_hook.py` is
dispatched per event with the full payload in hand; `notification_hook.py`
computes an idle event carrying the worker's last message and then discards it
for agent-spawned sessions because the only sink is Telegram. Meanwhile the
handle it writes is a lossy projection that cannot express *which* event fired or
in what order. See [brd.md](./brd.md) §1 and
[architecture.md](./architecture.md) §2.

## Read first

- [brd.md](./brd.md) §§1, 4.1, 4.5 · [architecture.md](./architecture.md) §§2, 3, 7
- [evidence.md](./evidence.md) §1 — the failure this prevents
- `.claude/hooks/spawn_producer_hook.py` — the existing producer and its
  event dispatch, fail-open discipline, and handle-gating
- `.claude/hooks/notification_hook.py` and
  [`../24_idle_notification_origin.md`](../24_idle_notification_origin.md) —
  the origin gate whose silent branch gains a second sink
- [`../10_spawn_sessions/architecture.md`](../10_spawn_sessions/architecture.md)
  §6.0 — the binding handle schema: the field set a producer may write, and the
  rule that state is derived at read time and `stuck` never persisted. Note its
  "**No task invents fields outside this list**" — see work item 7 for how that
  is extended rather than broken
- `.claude/hooks/amux_spawn_lib.py:686-760` — the Codex artifact naming,
  extension-collision reasoning, and collision-sequence allocation this task
  should mirror rather than reinvent
- [`../20_codex_background_workers/architecture.md`](../20_codex_background_workers/architecture.md)
  §§3–6 — the artifact contract being generalized
- [`../23_async_questions/architecture.md`](../23_async_questions/architecture.md)
  — durable-write-before-notify, and the one-module-owns-the-format rule
- `tests/test_unit_spawn_producer.py`, `tests/test_unit_notification_hook.py`,
  `tests/fake_amux_env.py`

## Work

1. Define the Claude lifecycle event record: what a consumer needs in order to
   act **without opening the worker's transcript or its pane**. At minimum it
   must distinguish which lifecycle event fired, carry the resulting state, and
   let a reader tell "finished" from "finished with background work outstanding"
   from "waiting on a permission decision" from "a turn is open right now" from
   "gone".
2. Emit one record per lifecycle transition, appended durably and in order,
   for tracked Claude sessions only.
3. **Record the start of a turn, not only its end.** `Stop` fires at turn end,
   so the existing four events cannot express "a turn is open" — which is the
   whole job of the transcript-tail `open_turn` parsing that 37-02 is expected
   to delete. Without a turn-start record, a worker handed a follow-up through
   `amux send` still reads `idle` off its last `Stop`, and `--wait` on that
   follow-up returns the *previous* turn's `last_message` immediately. Epic 10
   [`architecture.md`](../10_spawn_sessions/architecture.md) §6.0 calls
   open-turn detection **load-bearing** for exactly this reason.
   `UserPromptSubmit` is the hook that carries the fact; it is not registered
   today (`install-claude-config.sh:664-745` wires `PreToolUse`,
   `PermissionRequest`, `PostToolUse`, `Notification`, `Stop`, `SubagentStop`,
   `SessionEnd` and nothing else). **Register it here**, in the same jq block as
   the other producer events — otherwise the event exists only in fixtures and
   nothing fires it on a real machine; 37-05 then verifies and documents the
   packaging rather than discovering it. Keep it handle-gated and fail-open like
   every other producer event, and remember that an edited hook does not take
   effect until `./install-claude-config.sh` re-runs.
4. Give the producer ownership of the facts it already knows, instead of
   restating them as file stats. In particular the *"the turn ended"* fact and
   the *"when"* fact are two different things and both belong to the hook.
5. Record the idle transition that is currently dropped for agent-spawned
   sessions — **sourced from the producer path, not from `notification_hook`'s
   transcript extraction**. Concretely: the idle event in this log is the
   producer's `Stop` with no outstanding background work, *not* the harness's
   `idle_prompt` `Notification`, which is a different event at a different
   moment and cannot carry the payload. See the warning below; this is the one
   place where the obvious implementation is a trap. `notification_hook` is not
   modified: human-started sessions keep the existing Telegram path, and the
   agent-spawned branch stays silent.
6. **Bound the record.** Cap the serialized size so one append stays atomic
   (under `PIPE_BUF`, 4096 bytes on Linux), truncating an oversized
   `last_message` with a marker rather than dropping the record. Do not take a
   lock and do not `fsync` — see [architecture.md](./architecture.md) §3 for why
   both would be paid on the hot path of every turn-end of every tracked session
   to buy back what the size bound already gives.
7. Extend the handle schema explicitly rather than by drift. A producer-owned
   turn-end timestamp (and whatever else the reduction needs) is a new §6.0
   field, and §6.0 says no task invents fields outside its list — epic 20
   already extended it once, so the precedent is *amend the list*, not ignore
   it. Any new field goes into `lib.HANDLE_FIELDS`, into epic 10's §6.0 block,
   and into this epic's `state.md` log. Three suites assert the exact key set
   (`tests/test_unit_spawn_producer.py:123`,
   `tests/test_unit_codex_reducer.py:136`, `tests/test_unit_amux_spawn.py:149`)
   and will tell you immediately if you missed one.
8. Decide and document the artifact's lifecycle: naming (a suffix distinct from
   the Codex `.events.jsonl` — [architecture.md](./architecture.md) §3), where a
   reused handle name goes, growth bounds, and what `rm` does with it. **The
   `rm` half is not a free choice**: `cmd_rm` currently deletes the Codex
   artifacts on reap, the driving run reaped every worker, and
   [brd.md](./brd.md) §6 requires a run to stay reconstructable from the streams
   afterwards. Whatever you decide has to satisfy that, and
   [37-06](./37-06-live-verification_human.md) item 11 tests it.
9. Preserve every existing gate and guarantee: handle-gated, provider-gated,
   fail-open, atomic handle writes, unrelated §6.0 fields untouched.

Consumers are not changed here. `status` may keep deriving exactly as it does
today; 37-02 replaces that.

## Implementation hints — suggestions, not instructions

Take these if they hold up; discard any that do not, and record the reasoning.

- The Codex provider already has this artifact:
  `~/.amux/spawn/<name>.events.jsonl`, append-only, with `.err`/`.rc` siblings
  and `<name>.2.events.jsonl` collision sequencing
  (`amux_spawn_lib.py:690`, `:701`). Reusing the convention means one artifact
  contract instead of two, and `.jsonl` provably cannot collide with the
  `*.json` handle glob (`:698`).
- `spawn_producer_hook.py:150` and `:173` currently write
  `handle["mtime_at_stop"] = _current_mtime(transcript_path)`. A producer-owned
  timestamp written alongside it — the hook knows the turn ended, and knows
  when — would remove the transcript dependency without changing any existing
  field. Keeping the old field written as-is during this task keeps 37-01
  non-breaking.
- **Do not build the agent-facing idle event on `notification_hook`'s inputs.**
  Both of them come from the transcript — `extract_last_agent_message()` returns
  `None` when the transcript is "missing/unreadable"
  (`notification_hook.py:440-452`), and `has_active_background_agents()` bails at
  `:231-233` for the same reason. For a spawned worker with no transcript, that
  path yields an empty event *precisely in the case this epic exists to fix*.
  `spawn_producer_hook.handle_stop` has both facts from the **hook payload**
  instead (`last_assistant_message` at `:138`, `background_tasks` via
  `_payload_background_tasks()` at `:133`) —
  which is why handles carried good `last_message` values all through the
  driving run while no transcript existed. The producer already holds everything
  the event needs.
- The origin gate itself (repo `architecture.md:367`,
  `session_started_by_agent → silent`) is still the right place to express
  *"agent-spawned sessions do not page a human"*. What changes is that the
  branch stops being a dead end. The human path is the other branch and should
  not be touched.
- Ordering: append **after** the handle write, so no event ever describes a state
  that is not yet on disk.
- Keep the append inside the existing fail-open guard. An `O_APPEND` write of one
  short line is the reason the producer can stay on the session's hot path at
  all — anything that can block or hang does not belong here
  ([architecture.md](./architecture.md) §3).
- One module should own the record format, imported by both producer and every
  reader, per epic 23's rule for `questions_store.py`.

## Done when

- A tracked Claude worker with **no transcript file** produces a complete,
  ordered event record for a full lifecycle: spawn → turn start → turn end with
  background work → subagent completion → turn start → turn end clean →
  session end.
- Each of turn start, `Stop`, `SubagentStop`, `SessionEnd` and the
  permission-prompt notification is distinguishable in the record from the
  others.
- After a settled worker is handed a follow-up turn, the log alone shows a turn
  is open — no transcript consulted.
- An idle transition for an agent-spawned session is recorded from the producer
  path. `notification_hook` is unchanged: a human-started session's idle
  notification still reaches Telegram byte-identically, and the agent-spawned
  branch is still silent.
- A record whose payload would exceed the size bound is truncated, still parses,
  and still identifies its event.
- A hook whose event write fails for any reason still exits 0 and leaves the
  session undisturbed and the handle uncorrupted.
- Sessions with no tracked handle produce nothing — including for the newly
  registered turn-start hook.
- Codex handles and their artifacts are unaffected, and no Claude log is written
  to a path the Codex provider owns.
- Existing producer and notification-hook suites pass; new fixtures cover the
  no-transcript worker.
