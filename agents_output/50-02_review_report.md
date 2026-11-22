# Review Report - 50-02 Lifecycle: clear the `terminated` latch

## Verdict: PASS

Compile: `python3 -m py_compile .claude/hooks/claude_event_reducer.py` ok.
Suite: `python3 tests/run_all_tests.py` -> `Ran 2488 tests in 348.626s`, `OK (skipped=2)`. Baseline 2482 + 6 (5 reducer replacing 1, +1 producer, +1 derived status).

## Focus 1: the inverted test (justified)
`git show HEAD:tests/test_unit_claude_reducer.py` line 228: events were `[turn_start, session_end, turn_start]`. The last `turn_start` comes AFTER the `session_end`, so P0-4 requires the new outcome (running, not terminated). The inversion is justified, and no `turn_start, session_end` (turn open before the end) case was inverted. In the reducer, `session_end` sets `terminated=True, turn_open=False` (unchanged), so a turn opened before the end stays latched. Traced: `session_end` -> terminated; `session_end, turn_start` -> running; `+stop` -> idle; `+session_end` -> terminated; partial tail after `session_end` -> `reduce_log` drops the unparsed tail line, so the latch holds (test covers it, asserts `truncated_tail`). The case `turn_start, session_end` alone is not tested explicitly (see LOW 1) but the code path is unchanged.

## Completeness Check
- Reducer clears latch on `turn_start` (claude_event_reducer.py:168): met. Diff is one line plus a comment; no other branch changed.
- Pure stdlib: met (imports are `__future__`, json, pathlib, typing).
- Scope 2, producer: `handle_user_prompt_submit` writes `state="running"` unconditionally (spawn_producer_hook.py ~312); pinned by the new test, which starts from a stored `terminated` handle. Met.
- Scope 2, derived status: the new test stores `spawning`, patches a live pane, logs `session_end, turn_start`, and expects `running` via `cli._derive_status`. `_derive_claude_status` (amux-spawn:2280) reduces the log via `_claude_reduce` -> `reduce_log`, so the state comes from the reducer. Met. 50-01's resume writes `spawning` (amux-spawn:1402).
- Scope 3: reducer docstring table and a new paragraph updated; `docs/amux-spawn-fleet-supervision.md` gained a paragraph. Grep of docs/code for "permanent/forever/stays terminated/latch" found no other doc describing the latch as permanent (the hits are unrelated Codex "never latch" or relay waiter text). Met.
- Existing suite otherwise untouched: only the one justified test replaced.

## Focus 5: other consumers
`reduce_log` is called only from `amux-spawn` (`_claude_reduce` at 2173, and the `--since` activity check at 3078, which uses only `last_event_ts`, unaffected). No keep_awake, statusline or hook calls the Claude reducer. Codex has its own reducer, not touched. No unintended behaviour change.

## Issues Found

### Issue 1: LOW
- **File:** tests/test_unit_claude_reducer.py
- **Problem:** No explicit test for `turn_start, session_end` (no stop) staying terminated; it is the case the replaced test's name hinted at. Code path is correct, just unpinned.
- **Fix:** Add a one-line test asserting `state_hint == "terminated"` for `[turn_start, session_end]`.

### Issue 2: LOW
- **File:** .claude/bin/amux-spawn:2280
- **Problem:** `_derive_claude_status` still short-circuits to `terminated` when the stored handle state is `terminated`, ahead of the reducer. A resumed session reads correctly only because 50-01's resume writes `spawning` and `UserPromptSubmit` writes `running`. A session that ended (stored `terminated`) and then continues in a live pane reads `terminated` until its first prompt hook rewrites the stored state. This matches the task's design (scope 2 says no change), so not a defect; worth noting for 50-05.
- **Fix:** None required.

## Code Quality Notes
- state.md status moved to `in_progress` (fine for the in-flight task).
- Reminder: installed hooks need `./install.sh` to go live; the reducer is read by the installed `amux-spawn`. Implementer report did not need to flag this as a blocker.
- `git status` clean apart from the six modified files; I mutated nothing.

## Questions for User
None.
