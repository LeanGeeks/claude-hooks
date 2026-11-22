# Review Report - 50-05 Session reconcile and stall detection

## Verdict: PASS

No BLOCKER or HIGH issue. Two MEDIUM and four LOW. Compile and import: `py_compile` on `epic_driver_lib.py`, `amux_spawn_lib.py` and `tests/test_unit_epic_driver.py` is clean. The module test run (`--module unit_epic_driver`) is 229 OK. Full suite: see the last section.

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| 1 Session readings via `World` (tmux, pane_dead/in_mode/attached/pid, handle, 50-03 helpers); liveness from tmux + reducer, not stored state | met | `take_readings`, `_read_session`, `session_status` (epic_driver_lib.py ~3494, 3545). `reading.alive` = tmux present and pane_dead is not True; a stored `terminated` never decides |
| 2 Verdict grammar, invalid cases, exemptions, application per verb, grants | met | `parse_verdict_line`, `verdict_line_of`, `read_verdict`, `_apply_step_verdict`. 29 hand cases run, see below |
| 3 Delivery confirmation and "provably did not land" | met, with a MEDIUM (Issue 1) | `delivery_confirmation`, `delivery_provably_absent` |
| 4 Stalls: stuck/tool shell/subagent growth, limits, ladder, model check, compaction, open prompts, clock, foreign/orphan | met | `_reconcile_stalls`, `_reconcile_limits`, `_reconcile_model_and_compaction`, `_reconcile_clock`, `_reconcile_foreign` |
| 5 Startup dialogs: classifier on fixtures, listed answers, condition | met, with LOWs | `classify_pane`, `_reconcile_dialogs` |
| 6 Question reconcile via `open_store(...).all_entries()` | met | `RealWorld.question_entries`, `_reconcile_questions` |
| 7 Output is conditions and items only, nothing sent or notified | met | `reconcile` has no I/O: the state is deep-copied by JSON, no `World` call, no `os`/`time`/`open` in the pure half. The `RealWorld` effect test allows only the tmux verbs `display-message` and `capture-pane` |
| Done when: pure pass; every fed §16.2 row has a raise and a clear test | met | table below |

### Verdict parser, checked by hand

I ran `read_verdict` on 29 inputs.
- Fence: odd fence count is invalid (also `~~~`, and a second, unclosed fence). A closed fence earlier in the message is fine.
- Two verdict lines: invalid. Trailing whitespace: ok.
- Unknown step id: invalid. `HANDOFF` outside rotate: invalid, and ok inside rotate.
- `DONE` from the driver: invalid. Worker `DONE`: ok.
- `HALT: x; y`: valid and covers all ids. `ADVANCED: S1; HALT: x` is "no match", so HALT is not accepted mid-line.
- Uppercase sha, markdown-wrapped line, `;` without a space: all invalid.
- `ADVANCED: SA4` without a grant leaves SA4 `unverified` (missing); with the grant it is covered. `ASKED: Q-17 SA4` is covered.
- Partial coverage and `WAITING` forms (prefixed with a time, unprefixed text, `WAITING: S1` alone, a ledger id not in the event) behave as designed.

### Reset-time parsing, recomputed by hand

Run in Europe/Berlin, observed 2026-11-22.

| Text | Observed | Result |
|---|---|---|
| `resets 3pm` | 10:00 | 15:00 the same day |
| `resets 3pm` | 16:00 | 15:00 the next day |
| `resets 3pm` | 15:00 exactly | rolls to tomorrow (`<=`) |
| `resets 3:30pm` | 10:00 | 15:30 |
| `resets 12am` | 10:00 | 00:00 the next day |
| `resets 12pm` | 10:00 | 12:00 |
| `resets at 15:00` | 10:00 | 15:00 |
| `resets 9:40pm (Asia/Yerevan)` | 10:00 | 18:40 CET, correct |
| `resets 3`, `13pm`, `0:30am` | 10:00 | None |
| `resets Nov 20, 5pm` (past) | 10:00 | 2027-11-20 |
| `reset at 2026-07-02 20:16:08` | 10:00 | the dated form, parsed |

The ladder gives 15m, 30m, 1h, 2h, 4h, 4h, 4h. The last assistant entry must carry `error == "rate_limit"` and `isApiErrorMessage`. The limit check runs before the missing-verdict rule (reconcile order, `_turn_limited`).

### §16.2 rows fed by this task

| Alarm | Raising test | Clearing test |
|---|---|---|
| `session_stuck` | `TestStuck.test_stuck_after_no_growth_and_no_tool_shell` | `test_suppressed_for_the_tick_of_a_clock_jump_and_cleared_when_the_turn_closes` |
| `permission_open` | `TestOpenPrompts.test_permission_prompt` | `test_closed_prompt_clears` |
| `ask_user_question_open` | `test_ask_user_question_in_flight` (the 9 min no-raise case is in the same class) | `test_answered_ask_user_question_clears` |
| `usage_limit` | `TestUsageLimits.test_limit_entry_raises_holds_dispatch_steps_the_ladder_and_clears` | same test, and `test_past_the_reset_without_a_completed_turn_stays_raised` |
| `model_mismatch` | `TestModelCheck.test_mismatch_raises_and_holds_until_rotated` | same test |
| `compaction` | `TestCompaction.test_after_the_spawn_raises_once_before_is_ignored` | same test |
| `startup_dialog` | `test_unlisted_dialog_raises_and_is_never_answered`, `test_listed_dialog_answered_once_then_the_alarm` | the dialog gone, in the listed test; unreadable keeps it (`test_an_unreadable_pane_keeps_a_raised_alarm`) |
| `foreign_handle` | `test_foreign_handle_raises_and_clears_when_gone` | same test |
| `orphan_session` | `test_orphan_session_by_the_grammar` | same test |
| `verdict_missing` | `test_merged_event_partly_covered_gets_one_reminder_then_verdict_missing` | same test |
| `halt` (step row) | `test_driver_halt_raises_halt_and_a_later_verdict_clears_it` | same test |

`delivery_*`, `handoff_timeout`, `spawn_failed`, `resume_failed` and the run rows belong to later tasks.

### Other checks

- Startup dialogs: the classifier gives the expected kind on every fixture. Real folder-trust (both widths) is a dialog, the real idle box and the banner pane are no dialog, blank and splash are unreadable, and the synthetic code-mode opt-in is a dialog. A confirmed prompt means no capture. The ledger default is empty, so nothing is answered unless it is listed.
- Fixture scrub: grep for `/home`, `/data`, user and project names and `@` finds nothing. The provenance claims hold. `claude --version` is 2.1.330, and the `/tmp/epic-driver-fixture` path shows scrubbing. The synthetic code-mode fixture is labelled synthetic in the test header comment and the report.
- Questions: resolved gives one `answer` item and the status flips to `resolved`, so it is not repeated. Withdrawn closes with no item. Both free `approval_question`. Statuses come from `open_store(...).all_entries()` with an explicit `roles_path`.
- Model check: alias goes through the profile mapping, `model_matches` is used, a legacy handle without a model is skipped, and a resume re-checks. Compaction counts only at or after the launch.
- Clock: boot id gives reboot facts (`resume_now` = a turn open or a step in flight). NTP jump uses |Δwall−Δboottime|>60. Suspend uses Δboottime−Δmono>60. New stuck alarms are suppressed that pass, and a raised one is kept.
- 50-04 regression: the `STEP_TRANSITIONS` change adds `verifying→halted` and `halted→verifying/waiting/asked/done`. It is needed for the `halt` row, and `halted→due` is still refused. The full diff shows only these deletions, no other removals. The `amux_spawn_lib` changes are additive: `last_assistant_entry` output is unchanged, and `inflight_foreground_tool` gains a `timestamp` key.

### Mutation checks

Backed up with `cp`, restored from the copy, `diff` clean at the end. Each mutation was killed by the named test.

| Mutation | Killed by |
|---|---|
| Fence rule disabled | `test_inside_a_code_fence_is_invalid`, `test_invalid_verdict_counts_as_missing` |
| Limit-before-missing disabled | `test_limited_turn_is_not_missing_a_verdict` |
| Confirmation without the `turn_start` | `test_confirmed_needs_the_prompt_and_a_turn_start_after_the_send` |
| Dialog answer-once guard removed | `test_listed_dialog_answered_once_then_the_alarm` |

### Housekeeping

`tmux ls` shows 7 sessions, all with `amux-` names (workspace-108, -113, e037-wave1-mgr, claude-hooks-epic50-mgr, tradingtips-53 and -56, valheim-5). None looks like a leftover of this task. The throwaway session directories are gone from `/tmp` and `/var/tmp` (`~/.claude.json` has no `epic-driver-fixture` entry). One stale directory remains, `/tmp/epic-driver-test-m6zfhqa7`, with a dead `child.pid` from 17:07. It was created by an earlier 50-04 test (`mkdtemp`, test at line ~1296), not by 50-05.

## Issues Found

### Issue 1: [severity: MEDIUM]

- **File:** `.claude/hooks/epic_driver_lib.py`, `delivery_provably_absent` (~3912)
- **Problem:** Invariant 2 depends on this test, and it treats an unreadable transcript as proof of absence. With `transcript_path=None`, or a prompt older than the 1 MiB tail that `asl._tail_entries` reads, `reading.prompts` is empty. If the pane was captured and does not show the id, the function returns True even though a `turn_start` follows the send. I confirmed this by running it: it returned True with no transcript and a `turn_start` 5 s after the send. A real landed event on a busy session would then be re-sent.
- **Fix:** Return False when `reading.transcript_path` is unset or the tail may be truncated. Also return False when a `turn_start` exists at or after the send, since something started a turn.

### Issue 2: [severity: MEDIUM]

- **File:** `.claude/hooks/epic_driver_lib.py`, `_event_turns` / `_reconcile_verdicts` (~4538, ~4620)
- **Problem:** The turn's `stop` is the first stop at or after its `turn_start`, with no upper bound at the next `turn_start`. Esc fires no Stop (memory note). If the operator interrupts the event turn and types another, the later turn's `stop` and message are read as the event turn's verdict. This gives a spurious `VERDICT?` reminder, or a wrong `verdict_missing`. It also stretches the "operator turn exempt" rule.
- **Fix:** Accept a stop only if no other `turn_start` lies between the start and the stop, and treat the interrupted turn as unsettled.

### Issue 3: [severity: LOW]

- **File:** `classify_pane` (~4059)
- **Problem:** A conversation row such as `> quote` followed by a line indented two columns (a bullet) classifies the pane as a dialog. It matches the pointer regex, which accepts `>`, plus an option-like sibling. I confirmed it with a synthetic pane. The effect is an alarm, never an answer, unless a listed regex also matches. It happens only while the launch prompt is unconfirmed.
- **Fix:** Require `❯` rather than `>` for the pointer, or require the hint line or a numbered or short option row.

### Issue 4: [severity: LOW]

- **File:** `_reconcile_dialogs`, `record_dialog_answer`
- **Problem:** The "once per launch and dialog id" rule holds only because 50-06 calls `record_dialog_answer` before sending. The pure pass emits the same `startup_dialog_answer` item on every pass until then.
- **Fix:** Make the 50-06 brief and its tests bind this explicitly. Alternatively record the answer in the returned state when the item is emitted.

### Issue 5: [severity: LOW]

- **File:** `tests/fixtures/epic_driver/panes/cc-2.1.330-startup-no-dialog.txt`
- **Problem:** The banner shows the account tier ("Claude Ultra") and a login-expiry line. Not a path, name or email, but it is account detail in a committed fixture.
- **Fix:** Replace the tier text with a neutral string.

### Issue 6: [severity: LOW]

- **File:** `launch_entry` / `_needs_capture`
- **Problem:** When a session record has no `launch_event`, startup-dialog capture depends on a fallback that matches the earliest outbox send at or after the launch. If 50-06 never writes `launch_event` and there is no such entry, no pane is ever captured and a startup dialog is invisible.
- **Fix:** Make `launch_event` a 50-06 test obligation.

## Code Quality Notes

- Tests exercise the real code through a `FakeWorld`/`ReconWorld`, with a purity test (input state unchanged, results identical across calls). Each assertion checks values.
- `is_limit_entry` requires `is_api_error` as well as `error == "rate_limit"`. That matches §12.4's parenthesis. It does not classify by text.
- `session_stuck` on an owner process that cannot be found counts as "no tool shell". That can false-alarm, but it is alarm-only.
- The worker-verb restriction (a worker may end only DONE or HALT) goes beyond §10's literal text but is consistent with §14.3.
- Reading pane and tail sources are bounded. Compaction and model-check detection share the same 1 MiB tail limit as Issue 1, so a very busy session between ticks could miss a first reply or a boundary.

## Questions for User

None.

## Full suite

See the appended line below.

Summary line (python3 tests/run_all_tests.py, foreground-equivalent run): `Ran 2770 tests in 534.225s` / `OK (skipped=2)`.
Tree after review: `git status` and `git diff --stat` unchanged from the start (4 modified files, 3271 insertions, 16 deletions, fixtures untracked); lib restored byte-identical after the mutations (diff clean).
