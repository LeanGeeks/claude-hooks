# amux-spawn fleet supervision — orchestrator guide

How to supervise a wave of tracked Claude workers from an orchestrating agent
using `amux-spawn watch` (epic 37). This is the correct pattern: one persistent
subscription for the whole fleet, no blocking poll loops, digests that coalesce
concurrent completions.

For Codex workers, see `docs/amux-spawn-codex-workers.md`. For the
single-worker blocking path, see `amux-spawn spawn --wait`.

---

## Quick reference

```bash
# Mint a run id for the wave (do this ONCE before any spawn).
RUN_ID=$(amux-spawn spawn first-worker --run-id new --dir /path -- "…" \
         | grep 'run_id:' | awk '{print $2}')

# Pass --run-id on every subsequent spawn of the same wave.
amux-spawn spawn second-worker  --run-id "$RUN_ID" --dir /path -- "…"
amux-spawn spawn third-worker   --run-id "$RUN_ID" --dir /path -- "…"

# Arm one persistent subscription for the whole wave — then keep working.
# Use Monitor with persistent: true; do NOT put a loop inside it.
# amux-spawn watch --run-id <run-id> [--debounce 8s] [--since <cursor>]

# Inspect the event stream by hand.
cat ~/.amux/spawn/<name>.lifecycle.jsonl

# Read a worker's status including artifact paths.
amux-spawn status <name> --json

# Reap when you are done.
amux-spawn rm <name>
```

---

## Wave-identity discipline — the one thing to get right first

`amux-spawn watch` keys its subscription on `run_id`. The whole design depends
on every worker in the wave sharing the **same** run id — without it, a
subscription is impossible to arm, and nothing else in this guide helps.

`resolve_run_id` (`.claude/bin/amux-spawn:183`) mints a **fresh** run id per
spawn whenever the caller has no tracked handle of its own. A human-started
orchestrator session has no handle, so without intervention twenty workers carry
twenty run ids and no single subscription covers them.

The fix is discipline: **mint or capture one run id and pass `--run-id` on every
spawn of the wave**.

### Minting a run id

`spawn` accepts `--run-id new` to force-mint a fresh id and prints it on the
`run_id:` line of its output. Capture it before the second spawn:

```bash
# First worker mints the wave's run id.
amux-spawn spawn worker-001 --run-id new --dir /path/to/project \
    -- "Read CLAUDE.md and complete task 001"

# The output includes:
#   amux-spawn: spawned tracked session 'myproject-worker-001' in /path/to/project
#     provider:   claude
#     run_id:     3f9c7a1d-8b2e-4f5a-9c3d-1e2f3a4b5c6d
#     ...

RUN_ID=3f9c7a1d-8b2e-4f5a-9c3d-1e2f3a4b5c6d

# All subsequent spawns in this wave.
amux-spawn spawn worker-002 --run-id "$RUN_ID" --dir /path/to/project \
    -- "Read CLAUDE.md and complete task 002"
amux-spawn spawn worker-003 --run-id "$RUN_ID" --dir /path/to/project \
    -- "Read CLAUDE.md and complete task 003"
```

### Inheriting a run id (orchestrators that are themselves tracked workers)

If the orchestrator is itself a tracked `amux-spawn` session, `resolve_run_id`
inherits its run id automatically for workers it spawns — the inheritance is
free, but the self-exclusion below still applies.

---

## Arming the subscription

One persistent `Monitor` subscription covers the whole wave:

```
Monitor(
  command="amux-spawn watch --run-id <run-id> --debounce 8s",
  persistent=true
)
```

Armed once at wave start. Left running for the life of the wave. Stopped
with `TaskStop` when the last worker settles.

`watch` emits level-triggered JSON digests to stdout: each emission states
everything currently settled and unacknowledged. Successive emissions are
**supersets** — reading only the newest is always safe.

### Two Monitor shapes — do not confuse them

**This pattern: persistent stream.** `Monitor` with `persistent: true` runs the
command as a long-lived process. Each stdout line is a discrete notification to
the agent. The agent is **not blocked** — it keeps working while the monitor
runs. This is the pattern for fleet supervision.

**The one-shot wait.** `Monitor` as a `run_in_background` command that exits
when a condition is met. The agent waits on it explicitly, blocking that turn.
Equivalent to `amux-spawn spawn --wait` for a single worker. Correct only when
the consumer genuinely has nothing else to do.

Conflating the two — putting an `until` loop inside a `Monitor` — holds the
turn open (blocking) while also running a process that emits to the agent
(streaming), combining the cost of both and the benefit of neither.

---

## Working while watching

The agent arms the subscription, **then does other work**. Each digest is a
notification delivered at a natural boundary; the agent need not stop to poll.

```
1. Arm Monitor with persistent: true.
2. Begin the orchestration work — planning, spawning, bookkeeping.
3. On notification: read the digest; act on newly settled handles;
   resume other work.
4. Repeat until all handles are settled and the wave is complete.
5. TaskStop the monitor.
```

The digest JSON has this shape:

```json
{
  "seq": 3,
  "ts": "2026-09-07T14:22:01.123456+00:00",
  "cursor": "1757257321.456",
  "settled": [
    {"name": "myproject-worker-001", "state": "idle",
     "last_message": "Task 001 complete. Output written to…"},
    {"name": "myproject-worker-003", "state": "terminated",
     "last_state": "running"}
  ],
  "watching": 5,
  "pending": 2
}
```

- `settled` — every handle that has reached a terminal state since the
  subscription was armed (or since `--since <cursor>` if resuming).
- `pending` — count of handles still running.
- `cursor` — pass as `--since` to resume without replaying already-seen entries.

### Late join and resume

Workers spawned **after** the subscription is armed are automatically covered —
`watch` re-enumerates the registry on every poll cycle, not once at startup.

To resume after a restart or reconnect without replaying history, pass the last
known cursor:

```
Monitor(
  command="amux-spawn watch --run-id <run-id> --since 1757257321.456",
  persistent=true
)
```

---

## Complete multi-worker example

```bash
# ── Orchestrator: arm subscription and spawn wave ────────────────────────────

# Step 1: mint run id via the first spawn.
FIRST_OUT=$(amux-spawn spawn build-001 --run-id new --dir /repo -- \
    "Complete build task 001 and write results to output/001.md")
RUN_ID=$(echo "$FIRST_OUT" | awk '/run_id:/{print $2}')

# Step 2: spawn the remaining workers with the same run id.
amux-spawn spawn build-002 --run-id "$RUN_ID" --dir /repo -- \
    "Complete build task 002 and write results to output/002.md"
amux-spawn spawn build-003 --run-id "$RUN_ID" --dir /repo -- \
    "Complete build task 003 and write results to output/003.md"

# Step 3: arm the subscription. In the agent role, this is a Monitor call.
# The agent is NOT blocked after this.
# Monitor(command="amux-spawn watch --run-id $RUN_ID --debounce 8s",
#         persistent=true)

# ── On each digest notification ──────────────────────────────────────────────
# The agent reads the digest JSON from the notification content.
# For each entry in "settled":
#   - state == "idle"        → worker finished; read last_message or status
#   - state == "terminated"  → worker crashed; investigate before retrying
#   - state == "stuck"       → threshold exceeded; worker may be wedged
#   - permission_pending == true  → worker blocked on a permission prompt;
#                                   decide via Telegram or amux send
```

---

## Self-exclusion for orchestrators that are tracked workers

When the orchestrator is itself a tracked `amux-spawn` session, its own
handle carries the wave's run id. An unfiltered subscription would include
it — and the orchestrator would be woken by its own turn-end.

This is **not just noise**. Reacting to the notification ends the
orchestrator's turn, which produces another event, which triggers another
wake-up. The harness's rate limiter stops a monitor that emits too fast,
switching the supervision channel off mid-wave exactly when it is in use.

`watch` excludes the subscriber's own handle by default. The flag
`--include-self` re-enables inclusion for testing.

---

## Cost model

Each wake-up is a **full request at the consumer's current context size**.
Late in a long orchestration run, that context can reach hundreds of thousands
of tokens. Eight uncoalesced completions at 400 K tokens each cost eight full
requests.

This is why the debounce window exists (`--debounce`, default 8 s): it
coalesces completions that land within the window into one digest and one
wake-up. Tuning the window trades latency for cost; the default is a
conservative starting point.

Every time a subscription is re-armed (after a restart or reconnect), the
agent pays one wake-up to reconverge on current state — which is why resuming
from a cursor (`--since`) is cheaper than a fresh arm when the history is long.

---

## Anti-patterns

### Blocking poll loop inside Monitor

```
Monitor(command="while true; do amux-spawn status worker-001 --json; sleep 5; done")
```

Wrong in three ways:

1. **Blocks the turn.** The `while` loop holds the shell process alive. The
   `Monitor` call does not return until the loop exits, so the agent cannot
   act on anything else while it waits.

2. **Serialized supervision.** One monitor per worker means N monitors for N
   workers, each holding a turn open. While worker 003 is watched, workers
   001 and 002 get no attention.

3. **O(N) wake-ups per completion.** A burst of simultaneous completions
   produces N monitor exits, N separate notifications, and N turns of work to
   process them — one at a time, at current context size each.

Use a single persistent `Monitor` with `amux-spawn watch --run-id` instead.

### One watcher per worker

```
Monitor(command="amux-spawn watch --handle worker-001", persistent=true)
Monitor(command="amux-spawn watch --handle worker-002", persistent=true)
Monitor(command="amux-spawn watch --handle worker-003", persistent=true)
```

This gives up bulk coalescing: simultaneous completions produce three
notifications and three turns instead of one. It also requires the
orchestrator to manage N subscriptions rather than one.

Use `--run-id` to cover the whole wave with one subscription, or pass
multiple `--handle` arguments to a single `watch` invocation.

### Filtering for success only

Acting only on `state == "idle"` and ignoring `terminated`, `stuck`, and
`permission_pending` leaves the orchestrator silent for anything that went
wrong. A crashed worker reads `terminated`, a wedged worker reads `stuck`,
and a blocked worker carries `permission_pending: true` — none of which
reach `idle`. Ignoring the other terminal states is what makes a crashloop
invisible for hours.

`watch` emits on **every** terminal outcome. Treat each one explicitly.

---

## Diagnostic path

### Inspect a worker's event stream by hand

```bash
cat ~/.amux/spawn/<name>.lifecycle.jsonl
```

Each line is a bounded JSON record. The `event` field is one of:
`turn_start`, `stop`, `subagent_stop`, `permission_prompt`, `session_end`.

The most recent `stop` event carries the worker's final `last_message` and
`state`. A `turn_start` with no following `stop` means a turn is currently
open.

### Tell "no events" from "no worker"

```bash
amux-spawn status <name> --json
```

Look at the `artifacts.lifecycle_log` block in the output:

```json
"lifecycle_log": {
  "path": "/home/you/.amux/spawn/myproject-worker-001.lifecycle.jsonl",
  "status": "present"
}
```

- `status: "present"` — the log exists and the worker has produced at least
  one event. If the log exists but appears empty, the worker was registered
  but the hook has not fired yet (spawning state).
- `status: "absent"` — the session has a handle but the event log was never
  written. This means either the session has not started its first turn yet,
  or the producer hook is not registered (run `install.sh --yes` and
  restart the session).

The `state` field in the same output is the authoritative reduced state from
the event log. If `state` is `idle` but `lifecycle_log.status` is `absent`,
the worker was registered but the log file was removed (not normal —
`.lifecycle.jsonl` survives `amux-spawn rm` by design).

### Confirm the producer hook is registered

```bash
python3 -c "
import json, pathlib, os
cfg = pathlib.Path.home() / '.claude' / 'settings.json'
hooks = json.loads(cfg.read_text()).get('hooks', {})
print('UserPromptSubmit:', bool(hooks.get('UserPromptSubmit')))
print('Stop:', bool(hooks.get('Stop')))
"
```

Both should print `True`. If not, run `./install.sh --yes` and
restart any affected Claude sessions (editing the repo hooks does not take
effect until the installer re-runs).

---

## Consumer project guidance

An orchestrator role prompt should state:

1. **Wave identity:** "Before spawning any workers, capture or mint one run id
   and pass it as `--run-id` to every `amux-spawn spawn` call in the wave."

2. **One subscription:** "Arm one `Monitor` with `persistent: true` running
   `amux-spawn watch --run-id <id>` before the first spawn, and keep it
   running for the life of the wave."

3. **Non-blocking supervision:** "After arming the monitor, continue with
   other work. Act on digests when they arrive as notifications — do not loop,
   do not re-arm after each notification, do not block."

4. **All terminal outcomes:** "A digest entry with `state: terminated`,
   `state: stuck`, or `permission_pending: true` requires attention just as
   `state: idle` does. Silence means nothing has settled, not that everything
   is fine."

5. **Cost awareness:** "Each notification is a full request at current context
   size. Prefer larger debounce windows later in a run when context is large,
   and stop the monitor with `TaskStop` promptly once the wave completes."

These five points are the minimum. The workflow file does not need to prescribe
a specific orchestration strategy beyond them.
