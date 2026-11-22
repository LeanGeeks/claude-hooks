# Review Report - 50-08 Tick, CLI, alarms, notify, tmux server

## Verdict: PASS

No BLOCKER or HIGH issue. 2 MEDIUM, 5 LOW (below). The change is uncommitted on top of 4b24190.

Baseline run before any mutation: `python3 tests/run_all_tests.py` in the foreground gave
`Ran 2992 tests in 436.851s` / `OK (skipped=2)`. This matches the implementer's number. Compile and import: `epic_driver_lib`,
`questions_mcp_lib` and the CLI load fine under the suite.

## Completeness Check

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 1 | CLI: argparse for every §18 verb, stamp check before any non-stdlib import, exit codes 0/1/2/3, lock handling | met | `.claude/bin/epic-driver` `build_parser`, `main` (the only imports before `check_stamps` are stdlib; `epic_driver_lib` is imported after it); `cli()` lib:8900 |
| 2 | Tick pass in §23 order, `(mutation, effect)` with a commit between each | met | `tick_pass` lib:8173; `execute_dispatch` does mutate, COMMIT, effect, on_result, COMMIT (lib:6985); `run_alarms` and `run_heartbeats` commit before and after each notify |
| 2a | `last_tick_at`, `last_tick_ok_at`, `last_tick_error`; exception gives `tick_error`, commit, exit 1; the next good pass clears it | met | lib:8265 (set at pass end), `record_tick_error` lib:8293 (on the last committed state), `run_tick` lib:8324; `TestTickCrash` |
| 2b | `--dry-run` writes nothing | met | `commit` is a no-op, `keep_copy=not dry_run`, no lock, no tick log; mutation M4 below is caught. See LOW 3 for a gap |
| 2c | `SSH_AUTH_SOCK` unset when the socket is gone | met | `fix_ssh_auth_sock` lib:7499 (also in `tmux_start_env`) |
| 2d | The tick never runs an action (invariant 1) | met | only `run_checks` (checks and triggers) and the dispatch effects run; no step action is executed |
| 3 | tmux: detect, own scope, cgroup verify, `AttachProcessesToUnit` in `enable`, `tmux_server` alarm, no amux call can start a server in the service cgroup | met | lib:7524-7640. `check_infrastructure` runs before `take_readings`. With no server and a failed start, `dispatch_paused` is forced True. `amux ls` is read-only (I read `cmd_ls` in /usr/local/bin/amux). The only `new-session` is in `tmux_start_args`, called only through `ensure_tmux_server` |
| 4 | Env snapshot written by `enable`, used for the server and for checks | met | `env_snapshot`, `write_env_snapshot`; snapshot goes to `tmux_start_env` and `run_checks` |
| 5 | Infrastructure checks: one start attempt each, then the alarm; the inhibitor is also verified in `systemd-inhibit --list` | met | `ensure_unit`, `inhibitor_lock_held`; I checked the parser against the real `systemd-inhibit --list` column layout (WHO first, MODE last) |
| 6 | Alarm engine: raise, repeat, clear, `alarm_cleared`, `ack=True`, `due_not_started` | met | `run_alarms` lib:7854; the ladder rule is in `repeat_due_at` |
| 7 | Notify path: keyword-only `workspace_dir`, failures to `notify.log`, `notify_failures` and `status` | met | questions_mcp_lib diff (13 lines): `None` goes through the unchanged `_resolve_workspace_dir()`; `Notifier.send`; `render_status`. The questions-mcp tests are unchanged and pass |
| 8 | Heartbeats: enable, disable and phase transitions only; no daily line | met | `run_heartbeats`, `cmd_enable` step 11, `cmd_disable`; no daily path exists |
| 9 | Verbs and the §18.1 gate order | met | `cmd_enable` lib:8660: gates 1-11 in the exact order; `sync = "push"` refused; units rolled back on failure of gate 10 |
| 10 | All system calls go through `World` | met | every `subprocess.` in the lib is inside `RealWorld` (lib:657-864); the CLI has none. One exception, see LOW 4 |
| 11 | Tests | met, with gaps | `TestTickFullPass`, `TestTickLock`, `TestTickCrash`, `TestTickTiming`, `TestDryRun`, `TestStamps`, `TestEnable`, `TestVerbs`, `TestStatus`, `TestTmuxServer`, `TestInfrastructure`, `TestAlarmEngine`, `TestAlarmCatalogue`, `TestHeartbeats`; the 80 + 4 counts add up to the suite's 2992 |

### Item-by-item notes

**1. Tick pass.** The order follows §23. After the checks the clock is re-read for `advance_schedule` and `select`.
The crash path writes to the last committed state, so a half-applied pass is never recorded. `tick_error` is cleared by the
next `run_alarms` (`clear=True`) and is not repeated more than every 4 h. The dry run takes no lock, starts no unit or
server, runs no check, creates no worktree and sends nothing.

**2. Timing bound (recomputed from the code).** Budgets: `TICK_DEADLINE_S` 270, `TICK_DISPATCH_RESERVE_S` 90,
`CHECK_BUDGET_S` 150, `TICK_HARD_LIMIT_S` 290, `NOTIFY_TIMEOUT_S` 45. Gated phases, bounded:
- Effects: `execute_dispatch(..., gate=clock.gate)` starts an effect only when `elapsed + EFFECT_MAX_S[kind] <= 270`.
  A spawn is 60 s, a resume 60 s (the 30 s growth wait fits inside it), a reap 60 s, a send 40 s. Several resumes after a reboot cannot
  stack past 270 s plus one effect's own bound: the later ones are committed and run next pass.
- Notifies: `notify_ok()` is `elapsed + 45 <= 290`, so a notify per alarm cannot push the pass past 290 s if
  the 45 s holds.
- Checks: a job starts only when `elapsed + timeout <= budget`.

Not bounded, so a pass can still exceed 300 s (see MEDIUM 1):
- `check_infrastructure`: with every call at its timeout this alone is
  5 (tmux probe) + 30 (`systemd-run`) + 5 + 2 x (30 is-active + 30 start + 30 is-active) + 10 (inhibit list) = 230 s.
- `take_readings` is ungated: about 3 tmux reads x 5 s per session, plus `amux ls` 10 s.
- `run_sha_checks` (git, 30 s each) runs after the check budget was computed (lib:8225 versus 8229).
- `prepare_worker_steps` (git worktree and base-ref calls, 30 s each) runs before the dispatch gate.
- `run_checks` always runs the first job, whatever its timeout, and adds up to 5 + 5 s of kill grace.
- `NOTIFY_TIMEOUT_S` is an assumption about the relay client, not an enforced wall-clock limit.

The implementer's "255 s with every call at its timeout" holds only for a fake world that charges time for systemctl,
inhibit, tmux pid, systemd-run, spawn, notify and checks. It charges nothing for readings, sha checks or worktree git.
The failure mode if a pass overruns is benign: systemd kills it at 300 s, flock dies with the process, and invariant 5
means the next pass resumes from a consistent state.

**3. Lock.** `acquire_lock` uses `flock(LOCK_EX|LOCK_NB)` with a poll loop. The tick uses wait 0 and exits 0 quietly with a
log line; a lock left by a dead process is free (tested with a real subprocess that is killed); the other writers use
`LOCK_WAIT_S = 300`. The lock fd is not inherited by children (Python's default non-inheritable fds).

**4. Stamps.** `check_stamps` runs before the lib import and uses the stdlib only. The manifest must list every name in
`STAMPED_FILES` at the exact path under `~/.claude`; every entry is hashed. A mismatch makes a tick record `stamp_mismatch`
and exit 1 (nothing else touched); `status`, `disable`, `pause` and `resume` still run; other verbs refuse with exit 3.
`EPIC_DRIVER_ALLOW_UNSTAMPED` is read from the process environment only and requires exactly `"1"`. Leak path (LOW 2): nothing in the code
stops it reaching production if it is exported in a shell or imported into the user manager's environment.
`status` does print `stamps unchecked (EPIC_DRIVER_ALLOW_UNSTAMPED)`, which is the only guard.

**5. tmux.** Verified in the code (the real tmux and systemd were not touched):
- A server is started only via `systemd-run --user --scope --unit=epic-driver-tmux tmux new-session -d -s epic-driver-keepalive`,
  with the snapshot over the service environment, and verified through `/proc/<pid>/cgroup`.
- `tick` never moves a server (it only alarms); `enable` does it through `StartTransientUnit` when the scope is absent and
  `AttachProcessesToUnit` when present, verified in the cgroup, refusing with both remedies on failure.
- While paused, or with no server and a failed start, no amux or amux-spawn effect is planned.
- Reads that cannot start a server (`list-sessions`, `display-message`, `capture-pane`, `has-session`, `amux ls`) may run without a server.
- Not verifiable without starting a real server: whether `subprocess.run(capture_output=True)` returns promptly when the
  daemonised tmux server inherits the pipes. tmux redirects stdio when it daemonises, so I expect this to be fine, but it
  is worth one live check in 50-12.

**6. Alarm engine.** Raise: one `ack=True` notify, the attempt committed before the notify and the result after.
Repeat: `repeat_due_at = max(last_attempt, ladder_until if message in index) + 4h`. I confirmed the implementer's claim in
`questions_listen_lib`: an acknowledged `messages` entry is never removed (`process_answer` finalises and moves the watermark),
so the span alone decides in practice; membership is still checked. Clear: keeps `cleared_at`, no notify; `alarm_cleared` items
are queued for driver steps in flight or halted. Failures: `notify.log`, `state.notify_failures` (last 20), `status`; no
retry loop (the next repeat time retries). The alarm table in the report matches the tests I spot-checked:
- `inhibitor_down`: `test_the_inhibitor_lock_is_verified_in_systemd_inhibit_list` raises it and clears it. Good.
- `startup_dialog`: `test_listed_dialog_answered_once_then_the_alarm` raises (dialog still there after its listed key) and clears (dialog
  gone). The unlisted-dialog raise is in the sibling test. Good. These are 50-05 reconcile-level tests that stop at the condition; the engine maps
  conditions one-to-one and `TestAlarmCatalogue` checks the 29 ids against the §16.2 table.
- `tick_error`: raised and cleared in `TestTickCrash`.
- `foreign_handle` and `session_stuck`: raise and clear exist (50-05 and `TestReconcileAlarmsThroughTheTick`).
- `tmux_server`: three raising tests and clears.

**7. Notify change.** The diff is 13 lines; `workspace_dir` is keyword-only and `None` keeps the same call, so it is unchanged byte for byte. Tests:
the 52 existing questions-mcp tests are unchanged, plus 4 new. `CLAUDE_HOOKS_REPO` is set in `main()` before the lib import
(root.parent: `$HOME` when installed, the repo for the repo layout) and `CLAUDE_PROJECT_DIR` is dropped. Side effect: see LOW 1.

**8. Heartbeats.** Enable, disable and phase transitions only; `phases.announced` is committed before the notify. No daily line, no flag.

**9. Verbs.** `enable` gates verified in the code and by the `test_each_gate_failing_in_turn...` subtests (gates 1, 3, 4, 5 x3, 6, 7, 10 x2, plus 2). Gate 10
rollback is `systemctl disable --now` of both units plus removal of `active.json`. Re-enabling an already running ledger
and failing gate 10 also turns the running timer off, which matches "leaves no timer enabled". The pointer file is the only tracked-tree write and is
skipped when unchanged. Gates 8 and 9 failures (unreadable existing state) are covered only by reading the code.
`mark --grant` is idempotent and the only writer of `state.grants`; `queue` validates the role key.

**10. World.** All subprocesses sit in `RealWorld`. The contract test pins where each effect appears.

**11. No real system state touched** (read-only checks):
- `systemctl --user list-unit-files | grep -i epic`: nothing.
- `~/.local/state/epic-driver`: does not exist. `~/.claude/epic-driver`: does not exist.
- `epic-driver-tmux.scope`: could not be found.
- `find /data/sync/work -name epic-driver.run.json`: none.
- `/tmp/epic-driver-test-*`: 25 directories; the count did not change across two full suite runs, so the tests do not leak new ones (left over from earlier runs; I did not remove them).

**12. Mutation checks** (each: `cp` backup, edit, run `--module unit_epic_driver`, restore from the copy, `diff`/`md5sum` identical):

| Mutation | Result |
|---|---|
| Tick lock: `if fd is None:` changed to `if False:` in `run_tick` | caught: `TestTickLock.test_a_second_tick_while_one_holds_the_lock_exits_0_quietly` |
| Ladder: `repeat_due_at` ignores `ladder_until` | caught by 2 tests in `TestAlarmEngine` (`test_no_repeat_while_the_ladder_is_live_then_every_4h`, `test_a_long_ladder_holds_the_repeat_until_it_ends`) |
| `enable`: no `_rollback_units` when the inhibitor lock is absent | caught: gate-10 "no sleep:idle block lock" subtest |
| `--dry-run`: `commit` always saves | caught: `TestDryRun.test_computes_and_prints_and_writes_nothing` |
| `--dry-run`: `keep_copy=True` (writes `ledger.last_valid.yaml`) | NOT caught (LOW 3) |

After the mutations the lib is byte-identical to the original (md5 equal).

## Issues Found

### Issue 1: [severity: MEDIUM]

- **File:** `.claude/hooks/epic_driver_lib.py:8173-8260` (`tick_pass`), `TestTickTiming`
- **Problem:** the 5 min bound is guaranteed only for the gated phases (effects, notifies, checks). Infrastructure
  (up to 230 s when systemctl, systemd-run and tmux all hang), `take_readings` (about 15 s per session at timeouts),
  `run_sha_checks` and `prepare_worker_steps` (30 s git timeouts, before the dispatch gate) and the always-run first
  check are ungated. The check budget (lib:8225) is computed before `run_sha_checks` (lib:8229), so the sha time is
  not deducted from the 150 s the checks may then use. The report's "255 s with every call at its timeout" is therefore not the
  worst case; the fake world charges nothing for readings or git. Resumes (30 s growth wait) and notifies are correctly gated and cannot cause an overrun
  on their own. An overrun is fail-safe (systemd kills at 300 s, lock released, state consistent), so I rate it MEDIUM, not HIGH.
- **Fix:** compute the check budget after the sha checks (and after any ungated phase), make the first-check exception apply only when
  elapsed is small, gate `prepare_worker_steps` and the readings on the remaining time, and extend `TestTickTiming` so
  readings, git and the sha check also cost time on the fake clock.

### Issue 2: [severity: MEDIUM]

- **File:** `.claude/bin/epic-driver:check_stamps`, `lib: cmd_enable` gate 2
- **Problem:** `EPIC_DRIVER_ALLOW_UNSTAMPED=1` makes `check_stamps` return `ok: True, checked: False`, so `enable` passes
  gate 2 and a tick runs unstamped if the variable is in the environment. The unit does not set it, but nothing stops it reaching
  the timer's environment (`systemctl --user import-environment`, `environment.d`, or an operator export before `enable`), and it is not in the env snapshot
  allowlist but is inherited by the tmux server and spawned sessions via `tmux_start_env(environ, ...)`. Only `status` shows
  "stamps unchecked".
- **Fix:** refuse `enable` (gate 2) when the variable is set outside a test, or print a loud warning and raise a
  `stamp_mismatch`-class alarm from the tick when `checked` is false; strip the variable from the environment passed to checks, tmux and spawns.

### Issue 3: [severity: LOW]

- **File:** `.claude/bin/epic-driver:main`
- **Problem:** `CLAUDE_HOOKS_REPO=<root.parent>` is set in `os.environ` for the whole process, so it also reaches the checks (`base_env`),
  the tmux server and amux-spawn children (the amux allowlist is prefix-based, `CLAUDE_*`). `permissions_mcp_lib` and
  `permission-review-daily.sh` read this variable. For driven sessions it points at `$HOME` instead of the checkout; the installer's MCP registration sets its
  own value, so I expect no effect, but it is a new cross-feature side effect.
- **Fix:** set it only around the notify and import (or pass it only to `questions_mcp_lib`), or drop it from the environment passed to children.

### Issue 4: [severity: LOW]

- **File:** `tests/test_unit_epic_driver.py:6936` (`TestDryRun`); `.claude/bin/epic-driver:tick_stamp_mismatch`
- **Problem:** (a) the dry-run test compares only `state.json` bytes and the file names under the run dir, so a dry run that writes
  `ledger.last_valid.yaml` with identical content, or touches a file's mtime, is not caught (mutation M5 survived). It does not check the whole state root.
  (b) `tick_stamp_mismatch` imports `questions_mcp_lib` (a stamped, possibly mismatched file) to notify, and
  repeats the notify never (once per episode; §16.1 says every 4 h after the ladder). Both are documented by the implementer; (a) is a test gap, (b) a deviation.
- **Fix:** snapshot (path, size, mtime, content hash) of every file under the state root before and after; accept or document the once-per-episode choice.

### Issue 5: [severity: LOW]

- **File:** `lib:7819` (`repeat_due_at`)
- **Problem:** the first repeat comes 4 h after the ladder ends (`max(last_attempt, ladder_until) + 4h`), not when the ladder ends and then every 4 h. §16.1 ("none while live; after that every 4 h") can
  be read either way; the implementation and its tests are consistent with one reading and the report says so.
- **Fix:** none required; confirm the reading with the operator.

### Issue 6: [severity: LOW]

- **File:** `lib:8660-8770` (`cmd_enable`)
- **Problem:** the gate-10 rollback leaves `state.json`, the env snapshot, the last-valid ledger copy and the tracked pointer file in place (only the units and `active.json` are undone), and gates 8-9 have no failing-gate test. The tmux server stays moved even when a later gate refuses (harmless).
- **Fix:** add a test for a gate-9 failure; document that the pointer file remains after a rollback.

### Issue 7: [severity: LOW]

- **File:** `lib:7733` (`Notifier`)
- **Problem:** `NOTIFY_TIMEOUT_S = 45` is a gating estimate only; the in-process `questions_mcp_lib.notify` has no wall-clock limit, so a hung relay call could outlast it.
- **Fix:** run the notify in a thread with a join timeout, or confirm the relay client's hard timeouts bound it.

## Code Quality Notes

- The 50-08 section of the lib is long (about 1500 lines), but it is cleanly separated and the module docstring table was updated.
- The two changed contract tests are tightened, not loosened (they now pin where each new effect may appear).
- `execute_dispatch(gate=)` is the one change to 50-06 code; it is a no-op when omitted and the 50-06 tests pass unchanged.
- Lowering `SPAWN_TIMEOUT_S` 180 to 60 and `RESUME_TIMEOUT_S` 120 to 60 is a behaviour change to 50-06 defaults, justified in Decision 1; the `amux-spawn` calls in 50-12 should confirm 60 s is enough for a slow spawn.
- `stamp_mismatch` is recorded by stdlib code in the CLI; it duplicates a small part of the alarm record schema (`_new_alarm`), so the two must be kept in sync.

## Questions for User

None.
