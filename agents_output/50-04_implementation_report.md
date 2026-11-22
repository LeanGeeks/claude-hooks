# Implementation Report - 50-04 Ledger, schedule, checks, runtime state (pure core)

## Summary

Created `.claude/hooks/epic_driver_lib.py`. It holds the session-free core of the epic driver: the §5.1 value grammar and the §3 name builder, the ledger model and full §5.5 validator (including `startup_dialogs` and the §11.6 compaction rule), the §6 step state machine with due and dispatchable selection and hold reasons, the §15.1 approval selection and `grant`, the §7 check and trigger runner with process-group kill, the 150 s round-robin budget and `check_every`, the §8 runtime state with atomic fsynced writes and the `mark done|reset` rules, and the step-level §16.2 alarm conditions. Every effect goes through an extensible `World`. Nothing in the module starts a session, sends to a pane or notifies. Tests are in `tests/test_unit_epic_driver.py` (122 tests). The module is registered with the runner.

## Files Created

- `.claude/hooks/epic_driver_lib.py`: the lib, in delimited sections (Constants, Time, World, Value grammar, Ledger, Runtime state, Schedule, Approvals, Checks and triggers, Step alarm conditions). 50-05 to 50-08 append their own sections and add `World` methods.
- `tests/test_unit_epic_driver.py`: classes `TestGrammar`, `TestLedgerValidation`, `TestDueSelection`, `TestCheckWindow`, `TestStepAlarmConditions`, `TestCheckRunner`, `TestRuntimeState` and `TestModuleContract`, under "50-04 —" banners. It also has the shared `FakeWorld` (fake clock, fake git repos, scripted checks, fake profiles) and the `_Base`/`_Sched` fixtures that later tasks can reuse.

## Files Modified

- `tests/run_all_tests.py`: registered `"unit_epic_driver": "test_unit_epic_driver"`. Added `os.environ.setdefault("EPIC_DRIVER_STATE_ROOT", <tmp>/epic-driver)` next to the other isolation overrides, so no test run can reach `~/.local/state/epic-driver`. I confirmed the runner loads and runs the module ("Loaded tests from: test_unit_epic_driver"; `--module unit_epic_driver` runs 122 tests).
- `tests/conftest.py`: the same `EPIC_DRIVER_STATE_ROOT` default for the pytest path.
- `tests/test_unit_python_compat.py`: the static checks glob `.claude/hooks/*.py`, so they already cover `epic_driver_lib.py`. New `TestEpicDriverCovered` pins that. I extended the three static checks (floor syntax, tomllib guard, PEP 604 unions) with `EPIC_DRIVER_IMPORT_MODULES`: `context-mcp/context_mcp_lib.py`, `.claude/statusline/statusline.py` and `questions-mcp/questions_mcp_lib.py`. These are the driver's §17 imports that live outside the hooks dir.
- `context-mcp/context_mcp_lib.py`: added `from __future__ import annotations`. **Finding:** this module raised `TypeError: unsupported operand type(s) for |` at import on Python 3.9 (verified with uv's cpython 3.9.21) because of its `str | None` annotations. The compaction validation imports it per §17, so the driver could not run under the floor. The change is a single line and changes no behaviour (the 50-03 measurement tests stay green). The extended compat test now pins it.

## Verification

- Compile/import: PASS. `py_compile` on all changed files. The module imports on 3.14. On **Python 3.9.21** it imports with PyYAML and without it (`yaml is None`), using tomli from the uv cache for `amux_spawn_lib`. The whole `test_unit_epic_driver` module also passes under 3.9.21 (122 OK).
- Tests: `python3 tests/run_all_tests.py --module unit_epic_driver`: 122 tests, OK. `--module unit_python_compat`: 15 tests, OK (1 skipped, as before). Full suite `python3 tests/run_all_tests.py`: **2644 tests, OK (skipped=2)**, 0 failed (baseline 2520 + 122 new + 2 new compat tests = 2644).
- Mutation checks (cp backup, then restore, no git checkout): with the post-exit group sweep removed, `test_leftover_children_are_killed_after_a_normal_exit` fails. With `start_new_session=False`, the run hangs on the timeout test because the group kill cannot reach the shell; that run was stopped by `timeout`. The file was restored byte-identical and no `sleep` children were left behind.
- Installed (install.sh --yes re-run): not applicable. The installer feature is 50-10's, and nothing here is wired into a hook.

## Public API (the contract for 50-05 to 50-08)

**Constants** (one block): `LEDGER_VERSION`, `STATE_SCHEMA`, `STATE_ROOT_ENV="EPIC_DRIVER_STATE_ROOT"`, `DEFAULT_*` (check timeout 60 s, check every 10 m, step timeout 6 h, max attempts 3, warn before 24 h, cap 250000, bootstrap 40000, keep_warm, stuck_after 30 m), `MAX_SESSION_NAME_LEN`, `CHECK_BUDGET_S=150`, `CHECK_KILL_GRACE_S=5`, `CHECK_OUTPUT_TAIL_BYTES=4096`, `CHECK_ERROR_ALARM_AT=3`, `MIN_CHECK_WINDOW_S=1200`, `CHECK_EVERY_SLACK_S=180`, `CHECK_SSH_OPTS`, `FALLBACK_CHECK_PATH`, `DUE_NOT_STARTED_TICKS=2`, `LOG_ROTATE_BYTES`/`LOG_KEEP`, `ENV_SNAPSHOT_KEYS`/`ENV_SNAPSHOT_PREFIXES`, `STARTUP_DIALOG_KEYS`, `PINNABLE_ENV_KEYS` (re-exported from `amux_spawn_lib`), and the §4 file names.

**Time:** `UTC`, `require_aware(now)`, `iso(dt)`, `from_iso(value)` (built on `amux_spawn_lib._epoch_of`; naive values read as UTC), `tod_seconds(dt)`.

**World:**
- `World` has `now()`, `git(args, cwd, timeout_s=30)`, `run_check(command, cwd, env, timeout_s)` and `profile_env(name)`. The base methods raise `NotImplementedError`, so a fake fails loudly on an effect it did not expect. Later tasks add one method per effect to `World` and `RealWorld`.
- `RealWorld.git` runs `git -c core.hooksPath=/dev/null -C <cwd> …` with a timeout, `GIT_TERMINAL_PROMPT=0` and no stray `GIT_*` variables (the `questions_git` discipline, as 50-07 asks).
- `RealWorld.run_check` is the §7.2 runner.
- Result types: `CommandResult(rc, stdout, stderr, error)` with `.ok`, and `CheckRun(rc, started_at, duration_s, output_tail, timed_out, error)`.

**Grammar and names:** `parse_duration`, `format_duration`, `parse_utc_time`, `parse_tod`, `parse_daily_window` → `DailyWindow` (`length_s`, `crosses_midnight`, `offset(t)`, `contains(t)`, `coverage_s(a, b)`, `next_open(t)`), `is_step_id`, `sanitise`, `session_suffix(epic, role, step, gen)`, `worker_dir_name(epic, step)`, `session_name(dir_basename, suffix)`. The regexes are `STEP_ID_RE`, `RUN_ID_RE`, `WORKSPACE_BASENAME_RE` and `STARTUP_DIALOG_ID_RE`.

**Ledger:**
- `validate_ledger(path, world=None, *, verify_base_refs=True) -> (Ledger | None, errors, warnings)`.
- `load_ledger(path, world=None, *, verify_base_refs=True) -> Ledger`, which raises `LedgerError(.errors, .warnings)`.
- Issues are `LedgerIssue(category, where, message)`. The error categories are `E_SCHEMA`, `E_GRAPH`, `E_FILES`, `E_NAMES`, `E_APPROVALS`, `E_TIMING`, `E_RESOURCES`, `E_REPOS`, `E_COMPACTION` and `E_STARTUP_DIALOGS`; the warnings are `W_DRIVER_STEP_NO_CHECK` and `W_SSH_NO_TIMEOUT`. `where` names the field, e.g. `steps.<id>.after.step` or `roles.<key>.cap`.
- Dataclasses: `Ledger` (`run_id`, `epic_key`, `plan_dir`, `worktree_root`, `worktree_root_default`, `sha256`, `roles`, `resources`, `startup_dialogs`, `approvals`, `steps`, `warnings`; methods `step(id)`, `has_step`, `steps_by_id`, `approvals_in_order()`, `resolve_path`), `Defaults`, `Role`, `CommandSpec`, `After`, `Every`, `CheckWindow`, `Worker`, `WorkerRepo`, `Step`, `StartupDialog` and `Approval`.
- `Role` also carries the derived fields `expected_model_id`, `context_window`, `context_window_source`, `auto_compact_point` and `idle_compact_point`.
- `Step` has `is_watch`, `is_recurring`, `target_role` and `latest_start_offset_s()`.
- `StartupDialog` has `.pattern` (compiled) and `.matches(pane_text)`.

**Runtime state:**
- TypedDicts: `RunState`, `StepState`, `CheckRecord`, `TriggerRecord`, `Grant`.
- Paths: `state_root()` (honours `EPIC_DRIVER_STATE_ROOT`), `state_dir(run_id)`, `run_paths(run_id) -> RunPaths` (`active`, `state`, `lock`, `paused`, `env_snapshot`, `last_valid_ledger`, `events`, `handoffs`, `reports`, `logs`, `tick_log`, `notify_log`, `worktrees`, `check_log(step)`), `pointer_path(ledger)`.
- Constructors: `new_state(ledger, now)`, `new_step_state()`, `new_check_record()`, `ensure_step_states(ledger, state)`.
- Persistence: `atomic_write_text(path, text)`, `save_state(path, state)`, `load_state(path, run_id=None)`, which raises `StateError`. `load_env_snapshot(paths)`.
- Marks: `mark_done(ledger, state, step, now)` and `mark_reset(ledger, state, step, now) -> {"cancelled_events", "cleared_alarms"}`.

**Schedule:**
- Statuses `PENDING` … `WATCHING` and `STEP_STATUSES`. The state machine as data: `STEP_TRANSITIONS`, `MARK_TARGETS`, `can_transition`, `set_status(rec, new, by_mark=False)`. Status sets `RESOURCE_HOLDING` and `CHECKED_STATUSES`.
- `due_steps(ledger, state, now) -> [DueStep(step_id, due_at, occurrence, why)]`.
- `select(ledger, state, now, *, paused=False) -> Selection(due, dispatchable, units, held: {id: [Hold(reason, detail)]}, approval_to_ask)`. `dispatchable(...)` returns `(ids, held)`.
- Hold reasons: `HOLD_PAUSED`, `HOLD_APPROVAL`, `HOLD_APPROVAL_ASKED`, `HOLD_APPROVAL_CONDITIONS`, `HOLD_WINDOW`, `HOLD_LATEST_START`, `HOLD_RESOURCES`, `HOLD_MODEL_LIMIT`, `HOLD_GROUP`, `HOLD_ATTEMPTS`. `QUIET_HOLDS` and `due_not_started_quiet(holds)` serve `due_not_started`.
- `note_undispatched(ledger, state, selection, dispatched)`.
- `advance_schedule(ledger, state, now) -> [(step, old, new)]` covers the recurring rollover, pending→due, pending→watching and due→pending.
- Time helpers: `step_due_at(ledger, state, step)` (future due times, for 50-06's keep-warm), `occurrence_due_at`, `series_reference`, `occurrence_deadline`, `step_is_done`, `step_done_at`, `approval_granted`, `approval_conditions_done`, `model_limited(ledger, state, role, now)`.

**Approvals:** `next_approval_to_ask(ledger, state, selection) -> ApprovalAsk(approval_id, steps, conditions) | None` and `grant(state, id, question, note, now, *, ledger=None)`.

**Checks:**
- `eligible_checks(ledger, state, now)` and `order_by_cursor(jobs, ledger, cursor)`.
- `check_env(run_id, step, state_dir, snapshot, base_env=None)`.
- `apply_check_result(ledger, state, job, run, now) -> CheckOutcome(step_id, kind, outcome, rc, output_tail, attempt, consecutive_errors, error)`.
- `run_checks(ledger, state, world, *, snapshot=None, budget_s=150, base_env=None, log=True) -> CheckPass(ran, not_reached, cursor)`.
- `log_check_run(paths, job, run)`. Plus `check_window_status`, `check_anchor`, `CheckJob`, `KIND_CHECK`/`KIND_TRIGGER` and `OUTCOME_*`.

**Step alarm conditions:** `AlarmCondition(id, subject, detail)` with `.key`, `deadline_conditions(ledger, state, now)`, and `step_alarm_conditions(ledger, state, now, selection=None)`. The latter covers `deadline_near`/`deadline_missed`, `attempts_exhausted`, `check_error`, `step_timeout`, `halt`, and `due_not_started` when a selection is passed. `worktree_refused` is 50-07's.

## Decisions

1. **PyYAML.** It is importable here: system package 6.0.3 at `/usr/lib/python3/dist-packages/yaml`. It works on 3.14 and, in pure-Python mode, on 3.9. No other hook imports yaml. Hook modules handle optional deps with `try: import … except ImportError:` (the tomllib → tomli pattern), so I followed that: `yaml = None` when it is absent, which keeps every hook module importable. In that case `validate_ledger` reports a schema error ("PyYAML is not installed"). Only `yaml.safe_load` is used.
2. **`context_mcp_lib` import (§17).** It is imported lazily by `_context_mcp_lib()`: from `sys.path` first (the CLI puts `~/.claude/epic-driver/lib` there), else from beside the running copy (installed `~/.claude/epic-driver/lib`, or the repo's `context-mcp/`). Tests put the repo copy on `sys.path`. If it cannot be imported, the result is a compaction validation error, not a crash.
3. **`World.profile_env(name)`** was added so the compaction rule reads profiles through the boundary. `RealWorld` calls `amux_spawn_lib.resolve_profile`, and tests never read `~/.claude/profiles.toml`. A profile that does not resolve is a compaction error: the rule cannot be checked, and the spawn would fail anyway.
4. **Compaction rule.** The expected model is `expected_model_id(model, profile_env)`, and the window is `resolve_context_window(expected or the alias)`. An unmapped alias (`fable` on `claude-fable`) usually gets the 1M `default-fallback`. That is the formula as written, and the source is recorded on `Role.context_window_source`. A pinned value overrides the profile's. The check is `cap < min(known points)`, and the message names both points and points to OQ-2.
5. **An unquoted `epic: 037`** is an int (YAML octal 31). It is rejected as a schema error with a hint to quote it.
6. **The 037 fragment test** reads the fragment verbatim out of `architecture.md` §5.6, so spec drift shows up. It substitutes a temp workspace and creates the cards. Because the fragment names `e1-build` (in `needs` and in SA4's conditions) but never defines it, the test adds that one step. It validates with no errors and no warnings. Without the OQ-2 idle pin it fails with the compaction error, as intended.
7. **Interpretations within §5.5.** These make no new decisions:
   - `latest_start` or `duration` without a `window`, and a `duration` that leaves no start inside the window, count as "outside window" (timing).
   - `at` requires a plain whole-day `every` (timing).
   - "Worker `epic_prompt` missing" means set but absent. A worker role may omit `epic_prompt`.
   - Name lengths are checked at gen 1.
   - Repo `path` and `name` uniqueness is per step (the same repo is naturally reused across steps).
   - A watch step without a `check` is a schema error (§5.4: "Required for watch steps").
   - Worker implementer and reviewer must be known roles that differ (D14).
8. **ssh warning.** It scans the command and, when the command's first word is a file under the plan dir or an absolute file, that script too (up to 256 KB), because 037's checks are scripts. It does not warn when `ConnectTimeout` or `EPIC_DRIVER_SSH_OPTS` appears.
9. **`check_every` slack.** `CHECK_EVERY_SLACK_S = 180` (budget + 30 s timer accuracy). Without it, a 10-minute `every` would skip alternate 10-minute ticks, because a check can start up to 150 s into its tick and the timer has `AccuracySec=30s`. So a 30 m `every` runs again after at least 27 m.
10. **`due_since`** is the computed time the step became due (the latest of the needs' done times, `after` anchor + delay, `not_before`, the occurrence time and the trigger fire time). It falls back to `now` only when no time is known. The `check_window` anchor of a watch step (its activation) is this value, so it is exact rather than late by a tick.
11. **Recurrence.**
    - Plain `every`: the first occurrence is at `not_before`, else at `enabled_at` + period. Later ones are at `last_done_at` + period.
    - With `at`: the first slot is at or after `not_before`/`enabled_at`; later slots are the first slot strictly after `last_done_at` + (period − 1 d), so a late run stays on the grid.
    - Series without `from`: the reference is `enabled_at`.
    - `advance_schedule` rolls a done recurring step to `occurrence + 1` (pending, with attempts, check and trigger counters reset) and keeps `last_done_at`.
    - A recurring `needs` counts as done once it has a done occurrence.
12. **Holds.**
    - Ledger order allocates resources.
    - Resources are held while a step is `inflight`, `verifying`, `waiting` or `asked`, but not `halted`.
    - A group dispatches as one `unit` only when every member is due and unheld.
    - `HOLD_APPROVAL_ASKED` applies when any approval question is open (one at a time).
    - `QUIET_HOLDS` = paused, an approval question open, a model limit, a closed window, past the latest start (the window closed for today), and exhausted attempts (which have their own alarm). Any quiet hold suppresses `due_not_started`.
    - The model limit matches `state.limits` keys with `amux_spawn_lib.model_matches(role.expected_model_id, role.model, key)`, so an unmapped alias matches by family.
13. **Approval to ask.** It is asked only when every hold on a gated due step is the approval itself; resources are not pre-checked, so it may be asked slightly early. `grant` is idempotent: an existing grant is returned unchanged. It does not clear `approval_question`; per §15.1 that clears when the question closes.
14. **Check results.**
    - Check and trigger errors share one counter, `check.consecutive_errors`, which a 0, 1 or 2 of either resets.
    - A trigger exit 2 is an error (§7.4: "anything else").
    - Check rc 2 sets `failed` and increments attempts. The returned `CheckOutcome` has what 50-08's `check_failed` event needs.
    - Checks run only in `verifying`, `waiting` and `watching`. Triggers run only while `pending`, once every other due condition holds, and not after they have fired for the occurrence.
15. **The runner.**
    - The timeout sends SIGTERM to the group, waits up to 5 s for the group to empty, then sends SIGKILL.
    - Even after a normal exit, the rest of the group gets SIGKILL. Checks are read-only, and a leftover child holding the pipe would otherwise block the output reader.
    - Output is kept as a 4 KB tail (bounded memory).
    - "Cannot start" gives `rc=None` with an `error`.
    - The budget check is "stop starting new runs once elapsed ≥ 150 s", measured by `world.now()`. When every job was reached, the cursor clears.
16. **Check logs** (`logs/checks/<step>.log`) rotate at 1 MB, keeping 3, like `tick.log`; §4 gives no rule for them. `_append_rotating` can be reused for `tick.log` and `notify.log`.
17. **Check environment.** `PATH` comes from the snapshot, else `/usr/local/bin:/usr/bin:/bin` (the unit's PATH without `~/.local/bin`). `LANG` comes from the snapshot, else the service env, else `C.UTF-8`. `SSH_AUTH_SOCK` is passed only when it is an existing socket.
18. **`mark reset`.**
    - It keeps `occurrence`, `last_done_at` and `questions`, and also clears the trigger record ("start the step over").
    - An unconfirmed outbox entry loses the step; an entry left with no steps is dropped (cancelled).
    - Alarms whose subject is the step or a cancelled event get `cleared_at`.
    - `mark done` sets `by: "operator"`. Two state keys beyond the §8 example: `steps.*.by` (`"operator"` / `"check"`) and `check.last_error`.
19. **Deadlines.** Once missed, only `deadline_missed` is reported for the step; `deadline_near` is not reported alongside it.
20. **Watch steps** are returned by `due_steps` (so `advance_schedule` moves them to `watching`), but they are never dispatchable.

## Notes for later tasks (not blockers)

- 50-05: approval ids and step ids share the verdict `id` namespace, and the validator does not reject a collision (not in §5.5).
- 50-08: `mark_reset` clears the step's alarm records, but `step_alarm_conditions` recomputes conditions each pass. For example, `deadline_missed` comes back after a reset because the deadline is still past. The alarm engine has to decide how "cleared by mark" holds against a still-true condition.
- 50-08: the `ledger.last_valid.yaml` fallback and the `ledger_invalid` alarm are left to the tick. `RunPaths.last_valid_ledger` and `atomic_write_text` are ready for it.
- `.claude/statusline/statusline.py` and `questions-mcp/questions_mcp_lib.py` pass the static floor checks now that they are in the compat list.

## Blockers

None.

## Questions for User

None.
