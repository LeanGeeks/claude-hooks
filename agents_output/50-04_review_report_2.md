# Review Report - 50-04 (second pass)

## Verdict: PASS

No BLOCKER/HIGH/MEDIUM. 1 LOW found (merge-key regression in the new YAML loader). All 9 first-pass issues resolved as decided.

Compile: `py_compile` of epic_driver_lib.py, amux_spawn_lib.py, test_unit_epic_driver.py, run_all_tests.py: OK.
Full suite (foreground, `python3 tests/run_all_tests.py`): `Ran 2681 tests in 507.577s` / `OK (skipped=2)`.

## Completeness Check (the 9 issues)

1. Daily window across midnight: met (tests only; code was correct). Tests present; fixer's mutation reported.
2. `check_every` slack: met. `check_every_slack_s = min(180, every*30//100)` (epic_driver_lib.py:2663); `_rate_ok` uses it. 10m: slack 180, eligible at 420 s, not at 419 s (test_every_10m_runs_on_every_tick_despite_jitter). 3m -> 126 s, 2m -> 84 s limited. My mutation (percent 30 -> 10) failed 4 tests.
3. Budget: met. `run_checks` (l.2828+) breaks only `if result.ran and elapsed + job.timeout_s > budget_s`; first job always runs; timeouts passed unchanged; skipped job becomes cursor. My mutation (drop `result.ran and`) failed test_the_first_job_runs_even_if_its_timeout_exceeds_the_budget.
4. `grant()` id check: met (KeyError naming id and defined ids); tests present.
5. Compaction default-fallback warning: met; 037 fragment test asserts the single warning.
6. pgid recycle: met. `_wait_exited` uses `os.waitid(P_PID, pid, WEXITED|WNOHANG|WNOWAIT)`; group is signalled before `proc.wait()` reaps the leader, once. I ran RealWorld.run_check for: exit 3, a backgrounded child, timeout with sleep, a TERM-trapping shell (killed at ~6 s = 1 s timeout + 5 s grace), and a daemonised grandchild (`(sleep 200 &); sleep 200`). All returned correct rc/timed_out, left no zombies (no children of the process in /proc) and no surviving `sleep`. Removing WNOWAIT failed 5 tests (incl. both new ones and test_exit_codes).
7. `window_coverage` pure and `run_checks` writes `window_covered_s` before building jobs (l.2845-2847); `eligible_checks` writes nothing; test deep-equal. §8 documents `by` and `check.last_error`: met.
8. Validation: `version` int check, zero-duration, duplicate keys: met. Duplicate-key error reports key and line (verified: `found duplicate key 'a' ... line 3`). Anchors/aliases work. Merge keys do NOT (see Issue 1). The 037 fragment (§5.6) uses neither.
9. Public `asl.epoch_of`; `_epoch_of` delegates; `from_iso` calls it; unit_measurement_helpers green in the full run.

Item 3 of the brief: `unit_context_mcp` is registered in `tests/run_all_tests.py` and passes in the full run. Item 4: three mutations done (above), each restored from a `cp` copy and confirmed with `diff`; `git status` identical to the start and `git diff --stat` shows the same 7 files (49 insertions, 7 deletions), plus the two untracked files.

## Issues Found

### Issue 1: [severity: LOW]

- **File:** .claude/hooks/epic_driver_lib.py:67-87 (`_UniqueKeyLoader` / `_construct_unique_mapping`)
- **Problem:** The loader calls `loader.construct_object(key_node, deep=True)` on every key before `SafeLoader.construct_mapping` runs `flatten_mapping`. A YAML merge key (`<<: *x`) has tag `tag:yaml.org,2002:merge`, which has no constructor, so any ledger using merge keys now fails with "not valid YAML: could not determine a constructor for the tag 'tag:yaml.org,2002:merge'". Plain `yaml.SafeLoader` accepts them. Verified with `b: {<<: *x, p: 9}` and `<<: [*x]`. Anchors/aliases on values and keys are fine. The failure is loud, not silent, and the 037 fragment does not use merges, so impact is nil today; no test covers it.
- **Fix:** Call `loader.flatten_mapping(node)` first, tracking duplicates only among the node's own non-merge keys (skip keys whose tag is the merge tag), or reject merge keys explicitly with a clear message and add a test either way.

## Code Quality Notes

- The unsignalled-after-exit behaviour change in 6 (SIGKILL as soon as the shell exits at timeout) is documented and acceptable.
- `_epoch_of` private delegate is a harmless one-liner.
- Budget `break` (not skip-and-try-smaller) can starve nothing because the deferred job becomes the next pass's first job.
- Test quality: new tests assert values and fail under mutation; real-subprocess tests use only `exit`/`sleep` in temp dirs.

## Questions for User

None.
