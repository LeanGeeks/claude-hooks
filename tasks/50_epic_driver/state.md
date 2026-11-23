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
| 50-01 | [amux-spawn: flag persistence, pinned env, Claude resume](./50-01-amux-spawn-flags-and-claude-resume_opus.md) | done | Phase 0 | P0-3, P0-9 (spawn side) |
| 50-02 | [Lifecycle: clear the `terminated` latch](./50-02-lifecycle-terminated-latch_sonnet.md) | done | Phase 0 | P0-4 |
| 50-03 | [Measurement helpers](./50-03-measurement-helpers_sonnet.md) | done | Phase 0 | context, cache TTL, compaction, last assistant entry |
| 50-04 | [Ledger, schedule, checks, runtime state (pure)](./50-04-ledger-schedule-checks-state_opus.md) | done | Phase 0 | P0-10, P0-13 (selection side) |
| 50-05 | [Session reconcile and stall detection](./50-05-session-reconcile-and-stalls_opus.md) | done | 50-01, 50-02, 50-03, 50-04 | |
| 50-06 | [Dispatch: session policy and delivery](./50-06-dispatch-policy-and-delivery_opus.md) | done | 50-05 | D4, D5, M6, m2 |
| 50-07 | [Worker waves (tick side)](./50-07-worker-waves_sonnet.md) | done | 50-04 | P0-7 |
| 50-08 | [Tick, CLI, alarms, notify, tmux server](./50-08-tick-cli-alarms_opus.md) | done | 50-04, 50-05, 50-06, 50-07 | P0-5, P0-8, P0-11 (`last_tick_*`), P0-12 (check) |
| 50-09 | [Role prompts](./50-09-role-prompts_sonnet.md) | done | 50-06, 50-08 | |
| 50-10 | [Docs and installation](./50-10-docs-and-installation_sonnet.md) | done | 50-08, 50-09 | P0-12 (stamps); units |
| 50-11 | Workstation root setup (sleep lock with no login) | dropped (P0-6) | — | No file. The id is kept so 50-12 keeps its number. |
| 50-12 | [Live verification](./50-12-live-verification_human.md) | blocked | 50-10 | **human** |

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
- **2026-11-22 — startup dialogs added (lead note).** At the manager session's own start Claude Code showed a
  "Code Mode (experimental)" opt-in (Enter = "No, start without it") that blocked the seeded prompt until the lead
  pressed Enter. Spawned sessions can sit on such dialogs (this one, folder trust, others). Added: architecture §12.11
  (detect on the pane at spawn/resume while the prompt is unconfirmed), the `startup_dialog` alarm (§16.2), the ledger
  key `startup_dialogs` (§5.2, §5.5; empty by default, so nothing is auto-answered unless listed). Scope: 50-04
  (schema), 50-05 (classifier, condition), 50-06 (the one listed key, committed before the send), 50-12 check 13
  (and check 9 now expects `startup_dialog`). The implementation manager started 50-01.
- **2026-11-22 — 50-01 done** (2 review passes, 2 fix passes; suite 2482 OK, 2 skipped). Architecture §13.3 amended
  from what 50-01 found: the resume prompt goes without `--` (amux 0.3.0 would put `--resume` after it and start a fresh
  conversation); only a missing model refuses a resume, an effort is passed when known; the growth check runs only with
  a prompt, and a resume whose transcript does not grow still records `attempt` + 1, `state = "spawning"` before its
  exit 1. Installed copies not refreshed yet (`./install.sh --yes` is needed before amux-spawn's change is live).
  Reports: `agents_output/50-01_*`.
- **2026-11-22 — 50-02 done** (1 review, 1 fix; suite 2489 OK, 2 skipped). A `turn_start` after `session_end` clears
  the latch; a turn opened before the end still reads terminated (pinned). The old `test_terminated_dominates_open_turn`
  (order `turn_start, session_end, turn_start`) was inverted, as P0-4 requires. Review note carried into 50-05's scope:
  `_derive_claude_status` still short-circuits on a stored `terminated` until the next prompt hook rewrites it.
- **2026-11-22 — 50-03 done** (1 review, 1 fix; suite 2520 OK, 2 skipped). `measure_transcript`,
  `cache_seconds_left`, `last_assistant_entry`, `compaction_boundaries`; status line and context-mcp output unchanged.
  Timestamps with offsets and under a non-UTC `TZ` pinned; a naive timestamp reads as UTC. Known: a transcript with no
  usage record in its tail is parsed in full (as specified).
- **2026-11-22 — 50-04 done** (2 reviews, 2 fixes; suite 2685 OK, 2 skipped). New `.claude/hooks/epic_driver_lib.py`
  (grammar, ledger + validator incl. `startup_dialogs`, schedule, approvals selection, check runner, runtime state) and
  `tests/test_unit_epic_driver.py`. Manager decisions on review LOWs: a check is not started past the ~150 s budget
  (first job of a pass always runs; timeouts never clamped); `check_every` slack scales with the period; `grant()`
  needs the ledger and refuses unknown ids (pre-granting allowed); duplicate YAML keys rejected, merge keys supported;
  a zero duration is an error where meaningless; a `default-fallback` context window warns. Architecture §8 gained
  the state keys `by` and `check.last_error`. Side edits: `context_mcp_lib` gets `from __future__ import annotations`
  (Python 3.9 import crash), a public epoch helper in `amux_spawn_lib`, and `test_unit_context_mcp` is now registered
  in the runner (it was never run there).
- **2026-11-22 — 50-05 done** (2 reviews, 3 fixes; suite 2784 OK, 2 skipped). Reconcile pass: session readings, verdict
  parser, delivery confirmation, stalls, startup-dialog classifier (§12.11), question reconcile, alarm conditions.
  From review: `delivery_provably_absent` is True only on positive proof (readable transcript covering the send, no
  `E-<n>` entry, no `turn_start` after the send, pane without the line) — otherwise no retry; an event turn is bounded
  by the next `turn_start`, and an interrupted one (Esc, then an operator turn) goes down §10's one-reminder /
  `verdict_missing` path once the session is idle (an interrupted reminder turn ⇒ `verdict_missing`, never a second
  reminder); the dialog pointer is `❯`/`›` with a hint line or numbered row. Fixtures: the "Code Mode (experimental)"
  opt-in pane is synthesised (not reproducible here); account details scrubbed. Obligations moved into 50-06's task
  file: commit `record_dialog_answer`, `launch_event` and `reminded_at` before their sends; `delivery_unconfirmed`
  with no further sends when absence is unprovable.
- **2026-11-22 — 50-06 done** (2 reviews, 2 fixes; suite 2861 OK, 2 skipped). Dispatcher: event files, the §11.2
  policy, rotation, resume, spawn, keep-warm, delivery with the input-box parser (8 new real pane captures from a
  throwaway tmux server), `(mutation, effect)` pairs with a crash matrix; all four 50-05 Binding obligations tested.
  Manager decisions on review LOWs: a failed spawn/resume backs off 10 m → 20 m → 40 m → 1 h 20 m, capped at 4 h,
  reset on success and on a reboot (alarm stays until a launch succeeds); a dialog key the guard refused (never ran)
  may be answered on a later pass, a key whose command ran is never re-sent; a retry falls back to `event_line`, never
  an empty send; after `handoff_timeout` the same generation is reused for `ROTATE_RETRY_AFTER = 1h`.
- **2026-11-22 — 50-07 done** (2 reviews, 1 fix; suite 2908 OK, 2 skipped). Integration worktree on `epic/<epic>`,
  step worktrees, the `merge-base --is-ancestor` sha check against `refs/heads/epic/<epic>`, worker `--dir` wired into
  50-06's spawn. From review: invariants 3/4/6 are now enforced at runtime — `RealWorld.git` and the fake share an
  argv allowlist that refuses reset/clean/stash/push/checkout/switch/update-ref, branch deletes or moves, `worktree
  remove|prune|move`, `worktree add -B|--force|--detach|--orphan` and foreign `-c` before git runs; the operator-repo
  snapshot covers every ref and worktree with explicit allowed deltas; a `base_ref` starting with `-` and sanitised
  step-id collisions are rejected at validation. Accepted without change (review 2, optional): `_git_env` does not
  scrub `GIT_CONFIG_*`/`GIT_EXEC_PATH` (matters only with a hostile driver environment), and the `rev-parse`/`status`
  allowlist is a flag allowlist, not an argv-shape check (read-only forms).
- **2026-11-23 — 50-08 done** (2 reviews, 2 fixes; suite 3030 OK, 2 skipped). `.claude/bin/epic-driver` (all §18
  verbs), the tick pass, tmux-server ownership, the environment snapshot, infrastructure checks, the alarm engine with
  the full §16.2 catalogue, the notify path (`questions_mcp_lib.notify(..., workspace_dir=)`, None unchanged),
  heartbeats. From review: one pass deadline (~270 s) governs every phase — work that does not fit is deferred,
  raises nothing and clears nothing; `EPIC_DRIVER_ALLOW_UNSTAMPED` is honoured only from a repo checkout, refused by
  `enable`, stripped from children; `CLAUDE_HOOKS_REPO` never reaches children; a stamp mismatch anywhere in
  `questions_mcp_lib`'s import closure blocks the notify import; `enable` rollback restores a pre-existing pointer
  file byte for byte; notifies have a wall-clock limit. Architecture §16.1 clarified: the first repeat comes 4 h after
  the ladder ends. Nothing on the workstation was enabled or started. The stamps manifest format and the repo-vs-
  installed detection 50-10 must honour are in `agents_output/50-08_implementation_report.md` and `_fix_report.md`.
- **2026-11-23 — 50-09 done** (2 reviews, 2 fixes; suite 3046 OK, 2 skipped). `docs/prompts/epic-driver-driver.md`
  and `docs/prompts/epic-driver-worker.md`, with prompt tests (every fenced `verdict` example parses; every force
  mention is a prohibition; no `git push` without `-C`; untagged verdict-like blocks fail). From review: a per-item-kind
  list in the driver prompt; review feedback always queued to the step's `worker.implementer`; every closing/push
  command carries `git -C <worktree>`; merge conflict ⇒ `merge --abort` in the integration worktree and `HALT`; workers
  may end a rotate turn with `HANDOFF:` (the code already accepted it; now pinned). Architecture §14.3 corrected to
  `queue <step> --to <role>`. Known limit accepted: the push-without-`-C` test is per-line (a second needle test covers it).
- **2026-11-23 — 50-10 done** (1 review, 1 fix; suite 3056 OK, 2 skipped). Installer feature `epic-driver` (off by
  default; requires amux, statusline, keep-awake, questions; three units written, none enabled; uninstall keeps the
  state dir), `stamps.json` written last in every run where the feature is installed (16 entries = the CLI's
  `STAMPED_FILES`, drift-tested; §17's list lacked `questions_git.py`/`questions_ops.py`), `docs/epic-driver.md`,
  `docs/installer.md`, top-level `architecture.md`. Sandbox install (temp HOME, feature on): `epic-driver validate`
  runs from `~/.local/bin` with stamps agreeing; a tampered file is refused. **The real `./install.sh --yes` was
  re-run** (lead-allowed): the ten selected features updated, installed hooks now match the repo (50-01…50-09 live);
  `epic-driver` stays off, no unit exists; the install manifest now records `epic-driver` as `skipped` — turn it on
  with `./install.sh --yes --with epic-driver`.
- **2026-11-23 — 50-12 blocked: awaiting the operator.** Every engineering task (50-01…50-10) is done and committed on
  `main` (not pushed). 50-12 is a `_human` live verification: no agent runs it and no live check is faked. It needs the
  operator present (a reboot, an 8 h idle window, Telegram). Summary: `agents_output/50_manager_summary.md`.
- **2026-11-23 — 50-12 rig prepared (operator-approved), still blocked on the operator.** `main` pushed to origin
  (fast-forward to fcca922). `./install.sh --yes --with epic-driver` installed the feature: units present, none
  enabled. Scratch rig at `~/epic50-rig/` (workspace `ws` with questions sync off, toy ledger `ws/plan/ledger.yaml` —
  7 steps, validates with 3 benign `context_window_default_fallback` warnings — scratch repo with a bare remote).
  Runbook and evidence template: [50-12-signoff-evidence.md](./50-12-signoff-evidence.md). The operator fills in the
  ssh host, runs `epic-driver enable` (it moves the shared tmux server out of the terminal's scope), and the 13 checks.
  Report: `agents_output/50-12_rig_report.md`.
