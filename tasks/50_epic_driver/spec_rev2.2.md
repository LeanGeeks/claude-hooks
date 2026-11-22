# Task 50 — Epic driver: run a multi-day epic unattended, resumable after crashes and reboots

**Status:** draft (spec, not started) · **Type:** feature · **Created:** 2026-11-22 · **Rev:** 2.2

**Rev 2 (2026-11-22).** An independent review (25 findings, 4 blockers) is folded in. The four blockers:
- the tmux server dies with the tick's cgroup;
- the question system is not set up;
- pass/fail checks wake the LLM every tick during soaks;
- the machine suspends between events.

The lead verified on the workstation: suspend after 21600 s idle on AC; the tmux server sits in the terminal's
scope; GDM auto-login is off; `id_ed25519` and `id_rsa` have no passphrase.

**First user:** activecdn epic 037 (`/data/sync/work/activecdn/acdn-workspace/tasks/037_image_optimization/`). Its
workflow decisions WF-1..WF-9 are in its `state.md` log, entry of 2026-11-22.

**Scope (proposed):**
- `.claude/bin/epic-driver` (new CLI);
- `.claude/hooks/epic_driver_lib.py` (new; all logic, importable for tests);
- `.claude/bin/amux-spawn` (`resume` for Claude) and `.claude/hooks/claude_event_reducer.py` (the `terminated`
  latch);
- `context-mcp/context_mcp_lib.py` (a public `measure_transcript(path)`);
- the systemd user units `claude-epic-driver.{service,timer}` and `claude-epic-driver-inhibit.service`. These come
  through installer feature `epic-driver`, off by default;
- `docs/epic-driver.md`, `docs/prompts/epic-driver-{driver,worker}.md`;
- `tests/test_unit_epic_driver.py`.

## Goal

An epic that runs for days can keep moving while the operator is away and the workstation reboots. That covers code
waves, releases, canary soaks, data collection and analysis.

- **No silent stalls.** Every stop is either a verdict, a question the operator has seen, or an alarm.
- **Resumable from disk.** A crash, a reboot or a full context loses no decided work.
- **Reasonably autonomous.** The operator is asked only what the epic's rules reserve for him.
- **Attachable.** The operator can attach to any session and steer it.

## Non-goals

- **Not a 24/7 service.** It is switched on for one epic and off afterwards. The normal way of working stays the
  same: a foreground planning session creates tasks, and another foreground session manages implementation.
- **No hook fencing.** Rules are text instructions. Sessions run in YOLO mode (Anton, 2026-11-22: "proven during
  the past few months; strict fencing creates more issues than it prevents").
- **Not the epic's safety system.** Kill switches, leases, watchdogs and rollbacks live on the servers and must work
  with the workstation off.
- **No supervision from inside an LLM session.** No Monitor loops, no `amux-spawn watch`, no generation succession.

## Background: what two reference projects taught

Studied 2026-11-22: `natasha/hyppie-flow` (PM ticks) and `tradingtips/leads-platform` (fleet controller).

1. **Both converged on the same core:** durable state in files, short sessions, a fixed verdict per turn, and async
   questions while other work continues.
2. **Long-lived supervisor sessions failed in both.**
   - The hyppie-flow PM idled through a 19 h build and re-read its context cold on every wake (its ADR-0027).
   - leads-platform ran 23 blocking Monitors in one wave and had units idle for 7 h 44 m unnoticed (its ADR-012).
3. **Both left their simple external watchdog unbound.** This is the piece that matters most.
4. **Derived status lies.**
   - An API-killed child read `running` for 2 h 25 m.
   - Implementers reported `DONE` over red gates.
   - So: re-measure from ground truth.
5. **The longest stalls were transport, not logic:**
   - a permission prompt open for 14.6 h;
   - a 20 h usage freeze;
   - a 4 h 50 m Fable limit.
6. **Cost is context size.** Main sessions were 60–71 % of spend.

## Decisions

- **D1 — Three planes, three failure modes.**
  - *Safety* lives on servers: crons and dead-man timers.
  - *Driver* is this tool, on the workstation.
  - *Work* is the amux-spawn sessions it starts.

  A dead workstation pauses progress, nothing else. The docs tell epic authors to move anything with a deadline
  shorter than the operator's absence to a server cron: log TTLs, timed extracts, rollback triggers.

- **D2 — A ticker with no LLM in it decides when an LLM runs.**
  - **Timer:** `claude-epic-driver.timer`, `OnCalendar=*:0/10`, `Persistent=true`, `AccuracySec=30s`. Lingering
    is on (verified), so it runs after a reboot with no login. GDM auto-login is off (verified).
  - **Service:** `Type=oneshot`, `KillMode=process`. Sessions it starts must outlive it.
    - It also sets `Environment=PATH=` with `~/.local/bin`, `TimeoutStartSec=5min`.
    - It unsets `SSH_AUTH_SOCK` when the socket doesn't exist. The keys have no passphrase, so ssh works without an
      agent.
  - **The tick:** one-pass Python under a `flock`. It reads the ledger, the runtime state, the amux registry and the
    transcripts.
  - **Events:** the tick starts or wakes an LLM session only on an event:
    - a step became due;
    - a worker settled;
    - a check failed;
    - an answer arrived;
    - a session died;
    - an alarm cleared.

  Otherwise it costs nothing.

- **D3 — Sessions and their tmux server.**
  - **Spawning:** sessions are amux-spawn sessions, never `claude -p`. Each gets `--yolo`, pinned model and effort,
    an explicit `--dir`, the epic's run id and a **deterministic name** `<epic>-<role>-<step>-<attempt>`. A second
    spawn of the same name fails on `name_in_use`, which prevents duplicates after a tick killed mid-spawn.
  - **Orphans:** an orphan found by name (a session exists but its handle was never written) is adopted, not
    re-spawned.
  - **The tmux server:** if none is running, the tick starts it in its own scope with `systemd-run --user --scope
    --unit=epic-driver-tmux tmux start-server`. It never starts inside the tick's cgroup.
  - **Foreign handles:** the role prompts forbid `amux-spawn`, Monitor and `watch` inside driven sessions. The tick
    raises an alarm on any handle in its run id that it didn't create.

- **D4 — Reuse by cost (WF-6).** Opus 5.5 charges $8/MTok for a 1 h cache write and $0.20 for a hit; Fable 5.2 $16 and
  $0.20. For each event aimed at a role:
  1. **Reuse.** The session is alive, idle, and its cache warm with ≥ 5 min left. Its context is under the role's
     cap (default 250K). Then the event is delivered to it (D5).
  2. **Rotate.** The session is alive and idle but cold or over the cap.
     - The tick sends "write your handoff now" (WF-7).
     - It waits for a `HANDOFF: <path>` verdict, an idle session and a handoff file newer than the request.
     - Then it spawns a fresh session seeded with the handoff. The old one stays parked until the new one has
       started, then it is reaped. It is not reaped while a client is attached.
     - Events for the role wait in the outbox and go to the new session.
     - With no handoff after 2 ticks, the tick raises an alarm and falls back to Reuse.
  3. **Resume.** The session is dead (crash, reboot, killed pane) with no handoff. It is resumed in place, even with
     a cold cache: its context is not in the files.
     - **After a reboot**, only sessions that had a turn open or a step in flight resume at once. The others resume
       when their role next gets an event.
     - **The resume prompt** includes the in-flight marker (D7) and asks for a ground-truth check of the last action
       before continuing.
  4. **Keep warm.** A session idle 40–50 min gets a one-line ping. This happens only if the role's next scheduled
     event is within `40·B/C` hours on Opus (`80·B/C` on Fable). C is the session's measured context. B is a per-role
     config constant (default 40K), not measured. The ping asks for `WAITING:` and is exempt from the verdict rule.

  Warmth and TTL left come from `statusline.py`'s `read_cache_ttl`/`cache_state`, using the last assistant entry's
  timestamp rather than the file mtime. Context comes from `context_mcp_lib.measure_transcript(handle
  transcript_path)`. Under an overage the TTL drops to 5 min: keep-warm stops and Reuse needs ≥ 5 min left, as
  above.

- **D5 — Event delivery: through files, confirmed, never interleaved with the operator.**
  - **The outbox.** Each event is written to `events/E-<n>.md` and recorded in the outbox before sending. The tick
    then sends one line: `EVENT E-<n>: read <path>`.
  - **Before sending**, all of these must hold:
    - the session is idle (no open turn);
    - `#{pane_in_mode}` is 0, so the pane is not in copy-mode;
    - if a client is attached, the pane's input line is empty.

    Otherwise the event is deferred. After 3 deferrals the tick raises an alarm.
  - **Delivery is confirmed** by a transcript user entry containing `E-<n>`, followed by a turn start. Without it the
    tick retries once, then raises an alarm. It never sends twice unconfirmed.
  - **The operator's own messages don't pause anything (WF-8).** Work he controls himself runs in sessions he starts
    by hand, outside the run id.

- **D6 — The verdict protocol.** Every turn started by a tick event ends with exactly one final line:
  - `ADVANCED: <step> [<sha>]`
  - `WAITING: <until|condition>`
  - `ASKED: Q-<n> <step>`
  - `DONE: <task>`, from a worker; the worktree is left uncommitted (D14)
  - `HALT: <reason>`
  - `HANDOFF: <path>`

  The tick reads `handle.last_message`.
  - **Missing verdict:** only turns whose prompt carried an event id need one. The operator's corrections and
    keep-warm pings don't. A missing verdict gets one reminder, then an alarm.
  - **A verdict is a claim.** The tick re-runs the step's check before marking it done.

- **D7 — The step ledger is the epic's program.** It is a YAML file in the epic's folder. Each step has:

  | Field | Meaning |
  |---|---|
  | `id`, `title`, `card` | Identity, plus a short step card: what, how, checks, rollback, what not to do |
  | `needs` | Step ids that must be done first |
  | `after` | `{step, delay}`, a delay relative to a predecessor (e.g. us-east02 at +2 h after us-east01 passes) |
  | `not_before`, `not_after` | UTC bounds. `not_after` raises an alarm ahead of time (`warn_before`) |
  | `window` | e.g. `06:00-12:00Z`, may cross midnight. `latest_start` or `duration` keeps a roll from starting at the end |
  | `check_window` | When the check counts, if different from the action window (e.g. ≥ 1 h of 14–22Z) |
  | `every` / `at` | Recurrence (daily checks, exports every 5 days, check-ins ≤ 4 h apart) |
  | `trigger` | A check that makes the step due ("leftovers when C14 exceeds …", "near 0 for 24 h") |
  | `group` | Steps that must share one session and one window (a settings PUT and its refill) |
  | `resource` | A named cap, e.g. `mirror: 2` |
  | `approval` | An id in the ledger's `approvals:` map |
  | `role` | `driver`, or a worker spec (D14) |
  | `check` | A command. Exit codes: **0 done, 1 not yet (silent), 2 failed (event)**, anything else a check error (alarm after 3). It runs with a timeout, may use ssh (`ConnectTimeout=5`), and is rate-limited per step |
  | `max_attempts`, `timeout` | Retry limit, and the alarm when no verdict comes within the time |

  A top-level `approvals:` map holds `{id: {granted: <date|null>, conditions: [step ids]}}`. The tick never dispatches
  a step whose approval is ungranted or whose conditions aren't done. It raises an ask instead. `epic-driver mark
  --grant SA4` records the operator's answer.

  **Runtime state** is a separate JSON file next to the ledger, written atomically. It holds, per step:
  - status and attempts;
  - the `inflight {step, started_at, host, session}` marker, written **before** a step starts and cleared by its
    verdict;
  - the last verdict;
  - session handles;
  - the outbox;
  - question ids.

  Sessions read the ledger and their step card, plus only the plan sections the card points to.

- **D8 — Production writes run only inside sessions, through the epic's reviewed scripts under its standing
  approvals.** This is a text rule in the driver prompt. The tick itself runs only checks, never actions, so its
  runtime cap can't cut a production write. Long actions run detached on the server (`systemd-run`/`nohup` remotely)
  with their own record. Anything outside an approval becomes an `ASKED` question with a recommendation.

- **D9 — Questions never block a session.**
  - **Who asks.** Only the driver asks, with `mcp__questions__ask`. Workers draft the question and end with `HALT`.
    This keeps all questions in one queue: workers run in other repos' worktrees, which resolve their own roles
    config.
  - **No AskUserQuestion.** It is forbidden in driven sessions: the permission hook parks it on the relay for up to
    12 h, inside the turn. Epic plans that say "ask Anton (AskUserQuestion)" mean `mcp__questions__ask` here. The
    tick raises an alarm on an AskUserQuestion open for more than 10 min.
  - **Answers.** The listener writes the answer into the queue file. The tick sees `resolved` and raises an
    `answer` event. `withdrawn` counts as closed, not as an answer.
  - **Sync.** Questions sync for the epic workspace is set to `"commit"` or off, never `"push"` from a non-main
    branch. The driver stages paths explicitly, so it never sweeps queue files into its commits.
  - **Heartbeats.** `notify` on each phase transition and each `HALT`. No daily digest: epics have their own reports.

- **D10 — Stall detection, all from ground truth.**
  - **Dead** pane or process → `died` event.
  - **Stuck:** an open turn with no running tool shell under the owner process (task 44's test, reused). Also no
    transcript or subagent growth for `stuck_after`. → alarm. No automatic kill; a long tool call is legitimate.
  - **Usage limit:** classified by the entry's `error: "rate_limit"` / `isApiErrorMessage`, not by its text.
    - The reset time is parsed in the system timezone and rolled to the next day if it is in the past. Without a
      reset time, the tick backs off on a fixed ladder.
    - On a limit, all dispatch and pings for that model pause, since the limit covers the whole account. The limit
      check runs before the missing-verdict rule.
  - **Permission prompt or AskUserQuestion** open for more than 10 min → alarm.
  - **A due step not started** after 2 ticks → alarm. **A verdict not in** within the step's `timeout` → alarm.
  - **The listener** (`claude-questions-listen`) inactive → the tick tries `systemctl --user start` once, then raises
    an alarm.
  - **Clock jump** (suspend, NTP) detected → stuck alarms are suppressed for one tick.

  Alarms go through `notify` and repeat every 4 h until cleared.

- **D11 — The driver's dead-man: designed, implementation deferred** (Anton, 2026-11-22: "I like the idea, but for
  the first epic I'll be checking it manually"). Nothing in this epic builds it, and no other step depends on it.
  The design is kept for later: the relay server, which already runs off-box on h02 and reaches Anton on Telegram,
  gains a heartbeat endpoint (`POST /v1/heartbeats/<name>` with `{expect_every_s, grace_s}`, `DELETE` to disarm). Its
  reaper sends one alert when a heartbeat is overdue and one when it recovers. Each tick would send it last, with a
  5 s timeout.
  - **Until then:** `epic-driver status` and `notify` heartbeats on phase transitions are how Anton sees the driver is
    alive. A dead workstation is noticed by the absence of those notifies.

- **D12 — On and off.**
  - `epic-driver enable <ledger>` refuses unless all of these hold:
    - the ledger validates: no cycles, known `needs`, cards present, approvals known;
    - the listener is active;
    - a test `notify` succeeds.

    Then it starts the timer and `claude-epic-driver-inhibit.service`. That service is a long-running `systemd-inhibit
    --what=sleep:idle --mode=block sleep infinity`, so the machine doesn't suspend between events while the driver is
    on. Finally it sends one notify.
  - `pause` / `resume`: a pause file. The tick still watches and alarms, but starts nothing.
  - `disable`: stops the timer and the inhibitor, and leaves sessions and state alone.
  - `status`: one screen with steps, sessions (warm/ctx), the outbox, open questions and alarms.
  - `mark <step> done|reset` and `mark --grant <SA>`: the operator's manual overrides.

  Only one ledger is active at a time.

- **D13 — Claude resume in amux-spawn.**
  - `amux-spawn resume <handle>` gains a Claude branch with its own validation; the Codex branch refuses
    `--yolo`/`--profile`/`--dir`.
  - **Restart path:** use amux's own restart. `amux start` already adds `--resume <id>` when `meta.json` has
    `started=true`. `CC_FLAGS` already holds `--yolo`, model and effort from the spawn, so don't re-pass them, which
    would duplicate them. Do re-export the profile's env vars and `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, without
    `CLAUDE_CODE_CHILD_SESSION`.
  - **The `terminated` latch:** fix it in code. In `claude_event_reducer.py` a later `session_start` clears it.
    `spawn_producer_hook`'s `terminated` state yields to a newer start.
  - **Safety:** refuse when the transcript is missing, and verify afterwards that it is growing.

- **D14 — Worker waves.**
  - **The worker spec** carries `repos: [{path, base_ref}]`. 037 spans live-metrics, skynet, activecdn-module plus
    caddy-apps as a pair, and the workspace. Those are independent repos, so a worktree of the workspace doesn't
    contain them.
  - **The tick creates worktrees** outside /tmp, one per repo, and refuses a dirty or wrong-branch base. It always
    passes `--dir`.
  - **The implementer never commits.** It leaves its changes in the worktree and ends with `DONE: <task>`.
    Implementers used to commit before review ended (Anton, 2026-11-22; rule 7 in the activecdn implementer prompt).
    The worktree on disk plus resume-in-place keeps work safe without WIP commits.
  - **Review:** a reviewer of a different role runs after it. Its feedback goes back into the implementer's session
    under D4.
  - **Closing:** once review has ended, the driver re-runs the tests itself, commits, merges, and ends with
    `ADVANCED: <step> <sha>`. The step's check verifies that sha on the target branch.
  - **Pushing:** the driver decides whether and when to push (Anton: "the driver would know better than me"). Never
    force-push.
  - **Safety:** a dead worker is resumed (D4 rule 3). A dirty worktree is never discarded without asking.

## Tick algorithm (one pass)

1. Take the lock. Read the pause file, the ledger and the runtime state. Detect a clock jump. Make sure the tmux
   server is running (D3) and the listener is active (D10).
2. **Reconcile sessions.** For each handle in the run id, and each deterministic name expected, read:
   - liveness of the pane and process;
   - whether a turn is open, and whether a tool shell is running;
   - `last_message` and the verdict in it;
   - delivery confirmations;
   - warm/TTL and context;
   - limit entries.

   Record `died`, `settled`, `stalled` and `limited` events.
3. **Reconcile questions.** Raise `answer` events for newly `resolved` entries tied to steps.
4. **Run the checks** for running, waiting and triggered steps: 0 means done, 1 is silent, 2 raises an event, and any
   other exit code raises an alarm after 3.
5. **Find due steps**, using: `needs`, `after`, `not_before`, `window` with `latest_start`, `every`/`at`,
   `trigger`, approvals, `resource` caps, limits, pause.
6. **Dispatch.** At most one driver event per tick (several merged into one file), and one event per worker. The
   target session is chosen by D4. Delivery follows D5. The in-flight marker is written before delivery.
7. **Keep-warm pings** (D4 rule 4).
8. **Alarms:** new ones, and repeats due every 4 h. Write the runtime state atomically. Release the lock.

## Tests (unit, no live sessions)

- **Session handling:**
  - each D4 branch with fixture transcripts: warm, cold, over cap, dead, attached, overage TTL;
  - rotation that times out;
  - resume after a reboot, only the ones that should;
  - D5 deferral (copy-mode, input not empty) and confirmation/retry.
- **Verdicts:** valid, missing, two lines, inside a code fence, an operator turn exempt, a ping exempt.
- **Ledger:**
  - validation (cycles, unknown `needs`, missing cards, unknown approvals);
  - due-step selection: windows across midnight, `latest_start`, `after` delays, `every`, `trigger`, `resource`
    caps, an ungranted approval;
  - check exit codes 0, 1, 2 and other.
- **Stalls and limits:**
  - usage limits: a reset time in local 12-hour format, a past time rolled to tomorrow, no reset time;
  - stuck vs a long tool shell;
  - a clock jump.
- **Plumbing:**
  - deterministic names and orphan adoption;
  - the lock held by a crashed tick;
  - atomic state writes;
  - the amux-spawn Claude resume branch against a fake amux;
  - the reducer clearing `terminated` on a later start.

## Live checks before first use (operator present)

1. **Reboot with no login:**
   - the timer fires and the tick runs with PATH and ssh working;
   - a session the tick spawned is still alive 15 min later;
   - the machine doesn't suspend at the GDM login screen while the driver is on.
2. **No suspend** during 8 h of idleness while logged in, with the driver on.
3. **Attach** to a tick-spawned session; its transcript persists.
4. **Kill a worker's pane:** it is resumed in place, with its context.
5. **A forced rotate:** handoff, fresh session, and the old one parked until the new one starts.
6. **An event deferred** while the operator has text in the input line; it is delivered once that line is empty.
7. **A question through the relay:** the answer reaches the step. `withdrawn` closes it.

## Prerequisites outside this task

- **Install:** re-run `install.sh` so the installed question tools match the repo (the installed copy predates
  tasks 45–49). Enable `claude-questions-listen`.
- **Questions config:** add `.claude/roles.toml` and a queue file to the epic workspace, with sync `"commit"` or off.
- **Epic-side work, done by the epic itself:**
  - compile the ledger and step cards from its plan;
  - move rollback triggers with a deadline off the workstation (037: C10's trigger to h01);
  - replace "AskUserQuestion" in its plan with `mcp__questions__ask`.

## Open points

- **O1 — The ledger format.** YAML, decided by the lead: the checks and timings must be exact.
- **O2 — Settled in rev 2.1:** the dead-man is designed but deferred (D11); Anton checks manually for the first epic.
