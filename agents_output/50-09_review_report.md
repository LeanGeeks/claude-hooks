# Review Report - 50-09 Role prompts

## Verdict: PASS

No BLOCKER or HIGH issues. Three MEDIUM and five LOW findings, listed below. The MEDIUM ones are real gaps where a cold reader could act wrongly. They are worth fixing before 50-10 installs the prompts.

Basics: the changed Python test file compiles and imports. `python3 tests/run_all_tests.py` was run in the foreground before any mutation and exited 0. My `| tail -8` pipe cut off the summary line, so I did not capture the pass count. The new `TestRolePrompts` class alone ran 7 tests, all OK.

## Completeness Check

- Driver prompt (`docs/prompts/epic-driver-driver.md`, 149 lines): mostly met.
  - Event protocol, `role` and `handoff` items, merged events and the ping: met (§1).
  - Verdict grammar with one example per verb, a merged example, and the ping: met (§2).
  - Forbidden tools, and the force ban in all forms: met (§3). `-f` is not named (Issue 7).
  - D8 production writes with detached long actions: met (§4a).
  - Questions via `mcp__questions__ask` with a recommendation; approvals one at a time and `mark --grant`: met (§4).
  - Closing sequence, push rules, review loop and `queue`: met (§5-7), with Issues 2-3.
  - Never edit the ledger, epic records only in the integration worktree, dirty worktree only after asking, handoff only on a rotate event: met (§3, §5, §8, §9).
  - What to do on `step_due`, `check_failed` and `alarm_cleared`: partially met (Issue 1).
- Worker wrapper (`docs/prompts/epic-driver-worker.md`, 91 lines): met.
  - Reads the epic prompt at the path the event names, then overrides it.
  - Overrides: never commit, never ask (draft the question, end `HALT`), report at the event's path, end `DONE: <step>`.
  - Forbidden tools and the handoff (§1, §2, §4, §5).
  - m10 strength: the opener says the overrides "win on any conflict". Override 1 says "replaces any commit or push step". Critical rule 1 repeats it. This is strong enough that an epic prompt saying "commit your work" would not win.
  - The activecdn implementer rule 7 at line 135 is actually "Never commit", so the two prompts agree.
- Length: both under 250 lines. Style matches the house format.
- Test (`tests/test_unit_epic_driver.py`): met, with weak spots in Issues 6 and 8.

## Verification performed

- **Event kinds the driver can receive:** per the §9.1 table and `epic_driver_lib._item_lines`: `step_due`, `check_failed`, `worker_settled`, `answer`, `approval_needed`, `alarm_cleared`, `resume`, `rotate`, `queued`. All are named in §1.
- **Event kinds a worker can receive:** `step_due`, `review_due`, `feedback`, `resume`, `rotate`. All are named.
- **Hand-parsed examples** with `parse_verdict_line` and `read_verdict`:
  - Every valid example parses. The counter-examples are rejected.
  - `WAITING: <step> feedback queued to <role>` is valid and is read as a wait for that step.
  - Uppercase shas are rejected. `0123abc ` is accepted after the strip.
  - A trailing comma in a multi-repo sha list is rejected.
  - `HALT: x; ADVANCED: t1` parses as a single HALT, as documented.
- **CLI strings checked against argparse and the lib:**
  - `epic-driver mark --grant <id> --question Q-<n>` matches the argparse flags (`--grant`, `--question`).
  - `epic-driver queue <step> --to <role> --file <path>` matches `queue_request`. The role must be the step's implementer or reviewer. The path must be absolute. The step must be a worker step.
- **Paths and forms checked:**
  - Branches `epic-step/<epic>/<step>` and `epic/<epic>`, and the integration path `<worktree_root>/integration/<name>`, match §14.1.
  - The sha forms match `_parse_one` and §14.4.
  - The `role` item wording and the rotate and handoff wording match the code.
- **Mutations** (each prompt backed up and restored; `diff` confirmed restoration each time; `git status` is unchanged from the start):
  1. Invalid sha added to a driver `verdict` block: test fails.
  2. `--force-with-lease` removed from the worker prompt: test fails.
  3. `WAITING:` counter-example changed to a valid line: test fails.
  4. Driver prompt edited to recommend `git push --force-with-lease`: test still PASSES (Issue 6).

## Issues Found

### Issue 1: severity: MEDIUM
- **File:** docs/prompts/epic-driver-driver.md §1 and §5
- **Problem:** `step_due` (non-worker), `check_failed` and `alarm_cleared` are listed, but the prompt never says what to do for them.
  - It does not say to read the item's Card and carry out the step by the card and the epic's reviewed scripts.
  - It does not say that a `check_failed` card's rollback applies.
  - It does not say how to answer an `alarm_cleared`, for example by re-checking ground truth and ending with a verdict.
  - A cold reader has to guess. §9.1 says `check_failed` carries the card "(its rollback applies)", and the event text does say so, but the prompt does not tell the reader to follow it.
- **Fix:** add a short "per item kind" list to §1. Cover `step_due` (read the card), `check_failed` (the rollback applies, then retry or `HALT`), `alarm_cleared` (check the subject, then `WAITING` or `ADVANCED`) and `queued` (read the file and answer its steps).

### Issue 2: severity: MEDIUM
- **File:** docs/prompts/epic-driver-driver.md §5, the `queue` bullet
- **Problem:** the prompt says `<role>` is "the ledger name of the worker's role as shown in the item". A `worker_settled` item shows only the role that settled (`Worker: role (worker)`).
  - When the reviewer settles with issues, the item shows the reviewer role. The feedback must go to the implementer. `queue_request` accepts either role, so a reader who follows "as shown in the item" would queue the feedback to the reviewer. Nothing would fail; the loop would stall silently.
- **Fix:** say that review feedback goes to the step's `worker.implementer` role, read from the ledger, never the role shown on a reviewer's item. Feedback for a reviewer question goes to the reviewer role.

### Issue 3: severity: MEDIUM
- **File:** docs/prompts/epic-driver-driver.md §6 step 4 and §7
- **Problem:** the merge and push commands are written without `git -C <integration worktree>`: `git merge --no-ff epic-step/<epic>/<step>` and `git push <remote> epic/<epic>`. The surrounding text says to do it "in the integration worktree".
  - The driver's session directory may be the operator's checkout, or another repo. A reader who runs the bare command could merge in an operator checkout, which breaks invariant 6.
  - A merge with no `--no-edit` could also open an editor. A merge conflict has no stated action: abort in the integration worktree and `HALT`.
- **Fix:** write `git -C <integration worktree> merge --no-ff --no-edit epic-step/<epic>/<step>` and `git -C <integration worktree> push ...`. Add one line for conflicts. Also add "never run these from the operator's checkout" to the closing steps.

### Issue 4: severity: LOW
- **File:** docs/prompts/epic-driver-driver.md §6 step 6
- **Problem:** "wait a moment and retry once" sits next to the ban on sleep loops. A reader could take a single `sleep 1` as a violation, or a retry loop as allowed.
- **Fix:** write "retry once after one second (a single `sleep 1` is allowed here)".

### Issue 5: severity: LOW
- **File:** docs/prompts/epic-driver-worker.md §2 item 1
- **Problem:** the command list omits `git cherry-pick`, `git revert`, `git pull`, `git am`, `git tag` and `git branch`. The principle "never commit" probably covers them, but the list is explicit.
- **Fix:** add "or any other command that creates a commit or moves a ref". Also state that a FAIL review is still `DONE: <step>`; the report carries the verdict. §1 has "work only inside the worktree", which should say that reading the report and card paths is allowed.

### Issue 6: severity: LOW (test)
- **File:** tests/test_unit_epic_driver.py `test_forbidden_tools_and_force_ban_appear`
- **Problem:** it only checks that substrings appear. A prompt that recommends `--force-with-lease` still passes (mutation 4). There is no negative assertion that the prompts never tell the reader to commit or push forcefully.
- **Fix:** add assertions on the context, for example that every `--force*` occurrence is preceded by "never" on its line. Check that the worker prompt contains "NEVER COMMIT" and that no line outside a ban recommends `git push`.

### Issue 7: severity: LOW
- **File:** docs/prompts/epic-driver-driver.md §3 and §7, and the worker prompt §2 and §4
- **Problem:** the force-push bans list `--force`, `--force-with-lease`, `--mirror` and `+refspec`. The short form `-f` is not named. "In any form" covers it, but the task explicitly lists `-f`.
- **Fix:** add `-f`, and `--delete` or `:branch` refspecs.

### Issue 8: severity: LOW (test)
- **File:** tests/test_unit_epic_driver.py `_prompt_examples`
- **Problem:** extraction is by exact info string.
  - An example block tagged ```text is silently ignored. Inline placeholder verdicts such as `ADVANCED: <step> <sha>` are never parsed.
  - The extraction is robust to the nested four-backtick fence, to blank lines and to examples added later inside a `verdict` block. Mutations 1 and 3 confirmed this.
- **Fix:** optionally add a check that any fenced block whose lines start with a verdict verb is tagged `verdict` or `verdict-invalid`.

## Code Quality Notes

- Safety rules hold across both prompts, and nothing contradicts them:
  - Implementers never commit. Explicit staging excludes the question-queue directory.
  - The ledger is never edited. Grants come only through `mark --grant`, after the answer, one at a time.
  - Questions go only through `mcp__questions__ask`, with a recommendation.
  - Forbidden tools are listed. A dirty worktree is discarded only after asking. The handoff is written only on a rotate event.
  - I found no "push to main", "clean up the worktree" or "reset" wording that could be misread.
- §4a says production writes need "an approval granted by the operator". The spec (§19, D8) says "its standing approvals". Consider "approvals the epic's ledger or the operator has granted".
- The tasks edits (architecture §14.3 `queue --to <role>` and the state.md status) are consistent with the code.
- The checkout was left unchanged: after my restores, `git status` shows only the five files that were already modified or untracked, and the tree matches its state before my mutations.

## Questions for User

None.
