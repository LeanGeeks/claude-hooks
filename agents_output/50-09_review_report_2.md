# Review Report - 50-09 Role prompts (second pass)

## Verdict: PASS

All 8 first-pass issues are resolved as decided. No BLOCKER, HIGH or MEDIUM issues. Two LOW observations.

## Full suite (run first, before any mutation)
`python3 tests/run_all_tests.py > /tmp/rev2_suite.log` (foreground), summary lines:
`Ran 3042 tests in 452.423s` / `OK (skipped=2)`, exit code 0.
`TestRolePrompts`: 12 tests OK.

## First-pass issues
1. Per-item-kind list: RESOLVED. The kinds built in `_item_lines` (ITEM_ROLE, HANDOFF, STEP_DUE, CHECK_FAILED, WORKER_SETTLED, REVIEW_DUE, FEEDBACK, ANSWER, APPROVAL_NEEDED, ALARM_CLEARED, RESUME, ROTATE, QUEUED) are all covered in driver section 1 (role/handoff in the bullet above the list; review_due/feedback marked worker-only). Verdicts match the grammar.
2. Feedback routing: RESOLVED. Driver section 5 sends to `worker.implementer` from the ledger, never the reviewer role. `queue_request` accepts both implementer and reviewer, so the warning is accurate. The CLI syntax matches argparse (`queue <step> --to --file`; `mark --grant ... --question`).
3. `git -C` everywhere: RESOLVED. Commit, merge, merge --abort, push and rev-parse all carry `-C`. The path source is real: `status_data` prints no worktree path (verified); `integration_dir()` is `<worktree_root>/integration/<repo name>`; `state.worktrees.integration[name]` is written at line ~7465. Step branch name `epic-step/<epic>/<step>` matches the code.
4. Merge-conflict rule: RESOLVED (`merge --abort` in the integration worktree, then `HALT: merge conflict in <step>`).
5. Worker ref-moving list and "FAIL review still DONE": RESOLVED (worker section 2, items 1 and 4).
6. Force/delete bans: RESOLVED. `-f`, `--delete`, `:branch` and `+refspec` are named in driver sections 3 and 7 and worker sections 2 and 4.
7. Tests: RESOLVED, see below.
8. Line counts: driver 159, worker 91 (the 250 from `wc` is the sum). Both under 250.

## New contradictions
None. `merge --abort` is scoped by "(the integration worktree is driver-owned)" and the operator-checkout ban stays in section 3. The `sleep 1` exception is explicit and single-shot, consistent with the loop ban. `epic/<epic>[:<target>]` has a non-empty source, so it does not conflict with the `:branch` deletion ban. Every command is valid against the argparse in `.claude/bin/epic-driver`.

## Mutations (cp backup, restore from copy, `diff` clean after each)
- M1 recommends `push --force-with-lease`: `test_force_flags_are_only_prohibited` FAILS. Good.
- M2 drops `-C` from the push line: `test_closing_commands_carry_dash_c` FAILS. Good.
- M3 untagged block with `ADVANCED:` lines: `test_verdict_shaped_blocks_are_tagged` FAILS. Good.
- M4 removes "uncommitted" from the worker never-commit rule: `test_worker_has_never_commit_rule` FAILS. Good.
`git status` is unchanged after the mutations.

## Issues
- LOW: `test_no_push_without_dash_c` does not catch M2 on its own, because the driver section 7 line also contains "never a bare `git push`" and so counts as a prohibition. The needle test `test_closing_commands_carry_dash_c` is what fails. Per-line heuristics (already acknowledged by the fixer for a recommendation sharing a line with "never") remain a known limit.
- LOW: worker section 3 says workers use only `DONE` and `HALT`, but section 5 tells them to end a `rotate` turn with `HANDOFF:`. Pre-existing; the event's Reply rule for a rotate event names HANDOFF, so it works in practice, but a cold reader sees a small inconsistency.

## Final tree
`git status --short`: ` M tasks/50_epic_driver/architecture.md`, ` M tasks/50_epic_driver/state.md`, ` M tests/test_unit_epic_driver.py`, `?? docs/prompts/epic-driver-driver.md`, `?? docs/prompts/epic-driver-worker.md` (plus this report). No non-temporary file edited by this review; nothing committed.

## Questions for User

None.
