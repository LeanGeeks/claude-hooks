# Implementation Report - 50-08 Tick, CLI, alarms, notify, tmux server

## Summary

I put the 50-04 to 50-07 pieces together:

- the one-pass tick of §23, with `tick.lock` and a time bound under the unit's 5 min `TimeoutStartSec`;
- the `epic-driver` CLI with every §18 verb and the §17 stamp check, which runs before any non-stdlib import;
- the §16 alarm engine (raise, repeat, clear) and its notify path, which is `questions_mcp_lib.notify` called in
  process with the new keyword-only `workspace_dir`;
- the §16.5 phase heartbeats;
- the §12.10 listener and inhibitor checks;
- the §21 tmux server work: start it in its own scope with the environment snapshot, verify it through
  `/proc/<pid>/cgroup`, and let `enable` move it with `AttachProcessesToUnit`.

Every systemd, busctl, tmux-server and notify call goes through `World`. The tests use a fake `World` only. They
never touch real systemd, tmux, the relay or Telegram.

## Files Created

- `.claude/bin/epic-driver`: the CLI. It holds argparse, the path setup for the installed and repo layouts, the
  §17 stamp check (stdlib only), and the stdlib-only `stamp_mismatch` path of the tick. It then calls
  `epic_driver_lib.cli`.

## Files Modified

- `.claude/hooks/epic_driver_lib.py`:
  - A 50-08 constants block.
  - 11 new `World` methods (`systemctl`, `systemd_run`, `inhibit_list`, `busctl`, `tmux_server_pid`,
    `read_cgroup`, `notify`, `notify_index_ids`, `questions_settings`, `questions_check`, `sleep`) and their
    `RealWorld` implementations.
  - `_workspace_roles_path`, factored out of `question_entries`; its behaviour is unchanged.
  - `execute_dispatch` gains an optional `gate=` keyword. When it is omitted, behaviour is unchanged.
  - `SPAWN_TIMEOUT_S` 180 → 60 and `RESUME_TIMEOUT_S` 120 → 60 (both *defaults*; see Decision 1).
  - The new section "The tick, alarms and notify, infrastructure, the tmux server, the CLI verbs — 50-08". It
    covers the environment, the tmux server, infrastructure, notify, the alarm engine, heartbeats, the lock and
    `active.json`, the tick, status and the verbs.
  - The module docstring table.
- `questions-mcp/questions_mcp_lib.py`: `notify(text, role=None, ack=True, *, workspace_dir: Optional[str] = None)`.
  With `None` it calls the same `_resolve_workspace_dir()` as before; otherwise it uses the given directory. The
  docstring gained the new parameter. Nothing else changed.
- `tests/test_unit_epic_driver.py`, existing tests:
  - Two contract tests updated:
    - `test_no_session_pane_or_notify_effects` now pins where the new effects may appear (details in Decision 18).
    - The 50-07 git grep is now limited to the 50-07 section. Before, it reached the end of the file and so took in
      50-08's `"-d"` (part of `tmux new-session -d`).
- `tests/test_unit_epic_driver.py`, new test classes (80 tests):
  - the tick: `TestTickFullPass`, `TestTickLock`, `TestTickCrash`, `TestTickTiming`, `TestDryRun`;
  - the CLI and its verbs: `TestStamps`, `TestEnable`, `TestVerbs`, `TestStatus`;
  - infrastructure: `TestTmuxServer`, `TestInfrastructure`, `TestEnvironment`, `TestRealWorld50_08`;
  - alarms and heartbeats: `TestAlarmEngine`, `TestAlarmCatalogue`, `TestHeartbeats`,
    `TestReconcileAlarmsThroughTheTick`;
  - the shared fixtures `TickWorld`, `_Tick` and `_Engine`.
- `tests/test_unit_questions_mcp.py`: new class `TestNotifyWorkspaceDir` (4 tests). The existing tests are unchanged.
- `tasks/50_epic_driver/state.md`: already modified before I started (50-08 `in_progress`). I did not touch it.
- No runner registration was needed: no new test module.

## Verification

- **Compile/import: PASS.**
  - `python3 -m py_compile` passes on the lib, `questions_mcp_lib.py` and both test files.
  - The CLI parses under Python 3.9 syntax: `ast.parse(..., feature_version=(3, 9))`.
  - Under **Python 3.9.21** (uv) the whole `test_unit_epic_driver` module passes: 443 tests OK at that point, with
    PyYAML and tomli on `PYTHONPATH`.
- **Tests:**
  - `python3 tests/run_all_tests.py --module unit_epic_driver`: **447 tests, OK.** That is 367 before, plus 80.
  - `--module unit_questions_mcp`: **56 OK.** That is 52 before, plus 4; the existing tests are unchanged.
  - `--module unit_python_compat`: 15 OK, 1 skipped (as before).
  - Full suite, foreground: `python3 tests/run_all_tests.py` gave **Ran 2992 tests, OK (skipped=2)**, exit 0. That is the
    baseline of 2908 plus 84 (80 + 4). The new tests replace nothing, so the count change is fully accounted for.
- **Mutation checks.** For each, the lib was backed up with `cp`, changed, the tests run, then restored; `cmp`
  showed it byte-identical afterwards. Each change was caught:
  - dropping the deadline gate failed 2 tests (`TestTickTiming`);
  - ignoring the ladder in `repeat_due_at` failed 2 tests (`TestAlarmEngine`);
  - dispatching with no tmux server failed 2 tests (`TestTmuxServer`);
  - dropping the mark suppression failed 1 test.
- **Real system state:** nothing in the tests calls real systemctl, systemd-run, systemd-inhibit, busctl, tmux, the
  relay or Telegram.
  - The tick, verb and CLI tests use `TickWorld`.
  - The `RealWorld` argv tests mock `subprocess.run`.
  - The questions-mcp tests patch `roles_config.load_bindings`, `lib._client` and the index paths.
  - The one real subprocess is a `python3 -c` that holds `flock` on a temp lock file.
  - During development my only system contact was read-only: `systemctl --user is-active/list-units`,
    `tmux display -p '#{pid}'`, `cat /proc/<pid>/cgroup`, `systemd-inhibit --list`. I enabled no unit, started no
    tmux server, moved no process and sent no notify.
- **Installed (`install.sh --yes` re-run):** not applicable. Installation is 50-10's job, and I did not run
  `install.sh`.

## CLI verbs and exit codes

Exit codes for every verb: **0** ok, **1** error, **2** usage (argparse; its output goes to the verb's stderr),
**3** refused.

| Verb | Lock | Exit codes |
|---|---|---|
| `validate <ledger>` | none | 0 valid (warnings to stdout); 1 invalid (`error: [category] where: message` lines on stderr) |
| `enable <ledger>` | the §18.1 gates; `tick.lock` from gate 8 on | 0; 3 with `refused: gate N (<name>): <reason>` and a `fix:` line; 1 on an unexpected error |
| `disable` | waits up to 300 s | 0 (also when not enabled); 1 if `systemctl --user disable --now` failed |
| `pause [--reason TEXT]`, `resume` | waits up to 300 s | 0; 3 with no active run |
| `status [--json]` | none (read-only) | 0; 1 if the state is unreadable |
| `tick [--dry-run]` | non-blocking | 0, including "a pass is running" and "no active run"; 1 on `tick_error` or `stamp_mismatch`. `--dry-run` takes no lock and writes nothing |
| `mark <step> done\|reset` | waits up to 300 s | 0; 3 for an unknown step or no active run; 2 for a missing or invalid action |
| `mark --grant <approval> [--question Q-n] [--note TEXT]` | waits up to 300 s | 0 (idempotent); 3 for an unknown approval; 2 if combined with a step or action |
| `queue <step> --to <role> --file <path>` | waits up to 300 s | 0; 3 for a non-worker step, a role that is not its implementer or reviewer, or no active run; 2 if `--to` or `--file` is missing |

If a writing verb cannot get the lock within 300 s, it exits 1 with "`<lock>` is held".

When the stamps disagree:
- `tick` takes the stdlib mismatch path and exits 1.
- `status`, `disable`, `pause` and `resume` still run; `status` shows `stamps MISMATCH: …`.
- Every other verb refuses with exit 3: `refused: stamp_mismatch: …`, fix: re-run `./install.sh --yes`.

## The alarm table (§16.2): raising test → clearing test

Class names are in `tests/test_unit_epic_driver.py`. "(50-0n)" marks an earlier task's test.

The engine maps a condition to an alarm one to one. `TestAlarmEngine` tests that mapping:
- raise: one `ack=True` notify, with the attempt committed before the notify;
- repeat: none while the ladder is live, then every 4 h;
- clear: no notify;
- failure: recorded in `notify.log`, `notify_failures` and `status`;
- a pass with no time left defers the notify;
- a pass killed between the attempt and its result never sends twice.

`TestAlarmCatalogue.test_the_catalogue_is_the_architectures` checks `ALARM_IDS` against the §16.2 table parsed
from architecture.md: 29 ids.

| Id | Raised by | Cleared by |
|---|---|---|
| `tick_error` | `TestTickCrash.test_an_exception_mid_pass_records_tick_error_and_the_next_pass_clears_it` | same (the next pass completes) |
| `ledger_invalid` | `TestAlarmCatalogue.test_run_rows_are_raised_and_cleared_by_real_passes` | same (it validates again) |
| `stamp_mismatch` | `TestStamps.test_a_mismatched_tick_records_notifies_once_and_exits_1_then_a_good_tick_clears_it` | same (a tick with agreeing stamps) |
| `tmux_server` | `TestTmuxServer.test_a_failed_start_raises_tmux_server_and_nothing_is_launched`; `…_for_a_server_in_a_terminal_scope_and_never_moves_it`; `test_a_started_server_outside_its_scope_raises_tmux_server` | the first two (the start works; the server is in its own scope) |
| `listener_down` | `TestInfrastructure.test_one_start_attempt_each_then_the_alarm` | same |
| `inhibitor_down` | `TestInfrastructure.test_one_start_attempt_each_then_the_alarm`; `test_the_inhibitor_lock_is_verified_in_systemd_inhibit_list` | both |
| `due_not_started` | `TestAlarmCatalogue.test_step_rows[due_not_started]`; (50-04) `TestDueSelection.test_due_not_started_after_two_ticks_unless_quiet` | `test_step_rows` (dispatched) |
| `step_timeout` | `TestAlarmCatalogue.test_step_rows[step_timeout]`; (50-04) `TestStepAlarmConditions.test_step_timeout_attempts_halt` | `test_step_rows` (a verdict) |
| `attempts_exhausted` | `TestAlarmCatalogue.test_step_rows[attempts_exhausted]` | same (`mark reset`); `TestVerbs.test_mark_done_and_reset` |
| `check_error` | `TestAlarmCatalogue.test_step_rows[check_error]` | same (a run returns 1) |
| `deadline_near` | `TestAlarmCatalogue.test_deadline_rows` | same (done before the deadline; also replaced once missed) |
| `deadline_missed` | `TestAlarmCatalogue.test_deadline_rows` | same (done); `TestAlarmEngine.test_mark_clears_a_still_true_condition_until_it_has_gone_once` (mark) |
| `halt` | `TestAlarmCatalogue.test_step_rows[halt]`; (50-05) `TestReconcileVerdicts.test_driver_halt_raises_halt_and_a_later_verdict_clears_it` | both (a later verdict) |
| `delivery_deferred` | `TestAlarmCatalogue.test_dispatch_rows`; (50-06) `TestDelivery.test_three_deferrals_raise_delivery_deferred_until_delivered` | both (delivered) |
| `delivery_unconfirmed` | `TestAlarmCatalogue.test_dispatch_rows`; (50-06) `TestDelivery.test_retry_once_when_provably_absent_then_no_third_send` | both (a late confirmation) |
| `verdict_missing` | (50-05) `TestReconcileVerdicts.test_merged_event_partly_covered_gets_one_reminder_then_verdict_missing` | same (a valid verdict) |
| `handoff_timeout` | `TestAlarmCatalogue.test_dispatch_rows`; (50-06) `TestRotation.test_no_handoff_after_two_ticks_falls_back_to_reuse` | `test_dispatch_rows` (a later rotation completes) |
| `session_stuck` | `TestReconcileAlarmsThroughTheTick.test_session_stuck` (real passes); (50-05) `TestStuck.test_stuck_after_no_growth_and_no_tool_shell` | `test_session_stuck` (the turn closes); (50-05) `TestStuck.test_suppressed_for_the_tick_of_a_clock_jump_and_cleared_when_the_turn_closes` |
| `permission_open` | (50-05) `TestOpenPrompts.test_permission_prompt` | (50-05) `TestOpenPrompts.test_closed_prompt_clears` |
| `ask_user_question_open` | (50-05) `TestOpenPrompts.test_ask_user_question_in_flight` | (50-05) `TestOpenPrompts.test_answered_ask_user_question_clears` |
| `usage_limit` | (50-05) `TestUsageLimits.test_limit_entry_raises_holds_dispatch_steps_the_ladder_and_clears` | same (the reset passed and a turn completed) |
| `spawn_failed` | `TestAlarmCatalogue.test_dispatch_rows`; (50-06) `TestEventsAndSpawn.test_a_failed_spawn_backs_off_doubling_to_the_cap_and_resets_on_success` | both (spawned) |
| `resume_failed` | `TestAlarmCatalogue.test_dispatch_rows`; (50-06) `TestResume.test_a_failed_resume_is_resume_failed_and_tried_again_with_the_same_event` | both (resumed) |
| `model_mismatch` | (50-05) `TestModelCheck.test_mismatch_raises_and_holds_until_rotated` | same (rotated) |
| `compaction` | (50-05) `TestCompaction.test_after_the_spawn_raises_once_before_is_ignored` | same (the rotation completes) |
| `startup_dialog` | (50-05) `TestStartupDialogs.test_unlisted_dialog_raises_and_is_never_answered`, `test_listed_dialog_answered_once_then_the_alarm` | (50-05) `test_listed_dialog_answered_once_then_the_alarm` (the dialog is gone) |
| `foreign_handle` | (50-05) `TestForeignAndOrphans.test_foreign_handle_raises_and_clears_when_gone` | same |
| `orphan_session` | (50-05) `TestForeignAndOrphans.test_orphan_session_by_the_grammar` | same |
| `worktree_refused` | `TestAlarmCatalogue.test_worktree_refused`; (50-07) `TestStepWorktrees` (the refusal tests) | `test_worktree_refused` (the next ensure succeeds) |

Each `TestAlarmCatalogue` row goes through the real producer (`step_alarm_conditions`, `dispatch_conditions`,
`worktree_conditions`, or a real tick for the run rows) and through `run_alarms`. It asserts the key in `raised`,
and then `cleared_at` set, either by its condition or by mark.

## The stamps manifest (OQ-3) and what 50-10 must install

**Format.** `~/.claude/epic-driver/stamps.json`:

```json
{
  "revision": "<git describe --always --dirty of the repo>",
  "installed_at": "2026-11-23T08:00:00Z",
  "modules": {
    "<name>": {"path": "<absolute installed path>", "sha256": "<lowercase hex sha256 of the installed file>"}
  }
}
```

**The check.** It is in the CLI and uses only the standard library.
- The manifest must contain every name in `STAMPED_FILES`, each at exactly the listed path under `~/.claude` (the
  paths are compared after `realpath`).
- Every entry in `modules` is hashed, including extra entries.
- A missing manifest, a name that is not listed, a different path, a missing file or a hash that differs is
  `stamp_mismatch`.
- `revision` is shown by `status`.
- Only the exact value `EPIC_DRIVER_ALLOW_UNSTAMPED=1` skips the check, and only tests set it.

**Names and installed paths 50-10 must write** (relative to `~/.claude/`):

| Name | Installed path | Copied by |
|---|---|---|
| `epic-driver` | `bin/epic-driver` (mode 0755; plus the symlink `~/.local/bin/epic-driver` → it) | epic-driver |
| `epic_driver_lib.py` | `hooks/epic_driver_lib.py` (new `REQUIRED_HOOKS` + `MODULE_OWNERS` entry) | epic-driver |
| `amux_spawn_lib.py`, `claude_event_reducer.py`, `keep_awake_hook.py` | `hooks/…` | amux, keep-awake (shared) |
| `questions_store.py`, `questions_listen_lib.py`, `questions_git.py`, `questions_ops.py`, `roles_config.py` | `hooks/…` | questions/telegram (shared) |
| `statusline.py` | `statusline/statusline.py` | statusline |
| `context_mcp_lib.py`, `models.json`, `questions_mcp_lib.py` | `epic-driver/lib/…` | epic-driver |
| `epic-driver-driver.md`, `epic-driver-worker.md` | `epic-driver/prompts/…` (50-09's prompts) | epic-driver |

Notes for 50-10:
- `questions_git.py` and `questions_ops.py` are in the set although §17's list omits them: `questions_mcp_lib`
  imports both.
- The CLI itself is in the set too.
- The CLI recognises the installed layout by the directory `~/.claude/epic-driver/lib/`. With it, `sys.path` is
  `~/.claude/hooks`, `~/.claude/statusline`, `~/.claude/epic-driver/lib`, and the CLI sets
  `CLAUDE_HOOKS_REPO=$HOME` in process before `questions_mcp_lib` is imported.
- The CLI also drops `CLAUDE_PROJECT_DIR` in process.
- The manifest must be written last in the run, after the shared modules are installed (the task file already says
  so).
- `relay_server` keeps coming through the telegram feature's user-site `.pth`; it is outside the stamp set.

**Units.** As in §22: `claude-epic-driver.service` with `Type=oneshot`, `KillMode=process`, `TimeoutStartSec=5min`,
`Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`, and **`ExecStart=%h/.local/bin/epic-driver tick`**.
Also the timer and `claude-epic-driver-inhibit.service` exactly as §22 shows. The inhibitor's
`--who=claude-epic-driver --what=sleep:idle --mode=block` is what `enable` and the tick look for in
`systemd-inhibit --list`. No unit sets `EPIC_DRIVER_ALLOW_UNSTAMPED`, and the installer enables none of the units.

## What 50-09 needs from the CLI

The driver session calls these three. Workers call none.

**`epic-driver mark --grant <approval-id> --question Q-<n> [--note "<text>"]`**
- Run it after the operator says yes; then end the turn with `ADVANCED: <approval-id>`.
- Exit 0 prints `epic-driver: <id> granted at <ISO> (question Q-<n>)`. It is idempotent: running it again returns 0
  and changes nothing.
- Exit 3 for an approval id that is not in the ledger: `refused: "unknown approval 'X'; the ledger defines: …"`.
- Exit 2 if it is combined with a step or action.
- It waits up to 300 s for `tick.lock`; a tick holds the lock for at most about 5 min.
- The ledger is never edited; the grant goes to `state.grants` only.

**`epic-driver queue <step> --to <role> --file <path>`**
- `<role>` is the step's implementer or reviewer role **key** from `roles:` (for example `implementer`), not
  "implementer" as a word.
- A relative `--file` is made absolute.
- Exit 0 prints `epic-driver: queued R-<n> for <role>:<step>: <path>`. The next tick delivers it as a `feedback`
  event under §11.2.
- Exit 3 for a step that is not a worker step, or a role that is neither of its two.
- Exit 2 if `--to` or `--file` is missing.

**`epic-driver status [--json]`**
- Read-only, no lock. The text is the §20 screen.
- `--json` top-level keys: `epic`, `run_id`, `ledger`, `enabled`, `timer{active,next}`, `inhibitor{active,lock}`,
  `paused`, `last_tick_at`, `last_tick_ago_s`, `last_tick_ok_at`, `last_tick_error` (null or `{at, error}`),
  `tick_seq`, `tmux{pid,scope,own_scope,started,error}`, `listener`, `stamps{checked,ok,problem,revision}`,
  `phase{name,done,total}`, `steps[{id,status,since,detail}]`, `sessions[{key,name,state,cache,ctx,cap,gen,model}]`,
  `outbox[{event,to,sent,confirmed,deferrals,verdict}]`, `questions[{id,for,status}]`,
  `grants[{approval,granted_at,question}]`, `alarms[{key,id,subject,detail,raised_at,last_sent_at,sends,last_error}]`,
  `notify_failures`, `ledger_problem`.
- With no active run it prints `epic-driver: off (no active run)`, or `{"enabled": false}` with `--json`.

The driver prompt should forbid `mark <step> done|reset`, `pause`, `resume`, `enable` and `disable`; those are the
operator's. That is my suggestion; §19 does not say it.

## Does an acknowledged message leave the listener index? How "ladder live" is implemented

**No.** `questions_listen_lib` never removes a `messages` entry, anywhere in the repo.
- For an ack-notification (`qid` None), `process_answer` calls `_finalize` (it edits the Telegram message) and
  `_advance_only` (it moves the watermark), then returns `"acked"`. The entry stays in the index.
- `add_message_entry` and `claude-questions --reindex` only ever add entries.

So, as §16.1 provides for this case, **the span alone decides in practice**. Index membership is still checked, as
written: a message that is not in the index (its index write failed, or the index was reset) never counts as a live
ladder. An unreadable index (`notify_index_ids()` returns None) leaves the span alone to decide.

How it is built:
- **Span.** `nudge_span_s(<the workspace's [questions] nudge>)` adds up the rungs. Each rung counts from the one
  before, as in the relay reaper. A repeating last rung (`7d*`) is counted once. The default `4h,1d,3d,7d*` gives
  11 d 4 h.
- **Recording.** A dispatched `ack=True` notify records `message_id` and `ladder_until = sent_at + span`.
- **`ladder_live(rec, ids, now)`** = the message id is in the index (or the index is unreadable) and
  `now < ladder_until`.
- **The next repeat** is 4 h after the later of the last attempt and a live ladder's `ladder_until`. That gives
  "none while the ladder is live, then every 4 h", and the first repeat does not coincide with the ladder's last
  nudge.
- Every repeat is a new `ack=True` notify, which starts its own ladder.
- **Wall time.** Rungs are measured in wall time; the relay counts them in the chat's active-window time. The
  driver's estimate of a ladder's end is therefore at or before the relay's, which errs toward one notify too many,
  never a missed one.

## Decisions

1. **The time bound** (Done when: one pass within `TimeoutStartSec=5min`). All values are *defaults* and all are
   measured on `World.now`:
   - **Deadline.** `TICK_DEADLINE_S = 270` from the pass start.
   - **Check budget.** It is `min(150, 270 − TICK_DISPATCH_RESERVE_S(90) − elapsed)`. Slow infrastructure eats into
     the checks, never into the dispatch. When the budget is 0, no check starts.
   - **Effect gate.** `execute_dispatch(…, gate=TickClock.gate)` starts an effect only when
     `elapsed + its longest run (EFFECT_MAX_S) ≤ 270`.
   - **What happens when an effect does not fit.**
     - A spawn or resume is committed and not run. That is exactly the 50-06 crash matrix's "killed after the
       commit" case: the next pass launches it again with the same event, and no backoff applies, because no error
       was recorded.
     - Any other op (send, retry, reminder, key, ping, reap) is skipped whole, its mutation included, so nothing
       records an attempt that never happened.
   - **Notify gate.** A notify starts only when `elapsed + NOTIFY_TIMEOUT_S(45) ≤ TICK_HARD_LIMIT_S(290)`. A raise
     that does not fit is recorded and sent next pass.
   - **Spawn and resume timeouts.** I lowered `SPAWN_TIMEOUT_S` from 180 to 60 and `RESUME_TIMEOUT_S` from 120 to
     60. `amux-spawn spawn --detach` does not wait on the session, and a resume waits at most 30 s for growth. With
     180 s, a saturated budget (about 155 s) plus one spawn could not fit, so the spawn would starve every pass.
   - **How it is demonstrated.** In `TestTickTiming` every fake call advances the fake clock by its worst case.
     - Realistic infrastructure (1 s calls): 8 watch checks of 60 s run 2 (the third would end past 150 s), then one
       spawn and one alarm notify. **131 s** in total.
     - Every call at its full timeout (systemctl 30 s each, inhibit list 10 s, tmux read 5 s, spawn 60 s, notify
       45 s, checks 60 s): **255 s**. One check ran, the spawn ran, and the alarm notify was deferred to the next
       pass.
     - The other timing tests cover: a late pass leaves its spawn committed and launches it next pass; a skipped send
       records nothing; with fast infrastructure the checks get the full 150 s.
   - **Residual risk.** Not bounded: the pre-check phase's git calls (30 s timeouts; worktree creation and base-ref
     verification) and a first check whose own timeout is over about 150 s (50-04's "the first job always runs").
     The adaptive budget absorbs the first, and the validator could warn about the second. I did not change 50-04's
     rule.
2. **The `stamp_mismatch` tick is handled by the CLI**, using only the standard library. It reads `active.json` and
   `state.json`, takes the lock without blocking, records `last_tick_at`, `last_tick_error` and the alarm record
   (committed before the notify), and notifies **once per episode**. It imports `questions_mcp_lib` best-effort for
   that. Later mismatched ticks only record. I did not implement 4-hourly repeats here: the code that would check
   the ladder is the code that disagrees, and the ladder itself reminds the operator. The first tick whose stamps
   agree clears the alarm through the engine.
3. **`active.json`** carries `workspace`, `epic` and `notify_role` besides §4's keys, so that the stdlib mismatch
   path and the crash path can notify without the lib or a valid ledger. `disable` removes `active.json` (no active
   run, so another ledger can be enabled) and keeps the state dir. A refusal at gate 10 rolls back the units and
   removes the `active.json` it wrote. A failed re-enable therefore leaves the driver off.
4. **The tmux move** (§21.1). If `epic-driver-tmux.scope` is not active, `StartTransientUnit` creates it with
   `PIDs=[pid]`: a scope cannot exist empty, so creating it is the move. Otherwise `AttachProcessesToUnit` adds the
   pid. Either way the result is verified in `/proc/<pid>/cgroup`, up to 5 tries 0.5 s apart. The refusal text
   gives both remedies and the exact `busctl` command.
5. **No server, and the start failed** ⇒ that pass runs `select` and `plan_dispatch` as if paused. No amux or
   amux-spawn effect runs, so none can start a server inside `claude-epic-driver.service`'s cgroup. A server in the
   wrong scope is still a server, so dispatch goes on, under the `tmux_server` alarm. While paused the tick starts
   no tmux server, and no-server is not an alarm, because nothing would be spawned. The listener and inhibitor start
   attempts still happen while paused: they are watchers, not work. Each inactive unit gets one start attempt **per
   pass**.
6. **Mark versus a condition that is still true.** This is 50-04's note. `mark` sets `cleared_by: "mark"` and
   `suppressed` on the step's alarms. The engine does not raise a suppressed alarm again until its condition has
   gone once. Example: a deadline long past, then `mark reset`, then the next occurrence misses its deadline: that
   raises again.
7. **`alarm_cleared` items** (§9.1). These come from alarms whose subject is a driver step in
   `inflight/verifying/waiting/asked/halted`. They are excluded for `halt` (it clears on the driver's own verdict)
   and for alarms that `mark` cleared. They are queued in `state.alarm_cleared_pending` at step 8 and go into the
   next pass's dispatch as items. A crash between that event's commit and the end of the pass can deliver one twice,
   which is harmless.
8. **A crash** is recorded on the **last committed** state, never on the half-applied in-memory pass. That state is
   consistent by invariant 5. `last_tick_error` is `{"at", "error"}`; §8 left its type open. An invalid ledger with
   no valid copy is a `tick_error`.
9. **Notify commits.** The attempt (`last_attempt_at`) is committed before the notify and the result after it. A pass
   killed in between sends nothing more until the repeat time. A failed notify is retried at the next repeat time,
   never in a loop. Alarm records carry `detail`, `last_attempt_at`, `cleared_by`, `suppressed` and `last_error`
   beyond §8's example.
10. **Order within the pass**, following §23: a watch step that becomes `watching` at step 5 gets its first check in
    the next pass. After the checks, `advance_schedule`, `select` and the plan use the time read then, not the
    pass's start time.
11. **`workspace_dir` is honoured only without `CLAUDE_PROJECT_DIR`.** `roles_config.find_roles_file` prefers
    `CLAUDE_PROJECT_DIR`, and I was allowed to add only the parameter. So the CLI always drops `CLAUDE_PROJECT_DIR`
    in process (the driver's workspace is always the ledger's), the same way `claude-questions --workspace-dir`
    does. The questions-mcp test of the honoured path runs without it, as the CLI does.
12. **Phase heartbeats.** A phase is announced when every step carrying that `phase` is done (a recurring step counts
    once it has a done occurrence). The text is `phase E1 done; next: S0 (3 steps)` (next = the next phase in ledger
    order that is not done), or `phase P2 done; every phase is done`. Nothing else is sent: a 144-tick quiet day
    sends nothing.
13. **`status`** reads the timer (`systemctl show`), the inhibitor, the tmux server and the listener live, read-only.
    Session rows come from `take_readings`, best effort. If no ledger loads, the screen says so and still shows the
    state.
14. **`--dry-run`** takes no lock, starts and sends nothing, runs no check (it lists the eligible ones), creates no
    worktree, makes no notify and writes nothing. It plans as if a missing tmux server had been started.
15. **`is_secret_key`** is reproduced in the lib (amux-spawn's version is in its CLI script, which the lib cannot
    import). A test pins it to amux-spawn's function on 13 keys.
16. **`execute_dispatch`'s `gate=`** is the one change to 50-06 code. It is keyword-only and does nothing when it is
    not given. All 50-06 tests are unchanged and pass.
17. **Enable notifies twice**, as §18.1 says: gate 6's test notify (`ack=False`) and step 11's "epic-driver on for
    <epic>". A failure at step 11 is recorded and does not refuse.
18. **The contract test now pins where each new effect may appear:**
    - `new-session` appears only in `tmux_start_args`.
    - `World.systemd_run` is called only from `ensure_tmux_server`.
    - The systemd binaries and `questions_mcp_lib` are named only in `RealWorld`.
    - The `.notify(` calls are exactly `qml.notify(`, `self.world.notify(` (in the Notifier) and `world.notify(`
      (enable's test notify).
    - `kill-server` appears only as advice text.
    - The tmux read verbs are `{display-message, capture-pane, list-sessions}`.

## Blockers

None.

## Questions for User

None.
