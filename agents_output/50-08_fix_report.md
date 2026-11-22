# 50-08 fix report

Fixer for the review `agents_output/50-08_review_report.md` (PASS, 2 MEDIUM, 5 LOW). Nothing committed, no branch or
checkout operation, no real unit/tmux/notify touched. Files changed: `.claude/hooks/epic_driver_lib.py`,
`.claude/bin/epic-driver`, `tests/test_unit_epic_driver.py`. (`questions_mcp_lib.py` was not touched by this fix.)

## 1. MEDIUM: the timing bound

**Deadline value.** `TICK_DEADLINE_S = 270` stays: 30 s under the unit's 300 s `TimeoutStartSec`. It is the latest any
call but a notify may end. The last 30 s are the notify (45 s, started only when it can end by `TICK_HARD_LIMIT_S` = 290)
plus the final commits and log writes. Every call now starts only when `elapsed + its worst case <=` its phase end, and
the phase ends are all at or before the deadline:

| phase | ends by (s from pass start) | how |
|---|---|---|
| validator git (ledger load) | 30 (`LEDGER_END_S`) | `BoundedWorld` clamps `git` timeouts; on expiry the ledger is re-read with git assumed (structure still validated), no `ledger_invalid` |
| infrastructure (tmux probe, systemd-run, is-active/start, inhibit list) | 100 (`INFRA_END_S`) | `check_infrastructure(fits=)`; a call that does not fit is not made: unknown (`None`), no alarm, next pass |
| readings | 150 (`READINGS_END_S`) | `take_readings(fits=)`; per session 15 s (`SESSION_READ_WORST_S`), `amux ls` 10 s; if any session is unread, `Readings.incomplete`: no reconcile, no checks, no dispatch, no alarm cleared this pass |
| sha checks | 190 (`SHA_END_S`) | `run_sha_checks(fits=)`, 30 s per repo; a step that does not fit is left unchecked (not an error) |
| check budget | 180 | `clock.check_budget()` is now computed AFTER `run_sha_checks`; each job's fit test includes the 10 s kill grace (`CHECK_OVERHEAD_S`) |
| first-check exception | only while elapsed <= 90 s (`FIRST_CHECK_UNTIL_S` = deadline/3), and only if the job can end within the deadline; a job that can never fit (`too_long`) is skipped and noted, later jobs still run | `run_checks(first_exempt=, overhead_s=, pass_left_s=)` |
| worktree preparation | 200 (`PREPARE_END_S`) | `prepare_worker_steps(remaining=)` with `BoundedWorld`; a step that ran out of time gets its record put back (no `worktree_refused`) and it and later steps wait |
| dispatch effects | 270 | unchanged `EFFECT_MAX_S` gate |
| notifies | 290 | unchanged gate, now backed by a real 45 s thread join (item 7) |

**Computed worst case.** Theoretical: 290 s (the last gated notify), plus local commits. With every fake `World` call at
its timeout, measured on the fake clock: 273 s (7 sessions, systemctl/systemd-run/tmux all hung, 3 notifies), 253 s
(3 workers + driver resuming after a reboot, 2 of 4 resumes fit, the rest deferred), 169 s (3 sha checks at 30 s each),
all under 300. A normal pass (calls at 0.2 s) does everything in 1.6 s: 4 resumes, 4 alarm notifies.
Deferred work is committed consistently and finished by the next pass (tested: the next fast pass completes all 4
resumes and sends all 4 alarms once each; deferred sha check runs next pass; no `worktree_refused`/`check_error`).

Whatever a deferral costs: `Readings.incomplete` or a deferred infrastructure call makes `run_alarms(clear=False)` for
that pass (the missing conditions are unknown, so nothing is cleared and re-raised later).

**Tests (`TestTickTiming`, +6).** `everything_at_its_timeout()` wraps EVERY World call the tick can make (systemctl,
systemd-run, inhibit-list, tmux pid/has-session/display/capture, amux ls, git at its `timeout_s`, spawn, resume, reap,
send, keys, notify, and a nominal 1 s for the local reads) so it advances the fake clock by its worst case, and records
each call's end time. Tests:
- reboot with 7 sessions, all infrastructure hung, several alarms: ends < 300 and <= 290; the infrastructure phase ended
  <= 100, the readings <= 150;
- reboot with 4 resumes at 60 s: the ones that fit run, the rest wait, next pass completes (no duplicate alarms);
- normal pass: all work done, nothing deferred;
- sha checks charged before the check budget (2 of 3 fit, no check fits, third sha deferred then done);
- first-check exemption only while young (`run_checks` unit, and through the tick at 100 s elapsed);
- worktree preparation: `prepare_worker_steps` unit (30/30/30/10 s git, no refused record, resumes next pass) and
  through the tick (3 steps x 4 git calls stop at 200 s, nothing refused).
The old `test_every_call_at_its_timeout_still_ends_within_5_minutes` (charged only some calls; its 215 s alarm assertion
described the old ungated behaviour) is replaced by these.

Open question on a limit I could not remove: a check whose own timeout (+10 s) exceeds what a pass can ever give
(over ~260 s) is never started (it is skipped with a tick-log note). The ledger does not cap check timeouts. See Questions.

## 2. MEDIUM: `EPIC_DRIVER_ALLOW_UNSTAMPED`

- **Repo run vs installed run (50-10 needs this).** `layout()["installed"]` in `.claude/bin/epic-driver` is true when the
  resolved script directory is `<home>/.claude/bin` (the installer's bin dir, `$HOME` from the environment, else
  `Path.home()`), OR `<root>/epic-driver/lib/` exists (which the installer creates and a checkout never has). Otherwise it
  is a repo run. Installed copies are at `~/.claude/bin/epic-driver` (and the `~/.local/bin` symlink resolves to it), so
  50-10 only has to install into those places; no new marker is needed.
- `check_stamps`: the override is honoured only when `not lay["installed"]`. An installed copy ignores it and checks
  (tested, including a changed file with the variable set, and through `main` with a patched installed layout).
- `enable` gate 2 refuses with its reason when the variable is set (`stamps["override_env"]` from the CLI, or the
  variable in the environ passed to the lib); tested through the CLI and through `edl.cli` directly.
- Stripped from every child: `main` pops it (and `CLAUDE_HOOKS_REPO`) from the process environment after the stamp check;
  `child_env()` removes both from the explicit environment of every `RealWorld` subprocess (spawn, resume, reap, send,
  keys, systemctl, inhibit-list, busctl, tmux reads, git, systemd-run) and from `tmux_start_env`; `check_env` never copied
  the environment (fixed set). Tested: a tick's check env and the systemd-run env, and each `RealWorld` call's `env=`.

## 3. LOW: `CLAUDE_HOOKS_REPO`

`questions_mcp_lib` reads it only at import (`HOOKS_DIR`). It is no longer set in `main`. `RealWorld(hooks_repo=...)`
(the CLI passes `layout()["hooks_repo"]`) sets it around the import AND the call inside `RealWorld.notify` and restores
the previous value (or removes it); `tick_stamp_mismatch` does the same around its own import. It is excluded from every
child environment (item 2). Tests: set/restored (also a prior value restored, and no `hooks_repo` leaves it alone),
absent from every child env, absent after `main`. The updated test
`test_the_cli_drops_claude_hooks_repo_and_claude_project_dir` replaces the old "sets it" test.

## 4. LOW: dry-run and `stamp_mismatch`

(a) `TestDryRun` snapshots (path, size, mtime_ns, sha256) of every file under the isolated state root plus the epic
folder's pointer file before and after, with a 50 ms gap so an mtime touch shows; the ledger is edited first so the last
valid copy differs (otherwise M5 as stated, a rewrite of identical content, is not even reachable: `load_run_ledger`
writes only when the copy differs). Code: a dry run writes nothing (`keep_copy=not dry_run`, no lock, no tick log); the
test now bites (mutation M4a below) and a companion test proves the snapshot detects a forced `keep_copy=True`.
(b) `tick_stamp_mismatch` repeats like any alarm: 4 h after the later of `last_attempt_at` and the record's
`ladder_until` (the listener index is stamped code and is not read in this stdlib path); tests: raise, no repeat at 3 h,
repeat at 4.1 h, again at 8.2 h; a live ladder holds it to ladder end + 4 h. If `questions_mcp_lib.py` is among the
mismatched files (or the manifest cannot say, `files` is None) it is NOT imported: the attempt goes to `logs/notify.log`
and `state.notify_failures` (shown by `status`), tested with a poison module for three cases. `check_stamps` now reports
every mismatched file name (`files`) instead of stopping at the first.

## 5. LOW: repeat timing

No change, per the manager (§16.1 is now explicit).

## 6. LOW: `enable` rollback

Gates 8 to 11 moved into `_enable_write_and_start`; `cmd_enable` records whether the pointer file existed and, on any
`Refused` from there, removes it if THIS run created it. State-dir files (state.json, snapshot, last-valid copy) stay
(documented in the code). A pointer that was already there is not removed (tested). Gate 8 (snapshot write failure) is
now a refusal (it was an uncaught exception, exit 1). New tests: gate 8, gate 9 (unreadable state), gate 10 x2 removing
the pointer, gate 10 leaving a pre-existing pointer, and `gate_fails` now also asserts the pointer is absent for every
gate in the existing per-gate test. Every gate now has a failing-gate test; none has no-timer-enabled gaps.

## 7. LOW: notify wall-clock

`Notifier._call` runs `World.notify` in a daemon thread joined with `NOTIFY_TIMEOUT_S`; on timeout the result is a
failure (`notify timed out after Ns (it may still complete)`) recorded in `notify.log`, `state.notify_failures`, the
alarm's `last_error` and `status`, and the pass goes on. Code comment notes a late completion can still send (at worst
one duplicate). An exception from the world's notify still propagates (as before; `RealWorld.notify` never raises).
Tests (`TestNotifyTimeout`): a fake notify that blocks on an event with `NOTIFY_TIMEOUT_S` patched to 0.2 s: pass
finishes quickly, rc 0, failure recorded in log/state/status; releasing it later does not break anything; plus the
in-time case unchanged.

## Mutation results

Each: `cp` backup to `/tmp/mut_bk`, one edit, `--module unit_epic_driver`, restore from the copy, `diff` clean.
The lib md5 after all of them equals the md5 before (`a811e083d6f4c3023cdc625ffedf679d`).

| # | mutation | result |
|---|---|---|
| 1 | infrastructure not gated (`fits=None`) | caught: reboot timing test (phase end 100) |
| 1 | readings not gated | caught: same test (phase end 150) |
| 1 | check budget computed before the sha checks | caught: sha/budget test |
| 1 | sha checks not gated | caught: sha/budget test |
| 1 | first check always exempt | caught: 2 tests (young/late) |
| 1 | worktree preparation not bounded (`remaining=None`) | first run SURVIVED (only the unit called it with a deadline); added a through-the-tick test, now caught |
| 2 | installed copy honours the override | caught: 2 tests |
| 2 | enable gate 2 ignores the variable | caught: 2 subtests |
| 2 | tmux env keeps the override | caught: 1 test |
| 2 | `child_env` keeps the override | caught: 12 (every RealWorld child + tmux env) |
| 4a | dry-run keeps the last valid copy (`keep_copy=True`) | caught: `TestDryRun.test_computes_and_prints_and_writes_nothing` |
| 6 | no pointer rollback | caught: 4 tests |

## Counts and checks

- `python3 -m py_compile` on `.claude/bin/epic-driver`, `.claude/hooks/epic_driver_lib.py`,
  `questions-mcp/questions_mcp_lib.py`, `tests/test_unit_epic_driver.py`, `tests/test_unit_questions_mcp.py`: OK.
- `python3 tests/run_all_tests.py --module unit_epic_driver`: `Ran 473 tests` / `OK` (447 before: +26).
- `--module unit_questions_mcp`: `Ran 56 tests` / `OK`.
- Full suite `python3 tests/run_all_tests.py` (foreground): `Ran 3018 tests in 440.025s` / `OK (skipped=2)`, exit code 0
  (baseline 2992 + 26). After that run I only changed one test's setup (it had called `setUp` three times and left 3
  `/tmp/epic-driver-test-*` directories per run); the module was re-run after: 473 OK, and the directory count no longer grows.

## `git status --short`

```
 M .claude/hooks/epic_driver_lib.py
 M questions-mcp/questions_mcp_lib.py
 M tasks/50_epic_driver/architecture.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
 M tests/test_unit_questions_mcp.py
?? .claude/bin/epic-driver
```

## Questions for User

- A check with `timeout` over about 260 s can never end inside one pass; it is now skipped with a tick-log note rather than
  run past the deadline. Should the ledger validator cap check/trigger timeouts (for example at 240 s) so this is refused
  at `validate`/`enable` instead? Left for the manager; not done here.
