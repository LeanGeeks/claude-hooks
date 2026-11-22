# 50-05 — Session reconcile and stall detection

**Status:** todo · **Depends on:** 50-01, 50-02, 50-03, 50-04
**Read first:** [brd.md](./brd.md) §3 (lessons 4, 5) · [architecture.md](./architecture.md) §10, §11.1, §11.6, §12
(all), §13.2, §14.3, §15.2, §16.2 (the session, event and run rows) · [state.md](./state.md) P0-3, P0-4, P0-9,
invariant 7 · review m4 · `.claude/hooks/keep_awake_hook.py` (`descendants`, `_looks_like_claude`, `running_work`,
`_subagent_dir`) · `.claude/hooks/amux_spawn_lib.py` (`inflight_foreground_tool`, `detect_open_turn`, `list_handles`,
`tmux_has_session`) · `.claude/bin/amux-spawn` (`_derive_claude_status`)

## Goal

Every pass, turn the ground truth about each driven session into facts the dispatcher and the alarm engine use:
alive or dead, idle or in a turn, the verdict it ended with, whether it is stuck, limited, compacted or on the wrong
model, and which questions resolved. Never trust a derived status where a direct reading exists (brd §3 lesson 4).

## Scope

In `epic_driver_lib.py`:

1. **Session readings** (§11.1): one `SessionReading` per session record and per handle of the run id, built through
   `World` (tmux display of `pane_dead`, `pane_in_mode`, `session_attached`, `pane_pid`; the derived state; the
   handle's `last_message`; the 50-03 helpers).
2. **Verdict parser** (§10): the full grammar, merged-event coverage, the invalid cases (code fence, two lines,
   unknown step, `HANDOFF` outside rotate, `DONE` from the driver), exemptions (no event id in the turn's prompt; the
   ping). Application to the state: `ADVANCED` → `verifying` (approval ids: verified against `state.grants`),
   `WAITING` → `waiting` with a parsed time when it is one, `ASKED` → `asked` + `state.questions`, `HALT` (driver →
   halt condition; worker → `worker_settled`), `DONE` (implementer → `review_due`; reviewer → `worker_settled`;
   §14.3), `HANDOFF` → rotation progress. Inflight markers cleared by the verdict.
3. **Delivery confirmation** (§9.3, reading side): a user entry containing `E-<n>` at or after the send plus a later
   `turn_start`; the "provably did not land" test used by the retry.
4. **Stalls** (§12.3–§12.8): stuck (open turn, no tool shell under the owner via `keep_awake_hook.running_work`, no
   transcript or subagent growth for `stuck_after`); usage limits from `error == "rate_limit"` only, reset-time parsing
   in the system timezone with roll-over, the fixed ladder, the per-model `limits` record, and the rule that the limit
   check precedes the missing-verdict rule; the model check after spawn/resume (`model_matches`); compaction
   boundaries after spawn/resume (§11.6); permission prompt / `AskUserQuestion` open > 10 min; clock (boot id, NTP
   jump, suspend) with stuck suppression for one tick; foreign handles and orphan sessions (alarm only, never adopt —
   review m4).
5. **Startup dialogs** (§12.11, lead note 2026-11-22): for a spawned/resumed session whose prompt is unconfirmed,
   classify the captured pane (no dialog / dialog / unreadable) with a pure parser pinned by captured fixtures of the
   installed Claude Code version (at least the "Code Mode (experimental)" opt-in and the folder-trust dialog);
   match it against the ledger's `startup_dialogs`; produce either the `startup_dialog` condition or, for a listed
   dialog not yet answered for this spawn/resume, a "answer once with `key`" item for 50-06. Never an answer for an
   unlisted dialog.
6. **Question reconcile** (§15.2): statuses through `questions_store.open_store(workspace).all_entries()` for the ids
   in `state.questions`; one `answer` event item per newly resolved id; withdrawn closes and frees
   `approval_question`.
7. Output: alarm **conditions** (`(id, subject, detail)`) and event **items** for the dispatcher; nothing is sent or
   notified in this task.

## Tests

Fixture transcripts, lifecycle logs, handles and captured `tmux display` values; a fake `World`:

- Verdicts: each verb; merged events fully and partly covered; inside a fence; two lines; trailing whitespace; an
  operator turn exempt; the ping exempt; `ADVANCED: SA4` with and without the grant.
- Liveness: dead pane, missing tmux session, `terminated` latch cleared by a resume (50-02) reads alive.
- Stuck vs a long tool shell (a `/shell-snapshots/` descendant present); subagent growth counts as activity;
  suppressed after a clock jump.
- Limits: a reset time in local 12-hour format ("resets 3pm"), "3:30pm", 24-hour, a past time rolled to tomorrow, no
  reset time (ladder steps), classification ignores look-alike text without `error: rate_limit`; a limited turn with
  no verdict is not "missing".
- Model check: alias through the profile mapping, mismatch raises, legacy handle without a model is skipped.
- Compaction after spawn raises once; one before the spawn is ignored.
- `AskUserQuestion` in flight 11 min raises; 9 min does not. Permission prompt likewise.
- Clock: boot id change ⇒ reboot facts (which sessions had a turn open or a step in flight).
- Orphan: a grammar name in `amux ls` with no record ⇒ condition, no adoption. Foreign handle ⇒ condition.
- Startup dialogs: an unlisted dialog ⇒ condition, no answer item; a listed one ⇒ one answer item, then the
  condition if it is still there next pass and no second item; a confirmed prompt ⇒ no capture classified; the input
  box and an unreadable pane are not dialogs; the dialog gone ⇒ cleared.
- Questions: resolved ⇒ one answer item, not repeated next pass; withdrawn ⇒ closed, no item.

## Done when

- The reconcile pass is a pure function of the readings, the ledger and the state, plus `now`.
- Every session/event/run alarm row of §16.2 that this task's readings feed has a raising and a clearing test.
