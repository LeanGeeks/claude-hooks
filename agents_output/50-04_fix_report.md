# Fix Report - 50-04 (9 LOW issues from the review)

All nine fixed. Phase 0 (`state.md`) untouched by me. Nothing committed. Mutations ran on the shared files with a `cp` backup, restored and confirmed with `diff` after each.

Files changed by this fix: `.claude/hooks/epic_driver_lib.py`, `.claude/hooks/amux_spawn_lib.py`, `tests/test_unit_epic_driver.py`, `tests/run_all_tests.py`, `tasks/50_epic_driver/architecture.md`.

## Per issue

### 1. Daily `check_window` across midnight
- Change: none in code (it was correct); tests only.
- Tests: `TestGrammar.test_daily_window_coverage_starting_after_midnight` (22:00-04:00Z: `coverage_s(01:00, 03:00) == 2h`, 03:00-05:00 = 1h, 05:00-21:00 = 0); `TestCheckWindow.test_daily_window_across_midnight_with_an_anchor_after_midnight` (anchor 01:00, `min: 2h`: not eligible at 02:59 with coverage 1h59m, eligible at 03:00).
- Mutation: `d = a.date() - timedelta(days=1)` -> `d = a.date()`. Both new tests fail (2 failures). Restored.

### 2. `check_every` slack
- Change: `epic_driver_lib.py`: constants `CHECK_EVERY_SLACK_MAX_S = CHECK_BUDGET_S + 30` (180) and `CHECK_EVERY_SLACK_PERCENT = 30`, new `check_every_slack_s(every_s) = min(180, every_s * 30 // 100)`, used by `_rate_ok`. The rule is documented in the constant's comment.
- Why 30 %: at the 10-min cadence a run may start 150 s into its tick and the next tick may fire 30 s early, so the gap can be 420 s; `every: 10m` needs exactly 180 s of slack (30 % of 600 s). `min(180, every/10)` would give only 60 s and break that constraint. Shorter periods get proportionally less: 3 m -> 54 s (limit 126 s), 2 m -> 36 s (limit 84 s).
- Tests (`TestCheckRunner`): `test_check_every_slack_scales_with_the_period`; `test_every_10m_runs_on_every_tick_despite_jitter` (last run 01:02:30, eligible at 01:09:30, not at 01:09:29); `test_short_every_is_limited_when_tick_is_run_by_hand` (3 m: three `run_checks` 5 s apart run once; not at 125 s, runs at 126 s; 2 m limited too).
- Mutation: flat 180 s slack -> 2 tests fail. Restored.

### 3. Budget overrun
- Change: `run_checks`: a job is not started when `elapsed + job.timeout_s > budget_s`, unless it is the first of the pass (`result.ran` empty). The skipped job goes into `not_reached` and becomes `check_cursor`. Timeouts are passed unchanged (never clamped). Docstring updated.
- Tests (fake clock): `test_a_job_whose_timeout_ends_past_the_budget_is_not_started`; `test_budget_boundary_is_inclusive_and_timeouts_are_never_clamped` (ends at exactly 150 s: runs, timeouts stay 60, no error counted); `test_the_first_job_runs_even_if_its_timeout_exceeds_the_budget` (600 s timeout runs first). The existing `test_budget_stops_at_150s_and_resumes_at_the_cursor` now uses `timeout: 20s` checks (with the default 60 s the new rule correctly stops at s3).
- Mutation: `>` -> `>=` fails the boundary test (this was the surviving mutation (c) in the review). Restored.

### 4. `grant()`
- Change: signature is now `grant(ledger, state, approval_id, question, note, now)`; an approval id the ledger does not define raises `KeyError` naming the id and the defined ones. Conditions are not required to be done (docstring says so). No other caller exists in the repo.
- Tests: `test_grant_is_idempotent_and_checks_the_id` (unknown id raises, names `SA7` and `SA6`, no grant written; calling without a ledger raises `TypeError`); new `test_grant_before_the_conditions_are_done_is_allowed`. Existing grant calls updated to the new signature.

### 5. Compaction with `default-fallback`
- Change: `_validate_compaction` adds a warning `W_CONTEXT_FALLBACK` (`context_window_default_fallback`) at `roles.<key>`, naming the role, the model and the guessed window, when `resolve_context_window` returns source `default-fallback`. It is a warning, not an error. It fires whether or not a pct is pinned, as the decision said.
- Tests: `test_compaction_default_fallback_window_warns` (unmapped model: 0 errors, one warning with model and role; a known model: no warning); `test_compaction_cap_equal_to_the_point_fails` (idle point 250000: cap 250000 fails, 249999 passes; same boundary on the auto point).
- Mutation: `cap < min(points)` -> `cap <= min(points)` (written as `role.cap > min(points)`) fails the boundary test. Restored.
- Side effect: the 037 fragment (`fable` with profile `claude-fable`, no `ANTHROPIC_DEFAULT_FABLE_MODEL`) now yields exactly this one warning, because that window really is a guess. `test_the_037_fragment_validates` now asserts that single warning instead of an empty list. This is the warning working as designed, not a regression.

### 6. pgid recycle window in `RealWorld.run_check`
- Change: new `_wait_exited(pid, timeout_s)` waits with `os.waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)` (polling every 20 ms), so the zombie leader keeps the pid/pgid reserved. Flow: wait for exit (or timeout) -> on timeout SIGTERM to the group and wait up to the 5 s grace for the leader -> SIGKILL to the group -> `proc.wait()` reaps. `_group_alive` was removed (dead code now: `killpg(pgid, 0)` succeeds on a zombie-only group).
- Behaviour difference, stated: after the timeout SIGTERM, the final SIGKILL goes out as soon as the shell has exited (or after 5 s), no longer "when the whole group is gone". A child that is still shutting down after its shell died gets SIGKILL immediately. A zombie-only group cannot be told apart from a live one without `/proc`, so I kept the plain Linux-safe form. The timeout-kills-whole-group test and the leftover-children test stay green.
- Tests: `test_group_is_killed_before_the_leader_is_reaped` (spy on `_signal_group`: the only signal, SIGKILL, finds the leader unreaped: `waitid` still answers, no `ChildProcessError`) and `test_timeout_path_also_signals_an_unreaped_leader` (SIGTERM and SIGKILL both before reaping).
- Mutation: reap (`proc.wait()`) before the SIGKILL -> both tests fail. Restored.

### 7. `by`, `check.last_error`, and `eligible_checks` writing state
- Architecture §8: added `"by": null` to the step example and `"last_error": null` to `check`, plus a rule bullet: `by` = `"check"` / `"operator"` / null; `last_error` = text of the latest check error or null after a clean run.
- Decision: made scheduling pure instead of renaming. `eligible_checks` no longer writes. New pure `window_coverage(ledger, state, now) -> {step id: seconds}` (daily windows of steps in a checked status); `run_checks` writes those values into `window_covered_s` before building its jobs. This does not distort the design: `run_checks` already mutates the state, and non-eligible steps still get their coverage shown, which a coverage-in-the-job design could not do.
- Tests: `test_eligible_checks_does_not_write_the_state` (deep-equal before/after, also for `window_coverage`); `test_daily_coverage_accumulates_across_days` now reads `window_coverage`; `test_c15_daily_coverage` still passes through `run_checks`; `test_new_state_has_the_architecture_keys` no longer special-cases `last_error` (it compares to the §8 example).

### 8. Validation strictness
- `version`: must be an int equal to 1 (`_is_int`); `true`, `1.0`, `"1"` rejected. Test `test_schema_version_must_be_an_int`.
- Duplicate YAML keys: `_UniqueKeyLoader` (a `yaml.SafeLoader` subclass, defined only when PyYAML imports) with a mapping constructor that raises `ConstructorError("found duplicate key 'x'")` with the key node's mark; the error reads `not valid YAML: ... found duplicate key 'epic' in "<byte string>", line N, column 1`. Used by `validate_ledger` through `yaml.load(data, Loader=_UniqueKeyLoader)`. Unhashable keys are left to `construct_mapping`. Test `test_schema_duplicate_keys_are_an_error` (top level, in a step, in a role; one error each, key and exact line checked for the top-level case).
- Zero durations: `_Validator.duration(..., positive=True)` rejects `0s` with "must be > 0". Applied to: `defaults.timeout`, `defaults.check_timeout`, `defaults.check_every`, step `timeout`, command `timeout` and `every` (check and trigger), step `every` (string and `period`), step `duration`, `check_window.min`, role `stuck_after`. The old ad hoc `<= 0` checks in `_parse_command` and `_parse_every` were removed in favour of the flag. Left allowing zero on purpose: `after.delay` (no delay), every `warn_before` (no early warning), `check_window.from` (starts at the anchor). `check_window.to`, `deadline` and `max_attempts` are already constrained by their own rules. Test `test_schema_zero_durations_are_errors` covers all 13 rejected keys by subtest plus the allowed zeros.

### 9. `from_iso` and the private `_epoch_of`
- Change: `amux_spawn_lib.py` gets a public `epoch_of`; `_epoch_of` is now a one-line delegate (its existing call sites and the 50-03 tests stay untouched). `epic_driver_lib.from_iso` calls `asl.epoch_of`.
- Choice: a public name in `amux_spawn_lib` rather than a copy. §17 already imports `amux_spawn_lib` from `~/.claude/hooks` under the same stamp set, so no new edge appears, and a second ISO parser would be one more copy to keep equal to `statusline._parse_iso_epoch`.
- Test: `TestTime.test_from_iso_uses_the_public_epoch_parser` (private and public agree on 9 inputs; no `asl._` in the lib source). `unit_measurement_helpers`: 31 OK.
- `test_unit_context_mcp`: passes alone (19 OK, direct and via pytest), so it is registered in `tests/run_all_tests.py` as `unit_context_mcp` (`--module unit_context_mcp`: 19 OK).

## Decisions made under latitude
- Slack rule 30 % with a 180 s cap (see 2); exposed as `check_every_slack_s`.
- Budget: `break` at the first job that does not fit (it becomes the cursor), rather than skipping it and trying smaller jobs behind it; the next pass starts with it, and the first job of a pass always runs.
- Compaction warning fires for any `default-fallback` window, not only when a pct is pinned.
- Zero-duration list as in 8; `warn_before`, `after.delay`, `check_window.from` stay zero-able.
- `window_coverage` only covers steps in `verifying`/`waiting`/`watching` (same as before the change).
- Timeout-path kill ordering in 6.
- The 037 fragment test now expects the one fallback warning (see 5).

## Compile
`python3 -m py_compile` of `epic_driver_lib.py`, `amux_spawn_lib.py`, `test_unit_epic_driver.py`, `run_all_tests.py`: OK.

## Test counts
- `python3 tests/run_all_tests.py --module unit_epic_driver` (foreground): `Ran 140 tests ... OK` (was 122; +18).
- `--module unit_measurement_helpers`: `Ran 31 tests ... OK`. `--module unit_context_mcp`: `Ran 19 tests ... OK`.
- Full suite (foreground): `Ran 2681 tests in 532.587s` / `OK (skipped=2)`, exit code 0. 2681 = 2644 baseline + 18 new epic driver tests + 19 context-mcp tests now registered.
- Tests touch only temp dirs (state root set by the test and the runner); no tmux, network, real repos or real state root. The new real-subprocess tests only run `exit 0` / `sleep 300` in a temp plan dir and kill their own group.

## git status --short
```
 M .claude/hooks/amux_spawn_lib.py
 M context-mcp/context_mcp_lib.py
 M tasks/50_epic_driver/architecture.md
 M tasks/50_epic_driver/state.md
 M tests/conftest.py
 M tests/run_all_tests.py
 M tests/test_unit_python_compat.py
?? .claude/hooks/epic_driver_lib.py
?? tests/test_unit_epic_driver.py
```
(`context_mcp_lib.py`, `state.md`, `conftest.py` and `test_unit_python_compat.py` were already modified before this fix; `amux_spawn_lib.py`, `architecture.md` and `run_all_tests.py` changes include mine.) `agents_output/` is gitignored but tracked, so committers must use `git add -f` for this report.

## Questions for User

None.
