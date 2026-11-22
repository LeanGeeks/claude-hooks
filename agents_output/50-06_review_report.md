# Review Report - 50-06 Dispatch: session policy (D4) and delivery (D5)

## Verdict: PASS

No BLOCKER or HIGH issues. 0 BLOCKER, 0 HIGH, 0 MEDIUM, 4 LOW.

Full suite, foreground, run first and before any mutation: `python3 tests/run_all_tests.py` gives
`Ran 2848 tests in 395.738s` / `OK (skipped=2)`. Module `unit_epic_driver`: 307 tests OK.
`py_compile` of the changed files is clean (the module imports; the suite passes).
End state: `git status` shows the same 3 modified files + 8 untracked fixtures (11 entries) as at the start;
`git diff --stat` is 3 files, +3158/-9. All mutations restored from `cp` backups and confirmed with `diff`.

## Completeness Check

1. Event files (§9.1): met. `build_event`/`render_event`/`_commit_event` (epic_driver_lib.py, 50-06 sections); one driver event and one per worker per pass, `next_event_n` numbering, `events/E-<n>.md` written by `execute_dispatch` before the commit.
2. Policy (§11.2): met. `choose_action` checked row by row against the table: limited holds, no record spawns, dead resumes (even cold), dead with a completed handoff spawns gen+1, not idle waits, `compacted` or `C >= cap` rotates, TTL >= 5 min reuses, overage-cold reuses in place (m2), 1 h-bucket cold rotates. Tests: `TestPolicy` has one test per row.
3. Rotation (§11.3): met. `rotate`, `spawn_next`, `handoff_complete`, 2-tick timeout with `force_reuse`, parking, `reap` only after the launch entry is confirmed (first `turn_start`) and never while `attached` is >0 or unknown; `reaping_at` is committed before `amux-spawn rm`. Tests: `TestRotation` (full sequence, busy session, attached delays reaping, timeout fallback).
4. Resume (§11.4): met. `resume_args` = `resume <name> --json -- "<line>"`; verified against the real `.claude/bin/amux-spawn` parser (`_split_prompt`, `build_parser`) by `test_the_resume_form_matches_amux_spawns_parser`. After-reboot selection uses 50-05's `RebootFacts.resume_now` plus dead sessions with an inflight/unconfirmed event; others resume lazily on their next event (`test_after_a_reboot_...`).
5. Spawn: met. `spawn_args` has `--dir --yolo --model= --effort= [--profile] --run-id [--pin-env K=V sorted] --detach -- "<EVENT line>"`; the role prompt is named by the leading `role` item of the seed event file (§19 says exactly this); name per P0-1 via `session_name_for`; `name_in_use` is committed as `launch_error` (reason `name_in_use`) and surfaces as `spawn_failed`, and no spawn effect is planned.
6. Keep-warm (§11.5): met. 40-50 min window, `k*B/C` against `next_scheduled_event`, Opus 40 / Fable 80, suppressions for overage, limit, `keep_warm: false`; `last_ping_at` is committed before the ping and rolled back only when the pane check refused. `TestKeepWarm`.
7. Delivery (§9.2, §9.3, M6): met. Idle, `pane_in_mode` 0, input box empty always (attached or not), unparseable pane defers, a dialog pane is never a target; the pane is re-read immediately before every send/key in `run_effect`. Single retry gated by 50-05's `delivery_provably_absent`, plus an empty box, plus `len(sends) < 2`; otherwise a committed "no retry" decision and `unconfirmed_at` on the following tick, no further sends (Binding b). `VERDICT?` commits `reminded_at` before the send (Binding a).
8. Commit points (§23): met. `DispatchOp` = (files + mutation, effect, result mutation); `execute_dispatch` writes files, mutates, COMMITs, runs the effect, applies the result, COMMITs. Every effect kind was checked: send (`sends` stamp), retry (`sends` + `retry`), reminder (`reminded_at`), ping (`last_ping_at`), spawn / rotation spawn / resume (`spawning`/`resuming` + `launch_event` + outbox entry + inflight markers in the same commit), dialog key (`dialog_answers`), reap (`reaping_at`). No effect has its mutation committed after it.
9. Startup dialog (§12.11): met. `record_dialog_answer` is committed before the key; a second pass emits nothing; the key guard re-checks that the same dialog still shows. Tests in `TestStartupDialogAnswer` plus the crash matrix.
10. `feedback` (OQ-4): met. `queue_request` validates step/role/absolute path and stores `state.queue`; `feedback_items` turns it into an event under the same policy (`test_feedback_from_a_queue_request`).

## Issues Found

### Issue 1: [severity: LOW]
- **File:** .claude/hooks/epic_driver_lib.py, `_Dispatch.relaunch` / `_Dispatch.session` branch A
- **Problem:** A spawn or resume whose result keeps failing (any reason other than `name_in_use`) is re-issued on every tick with the same event and no attempt cap or backoff (`launch_attempts` is incremented but never read). The alarm (`spawn_failed`/`resume_failed`) is raised, so it is visible, and each failed attempt is rolled back by amux-spawn so invariant 2 holds, but the retry is unbounded.
- **Fix:** Cap on `launch_attempts` (or back off by time since `launch_error.at`) in 50-08 or a follow-up.

### Issue 2: [severity: LOW]
- **File:** .claude/hooks/epic_driver_lib.py, `_Dispatch.actions` (`keyed`)
- **Problem:** When the send-time guard refuses the dialog key (copy-mode, the dialog is gone, another dialog shows), the answer stays recorded (`not_sent`), so the key is never retried for that spawn/resume and dialog id. This is faithful to "at most once", but a dialog that was refused because of transient copy-mode is left unanswered until the 50-05 startup_dialog alarm is acted on.
- **Fix:** Document the behaviour, or clear the record when the guard refused (`ran` False) so the next pass may retry; the key was never sent so invariant 2 is not at risk.

### Issue 3: [severity: LOW]
- **File:** .claude/hooks/epic_driver_lib.py, `_Dispatch.retry_decisions`
- **Problem:** The retry effect uses `line=e.get("line")` with no fallback; an outbox entry without `line` would send an empty `amux send <name> ""`. 50-06 always writes `line`, so only a hand-edited or foreign entry hits it.
- **Fix:** Fall back to `event_line(event, path)` as `deliver` does.

### Issue 4: [severity: LOW]
- **File:** .claude/hooks/epic_driver_lib.py, rotation timeout (`session()` branch B)
- **Problem:** After `handoff_timeout` the fallback to Reuse lasts one pass (`force_reuse`). While the session stays compacted or over the cap, the next event rotates again, so each event can produce a rotate + 2-tick timeout cycle. The alarm is raised each time; no double send results.
- **Fix:** Remember the timeout (it is in `handoff_timeout`) and suppress re-rotation for some interval, or leave to operator policy; confirm this is intended.

## Invariant traces

- **Invariant 2 (send paths):** event send (`deliver`): recorded in `sends` before the effect; only `_unsent` entries are delivered; the pane is re-read right before `amux send`. Retry: only when `delivery_provably_absent` is True, the box is empty, and fewer than 2 sends; the retry age gate (120 s from the last send) applies. Not provable: a "no retry" decision is committed, then `unconfirmed_at` at the next tick, no send. `VERDICT?` once (`reminded_at`). Ping once per reply (`last_ping_at`). Rotate event is an ordinary single send. Resume/spawn prompt: `sends=[stamp]` committed with `launching`; a refused or failed launch clears `sends` (amux-spawn rolled back). Dialog key: once per (launch, dialog). No path found that sends one event more than twice or sends twice with an earlier send unresolved.
- **Invariant 5:** see Completeness item 8. Crash matrix: 7 scenarios (spawn, send, retry after a lost send, resume, rotation spawn, dialog, reminder), each killed at every commit in both "after" and "before" mode, then 4 further passes, asserting no event lands twice, at most two sends per event, no name spawned twice, at most one dialog key, and no lost or markerless inflight step. The tests really kill between commit and effect (a `Killed` raised from the commit callback, with the fake `World` recording effects).

## Mutation checks (each restored from a `cp` backup and confirmed with `diff`)

| Mutation | Result |
|---|---|
| Retry gate: `delivery_provably_absent` removed | killed: `test_a_line_sitting_in_the_input_box_is_not_resent`, `test_binding_b_...` |
| Commit after effect in `execute_dispatch` | killed: crash matrix (3 errors + 1 fail), the reminder and dialog-key tests |
| Input-box parser: no box found reads as empty | killed: `test_an_unparseable_pane_defers`, `TestInputBox.test_fixtures` (shell-mode, splash), `test_pane_ready_reasons` |
| Input-box parser: first row only | killed: multi-line paste, wrapped-40col, synth multi-line fixtures |
| Policy: overage-cold no longer reuses (m2) | killed: `test_overage_cold_reuses_in_place` |
| Policy: `compacted` ignored | killed: `test_compacted_rotates` |
| `reminded_at` never set | killed: `test_binding_a_...`, `test_a_reminder_the_pane_refuses_...` |

I also probed the parser directly: empty box (NBSP and spaces), a `>` glyph, a 9-row and 20-row draft, a 30-row draft (unreadable, so it defers), a glyph followed by text only on row 2, and a stale empty box above an unclosed one (unreadable). All are safe (defer), none read as empty wrongly.

## Fixtures

The 8 new `cc-2.1.330-*` captures cover typed, multi-line paste, pasted placeholder, event line in the box, 40-col wrap, empty-after-clear, copy-mode and shell mode, and each is pinned by `TestInputBox.test_fixtures`. The provenance note (throwaway tmux server, hooks disabled, never submitted, server killed) is in the test file header. Grep of the new fixtures for `/home/`, user names, emails, tier names and project names: no hits; paths are `/tmp/epic-driver-fixture`.

## Code Quality Notes

- `_input_box_rows` was refactored onto `_input_boxes`; 50-05's classifier tests still pass (suite green), no regression found in 50-04/50-05 sections. The two 50-05 tests the implementer changed (`test_no_session_pane_or_notify_effects`, `TestPaneClassifier.test_fixtures`) were widened in a justified way (effect names confined to `RealWorld`/`run_effect`).
- `_find_entry`-based mutations make the ops safe to apply to the state as it is at run time, which is what lets a crashed pass resume cleanly.
- `queue_request` ids come from `len(queue)+1` with a collision loop, so ids can be reused after completed requests are removed; harmless but worth knowing for 50-08's CLI output.

## Leftover tmux sessions

`tmux ls` (read-only): no throwaway session from the implementer. The only epic-50 entry is `amux-claude-hooks-epic50-mgr`, the manager's own session; I did not touch it.

## Questions for User

None.
