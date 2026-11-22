# amux-spawn — agent orchestration reference

Reference for an agent that spawns and supervises other Claude agents.

## Spawning workers

```bash
# First worker mints the wave's run-id.
amux-spawn spawn <suffix> --run-id new --dir <workspace> [-- "seed prompt"]

# Output includes:
#   run_id: <uuid>

# All subsequent workers use the SAME run-id.
amux-spawn spawn <suffix> --run-id <uuid> --dir <workspace> [-- "seed prompt"]
```

Every worker in a wave **must** share one run-id. Without it, bulk
supervision is impossible. `--run-id new` mints a UUID; capture it from the
output and pass it to every subsequent spawn.

If the orchestrator is itself a tracked `amux-spawn` session, its run-id is
inherited automatically — no `--run-id` flag needed.

Workers are spawned detached when stdin is not a TTY (the normal agent case).

## Supervising a wave

Arm **one** persistent Monitor for the whole wave, then keep working:

```
Monitor(
  command="amux-spawn watch --run-id <uuid>",
  persistent=true
)
```

Do not loop. Do not block. Do not re-arm after each notification.

Each notification is a JSON digest on one line:

```json
{
  "seq": 2,
  "ts": "2026-09-07T14:22:01+00:00",
  "cursor": "1757257321.456",
  "settled": [
    {"name": "proj-worker-a", "state": "idle",
     "activity_age_s": 12.3,
     "last_message": "Done. Output at output/a.md"},
    {"name": "proj-worker-b", "state": "terminated",
     "last_message": "Working on task b..."},
    {"name": "proj-worker-c", "state": "error",
     "error": "OSError: lifecycle log unreadable"}
  ],
  "watching": 5,
  "pending": 2
}
```

| Field | Meaning |
|-------|---------|
| `settled` | All handles that reached a terminal state, plus any whose status could not be derived (`state: "error"`) — cumulative, not incremental |
| `pending` | Count of handles still running |
| `watching` | Total handles in the subscription |
| `cursor` | Pass as `--since` to resume without replay |

In every digest, `len(settled) + pending == watching`. A handle is never
dropped from the stream: one whose status cannot be derived is reported
as a `state: "error"` entry instead of vanishing between the counts.

### Acting on a digest

For each entry in `settled`:

| State | Meaning | Action |
|-------|---------|--------|
| `idle` | Finished, nothing outstanding | Read `last_message`; process result |
| `terminated` | Session died or was killed | Investigate; retry if appropriate |
| `stuck` | Exceeded activity threshold | Query `amux-spawn status <name> --json` and check `signals.turn_open` — if true, may be legitimately busy; if false, likely wedged |
| `error` | The worker's status could not be derived — something is wrong with this handle's evidence | Investigate (`amux-spawn status <name> --json`, check the lifecycle log); do not treat as pending |
| any + `permission_pending: true` | Blocked on a permission prompt | Approve via Telegram or `amux send` |

When all handles have settled (`pending: 0`), stop the monitor with
`TaskStop` and proceed to teardown.

## Sending follow-up prompts

```bash
amux send <handle-name> "your follow-up instruction"
```

The worker will show `state: running` while processing the follow-up. Do not
poll for completion — the watch digest will report it.

## Querying individual workers

```bash
amux-spawn status <name> --json   # full state, signals, artifacts
amux-spawn last <name>            # last assistant message
```

Handle names support suffix resolution: if you spawned `worker-a`, you can
query it as `worker-a` instead of the full `proj-worker-a`.

## Blocking wait (single worker, nothing else to do)

For the degenerate case where the orchestrator has only one worker and
nothing to do while it waits:

```bash
amux-spawn spawn <suffix> --wait --dir <workspace> -- "prompt"
# Blocks until idle. Prints last_message to stdout.
# Exit 0 = idle, exit 3 = timeout.
```

Or for multiple handles without a persistent stream:

```bash
amux-spawn watch --run-id <uuid> --block --timeout 30m
# Prints each handle as it settles. Exit 0 = all done, exit 3 = timeout.
```

## Resuming a dead session

A tracked Claude session that crashed, was killed, or did not survive a
reboot is restarted **in place** — same name, same transcript, same model,
effort and profile:

```bash
amux-spawn resume <name> [--json] [--wait] [--timeout T] [-- "prompt"]
```

- It resumes only a **terminated** session: tmux gone, or a session whose
  pane is dead (that tmux session is removed first). A live session is
  refused — talk to it with `amux send`.
- The **handle decides** everything: amux-spawn re-exports the spawn's
  `--profile`, forces `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, drops
  `CLAUDE_CODE_CHILD_SESSION`, re-passes the `--pin-env` pairs, and adds
  `--model`/`--effort` only when amux's persisted `CC_FLAGS` lack them.
  `--yolo`, `--profile`, `--dir` and every pass-through flag are refused.
- Refused too: a missing transcript (the resume would be an empty, unrelated
  conversation) and a session with no model in either `CC_FLAGS`
  or the handle (it would run on the harness default). A missing effort is
  not refused: `--effort` is passed only when a source has one.
- amux appends `--resume <session_id>` itself. The prompt, if given, must
  not start with `-`.
- With a prompt, success means the transcript grew within 30 s. If it does
  not, the exit is 1 with "resumed but the transcript is not growing", the
  session is left running for inspection, and the handle is still updated
  (`attempt` + 1, `state` `spawning`) because the session was launched.
- `--json` prints `{name, provider, state, attempt, session_id,
  transcript_path, model, effort, profile}`; `attempt` goes up by one per
  resume and `state` is `spawning`. `--wait` has the same exit-code contract
  as `spawn --wait`.

Codex workers keep their own `resume` (the next bounded turn on the captured
thread); see `amux-spawn resume --help`.

## Listing and tearing down

```bash
amux-spawn ls --run-id <uuid>     # list wave members
amux-spawn ls --json              # machine-readable, all handles
amux-spawn rm <name>              # reap a finished worker
amux-spawn rm <name> --force      # kill and reap a stuck/running worker
```

Event logs (`~/.amux/spawn/<name>.lifecycle.jsonl`) survive `rm` — they are
the post-mortem record.

The handle (`~/.amux/spawn/<name>.json`) holds only the fields listed and
documented in `HANDLE_FIELDS` in `.claude/hooks/amux_spawn_lib.py`. Since
epic 50 that includes `profile`, `model`, `effort` and `pinned_env`, which
`spawn` records for Claude sessions and `resume` reads. Older handles do not
have these fields, and readers treat them as null.

## Self-exclusion

When the orchestrator is itself a tracked worker, `watch` excludes it from
its own subscription by default. This prevents a feedback loop where the
orchestrator's own turn-end wakes itself, which ends another turn, which
wakes itself again — until the harness rate-limiter kills the monitor.

Override with `--include-self` for testing only.

## Key flags

| Flag | On | Default | Purpose |
|------|----|---------|---------|
| `--run-id <uuid\|new>` | `spawn`, `watch` | auto-mint | Wave identity |
| `--debounce <duration>` | `watch` | `8s` | Coalesce burst completions |
| `--since <cursor>` | `watch` | none | Resume without replay |
| `--block` | `watch` | off | Edge-triggered blocking mode |
| `--timeout <duration>` | `watch`, `spawn --wait` | none | Patience limit (exit 3) |
| `--stuck-after <duration>` | `spawn`, `status`, `watch` | `600` (10 min) | Watchdog threshold |
| `--exclude <name>` | `watch` | none | Manually exclude a handle |
| `--handle <name>` | `watch` | none | Watch specific handles (repeatable) |
| `--force` | `rm` | off | Kill before reaping |
| `--pin-env KEY=VALUE` | `spawn` (Claude) | none | Pin a compaction variable in the pane, over the profile; recorded for `resume`. Keys: `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` (repeatable) |

Model selection has its own three flags — use the `--flag=value` form:

| Flag | On | Default | Purpose |
|------|----|---------|---------|
| `--profile <name>` | `spawn` | none | Backend (base URL, auth, tier alias map) |
| `--model=<alias\|id>` | `spawn` | inherited from parent's `--model` | Model tier |
| `--effort=<level>` | `spawn` | harness default (floats!) | Thinking budget |

An automated spawn should pin both `--model=` and `--effort=`; unpinned, the
child reads the operator's last interactive `/model`, and amux-spawn warns on
stderr. `--profile` alone pins neither. For Claude, amux-spawn rewrites
`--model=X`/`--effort=X` to `--model X`/`--effort X` before calling amux,
because amux persists only the space form in `CC_FLAGS` — that is what lets a
`resume` restart on the same model and effort. See
[`amux-spawn-model-selection.md`](./amux-spawn-model-selection.md) for the
full rules, the `--model opus` space-form trap, and `amux-spawn profiles`.

## What not to do

- **Do not poll.** No `while true; do amux-spawn status ...; done` inside a
  Monitor. Use `watch`.
- **Do not use one watcher per worker.** Use one `watch --run-id` for the
  wave.
- **Do not filter for idle only.** `terminated`, `stuck`, `error`, and
  `permission_pending` all require action. Ignoring them makes failures
  invisible.
- **Do not re-arm the monitor after each notification.** It is persistent;
  it keeps emitting.
- **Do not read transcripts to determine state.** Use `status --json`. State
  is reduced from the event log, not inferred from files.
- **Do not spawn on an unpinned model or effort.** Pass `--model=<alias>` and
  `--effort=<level>`; see
  [`amux-spawn-model-selection.md`](./amux-spawn-model-selection.md).
