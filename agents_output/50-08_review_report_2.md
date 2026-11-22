# Review Report - 50-08 tick, CLI, alarms (second pass)

## Verdict: PASS

No BLOCKER or HIGH issue. 3 MEDIUM (two are test gaps that mutation checks exposed, one is a stamp_mismatch hole), 2 LOW.

Baseline before any mutation: `python3 tests/run_all_tests.py` (foreground) gave `Ran 3018 tests in 436.333s` / `OK (skipped=2)`, rc 0.
That is the full suite with the fixer's late setUp change in place. The module run was 473 OK. py_compile and imports OK.

## Completeness Check

1. Pass deadline: met, by my own recompute.
   - Gated phase ends: ledger git 30 (`BoundedWorld`, epic_driver_lib.py ~8409), infra 100 (`fits_by(INFRA_END_S)`),
     readings 150, sha 190, check budget `min(150, 180 - elapsed)` computed after the sha checks (~8467), worktrees 200,
     effects `elapsed + EFFECT_MAX_S <= 270` (`TickClock.gate`), notifies `elapsed + 45 <= 290`.
   - Worst case is 290 s, the last gated notify, plus local commits. The notify thread join is 45 s, so it never exceeds that.
   - The first-check exception runs only while elapsed <= 90 s and only for a job whose timeout plus 10 s grace ends by 270.
     A job that can never fit goes to `too_long` and the later jobs still run.
   - Every call that can block starts only if its worst case fits its phase. This includes the post-start probe in
     `ensure_tmux_server` and the is-active after a unit start, which are covered by the gate on the whole sequence.
   - A skipped infra call returns `None`/`deferred`. There is no alarm (`l_ok is False` is required), and `run_alarms(clear=False)`
     for that pass. A partial reading sets `incomplete`, so there is no reconcile, no dispatch and no `undispatched_ticks` bump.
   - `TestTickTiming` charges every fake World call (`everything_at_its_timeout`) and records call end times.
   - Caveat: a first-exempt check that ends at about 270 s leaves no room for any notify (270 + 45 > 290). All alarm
     sends wait a pass. That is by design and consistent, not an error.
   - But see Issues 1 and 2: the "no false alarm / not dead" consequences are not pinned by tests.
2. Stamp override: met.
   - Honoured only when `not lay["installed"]` (bin/epic-driver `check_stamps`).
   - `installed` is true when `<root>/epic-driver/lib` exists OR the resolved script dir is `$HOME/.claude/bin`.
   - `resolve()` follows symlinks, so `~/.local/bin/epic-driver` and any relative-path or symlink invocation of the installed
     file land in the installed root. A changed `HOME` cannot hide the `lib/` marker. Only a physical copy of the script into
     another tree is a repo run, and it then imports that tree's own modules, so it is not an installed copy.
   - `enable` gate 2 refuses on the env var or on `stamps["override_env"]`. `main` pops the var, and the stamps result carries it.
   - Stripped from the check env (fixed set), from `tmux_start_env`, and from `child_env()` for all RealWorld children.
   - Mutations: see below, all caught.
3. `CLAUDE_HOOKS_REPO`: met.
   - `questions_mcp_lib` reads it only at import (`HOOKS_DIR = _resolve_hooks_dir()`, line 88).
   - The fix sets it around `import_module` and the call in `RealWorld.notify` and in `tick_stamp_mismatch`, and restores it.
   - It is popped from the process in `main` and stripped from every child env.
4. Dry-run: met. `TestDryRun` snapshots (size, mtime_ns, sha256) of the whole isolated state root plus the pointer, with a 50 ms gap.
   The M5 forced `keep_copy=True` companion test shows the snapshot detects a rewrite. `keep_copy=not dry_run`, no lock, no tick log.
5. `stamp_mismatch`: partially met. The mismatched `questions_mcp_lib.py` itself is not imported, or the manifest cannot say,
   so nothing is imported. Repeats follow §16.1 (4 h after the later of last attempt and `ladder_until`). But see Issue 3.
6. `enable` rollback: met. The pointer is removed only if this run created it. Gate 8 is a refusal now. Gate 10 (both) and the
   pre-existing pointer case are tested. `gate_fails` asserts the pointer is absent for every gate.
7. Notify thread: met. It is a daemon thread joined with 45 s, a timeout is recorded in notify.log, `state.notify_failures` and
   `last_error`, the pass goes on, and the mutation (long join) is caught.
8. Fixer's late setUp change (`addCleanup(mock.patch.stopall)` in `TestEnable`, and the temp-dir leak): correct. The full suite is
   green with it, and `/tmp/epic-driver-test-*` stayed at 97 across a full run plus 8 module runs, so there is no growth.
   `stopall` runs after tearDown, so it only stops already-stopped patchers.
9. Regressions in 50-04 to 50-07: none found. Their tests are unchanged in the full run. questions-mcp: `notify` gained a
   keyword-only `workspace_dir=None` (default behaviour unchanged), and the diff to `test_unit_questions_mcp.py` adds tests only.
10. Mutations (backup by `cp`, restore from the copy, `diff` clean, md5 of both files equal to the start, `a811e083...` and `9f2e7bc2...`):

| # | mutation | result |
|---|---|---|
| 1 | infra phase ungated (`fits=None`) | caught (reboot timing test) |
| 1 | check budget computed before the sha checks | caught |
| 1 | `first_exempt=True` always | caught (2 tests) |
| 1 | `if not l_ok:` (deferred infra raises `listener_down`) | SURVIVED |
| 1 | every `incomplete = True` removed from `take_readings` (unread sessions treated as read) | SURVIVED |
| 2 | installed copy honours the override | caught (2 tests) |
| 2 | installed detection by home bin only | caught (4 tests) |
| 2 | enable gate 2 ignores the var | caught (cli and lib) |
| 6 | no pointer rollback | caught (4 tests) |
| 7 | notify join effectively unbounded | caught |

11. No real system state touched: `~/.claude/bin/epic-driver`, `~/.claude/epic-driver` and `~/.local/state/epic-driver` do not
    exist, and no epic-driver unit is listed. Nothing was committed, no git state op was run, no notify sent.
    `git status` and `git diff --stat` at the end: 6 modified plus untracked `.claude/bin/epic-driver`, unchanged from the start.

## Issues Found

### Issue 1: [severity: MEDIUM]

- **File:** tests/test_unit_epic_driver.py (`TestTickTiming`, ~7151) vs epic_driver_lib.py `check_infrastructure` (~7878)
- **Problem:** nothing asserts that a skipped infrastructure check raises no `listener_down`/`inhibitor_down`.
  Changing `if l_ok is False:` to `if not l_ok:`, so that a deferred check raises a false alarm, passes all 473 tests.
  The reboot test only asserts there is no `tick_error`.
- **Fix:** in the reboot timing test (listener and inhibitor inactive and hung), assert that neither `listener_down:run` nor
  `inhibitor_down:run` is active or notified, and that `Infrastructure.deferred` is true. Add a unit for `ensure_unit` and
  `check_infrastructure` with a `fits` that always returns False.

### Issue 2: [severity: MEDIUM]

- **File:** tests/test_unit_epic_driver.py vs epic_driver_lib.py `take_readings` (~3938-3960) and `tick_pass` (~8452)
- **Problem:** nothing tests that a session the deadline left unread is not reconciled and does not look dead. The word
  `incomplete` appears in no test. Removing `out.incomplete = True` at the session sites survives. The reconcile then runs on a partial reading.
  The reboot test hides it only because no tmux server exists, so nothing is launched anyway.
- **Fix:** a tick test with a live session and a `fits` that refuses the reads (or enough sessions at 15 s each to cross 150 s)
  with a live server. Assert no reconcile (state unchanged except bookkeeping), no resume, no `session_*`/dead alarm,
  no alarm cleared, and that the next pass reads it normally. Also a direct `take_readings(fits=...)` test for `incomplete`.

### Issue 3: [severity: MEDIUM]

- **File:** .claude/bin/epic-driver `tick_stamp_mismatch` (`unsafe = ...`)
- **Problem:** the guard stops only the import when `questions_mcp_lib.py` itself is among the mismatched files. That module
  imports, at module level, `questions_store`, `questions_git`, `questions_listen_lib`, `questions_ops` and `roles_config`
  (questions_mcp_lib.py lines 92-96). All five are in `STAMPED_FILES`. If any of them is the mismatched file, the code still
  imports and executes that changed code in the tick, which is what §17 stamps exist to prevent. A test poisons only `questions_mcp_lib`.
- **Fix:** treat any mismatched name in the notify path's import closure (`questions_mcp_lib.py` plus those five, `amux_spawn_lib`
  is not needed) as unsafe. Add a poison-module test for one dependency.

### Issue 4: [severity: LOW]

- **File:** epic_driver_lib.py `RealWorld.notify` (~931-944)
- **Problem:** the `CLAUDE_HOOKS_REPO` set/restore runs in a notify thread that may outlive its join timeout. Two overlapping
  (abandoned plus new) threads can each capture the other's value as `prev`, and the final restore can leave the variable set
  in `os.environ`. Children are not affected (`child_env()` strips it), so the only effect is a stale value in the process.
- **Fix:** guard the set/restore with a module-level lock, or pass the repo to `questions_mcp_lib` without touching the environment.

### Issue 5: [severity: LOW]

- **File:** epic_driver_lib.py `cmd_enable` rollback (~8970)
- **Problem:** when the pointer file already existed with different content, gate 9 overwrites it with the new body and a later
  refusal does not restore it. Only creation is rolled back.
- **Fix:** keep the old bytes and restore them on `Refused`, or document that it stays.

## Code Quality Notes

- `override_env` also makes `enable` refuse from an installed copy, where the variable is ignored for the stamp check. That is stricter, and fine.
- A first-exempt check ending at about 270 s defers every notify of that pass. This is acceptable and recorded as notes.
- The fixer's open question stands (cap check timeouts at validate time, now about 260 s); that is a manager decision.

## Questions for User

None.
