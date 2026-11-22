# Review Report 2 - 50-05 Session reconcile and stall detection (second pass)

## Verdict: PASS

No BLOCKER, HIGH or MEDIUM. Three LOW. `py_compile` on `epic_driver_lib.py` and `amux_spawn_lib.py` is clean. Full suite, foreground, `python3 tests/run_all_tests.py`: `Ran 2782 tests in 440.414s` / `OK (skipped=2)`. The module run `--module unit_epic_driver` is 241 OK.

## Completeness Check (focus points)

1. `delivery_provably_absent` (epic_driver_lib.py ~3914): met. It returns False unless all hold: a send time and event id, not already `confirmed_at`, `transcript_path` set, `transcript_first_at` known and `<= send`, no `turn_start` at or after the send, no user entry with `E-<n>`, pane captured and non-blank, and the id absent from the pane. Every other case is False (None path, None first_at, truncated tail, turn_start at the send instant, typed pane, uncaptured pane, blank pane, landed prompt). Enter-not-registered: the typed line sits in the pane, so the pane check gives False, no retry, and the entry ends unconfirmed (invariant 2: no further sends). That path is acceptable. The pane check also covers the "line visible but no turn" case. The `delivery_unconfirmed` alarm is not raised in 50-05 (nothing in the lib names it). It belongs to the 50-06 delivery sender per architecture.md §9.3 (line 546) and §16.2 (line 901). See Issue 3.
2. Turn bounds and the interrupted path: met.
   - `_reconcile_verdicts` skips a turn whose first stop lies past the next `turn_start`, or that has no stop while a later start exists. Such a turn gets no verdict, no limit read and no entry in `verdict.turns`. The operator turn's stop and line are never applied to the event (a test pins it).
   - Interrupt, operator turn, idle gives exactly one `verdict_reminder` through the existing `reminded_at` guard. Then `verdict_missing` once the reminder turn has ended with no valid verdict. A later valid verdict clears it (tests at test_unit_epic_driver.py 2434 to 2492).
   - Nothing is emitted while a turn is open (`_settled_idle` False). The limit-before-missing order holds: `unanswered` is ANDed with `limited_at is None`.
   - **Once-ness:** the reminder is emitted on EVERY pass until `entry["reminded_at"]` is set, exactly as in the pre-existing 50-05 path. The pure pass does not set it. 50-06 must, in the same commit and BEFORE sending the `VERDICT?` line, record `entry["reminded_at"] = iso(now)` on the outbox entry. The reminder turn is then found as "the first turn_start at or after `reminded_at`", and `after_reminder` is derived from it. Without that, the reminder repeats forever. 50-06's task file does not yet say this (grep for `reminded_at` finds nothing). See Issue 3.
3. Classifier: met. Fixture classifications re-run: folder-trust (both widths) and `synth-code-mode-optin` are dialog; `cc-2.1.330-idle-input-box`, `startup-no-dialog`, both `synth-conversation-quote*`, `synth-input-box-multiline` and `synth-input-box-typed` are no_dialog; `synth-blank` and `synth-booting-splash` are unreadable. A synthetic empty box with `❯` or `>` is no dialog. `❯ 1. foo` is a dialog. `❯ x` plus an `Esc to cancel` line below it with no input box is a dialog (intended). Fixture grep for tier names, `/home`, `/data`, `@`, user/project names, `.com`, login, expire, subscription finds nothing. The only hits for "team" are the Claude Code trust-dialog sentence.
4. Surviving mutation: see Issue 1. They are NOT equivalent.
5. Regressions: none found. The diff to 50-04 sections is the `STEP_TRANSITIONS` addition only; `amux_spawn_lib.py` changes are additive. The 50-06 task file note and the state.md status flip are the only other changes.
6. `tmux ls`: 8 sessions, all `amux-` named: workspace-108, -113, e037-wave1-mgr, authenticity-7, claude-hooks-epic50-mgr, tradingtips-53, -56, valheim-5. None is a leftover of these agents (authenticity-7 was created at 19:55, during this review, by unrelated work). Stale `/tmp/epic-driver-test-m6zfhqa7` is the known 50-04 leftover.

## Issues Found

### Issue 1: [severity: LOW]
- **File:** `epic_driver_lib.py` `_settled_idle` (~4679); tests
- **Problem:** The fix-2 claim that the reducer idle check is redundant with "the latest turn has stopped" is wrong in one direction. Reducer `idle` implies the turn is closed (reduce_events sets `turn_open=False` before it can say idle), so the stop check is the implied one. The reverse does not hold: a `stop` with `background_tasks_count > 0`, or a `subagent_stop` as the last bg-draining event, gives `state_hint == "running"` with the latest turn stopped. I probed this. With such a stop (background_tasks_count=1, interrupted event turn), the real code emits no reminder. With the reducer check removed (cp-backed mutation, restored, `diff` clean), it emits a `verdict_reminder` while background work is running. The whole `unit_epic_driver` module stays green under that mutation (241 OK), so no test separates them. The other mutation (drop only the stop check) also survives, which confirms the stop check is the redundant one.
- **Fix:** Keep the reducer check (it is the one doing work). Add a test: interrupted event turn, operator turn stopped with `background_tasks_count=1`, expect no action. The latest-stop check may stay as a cheap guard or be removed.

### Issue 2: [severity: LOW]
- **File:** `_verdict_followups` / `_reconcile_verdicts`
- **Problem:** If the reminder turn itself is interrupted (reminder sent, `reminded_at` set, then Esc plus an operator turn), `after_reminder` is never set because the reminder turn is skipped as unsettled. `unanswered` is true, but `reminded_at` is set so there is no second reminder, and no `verdict_missing`. I probed it: no condition, no action. The event is silent again, though only after an operator has intervened twice in a row.
- **Fix:** Optional. In the unanswered case, if `reminded_at` is set and a settled-idle session has had a turn start after `reminded_at`, treat that as `after_reminder` (raise `verdict_missing`). Or accept it and note it in 50-06/50-08.

### Issue 3: [severity: LOW]
- **File:** `tasks/50_epic_driver/50-06-dispatch-policy-and-delivery_opus.md`
- **Problem:** 50-05 leaves two things to 50-06 that the task file does not yet bind. (a) The `VERDICT?` send must commit `entry["reminded_at"]` before sending (point 2 above); otherwise the reminder repeats every pass. (b) The `delivery_unconfirmed` alarm and the "no further sends" lock after a `False` from `delivery_provably_absent` (for example Enter not registered). Line 35 of the task file mentions the retry and the reminder, but not these.
- **Fix:** Add both to the 50-06 brief, with a test that a second pass after the commit emits no reminder, and one that an unprovable absence raises `delivery_unconfirmed` and sends nothing.

## Code Quality Notes
- The classifier hint-below rule scans every line below the pointer, not only nearby ones. A conversation row `❯ prompt` followed anywhere below by an `Esc to cancel` line, with no input box, reads as a dialog. This is the intended trade-off (raises an alarm, never answers an unlisted dialog) and only applies while the launch prompt is unconfirmed.
- The probe script is in `/tmp/rv2probe/probe_rv2.py`; nothing under the repo was edited. Backup `/tmp/rv2_edl.bak`, restored and confirmed with `diff` (no output).

## Questions for User
None.

## Final tree
`git status --short`: ` M .claude/hooks/amux_spawn_lib.py`, ` M .claude/hooks/epic_driver_lib.py`, ` M tasks/50_epic_driver/50-06-...md`, ` M tasks/50_epic_driver/state.md`, ` M tests/test_unit_epic_driver.py`, `?? tests/fixtures/epic_driver/`. `git diff --stat`: 5 files changed, 3485 insertions(+), 16 deletions(-), unchanged from the start. No commits, no checkout/stash/reset/clean, no tmux keys sent.
