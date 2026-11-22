# Epic 50 — State & orchestration

Read [brd.md](./brd.md), then [architecture.md](./architecture.md). Tasks run sequentially in table order
(`docs/prompts/implementation_manager.md`). The operator decisions in brd §4 are fixed and are not reopened by any
task, review or fix.

## Phase 0 — locked decisions (2026-11-22, by the lead; P0-6 and P0-11 by the operator; none pending)

Decided items bind every task. A task that finds one unworkable stops and reports a blocker; it does not choose
another way.

| # | Decision | Status | Reason | Architecture |
|---|---|---|---|---|
| P0-1 | **Name grammar.** Suffix `<epic>-<role>[-<step>]-g<gen>`, characters `[a-z0-9-]`, step ids sanitised. `gen` bumps on rotation only; a resume keeps the name. The tick fixes worktree directory names. | decided | The real name is `<basename(dir)>-<suffix>`, tmux rewrites `.`/`:`, and the handle's `attempt` already counts resumes (review m1). | §3 |
| P0-2 | **Runtime state and events** under `~/.local/state/epic-driver/<run_id>/` (state JSON, `events/`, outbox, handoffs, logs), with a small pointer file in the epic folder. Nothing churns in tracked trees. | decided | The epic workspace is already dirty and two writers (the driver, the questions listener) commit there (review m6). | §4 |
| P0-3 | **Model, effort and profile.** amux-spawn normalises `--model=`/`--effort=` to the space form before `amux exec`, so amux persists them in `CC_FLAGS`. The handle gains `profile`, `model`, `effort` (in `HANDLE_FIELDS`). The Claude resume re-exports the profile, passes model and effort only when `CC_FLAGS` lacks them, and checks the first post-restart assistant model against the handle; a mismatch is an alarm. | decided | amux stores only the space form and nothing records the profile, so a resume would run on the harness default (review B1). | §13.1–§13.3, §12.5 |
| P0-4 | **The latch.** A `turn_start` after a `session_end` clears `terminated`, in both the reducer and the stored handle state. No new hook. | decided | No `session_start` event exists; the cheaper option needs no installer wiring (review M3). | §13.4 |
| P0-5 | **The tmux server.** None running ⇒ the tick starts one with `systemd-run --user --scope --unit=epic-driver-tmux tmux new-session -d -s epic-driver-keepalive`, using the environment snapshot, verified through `/proc/<pid>/cgroup`. One inside a terminal's scope ⇒ `enable` moves the server pid into its own scope with `AttachProcessesToUnit`, or refuses with instructions. `enable` snapshots the operator's interactive environment (allowlisted keys) to the state dir. | decided | `tmux start-server` exits at once under `exit-empty`; the shared server lives in a terminal tab's scope; a service-started server gets a thin `PATH` (review M1, M2). | §21 |
| P0-6 | **The no-login sleep lock: none.** No polkit rule, no greeter change, no GDM auto-login, no workstation root setup. After a reboot the operator unlocks the disk and logs in; the driver recovers from that point, possibly with no terminal opened (so the timer and lingering still matter). The inhibitor stays: it holds while he is logged in. | decided (operator, 2026-11-22) | "Reboots are rare, and never unattended — the login screen won't stay up for long. Also, there's disk encryption, so a fully unattended reboot would never finish booting." Review B2's no-login case cannot occur. Task 50-11 dropped. | §22 (after a reboot), §21; brd §6 |
| P0-7 | **The integration target.** The driver integrates in its own integration worktree on a branch it owns (`epic/<epic>`). `base_ref` is a ref verified with `rev-parse`, never the state of the operator's primary checkout. A branch the operator has checked out is never moved. Targets move only by pushing to the remote, or by the operator. Repos without a remote stay on the epic branch. `repos[].path` absolute and unique. | decided | Git refuses to move a branch checked out elsewhere, and 037's repos are dirty, on other branches, or have no remote (review M4). | §14 |
| P0-8 | **The notify path.** The tick imports `questions_mcp_lib.notify` in process with an explicit workspace dir (the epic workspace). Alarms `ack=True`, no 4-hourly repeat while their nudge ladder is live. Phase heartbeats `ack=False`. A failed notify goes to the local log and to `status`. | decided | The tick has no LLM and the CLI has no notify flag; ladders plus repeats would double up (review M7). | §16 |
| P0-9 | **Compaction.** Pin the compaction environment for driven sessions at spawn and record it in the handle. The cap stays below the compaction point. A compaction boundary in the transcript is an unplanned rotation and raises an alarm. | decided | Profiles set autocompact at 65 % and idle compaction at 200K, which can rewrite a context D4 says is not in the files (review M9). | §11.6, §13.1 |
| P0-10 | **The tick's budget.** 60 s per check by default, about 150 s total per tick, round-robin when over budget. State committed after each dispatch. Checks in their own process group, killed on timeout, cwd in the epic workspace, fixed environment. The ledger rejects tick checks with a `check_window` under 20 min. | decided | No timeout or total budget existed, `KillMode=process` leaves children, and 037 has checks minutes long that a 10-min tick cannot serve (review m5, M10). | §7, §23 |
| P0-11 | **Liveness without a dead-man: no daily line.** No daily alive message and no config flag for one. Liveness is phase-transition and alarm notifies only, plus `epic-driver status` showing `last_tick_at` and the last tick error, which the operator checks himself. | decided (operator, 2026-11-22: "No daily line") | D9 already rules out a daily digest; the operator watches the first epic by hand. The silent-stall gap stays stated in brd §6 (review M8). | §16.5, §20 |
| P0-12 | **Imports.** The driver runs from installed copies placed by its installer feature. Each copy carries a version stamp; the tick refuses to run when the stamps of the modules it imports disagree. | decided | Same rule as every other feature; the statusline is installed while `context_mcp_lib` existed only in the repo (review m3). | §17 |
| P0-13 | **Approvals.** Grants live in the runtime state, not the ledger. The tick raises an `approval_needed` driver event; the driver asks with a recommendation through `mcp__questions__ask`, at most one approval question open at a time, in ledger order; on the answer it runs `epic-driver mark --grant <id>`. | decided | The tick cannot ask, a Telegram "yes" grants nothing by itself, and the ledger is LLM-compiled and git-tracked (review M5). | §15.1 |

Also adopted from the review as written: M6 (always check the input box; unparseable = defer), M10 (check windows),
m2 (overage reuses in place), m3 (numeric `cache_seconds_left`), m4 (alarm on orphans, never adopt; `resource`,
`trigger`, `check_window` built minimal), m5, m7 (live check 7 with a question that has buttons), m8 (`enable` runs
`claude-questions --check` and refuses `sync = "push"`), m9 (live check in a never-seen worktree), m10 (the worker
prompt wraps the epic's own prompts). M8's silent-stall gap is stated in brd §6.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 50-01 | [amux-spawn: flag persistence, pinned env, Claude resume](./50-01-amux-spawn-flags-and-claude-resume_opus.md) | todo | Phase 0 | P0-3, P0-9 (spawn side) |
| 50-02 | [Lifecycle: clear the `terminated` latch](./50-02-lifecycle-terminated-latch_sonnet.md) | todo | Phase 0 | P0-4 |
| 50-03 | [Measurement helpers](./50-03-measurement-helpers_sonnet.md) | todo | Phase 0 | context, cache TTL, compaction, last assistant entry |
| 50-04 | [Ledger, schedule, checks, runtime state (pure)](./50-04-ledger-schedule-checks-state_opus.md) | todo | Phase 0 | P0-10, P0-13 (selection side) |
| 50-05 | [Session reconcile and stall detection](./50-05-session-reconcile-and-stalls_opus.md) | todo | 50-01, 50-02, 50-03, 50-04 | |
| 50-06 | [Dispatch: session policy and delivery](./50-06-dispatch-policy-and-delivery_opus.md) | todo | 50-05 | D4, D5, M6, m2 |
| 50-07 | [Worker waves (tick side)](./50-07-worker-waves_sonnet.md) | todo | 50-04 | P0-7 |
| 50-08 | [Tick, CLI, alarms, notify, tmux server](./50-08-tick-cli-alarms_opus.md) | todo | 50-04, 50-05, 50-06, 50-07 | P0-5, P0-8, P0-11 (`last_tick_*`), P0-12 (check) |
| 50-09 | [Role prompts](./50-09-role-prompts_sonnet.md) | todo | 50-06, 50-08 | |
| 50-10 | [Docs and installation](./50-10-docs-and-installation_sonnet.md) | todo | 50-08, 50-09 | P0-12 (stamps); units |
| 50-11 | Workstation root setup (sleep lock with no login) | dropped (P0-6) | — | No file. The id is kept so 50-12 keeps its number. |
| 50-12 | [Live verification](./50-12-live-verification_human.md) | todo | 50-10 | **human** |

## Cross-task invariants

From the review (§3):

1. **The tick never runs an action, only checks.** Production writes happen only inside driven sessions (D8).
2. **The tick never sends twice unconfirmed.** At most two sends per event, the second only when the first provably
   did not land (architecture §9.3).
3. **No push from the driver is ever a force-push.**
4. **Implementers never commit.** Only the driver commits, in driver-owned worktrees.
5. **State is written atomically before any side effect it records** (architecture §23).
6. **Nothing touches the operator's primary checkouts**: no checkout, reset, stash, merge, commit or branch move by
   the tick or the driver. (The questions tool's own pathspec commits, epic 46, are not the driver's.)
7. **Handle fields come only from `amux_spawn_lib`** (`HANDLE_FIELDS`); no task writes a field the lib does not
   document.

Following from Phase 0:

8. **No runtime file in a tracked tree** except the pointer file `enable` writes once (P0-2).
9. **A grant exists only in the runtime state**, written only by `epic-driver mark --grant` (P0-13).

## Open questions (all settled by the lead, 2026-11-22: each default below is accepted as written)

These were raised while the folder was written. The lead accepted every proposed default, so treat them like
Phase 0 decisions. They stay listed here for the reasoning.

- **OQ-1 — Where worktrees live.** Default `worktree_root = <state_dir>/worktrees`
  (`~/.local/state/epic-driver/<run_id>/worktrees/`), overridable in the ledger. Touches 50-04 (default), 50-07.
- **OQ-2 — The pinned compaction values.** Fact: `~/.claude/profiles.toml` sets `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE =
  65` (`claude`, `claude-fable`) and `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS = 200000` (`[all-profiles]`), the latter
  below the 250K cap. Proposed for driven roles: keep 65 (650K on a 1M window) and pin the idle value to 300000; the
  validator enforces `cap < both`. Touches the 037 ledger and 50-12's toy ledger only.
- **OQ-3 — How a copy carries its stamp.** Architecture §17 realises P0-12 as a manifest (`stamps.json`: revision and
  sha256 per installed file) so installed files stay byte-identical to the repo. If the operator wants the stamp
  inside each file instead, only 50-08 (the check) and 50-10 (the writer) change.
- **OQ-4 — The driver → worker channel.** The spec says review feedback "goes back into the implementer's session
  under D4" but names no mechanism, and driven sessions may not run `amux`. Architecture §14.3 adds
  `epic-driver queue <step> --to <role> --file <path>`, which the tick delivers as a `feedback` event. Touches 50-06,
  50-08, 50-09.
- **OQ-5 — HALT and usage limits as alarms.** D9 calls a `HALT` notify a heartbeat; architecture §16.2 sends a driver
  `HALT` as the `halt` alarm (`ack=True`) because it is a stop that needs the operator, and a usage limit as the
  `usage_limit` alarm. A worker `HALT` is an event to the driver, not an alarm. Touches 50-08.

## Log

- **2026-11-22 — epic folder written** from spec rev 2.2 (kept verbatim as [spec_rev2.2.md](./spec_rev2.2.md); the
  original `tasks/50_epic_driver.md` was never committed and is now a redirect) and the handover-readiness review
  (`agents_output/50_spec_review_report.md`). The lead locked P0-1…P0-5, P0-7…P0-10, P0-12, P0-13; P0-11 waits for
  the operator. Twelve task ids per the review's split. No code changed. Five open questions recorded above.
- **2026-11-22 — P0-6 decided by the operator: no sleep-lock change.** Reboots are rare and never unattended; disk
  encryption means an unattended reboot never finishes booting. After a reboot he unlocks the disk and logs in, and
  the driver recovers from there (maybe with no terminal opened). Task 50-11 (root setup) dropped, id kept; 50-12's
  live checks changed from "reboot with no login" to "reboot, then login, no terminal opened"; the greeter-suspend
  check is gone. The environment snapshot (P0-5) and the inhibitor stay.
- **2026-11-22 — P0-11 decided by the operator: "No daily line".** No daily alive message and no `notify.daily_alive`
  flag; removed from the ledger schema, the runtime state, 50-08 and 50-12. Liveness = phase-transition and alarm
  notifies plus `status` (`last_tick_at`, last tick error). Phase 0 has no pending decisions.
- **2026-11-22 — OQ-1…OQ-5 settled by the lead:** every proposed default accepted (worktrees under the state dir;
  autocompact 65 with idle compaction pinned to 300000 and `cap < both` validated; a `stamps.json` manifest; `epic-driver
  queue` for driver → worker feedback; a driver `HALT` and usage limits as `ack=True` alarms). The folder is handed
  over to the implementation manager.
