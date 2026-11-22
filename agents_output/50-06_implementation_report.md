# Implementation Report - 50-06 Dispatch: session policy (D4) and delivery (D5)

## Summary

I added the dispatcher to `.claude/hooks/epic_driver_lib.py`. It has six new delimited sections:

- Input box
- Events
- Session policy
- Dispatch plan
- Dispatch execution
- Dispatch conditions

`plan_dispatch` is pure. It returns `DispatchOp`s, and each op is one `(state mutation, effect)` pair plus the files its commit writes and its result mutation. `execute_dispatch` (50-08's tick calls it) runs each op in this order: write the files, mutate, COMMIT, run the effect through `World`, apply the result, COMMIT.

All four Binding obligations are implemented, and each has its own test. Crash safety is checked by a matrix: seven scenarios, killed at every commit, both after the commit and as it is written, then four more passes each.

## Files Created

- `tests/fixtures/epic_driver/panes/cc-2.1.330-input-typed.txt`, `-input-multiline-paste.txt`, `-input-pasted-placeholder.txt`, `-input-event-line.txt`, `-input-wrapped-40col.txt`, `-input-empty-after-clear.txt`, `-copy-mode-empty-box.txt`, `-shell-mode.txt`: real captures of the input box. Provenance is given below.

## Files Modified

- `.claude/hooks/epic_driver_lib.py`:
  - A 50-06 constants block.
  - Five `World` effect methods: `spawn`, `resume`, `reap`, `amux_send`, `send_keys`. The base methods raise `NotImplementedError`; `RealWorld` implements them.
  - The six sections listed above, and the docstring table.
  - `_input_box_rows` is now built on a new `_input_boxes` (same behaviour), so the 50-05 classifier and the new input-box parser share one box finder.
  - New stdlib imports: `shutil`, `socket`, `typing.Callable`.
- `tests/test_unit_epic_driver.py`:
  - The 50-06 section: `DispatchWorld(ReconWorld)`, the `_Disp(_Recon)` base and these classes: `TestInputBox`, `TestPolicy`, `TestEventsAndSpawn`, `TestRotation`, `TestResume`, `TestDelivery`, `TestStartupDialogAnswer`, `TestKeepWarm`, `TestCrashSafety`, `TestDispatchContract`.
  - Two 50-05 tests updated:
    - `test_no_session_pane_or_notify_effects` now allows `"amux"`, `"amux-spawn"` and `"send-keys"`, each named exactly once and run only inside `RealWorld`. The five `world.<effect>(` calls must appear exactly once each, inside `run_effect`. `new-session`, `kill-session`, `questions_mcp_lib`, `.notify(` and `systemctl` are still forbidden.
    - `TestPaneClassifier.test_fixtures` lists the eight new fixtures.

`tasks/50_epic_driver/state.md` shows as modified in `git status`. That change (50-06 marked `in_progress`) was there before I started; I did not touch the file.

## Verification

- Compile/import: PASS. `python3 -m py_compile` on both changed files. The module also imports and passes under **Python 3.9.21**: `unit_epic_driver`, 307 tests OK, using pip's vendored pure-Python tomli on `PYTHONPATH`.
- Tests:
  - `python3 tests/run_all_tests.py --module unit_epic_driver`: **307 tests, OK**. That is 243 before, plus 64.
  - `--module unit_python_compat`: 15 OK (1 skipped).
  - Full suite, foreground, `python3 tests/run_all_tests.py`: **Ran 2848 tests, OK (skipped=2)**, exit 0. That is the baseline of 2784 plus the 64 new tests.
- Mutation checks (cp backup, restored, then `cmp` byte-identical after each run). Every mutation below made at least one test fail:

  | Mutation | Killed by (among others) |
  |---|---|
  | a send recorded only after the effect | `test_killed_between_every_commit_and_its_effect`, `test_confirmed_on_the_first_send` |
  | spawn without `launch_event` | `test_a_fresh_session_is_spawned_seeded_with_its_event`, `test_the_full_sequence` |
  | dialog answer not recorded before the key | `test_a_crash_between_the_commit_and_the_key_never_sends_a_key` |
  | `reminded_at` not committed before the send | `test_binding_a_…` |
  | no `delivery_unconfirmed` after the retry decision | `test_binding_b_…`, `test_retry_once_…` |
  | retry without provable absence | `test_a_line_sitting_in_the_input_box_is_not_resent`, `test_binding_b_…` |
  | the send-time pane check off | 14 tests |
  | m2 dropped (overage rotates) | `test_overage_cold_reuses_in_place` |
  | a busy session's rotate counted as a deferral | `test_an_unsent_rotate_waits_for_a_busy_session` |
  | reap while attached | `test_an_attached_client_delays_reaping` |
  | `approval_needed` raised every pass | `test_approval_needed_is_raised_once_per_ask_cycle` |
  | every input box reads as empty | 25 tests |
- Tests touch only temp dirs (the state root is set per test) and the fake `World`. They use no real tmux, amux, `~/.claude` or network.
- Installed (install.sh --yes re-run): not applicable. Nothing is wired into a hook yet; that is 50-08 and 50-10.

## Public API (the contract for 50-08)

**`World`**: each method takes the argument list a pure builder made, and `RealWorld` only runs it.
- `spawn(args)`, `resume(args)` and `reap(args)` run amux-spawn with those args. The binary is `shutil.which("amux-spawn")`, else `~/.local/bin/amux-spawn`, because the unit's PATH may lack it. Timeouts: 180 s, 120 s and 60 s.
- `amux_send(name, line)` runs `amux send <name> <line>`, with a 30 s timeout.
- `send_keys(name, key)` runs `tmux send-keys -t =amux-<name>: <key>`. It refuses any key that is not `Enter` or `Escape`.
- All five return a `CommandResult` and never raise.

**Command builders:**
- `spawn_args(ledger, key, gen, directory, line)` returns `["spawn", <epic>-<role>[-<step>]-g<gen>, "--dir", dir, "--yolo", "--model=<m>", "--effort=<e>", ["--profile", p], "--run-id", run_id, ["--pin-env", "K=V"]…, "--detach", "--", "EVENT E-<n>: read <path>"]`.
- `resume_args(name, line)` returns `["resume", name, "--json", "--", "RESUME E-<n>: read <path>"]`. This is the correct CLI form: amux-spawn's `_split_prompt` takes everything after its own first `--` and passes it to `amux start` without `--`. A test loads the real `.claude/bin/amux-spawn` parser and checks both forms against it.
- `reap_args(name)` returns `["rm", name]`, never `--force`.

**Names and paths:** `session_key_for_step`, `session_dir_for(ledger, key, worker_dirs)`, `session_name_for`, `role_prompt_paths` (§19), `event_id`, `event_number`, `report_path` (`reports/<step>-<implementation|review>-<n>.md`), `handoff_path` (`handoffs/<role>[-<step>]-g<gen>.md`).

**Lines (§9.2):** `event_line`, `resume_line`, `rotate_line`, `reminder_line`, `PING_LINE`.

**Input box:**
- `parse_input_box(text) -> InputBox(state, text)`, where `state` is one of `BOX_EMPTY`, `BOX_TEXT`, `BOX_DIALOG`, `BOX_UNREADABLE`.
- `pane_ready(pane_in_mode, pane_text)` returns None, or the deferral reason.
- `session_ready(facts)` returns None, or the reason to wait.

**Items:**
- `step_due_items(ledger, state, selection, worker_dirs)`
- `check_failed_items(ledger, outcomes)`
- `approval_needed_items(ledger, state, selection)`
- `feedback_items(state)`
- `queue_request(ledger, state, step, to_role, file, now)`: OQ-4. The CLI calls this; it stores `state.queue[] = {id: "R-<n>", step, to, file, queued_at, event: None}` and raises `ValueError` for anything but a worker step's implementer or reviewer with an absolute path.
- Event files come from `PlannedEvent` and `render_event`.

**Policy:** `choose_action(ledger, state, key, facts, now, *, handoff_done) -> PolicyDecision(action, reason)`. The actions are `ACT_SPAWN`, `ACT_RESUME`, `ACT_SPAWN_NEXT`, `ACT_WAIT`, `ACT_ROTATE`, `ACT_REUSE` and `ACT_HOLD`. Helper: `handoff_complete(rec, reading)`.

**Keep-warm:** `keep_warm_check(ledger, state, key, facts, now) -> (bool, why)`, `next_scheduled_event`, `price_class`, `keep_warm_k`, `keep_warm_horizon_s`.

**Plan:**
```
plan_dispatch(ledger, state, recon, readings, now, *, paths, selection=None, check_outcomes=(), items=(),
              worker_dirs=None, host=None, paused=False)
  -> DispatchPlan(ops, events, decisions, dispatched_steps, completed, notes, preview)
```
- `state` is the reconciled state after the checks and `advance_schedule`. The plan does not change it.
- `items` takes 50-08's `alarm_cleared` items.
- `worker_dirs` is 50-07's `{step: dir}`. When it is given, a worker step without a directory is not dispatched. When it is None, the default is `<worktree_root>/<epic>-<step>`.
- `dispatched_steps` is what `note_undispatched` needs.
- `completed` holds the outbox entries that left (completed, superseded, or removed by `handoff_timeout`), for `logs/tick.log`.
- `preview` is the state after every mutation and before any result, for `--dry-run`.

**Execution:**
- `execute_dispatch(plan, state, world, commit) -> [EffectOutcome]` mutates `state` in place. `commit` is the tick's `save_state`.
- `run_effect(world, effect)` checks the pane again just before every send and key, and may refuse with `EffectOutcome(ran=False, deferred=why)`.

**Conditions:** `dispatch_conditions(ledger, state, now) -> [AlarmCondition]`. It covers `delivery_deferred:<event>`, `delivery_unconfirmed:<event>`, `handoff_timeout:<name>`, `spawn_failed:<name>` and `resume_failed:<name>`, all derived from the committed state.

### The `(mutation, effect)` shapes

Every mutation looks things up by key or event id, so it applies to the state as it stands when it runs. `t` is the pass's `now`.

| Op (label) | Mutation committed before the effect | Effect | Result mutation |
|---|---|---|---|
| `settle` | completed outbox entries leave; a launch whose session is up but whose entry has no send gets `sends=[launch time]`; `launch_error` is cleared once the launch is confirmed; `spawning` + session gone + handle present ⇒ `dead`; a sent, unconfirmed entry for a name that is no longer current ⇒ `unconfirmed_at`; consumed queue requests are dropped | none | — |
| `event E-n → <key> (queued)` | the event file; an outbox entry `{event, to, session, kinds, steps, approvals?, created_at, sends: [], deferrals (carried from folded entries), confirmed_at: None, verdict: None, reminded_at: None, needs_verdict: True, path, line, via: "send", report?, supersedes?}`; an inflight marker `{step, event, started_at, host, session}` per step; `due → inflight` (+ `started_at`) for `step_due`; `queue[].event`; `approval_raised[id]`; the unsent queued entries it folds leave | none | — |
| `send E-n → <name>` | `sends += [t]`, `session`, `rec.last_event`; for a rotate, `rotation.requested_at = t` | `SEND` (guarded) | guard refused: the send is removed, `deferrals += 1`, `last_deferral`, a rotate's `requested_at = None`; amux failed: the send stays, `send_error` |
| `defer E-n (<why>)` | `deferrals += 1`, `last_deferral {at, why}` | none | — |
| `spawn <name> seeded with E-n` | event + entry (`via: "spawn"`, `sends: [t]`) + markers + record `{name, gen, dir, status: "spawning", spawned_at, resumed_at: [], model_verified, compacted: False, parked (+ the old name on a rotation), rotation: None, launch_event, launch_via, launch_attempts: 1, launch_error: None, dialog_answers: [], last_event}` | `SPAWN` | ok: `launch_error = None`; failed: `launch_error {kind: "spawn", at, rc, reason: name_in_use\|failed, error}`, `sends = []` |
| `spawn <name> refused: name in use` | the same record and event, `launch_error.reason = "name_in_use"`, no send | none | — |
| `resume <name> with E-n` | event (role, the resume item, folded entries) + record `status: "resuming"`, `resumed_at += [t]`, `launch_event`, `launch_via: "resume"`, `rotation: None`, `last_event`; entry `sends: [t]` | `RESUME` | failed: `launch_error {kind: "resume", …}`; the send is removed unless amux-spawn said "resumed but … not growing" |
| `<spawn\|resume> <name> again with E-n` | `spawned_at = t` or `resumed_at += [t]`, `launch_attempts += 1`, `sends = [t]` | `SPAWN`/`RESUME` | as above |
| `event E-r → <key> (rotate)`, then `send` | event + `rotation {event, requested_at: None, handoff_path, ticks: 0}` | none, then `SEND` | as for `send` |
| `<key>: waiting for the handoff (tick k)` / `no handoff after 2 ticks` | `rotation.ticks`; on timeout: `handoff_timeout {at, event, requested_at}`, `rotation = None`, the rotate entry leaves | none | — |
| `retry E-n → <name>` | `sends += [t]`, `retry {at, sent: True}` | `SEND` (guarded) | guard refused: the send is removed, `retry.sent = False`, `why` |
| `E-n: no retry (<why>)` | `retry {at, sent: False, why}` | none | — |
| `E-n unconfirmed after the retry decision` | `unconfirmed_at = t` | none | — |
| `answer startup dialog <id> on <name> with <key>` | `record_dialog_answer(rec, launch, id, key, t)` | `KEYS` (guard: the same listed dialog still shows) | `sent_at`, or `not_sent: why` |
| `remind E-n on <name>` | `reminded_at = t` | `SEND` (guarded) `VERDICT? …` | guard refused: `reminded_at = None` (nothing was sent) |
| `ping <name> (<why>)` | `rec.last_ping_at = t` | `SEND` (guarded) `PING …` | guard refused: the previous `last_ping_at` |
| `reap parked <name>` / `<name> is gone: unpark` | `parked[].reaping_at = t` / the entry is removed | `REAP` / none | ok: unparked; failed: `reap_error` |

New state keys that 50-08's `status` and §8 may want:
- Outbox entries: `path`, `line`, `via`, `supersedes`, `last_deferral`, `retry`, `unconfirmed_at`, `send_error`, `report`.
- Session records: `launch_via`, `launch_attempts`, `launch_error`, `last_event`, `last_ping_at`, `handoff_timeout`, `rotation.ticks`, `parked[].gen/reaping_at/reap_error`, `dialog_answers[].sent_at/not_sent`.
- Top level: `queue`, `approval_raised`.

I did not edit architecture §8. The checkout is shared, and the task names only the lib and the tests.

## §11.2 rows, Binding obligations and the other required tests

| Requirement | Test(s) |
|---|---|
| no session yet ⇒ Spawn gen 1 | `TestPolicy.test_no_session_yet_spawns`; end to end `TestEventsAndSpawn.test_a_fresh_session_is_spawned_seeded_with_its_event` |
| dead, no completed handoff ⇒ Resume, even cold | `TestPolicy.test_dead_resumes_in_place_even_cold`; `TestResume.test_a_dead_session_with_a_step_in_flight_resumes_in_place` |
| dead, a completed handoff ⇒ Spawn gen + 1 | `TestPolicy.test_dead_with_a_completed_handoff_spawns_the_next_generation` |
| alive, turn open ⇒ wait | `TestPolicy.test_busy_waits`; `TestDelivery.test_a_busy_session_is_waited_for_not_deferred` |
| `C ≥ cap` ⇒ Rotate | `TestPolicy.test_over_cap_rotates` |
| `compacted` ⇒ Rotate | `TestPolicy.test_compacted_rotates` |
| TTL ≥ 5 min ⇒ Reuse | `TestPolicy.test_warm_reuses` |
| cold only from overage ⇒ Reuse in place (m2); over the cap still rotates | `TestPolicy.test_overage_cold_reuses_in_place` |
| cold on the 1 h bucket ⇒ Rotate (incl. 4 min left) | `TestPolicy.test_cold_on_the_hour_bucket_rotates` |
| limited ⇒ hold (also with no session) | `TestPolicy.test_limited_holds`, `TestEventsAndSpawn.test_a_held_item_is_still_committed_into_an_event` |
| launch pending / rotation under way ⇒ wait | `TestPolicy.test_a_launch_not_up_and_a_rotation_under_way_wait` |
| **Binding: `record_dialog_answer` committed before the key; a second pass after the commit emits no item and sends no key** | `TestStartupDialogAnswer.test_the_listed_key_is_sent_once_recorded_before_the_send`; `test_a_crash_between_the_commit_and_the_key_never_sends_a_key` |
| **Binding: `launch_event` in the same commit as `spawning`/`resuming`: spawn, rotation spawn, resume** | `test_a_fresh_session_is_spawned_seeded_with_its_event`; `TestRotation.test_the_full_sequence` (the commit that names `…-g2`); `TestResume.test_a_dead_session_with_a_step_in_flight_resumes_in_place` |
| **Binding (a): `reminded_at` committed before the `VERDICT?` send; the next pass emits no reminder and sends nothing** | `TestDelivery.test_binding_a_the_reminder_commits_reminded_at_before_the_send_and_only_once` (it also kills the pass between the commit and the send); `test_a_reminder_the_pane_refuses_is_not_sent_and_not_recorded` |
| **Binding (b): `delivery_provably_absent` False ⇒ no retry; at the following tick `delivery_unconfirmed` and no further sends** | `TestDelivery.test_binding_b_no_retry_when_absence_is_not_provable`; `test_a_line_sitting_in_the_input_box_is_not_resent` |
| Rotation: the full sequence, queued events move, reap after the first `turn_start` | `TestRotation.test_the_full_sequence` |
| Rotation: the timeout and fallback to Reuse | `TestRotation.test_no_handoff_after_two_ticks_falls_back_to_reuse` |
| Rotation: an attached client delays reaping | `TestRotation.test_an_attached_client_delays_reaping` |
| Rotation: an unsent rotate waits for a busy session | `TestRotation.test_an_unsent_rotate_waits_for_a_busy_session` |
| Resume after reboot: at once vs on the next event | `TestResume.test_after_a_reboot_only_the_sessions_that_should_resume_at_once_do` |
| `resume_failed`; retried with the same event | `TestResume.test_a_failed_resume_is_resume_failed_and_tried_again_with_the_same_event`, `test_resumed_but_not_growing_keeps_the_send` |
| `name_in_use` ⇒ `spawn_failed` | `TestEventsAndSpawn.test_name_in_use_is_spawn_failed_and_the_same_event_is_tried_again`, `test_a_name_known_to_amux_is_never_spawned` |
| copy-mode defers | `TestDelivery.test_copy_mode_defers` |
| typed text defers, attached and detached; multi-line, paste placeholder, wrapped | `TestDelivery.test_typed_text_defers_attached_and_detached`, `test_a_typed_draft_seen_by_the_readings_defers_without_a_send_commit` |
| an unparseable pane defers | `TestDelivery.test_an_unparseable_pane_defers` |
| 3 deferrals ⇒ condition; cleared once delivered | `TestDelivery.test_three_deferrals_raise_delivery_deferred_until_delivered` |
| confirm on the first send | `TestDelivery.test_confirmed_on_the_first_send` |
| retry once when absent; no third send; a late confirmation clears | `TestDelivery.test_retry_once_when_provably_absent_then_no_third_send` |
| a line sitting in the input box is not re-sent | `TestDelivery.test_a_line_sitting_in_the_input_box_is_not_resent` |
| no event typed while a dialog shows (at plan and at send time) | `TestStartupDialogAnswer.test_no_event_is_typed_while_a_dialog_shows`, `test_the_key_is_not_sent_once_the_dialog_is_gone` |
| Keep-warm: 40–50 min window; `k·B/C` in/out; Fable vs Opus `k`; overage, limits and `keep_warm: false` suppress; a window opening is the next event; one ping | `TestKeepWarm` (all seven tests) |
| Crash safety: never two landed sends of one event, no name spawned twice, no lost marker | `TestCrashSafety.test_killed_between_every_commit_and_its_effect` (spawn, send, retry, resume, rotation spawn, dialog key, reminder; every commit, both modes, then 4 passes); `test_the_uncrashed_scenarios_make_progress` |
| Event files: front matter, numbering from `next_event_n`, one driver event and one per worker, the §19 role item, a worker's report and `DONE` rule | `test_a_fresh_session_is_spawned_seeded_with_its_event`, `test_one_driver_event_per_pass_merges_every_item`, `test_one_event_per_worker_and_a_worker_report_path` |
| `feedback` from `epic-driver queue` (OQ-4) | `TestEventsAndSpawn.test_feedback_from_a_queue_request` |
| `check_failed`, `worker_settled` report path, `approval_needed` once per ask cycle | `test_check_failed_and_worker_items_carry_what_the_driver_needs`, `test_approval_needed_is_raised_once_per_ask_cycle` |
| The input-box parser on every fixture | `TestInputBox` |
| Purity, paused, the `World` contract, the amux-spawn CLI form | `TestDispatchContract` |

What the kill matrix shows (it is conservative by design):
- A retry killed after its commit never goes out; the next pass raises `delivery_unconfirmed`.
- A reminder killed after its commit is never sent.
- A dialog key killed after its commit is never sent, and the dialog then raises `startup_dialog`.

In every case the commit counts as the attempt, as invariant 5 requires. This leans toward too few sends, never too many.

## Fixture provenance (Claude Code 2.1.330, the installed version)

- **The eight new files are real captures.** I started a throwaway Claude Code in a **separate tmux server** (`tmux -L edr5006`, its own socket, a new detached session) in a fresh `/tmp/epic-driver-fixture.XXXXXX`, with `--settings '{"disableAllHooks": true}'` so no hook or status line fired.
  - Text was typed with `send-keys -l` and pasted with `load-buffer` and `paste-buffer -p` (bracketed paste). It was never submitted: no Enter was ever sent, so no turn ran.
  - Copy-mode came from `tmux copy-mode`, and the 40-column wrap from `resize-window`.
  - I killed only that server, removed its directory, and sent no key to any other session.
  - Disabling hooks also hid the custom status line. This makes no difference to the box rule, which the existing 50-05 frame `cc-2.1.330-idle-input-box.txt` (status line present) also pins.
- **Scrubbed:** the temp path became `/tmp/epic-driver-fixture`, "Claude Ultra" became "Claude", and the login-expiry line became "Example notice line". The existing scrub test covers all fixtures.
- **Reused from 50-05:** the idle frame, the startup frame, the dialogs, and the synthesised typed, multi-line, splash, blank and quote panes.

## Decisions

1. **Busy is a wait, not a deferral.** §9.3 lists "idle" as precondition 1, but §11.2 says "alive, turn open: wait (the outbox keeps the event)". Only a pane that refuses the line counts a deferral: copy-mode, mode unknown, a non-empty box, a dialog, or an unreadable pane. Otherwise a single 30-minute turn would raise `delivery_deferred`.
2. **The pane is checked twice.** It is checked on this pass's readings when they captured it. It is checked again inside `run_effect` just before every send or key, because 50-05 captures the pane only while a launch or a send is unconfirmed, and an operator can start typing at any moment.
   - The send is committed (`sends += [t]`) before that check. If the check refuses, nothing reached the pane, so the result mutation takes the send back and counts a deferral.
   - A send that ran stays recorded even when `amux send` fails, since part of it may have landed.
3. **One pending event per session.** New items fold the unsent events queued for the session into one new event, through `queued` reference items that point at the older event files, and the deferral count carries over. Launch prompts and rotate events are never folded.
   - Every item is committed into an event in the pass that produced it, whatever the policy says (hold, wait, paused), because 50-05 emits `answer`, `worker_settled` and `review_due` only once.
4. **Launches.**
   - A launch's send is recorded before the effect and taken back when amux-spawn failed. The exception is a resume that says "resumed but … not growing": that session is running with the prompt.
   - A `spawning`/`resuming` record whose session is not up is launched again with the same event each pass. This covers a pass killed before the launch, and a failed launch. `name_in_use` still blocks a duplicate.
   - A name already known to amux or to a handle is not passed to amux-spawn at all; it is recorded as `spawn_failed` (`name_in_use`).
   - A `spawning` record whose handle exists but whose session is gone is `dead`: the spawn ran.
5. **The retry.**
   - A send younger than `DELIVERY_RETRY_AFTER_S = 120` (*default*) is not judged yet. A hand-run tick seconds later could otherwise retry a line the TUI has consumed but not yet written.
   - The decision is taken once. It is postponed only when another send already went to the session this pass, or the run is paused.
   - "The following tick" is any later pass after the decision.
   - A launch prompt gets the same single retry by `amux send` once the session is idle and absence is proven. There is no retry while a dialog shows.
6. **Resume.**
   - Besides 50-05's reboot selection, any dead session with a step in flight on it, or an event not yet confirmed, resumes at once; no other event would arrive for it.
   - A resume folds every incomplete entry of the dead session into the resume event, so a later reminder is not sent for an event the resume already covers. Markers are re-pointed to the resume event and keep their `started_at`.
7. **Rotation.**
   - The two ticks count from the rotate send (`requested_at` is set in the send commit), one per pass.
   - On timeout the rotate entry leaves the outbox and the queued event goes to the old session. A later event may request another rotation.
   - A session that dies mid-rotation without a handoff is resumed in place.
   - Reaping waits until the new generation's launch prompt is confirmed, which needs its first `turn_start`. It does not reap while attached, or while attachment is unknown, and never uses `--force`.
8. **Keep-warm.**
   - The last assistant entry's age is `ttl − cache_left` (the clock `cache_seconds_left` uses).
   - There is no second ping since the last reply.
   - The price class is Fable when the expected id, else the alias, contains "fable". So a `fable` alias that the profile maps to an Opus id is Opus-priced, `k = 40`.
   - The next event is computed from 50-04's `_time_conditions` and `_trigger_fired`. `step_due_at` cannot be used: it returns None both for "blocked" and for "no time condition".
   - Worker sessions have no scheduled event, so they are never pinged.
9. **Smaller choices.**
   - `approval_needed` is raised once per ask cycle (`state.approval_raised`). Without that, `select` would raise it on every pass until the driver asks.
   - A worker report is `reports/<step>-<implementation|review>-<n>.md`, where `n` is the number of that worker's event.
   - An event file a killed pass wrote but never committed is overwritten. No state named it, so nothing was sent for it.
   - Paused means events are written and no effect is planned.
   - Shell mode (`! `) reads as unreadable, and so defers.
   - A dim ghost suggestion in the box would read as text and defer. That is the safe direction.

## Blockers

None.

## Questions for User

None.
