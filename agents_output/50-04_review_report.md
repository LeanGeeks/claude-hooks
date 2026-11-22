# Review Report - 50-04 Ledger, schedule, checks, runtime state (pure core)

## Verdict: PASS

No BLOCKER or HIGH issue. 0 MEDIUM, 9 LOW (all hardening or test gaps).

Verification run:
- `py_compile` of `epic_driver_lib.py`, `test_unit_epic_driver.py` and `context_mcp_lib.py`: OK. The modules import.
- `python3 tests/run_all_tests.py` (foreground, full): `Ran 2644 tests in 427.162s ... OK (skipped=2)`.
- `--module unit_epic_driver`: 122 tests OK. `unit_python_compat`: OK (1 skip: no old interpreter with tomli).
- `tests/test_unit_context_mcp.py` (19 tests; NOT registered in `run_all_tests.py`, pre-existing) run directly: OK.
- Under real CPython 3.9.21 (uv, with tomli and pure-Python PyYAML supplied): `test_unit_epic_driver` 122 OK; `test_unit_context_mcp` + `test_unit_measurement_helpers` 50 OK. The Python floor is verified at runtime, not only by the AST check.
- Tree check: `git status` is the same set as at the start (M `context_mcp_lib.py`, `state.md`, `conftest.py`, `run_all_tests.py`, `test_unit_python_compat.py`; ?? `epic_driver_lib.py`, `test_unit_epic_driver.py`). No tracked or shared file was edited by me. The mutations ran on a copy under `/tmp/mut` (no shared-tree edit at all); each was restored from a `cp` backup and confirmed with `diff`.

## Completeness Check

1. Value grammar and names (§5.1, §3): met. `epic_driver_lib.py:392-550`. Grammar, tests at `test_unit_epic_driver.py:161-253`. Hand-checked: window coverage 22-04Z gives 4 h for 23:00-03:00; the 8 h window 14-22Z gives 3600 s for 13:00-15:00.
2. Ledger and validator (§5.2-5.5): met. Every §5.5 error has its own test (schema, graph, files, names, approvals, timing, resources, repos, compaction, startup dialogs; tests at `:284-640`). Messages carry `steps.<id>.<field>` (`LedgerIssue.where`, `str()` shows both). Both warnings are tested (`:618-640`). The §5.6 037 fragment is read verbatim from architecture.md and validates with 0 errors and 0 warnings (`:643`); without the OQ-2 pin it fails the compaction rule. The compaction rule uses `world.profile_env` (RealWorld: `asl.resolve_profile`), `pinned_env` (wins over the profile), `asl.expected_model_id` and `context_mcp_lib.resolve_context_window` (`:1274-1317`); `cap < min(auto, idle)`; the idle-only and auto-only failures are both tested. `startup_dialogs`: id regex, duplicate, regex compile, key in Enter/Escape, unknown keys (`:1396-1431`). `worktree_root` defaults per OQ-1.
3. Schedule (§6): met. State machine as data (`STEP_TRANSITIONS`) matches §6.1 (plus DUE->PENDING and the permissive FAILED re-entry). Due vs dispatchable and every hold reason are present (paused, approval, approval question open, approval conditions, window, latest_start, resources, model limit, group, attempts). Monterey numbers recomputed by hand and verified: due 11-24 09:28:20Z, near 11-27 09:28:20Z, missed 11-29 09:28:20Z (`occurrence_deadline`, `:2195`; test `:786`). Windows across midnight, `latest_start`/`duration`, `after` anchors (advanced_at vs done_at), `not_before`, `at`, `trigger`, resources and groups are tested. All times are UTC (aware datetimes everywhere). C15 passes only after >= 1 h inside 14-22Z (`:1010`). A span under 20 min is rejected (`:456`, with the 20 m boundary pinned).
4. Approvals (§15.1, P0-13): met. Grants only in `state.grants` via `grant()`. `next_approval_to_ask` is in map order, requires conditions done, requires the approval to be the only hold on a gated step, and returns None while `approval_question` is set. Tests at `:855-913`.
5. Check runner (§7, P0-10): met. `/bin/sh -c`, `start_new_session`, SIGTERM then SIGKILL after 5 s (a TERM-ignoring child was killed in 6 s, probed), fixed environment (exact-equality test plus a real `env` run), cwd = `plan_dir`, 4 KB tail, `check_every`, the 150 s budget with `check_cursor` round-robin, consecutive-error counting, rc 0/1/2/3. The process-group test really checks that the child `sleep` is gone (zombies count as gone via `/proc`); the shell-only-kill mutation fails it.
6. State (§8): met. `atomic_write_text` = mkstemp in the same dir, fsync file, `os.replace`, fsync dir; the crash test mocks `os.replace` and checks that the old bytes survive and no tmp is left; a separate test counts the 2 fsyncs. The schema version and run_id are checked on load and on save. `mark done|reset` match §8 (outbox cancelled, alarms cleared, sessions untouched). `EPIC_DRIVER_STATE_ROOT` override. Runtime files only under the state root; the pointer file is only a path function (invariant 8).
7. Purity: met. No tmux, amux, notify or systemctl in code (a test greps for it). Imports are stdlib + PyYAML (optional at import) + `amux_spawn_lib`; `context_mcp_lib` is lazy per §17.
8. Out-of-scope edit (`from __future__ import annotations` in `context_mcp_lib.py`): safe. The module has no dataclass, pydantic, `get_type_hints`, `__annotations__` or `isinstance(x, A | B)` use. The FastMCP tool signatures live in `context-mcp/server.py`, which is unchanged. The only importers are `server.py`, the epic lib and tests. Tests pass on 3.14 and 3.9. Needed: without it the module does not import on 3.9.
9. Test quality: good. Mutations on the copy (14 run; the first 9 listed first):
   - Killed: deadline-near offset, shell-only kill, no dir fsync, `>` vs `>=` on coverage, latest_start `>=`, approval order, cursor ignored, rc 3 not an error, after-anchor, rate limit removed, non-atomic write, 20-min boundary, window end inclusive, cwd, attempts not incremented, approval gate off, mark_reset outbox, env leak, `after` delay dropped, trigger refire.
   - Survived: (a) `coverage_s` start-of-yesterday removed (no test starts inside the post-midnight part of a crossing window); (b) compaction `cap == point` boundary; (c) budget `>` vs `>=` boundary.

## Issues Found

### Issue 1: [severity: LOW]
- **File:** `tests/test_unit_epic_driver.py:206-220`, `epic_driver_lib.py:2599`
- **Problem:** No test of a daily `check_window` that crosses midnight whose anchor lies after midnight (e.g. 22:00-04:00Z, anchor 01:00). The mutation "start the day loop at `a.date()`" survives. The code is correct (probed), the test is missing.
- **Fix:** Add `coverage_s(01:00, 03:00) == 2h` for 22-04Z and one eligibility case.

### Issue 2: [severity: LOW]
- **File:** `epic_driver_lib.py:104, 2620-2624`
- **Problem:** The `check_every` slack (180 s, documented decision 9) makes the limit weaker than "at most one run per check_every": 5 m runs again after 2 m, and `check_every` of 3 m or less is not limited at all (an operator running `tick` by hand repeatedly would run checks each time). Harmless at the 10-min cadence.
- **Fix:** Scale the slack (e.g. `min(slack, every/10)`), or state the rule in the docs.

### Issue 3: [severity: LOW]
- **File:** `epic_driver_lib.py:2778-2798`
- **Problem:** The budget is checked only before starting a job. A job started at 149 s can run its full timeout (60 s default, ledger-settable), so a pass can exceed ~150 s by a whole timeout.
- **Fix:** Clamp each job's timeout to the remaining budget (or document it).

### Issue 4: [severity: LOW]
- **File:** `epic_driver_lib.py:2531-2548`
- **Problem:** `grant()` takes `ledger` as optional; without it an unknown approval id is silently granted. It also does not require the approval's conditions to be done.
- **Fix:** Make `ledger` required in `mark --grant` (50-08) or in the signature.

### Issue 5: [severity: LOW]
- **File:** `epic_driver_lib.py:1274-1317`
- **Problem:** When the context window comes from `default-fallback` (an unmapped alias), the compaction rule passes or fails on a guessed 1M window with no warning. The source is shown only in the error text. This is spec-as-written. The `cap == point` boundary is untested (mutation survived).
- **Fix:** Add a warning on `default-fallback`; add a boundary test.

### Issue 6: [severity: LOW]
- **File:** `epic_driver_lib.py:332-344` (`RealWorld.run_check`)
- **Problem:** After a normal exit the shell is reaped by `wait()`, and then `killpg(pgid, SIGKILL)` runs. In theory the pid/pgid could have been recycled in that window (extremely unlikely).
- **Fix:** Signal the group before reaping, or guard with `_group_alive` and the session id.

### Issue 7: [severity: LOW]
- **File:** `epic_driver_lib.py:1682-1700`, state keys `by` and `check.last_error`
- **Problem:** Two state keys that §8 does not list were added (the implementer disclosed them). `eligible_checks` writes `window_covered_s` into the state, although it is described as pure.
- **Fix:** Add the two keys to architecture §8 during 50-08 or the docs task.

### Issue 8: [severity: LOW]
- **File:** `epic_driver_lib.py:1438-1480`
- **Problem:** `version: true` and `version: 1.0` pass the `version != 1` test; duplicate YAML keys inside a step are silently overridden by `safe_load`; a `timeout: 0s` silently falls back to the default.
- **Fix:** Check `_is_int(version)`; consider rejecting zero durations where meaningless.

### Issue 9: [severity: LOW]
- **File:** `epic_driver_lib.py:167`
- **Problem:** `from_iso` depends on the private `asl._epoch_of`; a rename there breaks the lib. `tests/run_all_tests.py` does not register `test_unit_context_mcp` (pre-existing).
- **Fix:** Expose a public helper or copy the 8 lines; register the context-mcp test module.

## Code Quality Notes

- Clean structure; every effect is behind `World`. `FakeWorld`, `_Base`, `_Sched` are reusable by 50-05 to 50-08.
- The hold semantics are consistent with §6.2. `QUIET_HOLDS` also includes `latest_start` and `attempts_exhausted` (documented in the code).
- Notes for 50-08 (the implementer's list is right): `mark reset` clears alarm records, but conditions are recomputed each pass; recurring steps' alarms self-clear only through that recomputation.
- The implementer's report matched the code on every claim I checked.

## Questions for User

None.
