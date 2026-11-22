# Implementation Report - 50-05 Session reconcile and stall detection

## Summary

Added the session reconcile to `.claude/hooks/epic_driver_lib.py`. It has two halves. `take_readings` measures every session record, its parked generations and every handle of the run id, all through `World`. `reconcile` is a pure function of the readings, the ledger, the state and `now`. It returns a new state plus §16.2 alarm conditions, event items and action items for 50-06.

The reconcile covers:
- liveness, from tmux and the reducer, never from the stored state alone;
- §9.3 delivery confirmation and the "provably did not land" test;
- the full §10 verdict grammar, its invalid cases and exemptions, and how a verdict is applied to the state;
- usage limits: reset-time parsing in the system timezone and the ladder;
- stuck sessions, permission prompts and `AskUserQuestion` open over 10 min;
- the model check and compaction after a spawn or resume;
- clock jumps and reboots;
- foreign handles and orphan sessions (alarm only, never adopted);
- startup dialogs (§12.11), with a pane classifier pinned by captured fixtures;
- §15.2 question statuses.

Nothing is sent, spawned or notified.

## Files Created

- `tests/fixtures/epic_driver/panes/*.txt`: nine pane fixtures for the startup-dialog classifier. Provenance is given below.

## Files Modified

- `.claude/hooks/epic_driver_lib.py`:
  - New constants block (50-05). New `World` and `RealWorld` read-only methods.
  - Six new delimited sections: Session readings, Verdicts, Delivery confirmation (reading side), Usage limits, Startup dialogs, Session reconcile.
  - Module docstring table updated.
  - `import claude_event_reducer as cer` (pure stdlib, one of §17's listed imports).
  - `STEP_TRANSITIONS` changed: `halted → verifying/waiting/asked/halted/done` (§16.2: `halt` clears on "a later verdict on the step") and `verifying → halted` (so a driver `HALT` on a verifying step raises `halt`). `halted → due` is still refused, as the 50-04 test pins.
- `.claude/hooks/amux_spawn_lib.py`, additive only:
  - `assistant_entries(path, since=None)` and `user_prompt_entries(path, since=None)` (bounded tail, oldest first).
  - A `_summarise_assistant` helper shared with `last_assistant_entry`, whose output is unchanged.
  - `inflight_foreground_tool` gains a `timestamp` key (the tool_use entry's time), which `ask_user_question_open` ages. amux-spawn's `reason_context.foreground_tool` gains the same key. No test asserted the exact dict.
- `tests/test_unit_epic_driver.py`:
  - 50-05 section: `ReconWorld(FakeWorld)`, the `_Recon` base, and the classes `TestVerdictGrammar`, `TestDeliveryConfirmation`, `TestPaneClassifier`, `TestReconcileVerdicts`, `TestLiveness`, `TestStuck`, `TestUsageLimits`, `TestModelCheck`, `TestCompaction`, `TestOpenPrompts`, `TestClock`, `TestForeignAndOrphans`, `TestStartupDialogs`, `TestQuestionReconcile` and `TestReconcileContract`.
  - Two 50-04 contract tests updated:
    - The import allowlist gains `claude_event_reducer`.
    - The effects test now allows tmux only for the read verbs `display-message` and `capture-pane`, through `RealWorld._tmux_read`. It still forbids `send-keys`, `new-session`, `kill-session`, `"amux"`, `"amux-spawn"`, notify and systemctl.

## Verification

- Compile/import: PASS. `py_compile` on all three changed Python files. The module also passes under Python 3.9.21: `unit_epic_driver`, 228 tests at that point, OK, with zoneinfo and the vendored tomli.
- Tests:
  - Full suite `python3 tests/run_all_tests.py`, final run: **2770 tests, OK (skipped=2)**, 0 failed. `--module unit_epic_driver`: 229 OK.
  - The count change is accounted for: HEAD has 144 tests in `test_unit_epic_driver.py`, now 229 (+85). 2685 + 85 = 2770.
  - Related modules green: `unit_amux_reads` 61, `unit_measurement_helpers` 31, `unit_python_compat` 15 (1 skipped), `unit_amux_supervise` 21, `unit_amux_watch` 40, `unit_keep_awake` 34.
  - Mutation checks (cp backup, then restore, byte-identical by `cmp`, no git checkout). Each mutation made at least one test fail:
    - the fence rule;
    - the two-lines rule;
    - the limit-before-missing rule;
    - the grant check;
    - answer once per launch;
    - stuck suppression after a clock jump;
    - limit classification by the error field only.
- Installed (install.sh --yes re-run): not applicable. Nothing is wired into a hook; the installer feature is 50-10's.

## Public API (the contract for 50-06 and 50-08)

**World additions** (base methods raise `NotImplementedError`; all are reads):

| Method | Returns |
|---|---|
| `tmux_has_session(name)` | bool |
| `tmux_display(name, fields)` | `{field: str}` or None |
| `capture_pane(name)` | str or None |
| `list_handles()` | list of handle dicts |
| `lifecycle_events(name)` | list of records (a partial tail line is skipped) |
| `amux_names()` | list of names |
| `process_work(pane_pid)` | `ProcessWork(owner_pid, tool_shell)`, via `keep_awake_hook.descendants` / `_looks_like_claude` / `running_work` |
| `clock_sample()` | `ClockSample(boot_id, wall, boottime, monotonic)` |
| `local_timezone()` | tzinfo (`system_timezone()`: `$TZ`, then `/etc/localtime`, then the fixed offset) |
| `question_entries(workspace)` | `QuestionsSnapshot(open_word, resolved_words, withdrawn_word, entries: {qid: QuestionEntry(qid, status, text, path)})` or None |

`question_entries` uses `questions_store.open_store(ws, roles_path=<the workspace's own .claude/roles.toml>)`. The `roles_path` is passed so a `CLAUDE_PROJECT_DIR` in the caller's environment cannot redirect the reader to another queue.

**Readings:**
- `take_readings(ledger, state, world, now) -> Readings(sessions, handle_names, amux_names, clock, tz, questions, questions_error)`.
- `SessionReading` has these fields: `name`, `kind` (`SESSION_RECORD`, `SESSION_PARKED` or `SESSION_FOREIGN`), `key`, `handle`, `tmux_present`, `pane_dead`, `pane_in_mode`, `attached`, `pane_pid`, `events`, `transcript_path`, `assistant`, `prompts`, `compactions`, `context_tokens`, `cache_left_s`, `cache_ttl`, `activity_at`, `owner_pid`, `tool_shell`, `foreground_tool`, `pane_text`, `captured`, `expected_model_id`, `handoff_mtime`. It also has the property `.alive`.
- The pane is captured only when the launch prompt is unconfirmed or a send is unconfirmed.
- Helpers: `session_status(reading)` → `ST_DEAD`, `ST_STARTING`, `ST_BUSY` or `ST_IDLE`. Also `session_launch_at(rec, handle)`, `launch_entry`, `launch_prompt_unconfirmed`, `reduction_of`, `permission_open_since`, `is_limit_entry` and `first_model_entry`.

**Verdicts:**
- `parse_verdict_line(line) -> [Verdict(verb, id, shas, text, question, raw)] | None` checks syntax only.
- `verdict_line_of(message) -> (line, invalid)`.
- `read_verdict(message, *, steps, approvals, from_driver, rotate, grants, worker_repos, ledger_ids) -> VerdictReading(line, invalid, verdicts, covered, wait, unverified, halt, handoff)`.
- Invalid reasons: the `INVALID_*` constants.

**Delivery:**
- `delivery_confirmation(entry, reading) -> {"prompt_at", "turn_start_at"} | None`.
- `delivery_provably_absent(entry, reading) -> bool`. An uncaptured or unreadable pane, or the event id anywhere on the pane, is not proof.

**Limits:**
- `parse_reset_time(text, observed_at, tz)`.
- `limit_record(entry, prev, tz, session)` returns `{until, source: "reset"|"ladder", ladder_step, entry_at, session, text}`.
- `LIMIT_LADDER_S`.

**Startup dialogs:**
- `classify_pane(text) -> PaneClass(kind, detail)`, where `kind` is `PANE_NO_DIALOG`, `PANE_DIALOG` or `PANE_UNREADABLE`.
- `match_startup_dialog(ledger, text)`.
- `dialog_answered(rec, launch, id)`.
- `record_dialog_answer(rec, launch, id, key, now)` is **the state mutation 50-06 commits before sending the key**. It appends `{launch, dialog, key, at}` to `rec["dialog_answers"]`.

**Reconcile:** `reconcile(ledger, state, readings, now) -> ReconcileResult`:
- `state`: a new state; the input is not mutated.
- `conditions`: `[AlarmCondition(id, subject, detail)]`. These are the tuples below.
- `items`: `[EventItem(kind, to, step, data)]`, where `to` is a session key.
- `actions`: `[ActionItem(kind, session, name, data)]`.
- `sessions`: `{key: SessionFacts}`.
- `reboot`: `RebootFacts(resume_now, resume_lazily)` or None.
- `clock_jump`, `confirmed`, `verdicts` (a log) and `notes`.

`SessionFacts` fields: `alive`, `status`, `died`, `came_up`, `turn_open`, `permission_pending`, `attached`, `pane_in_mode`, `context_tokens`, `cache_left_s`, `cache_ttl`, `overage`, `launch_at`, `launch_unconfirmed`, `pane`, `limited`, `on_limit_entry`, `model_verified`, `compacted`, `tool_shell`, `rotation_ready`.

Condition tuples:

| id | subject | raised when |
|---|---|---|
| `session_stuck` | session name | §12.3; no new raise in the pass that saw a clock jump or reboot; an already raised one is kept |
| `permission_open` | session name | the reducer has a prompt pending > 10 min (from the `permission_prompt` event's ts) |
| `ask_user_question_open` | session name | an `AskUserQuestion` tool_use with no result, > 10 min old |
| `usage_limit` | limits key | the role's `expected_model_id`, else its alias |
| `model_mismatch` | session name | the first real assistant model after the launch ≠ expected |
| `compaction` | session name | `rec.compacted` (set by a boundary at or after the launch) |
| `startup_dialog` | session name | detail = the dialog's first lines plus "(not in startup_dialogs)" or "(still showing after its listed key)"; an unreadable pane keeps an already raised alarm |
| `foreign_handle` | handle name | |
| `orphan_session` | name | |
| `verdict_missing` | event id | |

Event items:
- `answer` → `driver`: `{question, approval, answer, path, asked_event}`, plus `step`.
- `worker_settled` → `driver`: `{role, worker: implementer|reviewer, verdict, event}`, plus `step`.
- `review_due` → `<reviewer>:<step>`: `{implementer, verdict, event}`.

Action items:
- `verdict_reminder`: `{event, missing, invalid}`. 50-06 sends `VERDICT?` and sets `reminded_at`.
- `startup_dialog_answer`: `{dialog, key, launch, detail}`.

**State read or written** (the 50-06 contract):

Outbox entries:
- Read: `event`, `session`, `steps`, `sends`, `kinds`, `needs_verdict`, `reminded_at`.
- **New, read:** `approvals`, the approval ids an event carries (approval_needed, answer).
- Written: `confirmed_at`, and `verdict = {line, at, covered, missing, invalid, turns, after_reminder, limited_at, complete}`.

Session records:
- Read: `name`, `spawned_at`, `resumed_at`, `parked`, `rotation`.
- **New, read:** `launch_event`, the E-n of the seed or resume prompt, which 50-06 writes with the record. There is a fallback to the earliest outbox entry sent at or after the launch.
- Written: `status` (`live`, or `dead`; a `spawning`/`resuming` record is left alone while its session is absent), `model_verified`, `model_check`, `compacted`, `compacted_at`, `rotation.handoff`, `dialog_answers`.

Step records:
- Written: `status`, `inflight = None`, `last_verdict`, `advanced_at`, `done_at`, `by`, `due_since`, `wait`, `shas`, `questions`.

Also written: `state.questions`, `state.approval_question`, `state.limits` and `state.clock`.

## §16.2 rows fed by this task: raising and clearing tests

| Alarm | Raised | Cleared |
|---|---|---|
| `session_stuck` | `TestStuck.test_stuck_after_no_growth_and_no_tool_shell` | the turn closes, in `test_suppressed_for_the_tick_of_a_clock_jump_and_cleared_when_the_turn_closes` |
| `permission_open` | `TestOpenPrompts.test_permission_prompt` | `test_closed_prompt_clears` |
| `ask_user_question_open` | `test_ask_user_question_in_flight` | `test_answered_ask_user_question_clears` |
| `usage_limit` | `test_limit_entry_raises_holds_dispatch_steps_the_ladder_and_clears` | same test: the reset passed and a turn completed |
| `model_mismatch` | `test_mismatch_raises_and_holds_until_rotated` | same test: rotated |
| `compaction` | `test_after_the_spawn_raises_once_before_is_ignored` | same test: the rotation completes |
| `startup_dialog` | `test_unlisted_dialog_raises_and_is_never_answered`, and `test_listed_dialog_answered_once_then_the_alarm` | the dialog is gone, in the listed-dialog test |
| `foreign_handle` | `test_foreign_handle_raises_and_clears_when_gone` | same test |
| `orphan_session` | `test_orphan_session_by_the_grammar` | same test |
| `verdict_missing` | `test_merged_event_partly_covered_gets_one_reminder_then_verdict_missing` | same test: a later turn naming the event covers the rest |
| `halt` (step row, fed by verdict application) | `test_driver_halt_raises_halt_and_a_later_verdict_clears_it` | same test: a later event's verdict |

Rows not fed here:
- `delivery_deferred` and `delivery_unconfirmed` are raised by 50-06, which counts sends and deferrals. This task supplies `confirmed_at` (a late confirmation is what clears `delivery_unconfirmed`) and `delivery_provably_absent`.
- `handoff_timeout`, `spawn_failed` and `resume_failed` are 50-06's.
- The run rows are 50-08's.

## Fixture provenance (Claude Code 2.1.330)

- **Real captures**:
  - `cc-2.1.330-folder-trust.txt` (120x40) and `cc-2.1.330-folder-trust-wide.txt` (200x50): a throwaway Claude in a new detached tmux session in a fresh `/var/tmp` directory, which is not trusted. The default option is `❯ No, exit`.
  - `cc-2.1.330-startup-no-dialog.txt`: a fresh session in a `/tmp` subdirectory, which is trusted. It shows the banner "Code Mode (experimental) is on", which a listed `code-mode` regex must not match; a test pins that.
  - I created both throwaway sessions myself, sent them no keys and killed only them. Their directories were removed. Launching them added project entries for those temp paths to `~/.claude.json`, as Claude Code always does.
  - Temp paths are scrubbed to `/tmp/epic-driver-fixture` and `/var/tmp/epic-driver-fixture`.
- **Real frame with replaced content**: `cc-2.1.330-idle-input-box.txt`. The bottom rows were captured read-only (`tmux capture-pane -p`) from an existing session, and the conversation text above them is neutral replacement text. The input box row is `❯` followed by U+00A0.
- **Synthesised**:
  - `synth-code-mode-optin.txt`: the "Code Mode (experimental)" opt-in from the lead's description, Enter = "No, start without it", in the real folder-trust layout. It could not be reproduced here because `codeModeCaveatsAcceptedV1` is already set, and I did not change the config.
  - `synth-input-box-typed.txt` and `synth-input-box-multiline.txt`: built from the real idle frame.
  - `synth-booting-splash.txt` (the splash before the input box) and `synth-blank.txt`.
- A test checks that no fixture contains `$HOME` or the temp-dir names.

## Decisions

1. **A verdict's turn is found from the lifecycle log, not only from `last_message`.** Three kinds of turn count for an event:
   - the first `turn_start` after the first send;
   - the first one after `reminded_at`;
   - any turn whose prompt names `E-<n>` (its `turn_start` within 5 s of the prompt).

   The message is the handle's `last_message` when that turn is the latest. Otherwise it is the `last_message` of the turn's `stop` record, whose tail the lifecycle log keeps. This way an operator turn between two ticks cannot erase the event turn's verdict. A turn whose prompt carries no event id never answers one, so operator turns and pings are exempt by construction. A verdict is read once a turn has a `stop`, rather than on "idle", so background work does not delay it.
2. **Limit before missing.** A turn whose last assistant entry is a limit entry records `limited_at`. It gets no reminder and no `verdict_missing`. A session whose newest entry is a limit entry is also not `session_stuck`: a limit may end a turn without a `stop`. Classification uses `error == "rate_limit"` **and** `isApiErrorMessage: true`, never the text.
3. **Reset times.** These forms are parsed:
   - `resets 3pm`, `resets 3:30pm`, `resets at 15:00`;
   - `resets Nov 27, 5pm`;
   - the dated GLM form `reset at YYYY-MM-DD HH:MM:SS`, read in the system timezone.

   A zone in parentheses, as in the real "resets 9:40pm (Asia/Yerevan)", is honoured when zoneinfo resolves it; otherwise the system zone is used. A time of day already past at the entry's own timestamp rolls to the next day. A bare "resets 3" is not a time.
4. **Ladder and clearing.** The `limits` key is the role's `expected_model_id`, else its alias, which matches 50-04's `model_limited`. Each new limit entry while the record exists steps the ladder; the same entry seen again does not. The record, and with it `usage_limit`, is removed once `now ≥ until` and a session of that model has a non-error assistant entry after `until` that a `stop` closed. It is also removed when no session of the model remains. The dispatch hold (`model_limited`) ends at `until` even while the alarm stays raised.
5. **Verdict application.**
   - `ADVANCED` on a `failed` step moves it to `due` (a retry, §6.1, read literally). The attempt is not counted again, because 50-04 already counts it at check rc 2.
   - A driver step with no `check` goes to `done` with `by: "verdict"` (§5.4 "done (unchecked)"). That is a fourth value of `by`.
   - A worker step's `ADVANCED` must carry a sha per repo (§14.4). One repo accepts a bare sha or `name=sha`. The shas are stored in `steps[*].shas` for 50-07.
   - An `ADVANCED` on an approval is covered only when `state.grants` has it; otherwise that id alone stays missing.
   - A structural error invalidates the whole line.
   - An unprefixed `WAITING` covers every event id not otherwise covered.
   - A `WAITING` prefix naming a ledger id that is not in the event is invalid.
   - Workers may end only `DONE` or `HALT` (§14.3). A worker `WAITING`, `ADVANCED` or `ASKED` is invalid; otherwise a worker step would move into a checked status without the driver.
   - Any covering verdict clears the step's inflight marker, the worker's included.
6. **Events with no ids** (rotate, alarm_cleared) need one valid line. For a rotate event that line must be `HANDOFF`.
7. **Stuck.** Activity is the newest mtime of the transcript and its subagent transcripts (`keep_awake_hook.transcript_activity`). A prompt waiting on a human (permission or AskUserQuestion) has its own alarm and is not counted as stuck. An owner that cannot be found under the pane counts as "no tool shell". A clock jump stops new raises for that pass but keeps a raised alarm, so it does not clear and re-raise.
8. **Startup dialogs.**
   - Classified only while the launch prompt is unconfirmed, after this pass's confirmations.
   - A dialog is a pointer row outside the input box, with an option row beside it at the option indent or a menu key hint. The input box is the prompt-glyph row directly under a rule and closed by another rule.
   - "Once per spawn/resume" is keyed by the launch time (the latest of `spawned_at` and `resumed_at`). A resume therefore gets one more answer.
   - The ledger default is empty, so nothing is answered unless listed.
9. **Orphans.** Names are matched exactly by the §3 grammar with a free `g<gen>`: the driver `basename(workspace)-<epic>-driver-g<n>`, and each worker step's implementer and reviewer. "Known" means record names and parked names. Handles of any run id exclude a name from the orphan list.
10. **Session record status.** An alive session is `live`. A `live` record whose session is gone becomes `dead` (`facts.died`). A `spawning` or `resuming` record whose session is absent is left for 50-06's crash-recovery logic (invariant 5).
11. **Purity.** `reconcile` deep-copies the state through JSON and touches no `World`. Tests check that the input is unchanged and that results are identical across calls.

## Blockers

None.

## Questions for User

None.
