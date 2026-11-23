# Epic 50 — Architecture

Read [brd.md](./brd.md) first. Phase 0 decisions (P0-n) are in [state.md](./state.md); decisions D1–D14 are the
spec's ([spec_rev2.2.md](./spec_rev2.2.md)). Task files cite sections of this file. Numbers marked *default* are
tunable constants, not decisions.

## 1. Shape

Three planes, three failure modes (D1):

| Plane | Lives on | When it dies |
|---|---|---|
| **Safety**: kill switches, leases, watchdogs, dead-man rollbacks | the epic's servers (crons, timers) | the epic is unsafe; not this epic's concern |
| **Driver**: the tick, its timer, its state | this workstation | progress pauses; nothing else |
| **Work**: amux-spawn sessions the tick starts | this workstation, in tmux | the tick resumes them |

```
systemd --user
  claude-epic-driver.timer (*:0/10) ─► claude-epic-driver.service (oneshot, KillMode=process)
                                           └─ epic-driver tick ──────────────┐   no LLM, one pass, flock
  claude-epic-driver-inhibit.service  (systemd-inhibit … sleep infinity)     │
  epic-driver-tmux.scope              (the tmux server, P0-5)                │
       ├─ <ws>-037-driver-g1            driver session (amux-spawn, --yolo)  │
       ├─ 037-t1-037-implementer-t1-g1  worker sessions                      │
       └─ epic-driver-keepalive         plain tmux session, keeps the server │
                                                                             ▼
 reads:  ledger.yaml (epic folder) · state.json (~/.local/state/epic-driver/<run_id>/) · ~/.amux/{sessions,spawn}
         · transcripts · lifecycle logs · the epic workspace's question queue · systemctl/tmux/git
 writes: state.json, events/, logs/ · amux-spawn spawn|resume|rm · amux send · notify (questions_mcp_lib)
```

The tick starts or wakes an LLM session only on an event (D2): a step became due, a worker settled, a check failed,
an answer arrived, an approval is needed, a session died, an alarm cleared. Otherwise it costs nothing.

## 2. Module boundaries

| Module | Owns | Task |
|---|---|---|
| `.claude/hooks/epic_driver_lib.py` (new) | All tick logic. Sections: ledger (§5), schedule (§6), checks (§7), state (§8), events and delivery (§9), verdicts (§10), session policy (§11), reconcile (§12), worktrees (§14), approvals and questions (§15), alarms and notify (§16), stamps (§17), tmux and environment (§21), the tick (§23) | 50-04 … 50-08 |
| `.claude/bin/epic-driver` (new) | The CLI (§18): argparse, path setup, stamp check, then calls into the lib. No logic of its own | 50-08 |
| `.claude/bin/amux-spawn` | `--model=`/`--effort=` normalisation, `--pin-env`, the Claude `resume` branch (§13) | 50-01 |
| `.claude/hooks/amux_spawn_lib.py` | New handle fields; `expected_model_id`; `last_assistant_entry`; `compaction_boundaries` (§13, §11.6) | 50-01, 50-03 |
| `.claude/hooks/claude_event_reducer.py` | The `terminated` latch (§13.4) | 50-02 |
| `context-mcp/context_mcp_lib.py` | `measure_transcript(path)` (§11.1) | 50-03 |
| `.claude/statusline/statusline.py` | `cache_seconds_left(path, now)` (§11.1) | 50-03 |
| `questions-mcp/questions_mcp_lib.py` | `notify(..., workspace_dir=None)` (§16.4) | 50-08 |
| `docs/prompts/epic-driver-driver.md`, `epic-driver-worker.md` (new) | Role prompts (§19) | 50-09 |
| `install.sh` feature `epic-driver`, `docs/epic-driver.md`, `docs/installer.md` | §17, §22, §24 | 50-10 |

**The effect boundary.** Every subprocess, tmux, systemctl, git, amux, amux-spawn and notify call goes through one
object, `World` (a small class in the lib with one method per effect: `tmux_display`, `capture_pane`, `amux_send`,
`spawn`, `resume`, `reap`, `run_check`, `git`, `notify`, `systemctl`, `now`, …). The real implementation is
`RealWorld`; tests pass a fake. Everything else in the lib is pure over explicit inputs and an explicit `now` (UTC,
timezone-aware). This is what lets 50-04 … 50-07 be unit-tested with no live sessions.

**Python floor.** The lib and the CLI run under the hooks' floor (`MIN_PYTHON_MINOR=9` in `install.sh`;
`tests/test_unit_python_compat.py` checks it): no PEP 604 unions evaluated at import, no `tomllib`. YAML comes from
system PyYAML (6.0.3 present), imported with `yaml.safe_load` only.

## 3. Name grammar (P0-1)

```
sanitise(x) := lowercase(x); every run of characters outside [a-z0-9] → "-"; strip leading/trailing "-"
               (an empty result is a validation error)
suffix      := <epic> "-" <role> [ "-" <step> ] "-g" <gen>        all parts sanitised; characters [a-z0-9-]
name        := basename(<dir>) "-" <suffix>                       = amux_spawn_lib.pick_free_name(prefix, suffix)
gen         := 1, 2, …   bumps on rotation only (§11.3); a resume keeps the name (§11.4)
```

- `role` is a key of the ledger's `roles:` map (`driver`, `implementer`, `reviewer`, …). The driver role has no step.
- `basename(<dir>)` is amux-spawn's workspace prefix (`workspace_prefix`). For workers the tick fixes the directory
  name (§14.2): `<epic>-<step>`. For the driver it is the epic workspace's own basename, which must match
  `[A-Za-z0-9_-]+` (tmux rewrites `.` and `:`); `enable` refuses otherwise.
- Sanitised step ids and role keys must be unique within a ledger; the whole name must be ≤ 100 characters.
- The handle's `attempt` counts resumes, which is why the grammar says `g<gen>`, not `<attempt>` (review m1).
- A second spawn of the same name fails on `name_in_use` (D3); that is the duplicate guard after a tick killed
  mid-spawn.

Examples (037, workspace `/data/sync/work/activecdn/acdn-workspace`):

| Session | `--dir` | Name |
|---|---|---|
| driver, first generation | the workspace | `acdn-workspace-037-driver-g1` |
| driver after one rotation | the workspace | `acdn-workspace-037-driver-g2` |
| implementer of step `T1` | `<worktree_root>/037-t1` | `037-t1-037-implementer-t1-g1` |
| reviewer of step `T1` | `<worktree_root>/037-t1` | `037-t1-037-reviewer-t1-g1` |

## 4. Runtime layout (P0-2)

Nothing churns in a tracked tree. All runtime files live under the state root:

```
~/.local/state/epic-driver/
  active.json                      {"run_id", "ledger", "state_dir", "enabled_at"} — the one active run (D12)
  <run_id>/
    state.json                     runtime state (§8); atomic write + fsync of file and directory
    tick.lock                      flock target; the tick and every writing CLI verb take it
    paused                         present ⇒ paused; holds {"at", "reason"}
    env_snapshot.json              the operator's interactive environment (§21.2)
    ledger.last_valid.yaml         the last ledger copy that validated (§5.5)
    events/E-<n>.md                event files (§9.1)
    handoffs/<role>[-<step>]-g<gen>.md   handoff files (§11.3)
    reports/<step>-<kind>-<n>.md   worker reports named in events (implementation, review)
    logs/tick.log                  one line per pass + errors; rotated at 1 MB, 3 kept
    logs/checks/<step>.log         each run: start, rc, duration, last 4 KB of output
    logs/notify.log                every notify and every failure
    worktrees/                     default worktree_root (open question OQ-1, state.md)
```

- `run_id` = the ledger's `run_id`, default `epic-<epic>`. It is also the amux-spawn `--run-id` of every driven
  session (D3's foreign-handle test, §12.6).
- **Pointer file.** `enable` writes `<workspace>/<plan_root>/epic-driver.run.json` = `{"run_id", "state_dir"}`,
  once. It never changes while the run lives, so it is not churn.

## 5. The ledger (D7)

A YAML file in the epic's folder, compiled by the epic (OD-11). The tick re-reads it on every pass. The driver never
edits it; grants and progress live in the runtime state (P0-13).

### 5.1 Value grammar

| Type | Grammar | Examples |
|---|---|---|
| `duration` | one or more `<int><unit>`, unit ∈ `s m h d`, no spaces | `90s`, `20m`, `2h`, `1h30m`, `7d` |
| `utc_time` | ISO-8601 with `Z` or an offset; stored as UTC | `2026-11-24T09:28:00Z` |
| `tod` | `HH:MMZ`, a UTC time of day | `06:00Z` |
| `daily_window` | `HH:MM-HH:MMZ`, UTC; may cross midnight (`22:00-04:00Z`) | `06:00-12:00Z` |
| `step_id` | `[A-Za-z0-9][A-Za-z0-9._-]*`, unique after sanitising (§3) | `e1-useast01-roll`, `T1` |
| `command` | a string run by `/bin/sh -c` with the §7.2 environment and cwd | `ledger/checks/c15.sh us-east01` |
| `path` | relative paths resolve against `<workspace>/<plan_root>` unless stated | `ledger/cards/e1.md` |

### 5.2 Top level

| Key | Type | Required | Meaning |
|---|---|---|---|
| `version` | int = 1 | yes | schema version |
| `epic` | string | yes | epic id; sanitised for names and the branch `epic/<epic>` |
| `run_id` | string `[a-z0-9-]+` | no | default `epic-<epic>` |
| `workspace` | absolute path | yes | the epic workspace: the driver's `--dir`, the questions workspace, checks' cwd |
| `plan_root` | path relative to `workspace` | yes | the epic folder; cards and checks resolve against it |
| `worktree_root` | absolute path | no | default `<state_dir>/worktrees` (OQ-1) |
| `defaults` | map | no | `check_timeout` (60s), `check_every` (10m), `timeout` (6h), `max_attempts` (3), `warn_before` (24h) |
| `notify` | map | no | `role`: the questions role for alarms and heartbeats, default the workspace default. (No daily alive line exists, P0-11.) |
| `roles` | map role → §5.3 | yes | must contain `driver` |
| `resources` | map name → int | no | named caps (`mirror: 2`) |
| `startup_dialogs` | list of `{id, match, key}` | no | known-safe startup dialogs the tick may answer (§12.11). `id` `[a-z0-9-]+`, unique; `match` a regex that must match the captured pane, including the highlighted safe option; `key` one of `Enter`, `Escape`. Default empty: no dialog is ever answered. |
| `approvals` | ordered map id → `{conditions: [step_id]}` | no | standing approvals; **map order is ask order** (P0-13). No `granted` key: grants live in the runtime state |
| `steps` | list of §5.4 | yes | the program |

### 5.3 Roles

| Key | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | required | alias or full id, passed as `--model=` and recorded (P0-3) |
| `effort` | string | required | passed as `--effort=` and recorded |
| `profile` | string | none | a `~/.claude/profiles.toml` section; recorded and re-exported on resume |
| `cap` | int tokens | 250000 | context cap: Rotate at or above it (D4) |
| `bootstrap_tokens` | int | 40000 | B in the keep-warm formula (§11.5); a constant, not measured |
| `keep_warm` | bool | true | |
| `stuck_after` | duration | 30m | §12.3 |
| `pinned_env` | map string → string | none | compaction environment, keys limited to `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` and `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` (P0-9, §11.6) |
| `prompt` | path | the installed role prompt (§19) | |
| `epic_prompt` | path relative to `workspace` | none | workers only: the epic's own implementer or reviewer prompt that the worker prompt wraps (review m10) |

### 5.4 Steps

| Field | Type | Meaning |
|---|---|---|
| `id` | step_id | identity |
| `title` | string | one line |
| `phase` | string | phase label; a phase transition is the moment every step of a phase is done (heartbeat, §16.5) |
| `card` | path | the step card: what, how, checks, rollback, what not to do. Must exist |
| `needs` | [step_id] | done first |
| `after` | `{step: step_id, delay: duration}` | not due before the predecessor's anchor + delay. The anchor is the predecessor's `advanced_at` if it has a role, else its `done_at`. `step` must also be in `needs` |
| `not_before` | utc_time | not due before |
| `not_after` | utc_time | deadline: `deadline_near` at `not_after − warn_before`, `deadline_missed` at `not_after` |
| `warn_before` | duration | default `defaults.warn_before` |
| `window` | daily_window | dispatch only inside it |
| `latest_start` | tod | last start time inside `window` |
| `duration` | duration | alternative to `latest_start`: latest start = window end − duration. At most one of the two |
| `check_window` | `{daily: daily_window, min: duration}` or `{from: duration, to: duration}` | when the check counts (§7.3). Span < 20 min is rejected (P0-10) |
| `every` | duration, or `{period: duration, from: utc_time, deadline: duration, warn_before: duration}` | recurrence (§6.3) |
| `at` | tod | with `every: 1d` (or alone, implying it): occurrences fall at this time of day |
| `trigger` | `command` or `{run: command, every: duration}` | a check whose exit 0 makes the step due (§7.4) |
| `group` | string | steps that share one session and one window: dispatched together in one event (§6.2) |
| `resource` | map name → int | units of a `resources:` cap held while the step is in flight |
| `approval` | approval id | ungranted ⇒ never dispatched; the tick raises `approval_needed` instead (§15.1) |
| `role` | `driver`, or `{worker: {implementer: role, reviewer: role, repos: [{path, base_ref, name?}]}}`, or absent | absent = a **watch step**: no action, only its check (§6.1) |
| `check` | `command` or `{run: command, timeout: duration, every: duration}` | exit 0 done, 1 not yet (silent), 2 failed (event), other = check error (§7). Required for watch steps. Optional for driver steps (then `ADVANCED` marks it done, shown `done (unchecked)`). Worker steps get the built-in sha check (§14.4) in front of their own |
| `max_attempts` | int | default `defaults.max_attempts` |
| `timeout` | duration | no verdict (action step) or no passing check (watch step) within it ⇒ `step_timeout` |

`role.worker.implementer` and `.reviewer` name roles in `roles:` and must differ (D14). `repos[].path` is absolute
and unique by `realpath`; `name` defaults to the basename and must be unique (P0-7, §14).

### 5.5 Validation

`epic-driver validate <ledger>` and `enable` run the same validator; the tick runs it on every re-read. Errors:

- schema: unknown keys, wrong types, `version` ≠ 1, a missing `driver` role;
- graph: unknown ids in `needs`/`after`/`conditions`, `after.step` not in `needs`, cycles;
- files: a missing card, `workspace`/`plan_root` missing, a worker `epic_prompt` missing;
- names: sanitised id or role collisions, a name over 100 characters, a workspace basename outside
  `[A-Za-z0-9_-]+`;
- approvals: a step naming an unknown approval;
- timing: both `latest_start` and `duration`; `latest_start` outside `window`; a `check_window` whose span is under
  20 min (P0-10: "shorter checks belong to the action's step card or to server runners"); `every.deadline` ≤
  `every.period`;
- resources: an unknown resource, or a request above its cap;
- repos: a relative or duplicate path, a duplicate `name`, a path that is not a git work tree (`enable` and the tick
  also verify `base_ref` with `git rev-parse --verify <base_ref>^{commit}`);
- compaction: a role whose `cap` is not below its compaction point (§11.6);
- startup dialogs: a duplicate `id`, a `match` that does not compile, a `key` outside `Enter`/`Escape`.

Warnings (printed, not fatal): a driver step with no `check`; a `check` that calls `ssh` without `ConnectTimeout`.

When the tick finds the ledger invalid it raises `ledger_invalid` and keeps running on `ledger.last_valid.yaml`.

### 5.6 Worked fragment from 037

Illustrative: 037 compiles its own ledger (OD-11). Times and ids come from 037's `release_plan_20261122.md` (E1,
SA4, C15) and its `state.md` log of 2026-11-22 (the Monterey re-pull); script paths are invented for the example.

```yaml
version: 1
epic: "037"
workspace: /data/sync/work/activecdn/acdn-workspace
plan_root: tasks/037_image_optimization
defaults: {check_timeout: 60s, check_every: 10m, timeout: 6h, max_attempts: 3, warn_before: 24h}
roles:
  driver:
    model: fable
    effort: high
    profile: claude-fable
    cap: 250000
    pinned_env: {CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: "65", CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS: "300000"}   # OQ-2
approvals:                       # map order = ask order (SA9a before SA6 lives here)
  SA4: {conditions: [e1-build]}  # granted in the runtime state by `epic-driver mark --grant SA4`, never here
steps:
  - id: e1-useast01-roll
    title: "E1: roll the versioned image on us-east01 (stage, tag, roll, verify +0)"
    phase: E1
    card: ledger/cards/e1-useast-roll.md        # +0 checks (C19–C21) run inside the card: they need minutes
    role: driver
    needs: [e1-build]
    approval: SA4
    window: "06:00-12:00Z"                       # the plan's 06–12Z slot
    latest_start: "09:30Z"
    check: "ledger/checks/e1_plus0.sh us-east01" # re-verifies the +0 claim from ground truth
    timeout: 3h
  - id: e1-useast01-soak
    title: "C15–C21 at +2 h on us-east01"
    phase: E1
    card: ledger/cards/e1-useast-soak.md         # rollback: CADDY_TAG back + graceful-restart.sh
    needs: [e1-useast01-roll]
    after: {step: e1-useast01-roll, delay: 2h}   # watch step: no role; exit 2 wakes the driver with this card
    check: "ledger/checks/c15_c21.sh us-east01 plus2h"
    timeout: 4h
  - id: e1-useast02-roll
    title: "E1: roll us-east02 once us-east01 passes at +2 h"
    phase: E1
    card: ledger/cards/e1-useast-roll.md
    role: driver
    needs: [e1-useast01-soak]
    approval: SA4
    window: "06:00-12:00Z"
    latest_start: "09:30Z"
    check: "ledger/checks/e1_plus0.sh us-east02"
    timeout: 3h
  - id: e1-useast02-soak
    title: "C15–C21 at +2 h on us-east02"
    phase: E1
    card: ledger/cards/e1-useast-soak.md
    needs: [e1-useast02-roll]
    after: {step: e1-useast02-roll, delay: 2h}
    check: "ledger/checks/c15_c21.sh us-east02 plus2h"
    timeout: 4h
  - id: c15-us-daytime
    title: "C15: ≥ 1 h of 14–22Z soak, judged for both nodes before S0"
    phase: E1
    card: ledger/cards/c15-daytime.md
    needs: [e1-useast01-soak, e1-useast02-soak]
    check_window: {daily: "14:00-22:00Z", min: 1h}
    check: "ledger/checks/c15_daytime.sh us-east01 us-east02"
    timeout: 2d
  - id: monterey-export
    title: "Re-pull the Monterey h02 access log (pulls overlap; dedupe by request_id)"
    phase: R
    card: ledger/cards/monterey-export.md        # action: monterey-archive/bin/export_h02_access_log.sh '<last log_date>'
    role: driver
    every:
      period: 2d
      from: "2026-11-22T09:28:20Z"   # the hand pull of 11-22 covers up to here → next due 11-24 09:28Z
      deadline: 7d                   # h02's 7-day TTL: uncovered rows start expiring 11-29 09:28Z
      warn_before: 2d                # deadline_near from 11-27 09:28Z if the occurrence is not done
    check: "ledger/checks/monterey_export_fresh.sh"   # 0: newest max log_date ≤ 2 d old and rows = h02's count
    timeout: 2h
```

The occurrence deadline is measured from the previous occurrence's `done_at` (a pull covers up to the moment it runs,
so this approximates "the newest covered `log_date`").

## 6. Due-step selection and the step state machine

### 6.1 States

```
pending ──(needs done, after, not_before, every/at, trigger)──► due
due ──(approval granted, window open, resources free, no model limit, not paused, session deliverable)──► inflight
inflight ──verdict ADVANCED──► verifying ──check 0──► done            (recurring: done → pending, occurrence+1)
         │                          └──check 2──► failed ──event check_failed to the driver
         ├─verdict WAITING──► waiting (until a time, or a condition; its check keeps running)
         ├─verdict ASKED──► asked (until the question resolves → answer event)
         ├─verdict HALT──► halted (alarm `halt`; until `mark`)
         └─worker DONE / HALT──► §14.3
watch step: pending ──(needs, after, check_window)──► watching ──check 0──► done; check 2──► failed (driver event)
```

`failed` re-enters `due` when the driver's next verdict on it is `ADVANCED` again (a retry) and counts an attempt;
at `max_attempts` the step raises `attempts_exhausted` and is not dispatched again until `mark`.

### 6.2 Selection (tick step 5)

A step is **due** when all hold: every `needs` is done; `after` is satisfied; `now ≥ not_before`; for recurring
steps the occurrence time has come (§6.3); a `trigger`, if present, has fired; it is not already
inflight/verifying/waiting/asked/halted. It is **dispatchable** when it is due and also: its `approval` is granted
and the approval's `conditions` are done; `now` is inside `window` and not after the latest start; its `resource`
units are free; the target model is not limited (§12.4); the run is not paused; and, for a `group`, every member is
dispatchable (one event, one session, the intersection of the windows).

Ledger order breaks ties. Due but not dispatched for 2 ticks raises `due_not_started` unless the reason is one the
operator already sees: paused, an approval question open, a model limit, or a closed window. An approval question
that was answered counts as open until the answer has settled with the driver (50-13 F-2): from the pass that sees it
resolved (`questions[Q].answer_pending`, cleared when the `answer` event commits) until the outbox entry carrying the
answer is confirmed with a complete verdict. Settled without a grant, the approval is an ordinary hold again.

### 6.3 Recurrence

- `every: <duration>`: occurrence n+1 is due at `last_done_at + period` (the first at `enable` time + period, or at
  `not_before`).
- `every: {period, from, deadline, warn_before}`: the series' reference is `from` until the first occurrence is done,
  then the previous `done_at`. Due at reference + period; `deadline_near` at reference + deadline − warn_before;
  `deadline_missed` at reference + deadline.
- `at: HH:MMZ`: occurrences fall at that time of day (with `every: 1d` implied, or `every: Nd`).
- A recurring step's attempts and alarms reset with each new occurrence.

## 7. Checks and triggers (P0-10)

### 7.1 Exit codes

| rc | Meaning | Tick action |
|---|---|---|
| 0 | done | mark the step (or occurrence) done; clear its inflight marker |
| 1 | not yet | silent; try again at the next `check_every` |
| 2 | failed | `check_failed` event to the driver with the card and the output tail; attempts + 1 |
| other, timeout, cannot start | check error | counted; `check_error` alarm at 3 consecutive |

### 7.2 Runner

- `/bin/sh -c <command>` in its own process group (`start_new_session=True`), killed as a group (`SIGTERM`, then
  `SIGKILL` after 5 s) at its timeout (default 60 s). `KillMode=process` would otherwise leave its children running.
- cwd = `<workspace>/<plan_root>`. Environment, fixed: `PATH` from the env snapshot (§21.2), `HOME`, `LANG`,
  `EPIC_DRIVER_RUN_ID`, `EPIC_DRIVER_STEP`, `EPIC_DRIVER_STATE_DIR`, `EPIC_DRIVER_SSH_OPTS="-o ConnectTimeout=5 -o
  BatchMode=yes"`, and `SSH_AUTH_SOCK` only when that socket exists. Nothing else from the service environment.
- stdout+stderr captured, the last 4 KB kept in the state and in `logs/checks/<step>.log`.
- Per step, at most one run per `check_every` (default 10 min = every tick).
- **Budget.** About 150 s of checks per tick, in round-robin order from `state.check_cursor`: checks not reached this
  tick go first next tick. The tick never runs an action (invariant 1).

### 7.3 `check_window`

- `{daily: W, min: M}`: the check counts once the interval [anchor, now] overlaps the daily window W for at least M in
  total (anchor = the step's activation: `after` satisfied, or its `advanced_at`). Before that the check does not run.
- `{from: A, to: B}`: the check runs only inside [anchor + A, anchor + B]; past B with no pass ⇒ `step_timeout`.
- Validation rejects a span (window length, or B − A) under 20 min. C2 ("0–5 min after the swap"), C26 and C36
  (≤ 2 min) therefore belong in the step card or in 037's server runners, not in tick checks (review M10).

### 7.4 Triggers

`trigger` runs like a check (same runner, budget and rate limit; default `every` = `check_every`). Exit 0 makes the
step due; 1 is silent; anything else is a check error on the step. A fired trigger is recorded; it does not fire
again for the same occurrence. v1 keeps triggers minimal: no expressions, only a command (review m4).

## 8. Runtime state JSON

`<state_dir>/state.json`, written atomically (tmp + `os.replace` + fsync) at every commit point (§23). Schema
version 1:

```json
{
  "schema": 1,
  "run_id": "epic-037",
  "epic": "037",
  "ledger": "/data/sync/work/activecdn/acdn-workspace/tasks/037_image_optimization/ledger.yaml",
  "ledger_sha256": "…",
  "enabled_at": "2026-11-23T08:00:00Z",
  "last_tick_at": "2026-11-24T09:30:05Z",
  "last_tick_ok_at": "2026-11-24T09:30:05Z",
  "last_tick_error": null,
  "tick_seq": 214,
  "next_event_n": 42,
  "clock": {"boot_id": "…", "wall": 1795508405.1, "boottime": 81234.5, "monotonic": 80011.2},
  "check_cursor": "c15-us-daytime",
  "steps": {
    "e1-useast01-roll": {
      "status": "verifying",
      "occurrence": 0,
      "attempts": 1,
      "due_since": "2026-11-24T06:00:05Z",
      "started_at": "2026-11-24T06:00:05Z",
      "advanced_at": "2026-11-24T07:12:40Z",
      "done_at": null,
      "by": null,
      "last_done_at": null,
      "inflight": null,
      "last_verdict": {"event": "E-37", "line": "ADVANCED: e1-useast01-roll", "at": "2026-11-24T07:12:40Z"},
      "check": {"last_run_at": "…", "last_rc": 1, "consecutive_errors": 0, "last_error": null, "output_tail": "…"},
      "trigger": null,
      "window_covered_s": 0,
      "undispatched_ticks": 0,
      "questions": []
    },
    "monterey-export": {
      "status": "inflight", "occurrence": 1, "attempts": 0, "last_done_at": "2026-11-22T09:28:20Z",
      "inflight": {"step": "monterey-export", "event": "E-41", "started_at": "2026-11-24T09:30:05Z",
                   "host": "workstation", "session": "acdn-workspace-037-driver-g2"}
    }
  },
  "sessions": {
    "driver": {
      "name": "acdn-workspace-037-driver-g2", "gen": 2, "dir": "/data/sync/work/activecdn/acdn-workspace",
      "status": "live", "spawned_at": "…", "resumed_at": [], "model_verified": true,
      "compacted": false, "parked": [{"name": "acdn-workspace-037-driver-g1", "since": "…"}],
      "rotation": null
    },
    "implementer:T1": {"name": "037-t1-037-implementer-t1-g1", "gen": 1, "dir": "…/worktrees/037-t1", "status": "live"}
  },
  "outbox": [
    {"event": "E-41", "to": "driver", "session": "acdn-workspace-037-driver-g2", "kinds": ["step_due"],
     "steps": ["monterey-export"], "created_at": "…", "sends": ["2026-11-24T09:30:06Z"], "deferrals": 0,
     "confirmed_at": "2026-11-24T09:30:20Z", "verdict": null, "reminded_at": null}
  ],
  "grants": {"SA4": {"granted_at": "2026-11-23T19:02:00Z", "question": "Q-17", "by": "mark"}},
  "approval_question": null,
  "questions": {"Q-17": {"step": null, "approval": "SA4", "asked_event": "E-38", "status": "resolved",
                         "answer_event": "E-39"}},
  "limits": {"claude-opus-5-5": {"until": "2026-11-24T15:00:00Z", "source": "reset", "ladder_step": 0}},
  "alarms": {
    "delivery_deferred:E-40": {"id": "delivery_deferred", "subject": "E-40", "raised_at": "…",
      "last_sent_at": "…", "message_id": 8812, "ladder_until": "…", "cleared_at": null, "sends": 1}
  },
  "phases": {"announced": ["M1"]},
  "notify_failures": [{"at": "…", "what": "alarm listener_down", "error": "…"}],
  "worktrees": {"T1": {"live-metrics": {"path": "…", "branch": "epic-step/037/t1", "created_at": "…"}}}
}
```

Rules:

- Session keys are `<role>` for the driver and `<role>:<step>` for workers.
- `by` is who set `done`: `"check"` (a check returned 0), `"operator"` (`mark <step> done`) or `null` while not done.
  `check.last_error` is the text of the latest check error (`cannot start: …`, `timed out after 60s`, `exit 7`) or
  `null` after a clean run (0, 1 or 2); `consecutive_errors` counts the runs that set it.
- `inflight` is written **before** delivery and cleared by the step's verdict (D7).
- `questions[Q].answer_event` is the event that carried the answer (set when it commits); `answer_pending: true`
  exists only between the pass that saw the question resolved and that commit (§6.2, 50-13).
- `notify_failures` keeps the last 20. `outbox` keeps unconfirmed entries and entries awaiting a verdict; completed
  entries move to `logs/tick.log`.
- **`mark <step> reset`**: status → `pending` (due is recomputed), `attempts` → 0, `inflight`, `last_verdict` and the
  check counters cleared, unconfirmed outbox entries for the step cancelled; sessions untouched; the step's alarms
  clear. **`mark <step> done`**: status `done` (recurring: the occurrence), `done_at` = now, `by: operator`, inflight
  cleared, no check run.

## 9. Events, the outbox and delivery (D5)

### 9.1 Event file

`events/E-<n>.md`, `n` from `state.next_event_n`, never reused. Written before anything is sent.

```markdown
---
event: E-41
run_id: epic-037
created_at: 2026-11-24T09:30:05Z
to: driver
session: acdn-workspace-037-driver-g2
kinds: [step_due]
steps: [monterey-export]
needs_verdict: true
---
# E-41 — step due: monterey-export

## 1. step_due — monterey-export (occurrence 2)
- Card: /data/sync/work/activecdn/acdn-workspace/tasks/037_image_optimization/ledger/cards/monterey-export.md
- Why now: due since 2026-11-24T09:28:20Z (every 2d from the last pull).
- Deadline: 2026-11-29T09:28:20Z (warning from 11-27 09:28Z).
- In flight: marker written 09:30:05Z, attempt 1.

## Reply
End this turn with exactly one verdict line (architecture §10), covering every step above.
```

Kinds and their items:

| Kind | To | Carries |
|---|---|---|
| `step_due` | driver, or a worker for a worker step's first event | card, why now, deadline, inflight marker, attempt |
| `check_failed` | driver | step, card (its rollback), rc, output tail, attempt |
| `worker_settled` | driver | step, worker role, verdict line, report path (implementer `DONE` goes to the reviewer instead, §14.3) |
| `review_due` | reviewer | step, worktree paths, implementer report, where to write the review report |
| `feedback` | a worker | the file the driver queued with `epic-driver queue` (§18) |
| `answer` | driver | question id, step or approval, the answer as written in the queue file |
| `approval_needed` | driver | approval id, the steps it gates, its conditions (all done), the recommendation request |
| `alarm_cleared` | driver | alarm id and subject; only for alarms whose subject is a driver step in flight or halted |
| `resume` | the resumed session | the inflight marker(s), the last event, "check the last action against ground truth before continuing" |
| `rotate` | the session being rotated | the handoff path to write; needs a `HANDOFF:` verdict |

At most one driver event per tick (several items merged into one file) and one event per worker (D2 step 6).

### 9.2 Lines sent to a pane

Sent with `amux send <name> <line>` (text, then Enter):

```
EVENT E-<n>: read <abs path of the event file>
RESUME E-<n>: read <path>                     (as the resume prompt, §11.4)
ROTATE E-<n>: read <path>                     (the rotate event, §11.3)
VERDICT? E-<n>: end this turn with exactly one verdict line (see <path>)      (the one reminder)
PING: no action needed; reply exactly "WAITING: ping"                          (keep-warm, §11.5)
```

A fresh session receives its first event as the amux-spawn seed prompt (the same `EVENT` line), never by `send`.

### 9.3 Preconditions, deferral, confirmation

Before any send, all must hold, read on this pass:

1. the session is idle: the reducer says `idle`, no turn open, no permission prompt pending;
2. `tmux display -p -t =amux-<name>: '#{pane_in_mode}'` is `0` (not in copy-mode);
3. the input box is empty — **always**, attached or not (review M6): `tmux capture-pane -p -t =amux-<name>:`, take
   the input-box rows (the TUI's prompt box near the bottom of the pane; 50-06 derives the exact rule from captured
   panes of the installed Claude Code version and pins it with fixtures); anything other
   than the prompt glyph `>`/`❯` and whitespace is non-empty. **A pane the parser cannot read counts as a deferral.**
   The parser is tested on captured pane fixtures (empty box, typed text, multi-line text, copy-mode, a dialog).

Otherwise the event is **deferred** (the outbox entry's `deferrals` + 1). At 3 deferrals: `delivery_deferred`.

**Confirmation**: a transcript user entry whose text contains `E-<n>`, timestamped at or after the send, followed by
a lifecycle `turn_start` after the send. **Retry**: if not confirmed by the next tick *and* the transcript has no user
entry containing `E-<n>` *and* the input box does not show the line, the tick sends once more. Still unconfirmed at
the following tick: `delivery_unconfirmed`, and no further sends (invariant 2: never a send while an earlier send of
the same event could still land; at most two sends per event).

## 10. Verdict grammar (D6)

The verdict is the **last non-empty line** of the turn's final assistant message, read from the handle's
`last_message` (written by the producer on `Stop`).

```
verdict-line := verdict *( "; " verdict )
verdict      := "ADVANCED: " id [ " " shas ]
              / "WAITING: " [ step-id " " ] wait
              / "ASKED: " qid " " id
              / "DONE: " step-id                       ; workers only
              / "HALT: " text                          ; alone on the line; applies to every item
              / "HANDOFF: " abs-path                   ; alone; rotate events only
id           := step-id / approval-id
shas         := sha / repo-sha *( "," repo-sha )       ; repo-sha := repo-name "=" sha
sha          := 7*40HEXDIG (lowercase)
wait         := utc_time / text                        ; text ≤ 200 chars, no "; "
qid          := the workspace's question id (e.g. Q-17)
```

Rules:

- **Every step in the event must be covered** by one verdict on the line (merged events, §9.1); `HALT` covers all.
  Uncovered steps count as a missing verdict for those steps.
- **Invalid = missing**: no match; the line inside a code fence (an odd number of fence lines before it); two
  verdict lines at the end of the message (the last two non-empty lines both match); a step id not in the event;
  `HANDOFF` outside a rotate event; `DONE` from the driver.
- **Missing**: one reminder (`VERDICT?`), then `verdict_missing`. The limit check (§12.4) runs first: a turn that ended
  on a usage-limit entry is not missing a verdict.
- **Exempt turns**: a turn whose user prompt carried no event id (the operator's own corrections, OD-8) and the
  keep-warm ping (whose reply `WAITING: ping` is ignored).
- **A verdict is a claim.** `ADVANCED` moves the step to `verifying`; only its check makes it `done` (§7). For an
  approval id, `ADVANCED: SA4` is verified by the grant existing in the state (§15.1).
- `ASKED: Q-<n> <id>` records the question against the step or approval (§15).

## 11. Session policy (D4)

### 11.1 Inputs, measured each pass for every live driven session

| Input | Source |
|---|---|
| alive | `amux_spawn_lib.tmux_has_session(name)` and `#{pane_dead}` = 0 |
| idle / turn open / permission pending | the reducer via amux-spawn's derived status (`_derive_claude_status` logic, after 50-02) |
| TTL left, TTL bucket | `statusline.cache_seconds_left(transcript, now)` → `(seconds_left, ttl)` from the **last assistant entry's timestamp** and the usage bucket (5m/1h), not the file mtime (50-03) |
| context C | `context_mcp_lib.measure_transcript(transcript)["latest_turn"]["effective_context_tokens"]` (50-03) |
| attached | `#{session_attached}` > 0 |
| compaction | `amux_spawn_lib.compaction_boundaries(transcript, since)` (50-03) |
| model | `amux_spawn_lib.last_assistant_entry(transcript)` vs `expected_model_id` (50-01, §12.5) |

**Overage** = the last call's bucket is 5m (`ttl == 300`).

### 11.2 Decision for an event aimed at a role

| Situation | Action |
|---|---|
| no session yet | **Spawn** gen 1, seeded with the event |
| dead, no completed handoff | **Resume** in place (§11.4), even cold |
| dead, a completed handoff exists (a rotation was under way) | **Spawn** gen + 1 seeded with the handoff and the event |
| alive, turn open | wait (the outbox keeps the event) |
| alive, idle, `C ≥ cap`, or `compacted` | **Rotate** (§11.3) |
| alive, idle, TTL left ≥ 5 min | **Reuse**: deliver (§9) |
| alive, idle, cold only because of overage (`ttl == 300`) | **Reuse** in place (review m2): rotation is for over-cap sessions only |
| alive, idle, cold (1 h bucket expired) | **Rotate** |
| the session's model is limited (§12.4) | hold; no dispatch, no ping |

### 11.3 Rotation and the handoff file

1. The tick writes a `rotate` event and sends `ROTATE E-<n>: read <path>`. `sessions[role].rotation = {event,
   requested_at, handoff_path}`.
2. It waits for: a `HANDOFF: <path>` verdict, the session idle, and the handoff file's mtime newer than
   `requested_at`.
3. Then it spawns gen + 1 seeded with `EVENT E-<m>: read <path>`, where E-m's first item points at the handoff and
   the remaining items are every outbox event queued for the role meanwhile.
4. The old session is **parked**: alive, untouched. Once the new session's first `turn_start` is seen, the old one is
   reaped (`amux-spawn rm <old>`), unless a client is attached; then reaping is retried each tick.
5. No handoff after 2 ticks: `handoff_timeout`; the tick falls back to Reuse and delivers the queued events to the old
   session.

Handoff file `handoffs/<role>[-<step>]-g<gen>.md` (path given in the rotate event; the prompt fixes the sections):

```markdown
# Handoff — <session name> → g<gen+1> (<ISO time>)
## Where things stand        one paragraph per step in flight, waiting or asked, with its ground-truth evidence
## Decisions made here        decided in this session and not yet in any file, each with its reason
## Open threads              question ids, waits and their conditions, workers and their state
## First thing to check      the ground-truth check the next session runs before acting
## Files                     paths written or read that the next session needs (ledger, cards, reports)
```

### 11.4 Resume in place (OD-7)

- The tick calls `amux-spawn resume <name> --json -- "RESUME E-<n>: read <path>"` (§13.3). The resume event carries
  every inflight marker of the session and the last event it received, and asks for a ground-truth check of the last
  action before continuing.
- **After a reboot** (boot id changed, §12.7): sessions that had a turn open or a step in flight resume at once; the
  others resume when their role next gets an event.
- A failed resume, or a transcript that does not grow: `resume_failed`. The name stays the same (a resume never
  bumps `gen`).

### 11.5 Keep warm

For each live, idle session on the 1 h bucket whose last assistant entry is 40–50 min old: send `PING` (§9.2,
preconditions apply) if the role's next scheduled event is within `k·B/C` hours, with `k = 40` when the expected model
is Opus-priced and `k = 80` when it is Fable-priced (D4: Opus $8 write / $0.20 hit; Fable $16 / $0.20), `B =
bootstrap_tokens`, `C` the measured context. The next scheduled event is the earliest future due time among the role's
steps from `after`, `not_before`, `every`/`at` and window openings; triggers are not predicted. No ping under
overage, under a model limit, or with `keep_warm: false`.

### 11.6 Compaction (P0-9)

- **Pinned at spawn.** The role's `pinned_env` is passed to amux-spawn as `--pin-env KEY=VALUE`; amux-spawn forwards
  it as amux's `--env` (applied last in the pane, so it wins over the profile) and records it in the handle
  (`pinned_env`, §13.1). The resume re-passes it.
- **Cap below the compaction point.** For each role: `auto_point = pct/100 × context_window(expected model)` (pct
  from `pinned_env`, else the profile's `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`; window from
  `context_mcp_lib.resolve_context_window`), and `idle_point = CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` from the same
  sources. Validation requires `cap < min(auto_point, idle_point)` when either is known. Fact (2026-11-22):
  `~/.claude/profiles.toml` sets `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = 65` in `claude` and `claude-fable` and
  `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS = 200000` in `[all-profiles]`, **which is below the default 250K cap**: a
  role on those profiles must pin the idle value above its cap (OQ-2).
- **Detected.** A transcript entry `{"type": "system", "subtype": "compact_boundary"}` after the session's spawn or
  resume is an unplanned rotation: `compaction` alarm, `sessions[role].compacted = true`, and the role's next event
  goes through Rotate (the compacted session writes the handoff from its summary; gen + 1 starts fresh).

## 12. Reconcile and stall detection (D10)

All from ground truth, each pass, for every session record and every handle of the run id.

1. **Dead**: tmux session gone or pane dead ⇒ `died` (internal), handled by §11.2/§11.4.
2. **Settled**: idle after a confirmed event ⇒ read the verdict (§10).
3. **Stuck**: a turn open, no tool shell under the owner process, and no transcript or subagent-transcript growth for
   `stuck_after` ⇒ `session_stuck`. No automatic kill: a long tool call is legitimate. Owner = the Claude process
   under the pane (`#{pane_pid}` and its descendants, `keep_awake_hook.descendants`/`_looks_like_claude`); tool
   shell = `keep_awake_hook.running_work(owner_pid)` (task 44's test). Suppressed for one tick after a clock jump.
4. **Usage limit**: the last assistant entry has `error == "rate_limit"` (with `isApiErrorMessage: true`), never
   classified by text. Observed text: "You've reached your Fable limit. Run /usage-credits …" (no reset time).
   - A reset time in the text ("resets 3pm", "resets 3:30pm", "resets at 15:00") is parsed in the system timezone and
     rolled to the next day if already past. Without one: a fixed ladder 15m, 30m, 1h, 2h, 4h, then 4h.
   - `limits[<expected model id>] = {until, source, ladder_step}`. All dispatch and pings to sessions on that model
     pause (the limit covers the account). `usage_limit` alarm. This classification runs **before** the
     missing-verdict rule.
5. **Model check (P0-3)**: the first assistant entry after a spawn or a resume must carry `expected_model_id(model,
   profile_env)` (§13.2); otherwise `model_mismatch`. `sessions[role].model_verified` records the pass.
6. **Open prompts**: a permission prompt pending (reducer) or an `AskUserQuestion` tool in flight
   (`amux_spawn_lib.inflight_foreground_tool`) for more than 10 min ⇒ `permission_open` /
   `ask_user_question_open`.
7. **Clock**: `boot_id` (`/proc/sys/kernel/random/boot_id`) changed ⇒ reboot handling (§11.4). Otherwise a jump is
   `|Δwall − Δboottime| > 60 s` (NTP) or `Δboottime − Δmonotonic > 60 s` (a suspend); either suppresses stuck alarms
   for this tick.
8. **Foreign and orphan sessions** (review m4): a handle carrying this run id that no session record names ⇒
   `foreign_handle`. An amux session whose name matches this run's grammar with no state record and no handle ⇒
   `orphan_session`; never adopted, never re-spawned under the same name.
9. **Questions** (D9): see §15.2.
10. **Infrastructure**: the listener (`systemctl --user is-active claude-questions-listen.service`; one `start`
    attempt, then `listener_down`), the inhibitor (§22; one `start` attempt, then `inhibitor_down`), the tmux
    server (§21).
11. **Startup dialogs** (lead note, 2026-11-22). Claude Code can open an interactive dialog at startup — an opt-in
    such as "Code Mode (experimental)" (Enter = "No, start without it"), folder trust, others — and it blocks the seeded
    prompt until someone answers. For every session spawned or resumed whose seed/resume prompt is not yet confirmed
    (§9.3 confirmation), the tick captures the pane (`tmux capture-pane -p -t =amux-<name>:`) and classifies it with a
    pure parser pinned by captured fixtures: *no dialog*, *dialog* (a selection menu or confirmation that is not the
    input box), or *unreadable*. A dialog ⇒ `startup_dialog` alarm, detail = the dialog's first lines. The tick
    **never answers a dialog**, except one whose capture matches a `startup_dialogs` entry of the ledger (§5.2): then it
    sends that entry's `key` once per spawn/resume and dialog id (recorded in the session record before the send,
    invariant 5), with no alarm; if the same dialog is still there on the next pass, the alarm is raised and no second
    key is sent. A pane showing a dialog is never a delivery target (§9.3: it is not an empty input box). Folder trust
    is a security decision and belongs in no default list; `enable` gate 4 (§18.1) refuses up front when a folder a
    session will start in is not trusted (50-13 F-3).

## 13. amux-spawn and lifecycle changes (P0-3, P0-4, D13)

### 13.1 Spawn

- **Normalisation.** For Claude spawns, `--model=X` → `--model X` and `--effort=X` → `--effort X` in the forwarded
  flags, before `_amux_create_detached`, so amux's `parse_claude_flags` persists them in `CC_FLAGS` (review B1: amux
  stores only the space form; `--model=x` falls into `CC_EXTRA_ARGS`, which only the first start receives). Codex
  spawns are untouched.
- **`--pin-env KEY=VALUE`** (repeatable, Claude only, keys in `PINNABLE_ENV_KEYS =
  ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS")`; any other key is refused): forwarded
  to amux as `--env KEY=VALUE`.
- **New handle fields**, added to `HANDLE_FIELDS` with a comment block like the epic-20/37 ones:

  | Field | Type | Written by | Meaning |
  |---|---|---|---|
  | `profile` | str or null | spawn | the `--profile` name |
  | `model` | str or null | spawn | the `--model` value as given (alias or id) |
  | `effort` | str or null | spawn | the `--effort` value |
  | `pinned_env` | object or null | spawn | the `--pin-env` pairs |

  `new_handle` gains keyword arguments for them; legacy handles read as null.

### 13.2 Helpers in `amux_spawn_lib`

- `expected_model_id(model, profile_env) -> str | None`: an alias (`opus`, `sonnet`, `haiku`, `fable`) maps through
  `profile_env["ANTHROPIC_DEFAULT_<ALIAS>_MODEL"]` when the profile sets it (`profile.claude` maps `fable` to
  `claude-opus-5-5`), else returns `None` (family match only); a full id is returned as is.
- `model_matches(expected, alias, observed) -> bool`: exact when `expected` is known, else the alias is a substring
  of the observed id.
- `last_assistant_entry(transcript_path, since=None) -> dict | None`: `{timestamp, model, error, is_api_error,
  text}` of the newest assistant entry (optionally the first one after `since`), from a bounded tail read (50-03 adds
  it; 50-01 may add it first if it needs it).

### 13.3 Claude resume branch

`amux-spawn resume <handle> [--json] [--wait] [--timeout T] [-- "<prompt>"]`, Claude handles:

1. Refuse before any lock: `--yolo`, `--profile`, `--dir`, and any pass-through flag (the handle decides model,
   effort and profile). The Codex branch keeps its own validation unchanged.
2. Under `SpawnLock`: the handle exists and is Claude; the derived state is `terminated` (tmux gone, or pane dead —
   then the dead tmux session is removed first); the transcript file exists (**refuse when missing**).
3. Environment: export `resolve_profile(handle.profile)` if set; `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`; remove
   `CLAUDE_CODE_CHILD_SESSION`.
4. Command: `amux start <name> --no-attach [--env K=V …] [--model M] [--effort E] [<prompt>]` — the prompt as one
   trailing argv element, **without `--`** (50-01 verified: amux 0.3.0's `cmd_start` keeps a `--` in the inline args of
   an interactive Claude session and appends `--resume` after it, which would start a fresh conversation).
   `--model`/`--effort` are added **only when the persisted `CC_FLAGS` (`parent_cc_flags(name)`) lack them** and the
   handle has them; a model in neither `CC_FLAGS` nor the handle ⇒ refuse ("would resume on the harness default").
   A missing effort is not a refusal: the effort is passed when known (every driven role has one, §5.3). amux adds `--resume
   <claude_session_id>` itself because `meta.json` has `started=true`. The exact argv is verified against amux
   0.3.0's `cmd_start` and modelled in `tests/fake_amux_env.py`.
5. Confirm: `_launch_confirmed`, then, when a prompt is given (the tick always gives one), the transcript grows within
   30 s (*default*); not growing ⇒ the handle write of step 6 still happens (the session was launched), then exit 1
   with "resumed but the transcript is not growing" (the session is left running for inspection).
6. Handle write: `attempt` + 1, `state = "spawning"`, `updated_at`. `--json` prints `{name, provider, state, attempt,
   session_id, transcript_path, model, effort, profile}`.

The model check after a restart is the tick's (§12.5), because the first assistant entry arrives after amux-spawn has
returned.

### 13.4 The `terminated` latch (P0-4)

- `claude_event_reducer.reduce_events`: a `turn_start` after a `session_end` sets `terminated = False` (and the state
  hint follows the turn). No new event type, no new hook.
- The stored handle: `spawn_producer_hook.handle_user_prompt_submit` already writes `state = "running"`
  unconditionally, and the Claude resume writes `spawning` (§13.3) — so `_derive_claude_status`'s
  `stored_state == "terminated"` short-circuit no longer holds a resumed session. 50-02 pins both with tests.

## 14. Worker waves and integration (D14, P0-7)

### 14.1 Branches and worktrees

- `base_ref` is a ref resolved with `git -C <path> rev-parse --verify <base_ref>^{commit}`, **never** the state of the
  operator's primary checkout (dirty or not, on whatever branch).
- **Integration worktree**, one per repo: `<worktree_root>/integration/<name>` on branch `epic/<epic>`. Created on
  first use from `base_ref` (`git worktree add -b epic/<epic> <dir> <sha>`), or from the existing branch. If
  `epic/<epic>` is checked out in any other worktree (including the operator's), the tick refuses
  (`worktree_refused`): a branch the operator has checked out is never moved.
- **Step branches** are `epic-step/<epic>/<step>` (not `epic/<epic>/<step>`: git cannot hold both `refs/heads/epic/037`
  and `refs/heads/epic/037/t1`), created from the tip of `epic/<epic>` when the step's worktree is created.

### 14.2 Step worktrees and the session directory

- One repo: the worktree itself is the session `--dir`, at `<worktree_root>/<epic>-<step>`.
- Several repos: the session `--dir` is the wave directory `<worktree_root>/<epic>-<step>/`, holding one worktree per
  repo at `<…>/<name>`.
- Either way the directory basename is `<epic>-<step>` (P0-1: the tick fixes it). Outside `/tmp`.
- Refusals (`worktree_refused`): a base that does not resolve; an integration worktree that is dirty or not on
  `epic/<epic>`; an existing step worktree on another branch. The tick never resets, cleans, stashes, force-checks-out
  or removes a worktree with changes; a dirty worktree is discarded only after the driver has asked.

### 14.3 The wave

1. The step becomes dispatchable ⇒ the tick creates the worktrees, then spawns the implementer (role
   `role.worker.implementer`, name `…-<implementer>-<step>-g1`) seeded with a `step_due` event. Its prompt is the
   worker wrapper around the epic's implementer prompt (§19).
2. Implementer `DONE: <step>` ⇒ the tick sends a `review_due` event to the reviewer role (spawn or reuse under
   §11.2). Implementer `HALT` ⇒ `worker_settled` to the driver.
3. Reviewer `DONE: <step>` (its report at the path the event named) or `HALT` ⇒ `worker_settled` to the driver.
4. The driver reads the review. Issues ⇒ it writes feedback and runs `epic-driver queue <step> --to <role>
   --file <path>` (`<role>` is the step's `worker.implementer` role name in the ledger; worker steps only); the tick delivers it as a `feedback` event under §11.2 (back into the warm implementer session
   when possible), and the loop returns to step 2. A worker's drafted question ⇒ the driver asks it (§15) and queues
   the answer the same way.
5. Closing, by the driver, once review has ended: re-run the tests itself; in each step worktree stage paths
   **explicitly** (never the workspace's question-queue directory) and commit on the step branch; merge
   (`--no-ff`) into `epic/<epic>` in the integration worktree; end with `ADVANCED: <step> <sha>` (one repo) or
   `ADVANCED: <step> <name>=<sha>,…` (several).

### 14.4 The built-in check of a worker step

For each repo: `git -C <integration worktree> merge-base --is-ancestor <sha> epic/<epic>` (exit 0 ⇒ that repo
passes). All pass ⇒ the step's own `check`, if any, runs next. A sha missing from the verdict ⇒ invalid verdict.

### 14.5 Pushing

The driver decides whether and when to push, with plain `git push <remote> epic/<epic>[:<target>]` from the
integration worktree. Never `--force`, `--force-with-lease`, `--mirror` or a `+` refspec (invariant 3, prompt
text). Targets move only by such a push or by the operator. A repo without a remote (acdn-workspace) stays on
`epic/<epic>` for the operator to merge. A ref-lock failure on commit is retried once after 1 s (review m6).

## 15. Approvals and questions (P0-13, D9)

### 15.1 Approvals

- Grants live in `state.grants`, written only by `epic-driver mark --grant <id> [--question Q-n]` (which takes the
  tick lock). The ledger's `approvals:` map holds ids, conditions and order only.
- Each pass, if `state.approval_question` is null: the first approval **in map order** that is ungranted, whose
  conditions are done, and that gates a step that would otherwise be dispatchable ⇒ an `approval_needed` item in the
  next driver event. The driver asks with a recommendation through `mcp__questions__ask` and ends `ASKED: Q-<n>
  <approval id>`; the tick records `approval_question = {id, question}`. At most one approval question is open at a
  time.
- The answer ⇒ an `answer` event. On a yes the driver runs `epic-driver mark --grant <id> --question Q-<n>` and ends
  `ADVANCED: <approval id>`; the tick verifies the grant exists. On a no it ends `WAITING: <approval id> declined: …`
  or `HALT: …`. Either way `approval_question` clears when the question is no longer open.

### 15.2 Questions

- Only the driver asks, with `mcp__questions__ask` (its `--dir` is the epic workspace, so the workspace's
  `roles.toml` and queue resolve). Workers draft the question and end with `HALT`. `AskUserQuestion` is forbidden in
  driven sessions (prompt text); one in flight for > 10 min raises `ask_user_question_open` (§12.6).
- The tick reads statuses through `questions_store.open_store(<workspace>)` → `all_entries()` for the ids recorded
  from `ASKED` verdicts. A status in the resolved set ⇒ one `answer` event (recorded, never twice). The withdrawn word
  ⇒ closed, no event (it frees `approval_question`). Open ⇒ nothing.
- Sync for the epic workspace must be `"commit"` or off, never `"push"` (`enable` refuses, §18.1). The driver stages
  paths explicitly, so it never sweeps queue files into its commits.

## 16. Alarms and notify (P0-8)

### 16.1 Model

An alarm is `(id, subject)`. Each pass computes the set of active conditions; new ones are raised, vanished ones
cleared. State: `state.alarms["<id>:<subject>"]`.

- **Raise** ⇒ `notify(text, ack=True, workspace_dir=<workspace>)`. Text: `[epic-driver <epic>] ALARM <id> <subject>:
  <detail> — epic-driver status`.
- **Repeat**: none while the alarm's nudge ladder is live; after that every 4 h while active — the first repeat 4 h
  after the ladder ends (or after the last attempt, whichever is later), each repeat a new `ack=True` notify. *Ladder live* = the notify's `message_id` is still in the listener's index
  (`questions_listen_lib.read_index().messages`) and `now < sent_at + span(the workspace's [questions] nudge
  schedule)`. (50-08 confirms against the listener code whether an acknowledged message leaves the index; if it does
  not, the span alone decides.)
- **Clear** ⇒ the record keeps `cleared_at`; no notify, except `alarm_cleared` events to the driver (§9.1).
- **Notify failure** ⇒ `logs/notify.log`, `state.notify_failures`, and the `status` screen. Never retried in a loop:
  the next repeat time retries.

### 16.2 Catalogue

All alarms are sent with `ack=True` (P0-8).

| Id | Subject | Raised when | Cleared when |
|---|---|---|---|
| `tick_error` | run | the tick ended on an unhandled exception (also `last_tick_error`) | a tick completes |
| `ledger_invalid` | run | the re-read ledger fails validation (the tick runs on the last valid copy) | it validates |
| `stamp_mismatch` | run | the imported modules' stamps disagree (§17); the tick does nothing else | they agree |
| `tmux_server` | run | no server and starting one failed, or the server is in a terminal's scope | the server runs in its own scope |
| `listener_down` | run | the listener is inactive after one start attempt | active |
| `inhibitor_down` | run | the inhibitor service is inactive after one start attempt, or its lock is absent from `systemd-inhibit --list` | held |
| `login_expiring` | run | the claude.ai login expires within 3 days: `~/.claude/.credentials.json` `claudeAiOauth.refreshTokenExpiresAt`, the date behind Claude Code's "Your login expires in N days" banner (50-13 F-4); detail = the days left | a `/login` moves the expiry past 3 days (or it passes: `login_expired`) |
| `login_expired` | run | that expiry has passed: new and resumed sessions stop at login | a `/login` moves it into the future |
| `due_not_started` | step | due and not dispatched for 2 ticks, for a reason the operator does not already see (§6.2) | dispatched, or no longer due |
| `step_timeout` | step | no verdict (action step) or no passing check (watch step) within `timeout`, or past a `check_window.to` | a verdict, a pass, or `mark` |
| `attempts_exhausted` | step | `attempts ≥ max_attempts` | `mark` |
| `check_error` | step | 3 consecutive check or trigger errors | a run returns 0, 1 or 2 |
| `deadline_near` | step | `not_after − warn_before` (or the occurrence deadline − warn_before) passed, not done | done, or `mark` |
| `deadline_missed` | step | `not_after` (or the occurrence deadline) passed, not done | done, or `mark` |
| `halt` | step | a driver `HALT` verdict | a later verdict on the step, or `mark` |
| `delivery_deferred` | event | 3 deferrals (§9.3) | delivered, or the event cancelled |
| `delivery_unconfirmed` | event | unconfirmed after the retry | confirmed (late), or `mark reset` |
| `verdict_missing` | event | no valid verdict after the one reminder | a valid verdict |
| `handoff_timeout` | session | no handoff within 2 ticks of a rotate (the tick fell back to Reuse) | a later rotation completes, or the session is reaped |
| `session_stuck` | session | §12.3 | the turn closes or activity resumes |
| `permission_open` | session | a permission prompt open > 10 min | closed |
| `ask_user_question_open` | session | an `AskUserQuestion` in flight > 10 min | closed |
| `usage_limit` | model | a limit entry (§12.4) | the reset time passed and a turn completed on that model |
| `spawn_failed` | session | `amux-spawn spawn` failed (including `name_in_use`) | spawned |
| `resume_failed` | session | `amux-spawn resume` failed or the transcript did not grow | resumed |
| `model_mismatch` | session | the first assistant model after a spawn/resume ≠ expected (§12.5) | the session is reaped or rotated |
| `compaction` | session | a compact boundary after spawn/resume (§11.6) | the rotation that follows completes |
| `startup_dialog` | session | a startup dialog on the pane of a spawned/resumed session whose prompt is unconfirmed, not listed in `startup_dialogs` or still there after its one listed key (§12.11) | the dialog is gone from the pane |
| `foreign_handle` | handle | a handle with this run id that the tick did not create | the handle is gone |
| `orphan_session` | name | a session named by this run's grammar with no record and no handle | it is gone |
| `worktree_refused` | step | §14.1/§14.2 refusals | the condition goes |

### 16.3 What is not an alarm

`approval_needed` (an event), a worker `HALT` (an event to the driver), a deferral below 3, a check exit 1, a clock
jump, a parked session that cannot be reaped while attached.

### 16.4 The notify path

The tick imports the installed `questions_mcp_lib` (§17) and calls `notify(text, role=<notify.role>, ack=…,
workspace_dir=<workspace>)`. `workspace_dir` is a new keyword-only parameter (default `None` = today's resolution
from `CLAUDE_PROJECT_DIR`/`PWD`); 50-08 adds it with a test that the MCP tool's behaviour is unchanged.

### 16.5 Heartbeats (`ack=False`)

- `enable` and `disable`: one line each.
- A phase transition: "phase E1 done; next: S0 (n steps)".
- Nothing else. There is **no daily alive line and no flag for one** (P0-11, operator: "No daily line"). Between
  phase transitions and alarms the operator reads `epic-driver status` (`last_tick_at`, the last tick error, §20).

## 17. Import topology and version stamps (P0-12)

The driver runs from installed copies placed by the `epic-driver` installer feature, like every other feature.

```
~/.local/bin/epic-driver ─► ~/.claude/bin/epic-driver            (CLI; the unit's ExecStart)
   sys.path: ~/.claude/hooks, ~/.claude/statusline, ~/.claude/epic-driver/lib   (computed from the CLI's own path)
   env:      CLAUDE_HOOKS_REPO=$HOME   (so questions_mcp_lib._resolve_hooks_dir lands on ~/.claude/hooks)
   imports:
     ~/.claude/hooks/epic_driver_lib.py          owner: epic-driver   (new MODULE_OWNERS entry)
     ~/.claude/hooks/amux_spawn_lib.py           owner: amux/profiles/telegram
     ~/.claude/hooks/claude_event_reducer.py     owner: amux
     ~/.claude/hooks/keep_awake_hook.py          owner: keep-awake
     ~/.claude/hooks/questions_store.py, questions_listen_lib.py, roles_config.py   owner: questions/telegram
     ~/.claude/statusline/statusline.py          owner: statusline
     ~/.claude/epic-driver/lib/context_mcp_lib.py + models.json      copied by epic-driver
     ~/.claude/epic-driver/lib/questions_mcp_lib.py                  copied by epic-driver
     relay_server  (through the telegram feature's user-site .pth, as for every hook; outside the stamp set)
   runs:  ~/.local/bin/amux-spawn, /usr/local/bin/amux, tmux, systemctl, systemd-run, systemd-inhibit, git
   reads: ~/.claude/epic-driver/prompts/epic-driver-{driver,worker}.md    (copied by epic-driver)
```

**Stamps.** The installer writes `~/.claude/epic-driver/stamps.json` = `{"revision": "<git describe of the repo>",
"installed_at": "…", "modules": {"<file name>": {"path": "<installed path>", "sha256": "…"}}}` covering every module
and prompt above (OQ-3 records this choice of a manifest over an in-file stamp). The CLI, before importing anything
but the standard library, hashes each listed file; a missing manifest, a missing file, or a hash that differs ⇒
`stamp_mismatch`: the tick tries one notify, records the error, and exits 1 without touching sessions. A shared
module reinstalled by another feature therefore stops the driver until `install.sh` is re-run with the feature on.
Tests import the repo copies directly and set `EPIC_DRIVER_ALLOW_UNSTAMPED=1`; the unit never sets it.

## 18. CLI surface

```
epic-driver validate <ledger>                      lint only; exit 0/1
epic-driver enable <ledger> [--login-days N]       §18.1; exit 0, or 3 with the failed gate
epic-driver disable                                stop timer + inhibitor; sessions and state untouched; one notify
epic-driver pause [--reason TEXT] | resume         the pause file; the tick still watches and alarms, starts nothing
epic-driver status [--json]                        §20
epic-driver tick [--dry-run]                       one pass (the unit runs this); --dry-run computes and prints, writes nothing
epic-driver mark <step> done|reset                 §8 rules
epic-driver mark --grant <approval> [--question Q-n] [--note TEXT]
epic-driver queue <step> --to <role> --file <path> a feedback/answer event for a worker (§14.3; OQ-4)
```

Every writing verb (`enable`, `disable`, `pause`, `resume`, `mark`, `queue`, `tick`) takes `tick.lock` (`flock`,
waiting up to 300 s; a lock left by a crashed process is free, since flock dies with its holder). Exit codes: 0 ok,
1 error, 2 usage, 3 refused.

### 18.1 `enable` gates, in order (refuses on the first failure, with the reason and the fix)

1. No other run is active (`active.json`), unless it names the same ledger.
2. Stamps agree (§17).
3. The ledger validates (§5.5), including every `base_ref`.
4. Folder trust (50-13 F-3): Claude Code trusts each folder a driven session starts in, else its first spawn stops
   on the trust dialog, which the tick never answers (§12.11). The folders: the workspace; the state root
   `~/.local/state/epic-driver/` (wave directories, the default worktree root, hence the worker worktrees). Worker repos
   are not gated; whether a worktree still shows the dialog is an open point (50-12 check 9), and `startup_dialog`
   reports it. Trust is read from `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json`
   when set): `projects["<abs path>"].hasTrustDialogAccepted == true` on the folder or a parent, with Claude Code's
   git rule (as read in its 2.1.330 bundle and confirmed with probe sessions): inside a git repository or worktree
   only the main repository's root, or the folder and its parents up to the git root, count; trust above the git
   root does not reach in. Refused with the folders named and how to trust them (open `claude` there once, or set the
   key); a missing or unreadable `~/.claude.json` refuses too. `enable` never writes that file: trust is the
   operator's security decision.
5. The login (50-13 F-4): the claude.ai login (`~/.claude/.credentials.json` `claudeAiOauth.refreshTokenExpiresAt`,
   §16.2 `login_expiring`) has expired ⇒ refuse; it expires within `--login-days N` (default 7; 0 = never refuse on
   this) ⇒ refuse with the fix (`/login`, or `--login-days 0`). No such login (no file, an API key, …) ⇒ a note, no
   refusal.
6. The listener is active.
7. `claude-questions --check --workspace-dir <workspace>` passes and `[questions] sync` is not `"push"` (review m8).
8. A test notify (`ack=False`) succeeds.
9. The tmux server: none running ⇒ fine (the tick starts it); in its own scope ⇒ fine; inside a terminal's scope ⇒
   move it (§21.1); the move fails ⇒ refuse with instructions.
10. Snapshot the environment (§21.2).
11. Write `state.json` (fresh, or keep the existing one for the same run id), `active.json` and the pointer file.
12. `systemctl --user enable --now claude-epic-driver.timer claude-epic-driver-inhibit.service`; then
    `systemd-inhibit --list` must show the driver's `sleep:idle` block lock, else roll back the timer and refuse.
    (P0-6: the lock only needs to hold while the operator is logged in; no polkit or greeter change is made.)
13. One notify: "epic-driver on for <epic>".

## 19. Role prompts (D8, D9, m10)

`docs/prompts/epic-driver-driver.md` and `docs/prompts/epic-driver-worker.md`, installed under
`~/.claude/epic-driver/prompts/`. A fresh session's first event file (and every `resume` event) opens with a `role`
item: "read your role prompt at <path> first" (the role's `prompt`, and for workers also its `epic_prompt`). Both
prompts state:

- the event protocol (§9.2 lines; read the event file first) and the verdict grammar (§10) with examples, including
  merged events and the exempt ping;
- forbidden tools and commands: `amux-spawn`, `amux`, Monitor, `watch`, `AskUserQuestion`; no `sleep` loops waiting
  for time to pass (end with `WAITING:` instead);
- the handoff format (§11.3), written only when a rotate event asks for it.

The **driver** prompt adds: production writes only inside sessions, through the epic's reviewed scripts, under its
standing approvals (D8); long actions run detached on the server with their own record; anything outside an approval
becomes an `mcp__questions__ask` question with a recommendation; approvals one at a time (§15.1); the closing and
pushing rules (§14.3–§14.5) with explicit staging; never edit the ledger; epic records are written in the workspace
repo's integration worktree, never in the operator's checkout.

The **worker** prompt is a wrapper (review m10): it embeds the path of the epic's own implementer or reviewer prompt
(`roles.<role>.epic_prompt`) and overrides it on three points: never commit (OD-9), never ask the operator (draft the
question, end `HALT`), and end with `DONE: <step>` after writing the report to the path the event names.

## 20. `status` layout

One screen, newest facts first; `--json` emits the same data.

```
epic-driver 037 · run epic-037 · ON (timer active, next 09:40:00Z; inhibitor held) · not paused
last tick 2026-11-24T09:30:05Z (2 min ago) ok · last error: none
tmux server 16651 in epic-driver-tmux.scope · listener active · stamps ok (v1.4-12-g2a2f966)
phase E1: 3/5 done

STEPS
  id                 status      since        detail
  e1-useast01-roll   done        11-24 07:12  check rc 0 07:20
  e1-useast01-soak   watching    11-24 07:12  after +2h → 09:12; check rc 1 09:30
  monterey-export    inflight    11-24 09:30  E-41 confirmed; deadline 11-29 09:28Z
SESSIONS
  key              name                            state  cache      ctx         gen  model
  driver           acdn-workspace-037-driver-g2    busy   warm 54m   182K/250K   2    ok
OUTBOX
  E-41 → driver    sent 09:30:06 · confirmed 09:30:20 · verdict pending
QUESTIONS / APPROVALS
  Q-17 SA4 resolved (granted 11-23 19:02)
ALARMS
  (none)
NOTIFY FAILURES (last 3)
  (none)
```

## 21. The tmux server and its environment (P0-5)

### 21.1 Ownership

Each tick, before any spawn:

- **No server running** (`tmux list-sessions` fails with "no server"): start one with `systemd-run --user --scope
  --unit=epic-driver-tmux tmux new-session -d -s epic-driver-keepalive`, run with the environment snapshot (§21.2)
  applied over the service environment (a scope runs the command from `systemd-run` itself, so it inherits that
  environment, and the server's global environment is taken from it). Verify: the server pid
  (`tmux display -p '#{pid}'`) has `epic-driver-tmux.scope` in `/proc/<pid>/cgroup`. The keepalive session (a plain
  tmux session; `amux` ignores names without its `amux-` prefix) keeps the server alive under `exit-empty`.
- **A server running in its own scope**: nothing to do.
- **A server inside a terminal's scope** (e.g. `ptyxis-spawn-*.scope`): `enable` moves the server pid into its own
  scope with systemd's `AttachProcessesToUnit` (creating `epic-driver-tmux.scope` as a transient scope first if it
  does not exist), and verifies through `/proc/<pid>/cgroup`. If the move fails, `enable` refuses with instructions
  (close and restart the server outside a terminal scope, or run the command it prints). Found by the tick later ⇒
  `tmux_server` alarm.
- Never start the server inside `claude-epic-driver.service`'s cgroup: every amux/amux-spawn call is preceded by this
  check. tmux 3.6 already puts each pane in its own `tmux-spawn-*.scope`, so only the server process matters.

### 21.2 The environment snapshot

`enable` (run from the operator's interactive shell) writes `<state_dir>/env_snapshot.json` with the allowlisted
keys present in its environment: `PATH`, `GOPATH`, `GOROOT`, `GOBIN`, `CARGO_HOME`, `RUSTUP_HOME`, `NVM_DIR`,
`NVM_BIN`, `NVM_INC`, every other `NVM_*`, `LANG`, `LC_ALL`, `LC_CTYPE`, `EDITOR`, `PYENV_ROOT` (*default list*, a
constant `ENV_SNAPSHOT_KEYS` in the lib; `VIRTUAL_ENV` is left out on purpose, and nothing matching amux-spawn's
`is_secret_key` is ever written). It is used to start the tmux server (§21.1) and as the checks' `PATH` (§7.2). `SSH_AUTH_SOCK` is
never snapshotted: the tick unsets it when the socket does not exist (keys have no passphrase).

## 22. Unit files and the installer feature

`~/.config/systemd/user/claude-epic-driver.service`:

```ini
[Unit]
Description=Claude epic driver: one tick
After=network-online.target

[Service]
Type=oneshot
KillMode=process
TimeoutStartSec=5min
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/epic-driver tick
```

`claude-epic-driver.timer`:

```ini
[Unit]
Description=Claude epic driver tick timer

[Timer]
OnCalendar=*:0/10
Persistent=true
AccuracySec=30s
Unit=claude-epic-driver.service

[Install]
WantedBy=timers.target
```

`claude-epic-driver-inhibit.service`:

```ini
[Unit]
Description=Claude epic driver: block sleep while the driver is on

[Service]
ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle --mode=block --who=claude-epic-driver --why="epic driver is on" sleep infinity
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

**After a reboot (P0-6).** Reboots are rare and never unattended; with disk encryption the machine does not finish
booting until the operator unlocks it, and he then logs in. The driver recovers from that point and must not depend on
him opening a terminal: lingering is on (verified), so the user manager starts the enabled timer and inhibitor by
itself, the first tick starts the tmux server in its own scope with the environment snapshot (§21), and resumes the
sessions that had a turn open or a step in flight (§11.4). The inhibitor holds while he is logged in; nothing is
configured for the greeter. `KillMode=process` lets sessions the tick started outlive it; they are not in its cgroup anyway once §21.1 holds.

**Installer feature `epic-driver`** (`install.sh`, off by default; `requires` = `amux statusline keep-awake
questions`): copies the CLI (`~/.claude/bin/epic-driver`, symlinked into `~/.local/bin`), `epic_driver_lib.py` into
`~/.claude/hooks/` (a new `REQUIRED_HOOKS` + `MODULE_OWNERS` entry), `context_mcp_lib.py`, `models.json` and
`questions_mcp_lib.py` into `~/.claude/epic-driver/lib/`, the two prompts into `~/.claude/epic-driver/prompts/`; writes
`stamps.json` (§17); installs the three units with `daemon-reload` through the `ext_systemd_*` seams and **enables
none of them** (that is `epic-driver enable`'s job). Uninstall stops and removes the units and files and leaves
`~/.local/state/epic-driver/` alone. `NO_EXTERNAL` test mode as for the questions feature.

## 23. The tick, one pass

```
 0. CLI: stamp check (§17) → import → take tick.lock (non-blocking for `tick`: a held lock means a pass is running;
    log and exit 0). Read active.json; none → exit 0.
 1. Read: pause file, ledger (validate; invalid → last valid copy + ledger_invalid), state.json. now = UTC.
    Clock (§12.7). Fix the env (unset a dead SSH_AUTH_SOCK). Ensure the tmux server (§21.1), the listener,
    the inhibitor (§12.10). Read the login expiry (§16.2 `login_expiring`/`login_expired`).
 2. Reconcile sessions (§12): liveness, open turn, tool shell, last_message + verdict, delivery confirmations,
    TTL/context, limits, compaction, model check, foreign/orphans. Apply verdicts (§10, §14.3).
 3. Reconcile questions (§15.2).
 4. Run checks and triggers within the budget (§7). Apply results.
 5. Find due and dispatchable steps (§6.2); raise approval_needed (§15.1).
 6. Dispatch: build at most one driver event and one per worker; for each: choose the action by §11.2;
       COMMIT (event file, outbox entry, inflight markers, session record {status: spawning|resuming, name, gen})
       → side effect (spawn / resume / rotate / send) → COMMIT (result).
    Deferrals, retries (§9.3). Reap parked sessions (§11.3).
 7. Keep-warm pings (§11.5), each as COMMIT → send → COMMIT.
 8. Alarms: raise / repeat / clear (§16); phase heartbeats.
    last_tick_at, last_tick_ok_at, tick_seq. COMMIT. Release the lock.
 Any unhandled exception: record last_tick_error + tick_error (best effort), COMMIT, exit 1.
```

Invariant 5 is why every side effect sits between two commits: a tick killed at any point leaves a state from which
the next pass can tell what was attempted (a session record in `spawning` whose name exists → it came up; absent →
the spawn did not happen; `name_in_use` prevents a duplicate either way).

## 24. Epic-author guidance (`docs/epic-driver.md`)

- D1: move anything with a deadline shorter than the operator's absence to a server cron (log TTLs, timed extracts,
  rollback triggers). Server-side failure alerts reach the operator directly, never only through the driver.
- Checks: exit codes (§7.1); windows under 20 min belong in the card or on the server (§7.3); `ssh` with
  `$EPIC_DRIVER_SSH_OPTS`; idempotent and read-only (the tick never runs an action).
- Cards: what, how, checks, rollback, what not to do; the +0 checks a session runs itself.
- Approvals: map order is ask order; grants are given with `mark --grant`, already-granted approvals (037's SA1–SA3)
  are recorded the same way after `enable`.
- The silent-stall gap (brd §6) and how to watch the driver by hand: `epic-driver status`, `last_tick_at`.
- Turning it on and off; attaching to a session (`amux-spawn a <name>`); what an operator message does (OD-8).
