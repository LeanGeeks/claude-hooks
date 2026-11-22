# 50-06 Review Report (second pass)

## Verdict: PASS

Full suite (foreground, before any mutation): `python3 tests/run_all_tests.py` -> `Ran 2859 tests in 384.245s` / `OK (skipped=2)`.
Epic driver module: 318 tests OK. No BLOCKER or HIGH issues.

## Completeness Check

1. #2 cleared dialog-answer record: met. `keyed` (epic_driver_lib.py ~6391) deletes the record only when `outcome.ran` is False (guard refused, nothing sent). Crash points traced:
   - before the answer commit: nothing recorded, nothing sent;
   - after the answer commit, before the effect: record stays, key never sent, never answered again (safe, at most once);
   - after the key, before the result commit: record stays, no resend (`test_a_key_sent_before_a_crash_is_never_resent`);
   - send command failed: `not_sent` set, record kept.
   The matrix has the `dialog_refused` scenario at every commit in both modes, with `len(keys) <= 1` asserted and exactly one key in the uncrashed run. It is meaningful.
2. #1 launch backoff: met. `launch_retry_wait_s` doubles from 600 s, caps at 4 h and is measured from `launch_error.at`. `name_in_use` and "no error recorded" are not gated. `_launch_result` resets `launch_attempts` to 1 on success. The alarm stays raised. The gate sits before the commit-then-effect of a re-issue, so invariant 5 is unchanged.
   - Across a reboot `launch_attempts` is NOT reset (see Issue 1).
3. #3 retry line fallback: met. The line is `e["line"]`, else `event_line(event, path)`; with neither, `continue` (entry kept), so no empty send is possible.
4. #4 cool-down: met. `rotation_cooling_down` is true while `handoff_timeout.at` is younger than 3600 s: reuse inside the window (49m59s), rotate at exactly 1 h. The `handoff_timeout` clear rule is untouched. A `compacted` => rotate decision is also downgraded to reuse during the cool-down. That is consistent with the manager decision, and the alarm stays raised. Only ACT_ROTATE is rewritten; SPAWN_NEXT with a finished handoff is unaffected.
5. Regressions: none found; suite green.
6. Mutations (cp backup, restored, `diff` confirmed identical):
   - #1 gate disabled: killed (9 failures);
   - #2 never clear on refusal: killed (3);
   - #2 clear on `not ok` (also clears a ran-but-failed send): SURVIVED (Issue 2);
   - #4 cool-down removed: killed (1).

## Issues Found

### Issue 1: [severity: LOW]
- **File:** .claude/hooks/epic_driver_lib.py:5865-5882 (`launch_retry_wait_s`), 6145
- **Problem:** `launch_attempts` and `launch_error.at` survive a reboot. A session in `resuming`/`spawning` whose launch failed shortly before a reboot (up to 4 h at the cap) is still gated after the reboot comes up, although the failure cause (for example a dead tmux server) is the very thing the reboot removes. The delay is bounded (at most 4 h from the failure) and is not a permanent block. A `dead` session resumed through `resume()` resets `launch_attempts` to 1. There is no test of the reboot interaction.
- **Fix:** Optional. When reconcile reports reboot facts, clear `launch_error` or reset `launch_attempts` for sessions that are not alive. Alternatively document the bounded delay.

### Issue 2: [severity: LOW]
- **File:** tests/test_unit_epic_driver.py (dialog key tests)
- **Problem:** No test covers a key command that ran but failed (`ran` True, `ok` False). Mutating the `keyed` clear condition to `not outcome.ok` leaves all 318 tests green. That mutation would clear the record after a key of unknown outcome and could permit a second key.
- **Fix:** Add a test in which `world.send_keys` fails: the record stays with `not_sent`, and no second key is sent on later passes.

## Code Quality Notes
- `keyed` deletes the last record matching `dialog` only. If `answer()` skipped recording because the dialog was already answered but the effect still ran, a refusal could delete an older legitimate record. Recon makes this unlikely.
- In `resume()`, a stale `launch_error` is not cleared at commit. The gate only applies once the status is `resuming` and the session is not alive, and the error is normally old, so the effect is negligible.

## Questions for User
None.
