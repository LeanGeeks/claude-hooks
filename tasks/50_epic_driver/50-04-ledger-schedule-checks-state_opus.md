# 50-04 — Ledger, schedule, checks, runtime state (pure core)

**Status:** todo · **Depends on:** Phase 0
**Read first:** [brd.md](./brd.md) §5, §6 · [architecture.md](./architecture.md) §2 (the `World` boundary, the Python
floor), §3, §4, §5 (all), §6, §7, §8, §15.1, §16.2 (the step-level alarm rows) · [state.md](./state.md) P0-1, P0-2,
P0-10, P0-13, OQ-1, invariants 1, 5, 8, 9 · review M5, M10, m4, m5

## Goal

The part of the driver that needs no sessions: parse and validate the ledger, decide which steps are due and
dispatchable at a given `now`, run checks and triggers safely within a budget, and keep the runtime state on disk
atomically. Everything here is testable with a fake clock and a fake `World`.

## Scope

New module `.claude/hooks/epic_driver_lib.py` (this task creates it; later tasks add sections):

1. **Value grammar** (architecture §5.1): `duration`, `utc_time`, `tod`, `daily_window` (across midnight), `step_id`,
   `sanitise` and the name builder of §3 (`session_suffix(epic, role, step, gen)`, `worker_dir_name(epic, step)`).
2. **Ledger model and validator** (§5.2–§5.5): `load_ledger(path) -> Ledger` with `yaml.safe_load`, typed dataclasses,
   defaults applied, every validation error of §5.5 with the step id and field in the message, warnings separate.
   `base_ref` resolution and the work-tree test go through `World.git` so they can be faked. The compaction rule of
   §11.6 is validated here from the role's `pinned_env`, its profile (`amux_spawn_lib.resolve_profile`) and
   `context_mcp_lib.resolve_context_window` (import the repo or installed copy per §17; tests use the repo copy).
   `worktree_root` defaults per OQ-1.
3. **Schedule** (§6): the step state machine as data (statuses of §6.1); `due_steps(ledger, state, now)` and
   `dispatchable(…)` with the reasons a due step is held (paused, approval, window, latest start, resources, model
   limit, group incomplete) so 50-08 can decide `due_not_started` (§6.2); recurrence with `every`/`at`/
   `{period, from, deadline, warn_before}` (§6.3); `after` anchors; `not_before`/`not_after`/`warn_before`;
   `check_window` eligibility and coverage accounting (`window_covered_s`) (§7.3); `group` and `resource` kept minimal
   (review m4); deadline conditions for `deadline_near`/`deadline_missed` returned as alarm conditions, not sent.
4. **Approvals, selection side** (§15.1): grants read from `state.grants`; the "next approval to ask" function (map
   order, conditions done, gates an otherwise dispatchable step, no `approval_question` open); `grant(state, id,
   question, note, now)` used by `mark --grant`.
5. **Check and trigger runner** (§7.1, §7.2, §7.4): `/bin/sh -c` in its own process group, group kill at timeout,
   fixed environment from the snapshot, cwd `<workspace>/<plan_root>`, 4 KB output tail, per-step `check_every`,
   the ~150 s budget with `check_cursor` round-robin, consecutive-error counting. Results applied to the state per
   §7.1. The runner is the real `World.run_check`; the scheduling around it is pure.
6. **Runtime state** (§8): dataclasses or typed dicts for the JSON of §8, `load_state`/`save_state` (tmp +
   `os.replace` + fsync of file and directory), schema version check, the `mark <step> done|reset` rules, the paths of
   §4 (`state_root()`, `state_dir(run_id)`, honouring an env override for tests).
7. Constants of the architecture (*defaults*) as module-level names, in one block.

Out of scope: sessions, delivery, verdict parsing, alarms' notify side, the CLI.

## Tests

`tests/test_unit_epic_driver.py` (classes per area; later tasks add more):

- Grammar: durations, times with offsets normalised to UTC, windows across midnight, `sanitise` collisions.
- Validation, one test per §5.5 error and both warnings; the 037 fragment of §5.6 validates (with fake cards and a
  fake `World.git`).
- Due selection: `needs`; `after` with both anchor kinds; `not_before`; window and `latest_start`; `duration`;
  windows across midnight; `every` plain, with `at`, and with `{from, deadline, warn_before}` (the Monterey numbers:
  due 11-24 09:28:20Z, near from 11-27 09:28:20Z, missed at 11-29 09:28:20Z); `check_window` daily coverage (C15:
  passes only after ≥ 1 h inside 14–22Z) and relative; a span under 20 min rejected; `trigger`; `resource` caps; an
  ungranted approval holds the step and yields the approval to ask; one approval at a time in map order; `group`.
- Checks: exit 0, 1, 2, 3; a timeout kills the whole process group (a child `sleep` is gone afterwards); the budget
  stops at ~150 s and the next call resumes at the cursor; `check_every` rate limit; the environment holds only the
  fixed keys; cwd is the plan root.
- State: atomic write (a crash between tmp write and rename leaves the old file intact), schema mismatch refused,
  `mark reset` and `mark done` effects.

## Done when

- The module imports with the standard library, PyYAML and the hook modules only, under the Python floor
  (`tests/test_unit_python_compat.py` covers it).
- No function in this task starts a session, sends to a pane or notifies.
