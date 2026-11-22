# Fix Report - 50-09 Role prompts

Files changed: `docs/prompts/epic-driver-driver.md`, `docs/prompts/epic-driver-worker.md`, `tests/test_unit_epic_driver.py` (class `TestRolePrompts`). Nothing committed.

## Per issue

1. MEDIUM, per item kind. Driver prompt §1 has a nested list: `step_due` (read the Card, do the step through the card and reviewed scripts), `check_failed` (card rollback applies, retry within the card's rules or `HALT`), `worker_settled`, `answer`, `approval_needed`, `alarm_cleared` (re-check ground truth, then `ADVANCED`/`WAITING`/`HALT`), `resume`/`rotate`, and queued items. Each entry names the expected verdicts. It also says `review_due` and `feedback` go to workers only. The item kinds were checked against `_item_lines` in `epic_driver_lib.py`.
2. MEDIUM, queue role. Driver §5: review feedback goes to the step's `worker.implementer` role from the ledger, never the role on a reviewer's `worker_settled` item. A reply to a drafted question goes to the role that asked. The syntax is `epic-driver queue <step> --to <role> --file <path>`.
3. MEDIUM, `git -C`. §6 and §7 now use `git -C <step worktree> commit`, `git -C <integration worktree> merge --no-ff --no-edit epic-step/<epic>/<step>` and `git -C <integration worktree> push <remote> epic/<epic>`.
   - Added: never run these from the operator's checkout or the session's own directory unless it IS the worktree. On a conflict, `git -C <integration worktree> merge --abort`, then `HALT: merge conflict in <step>`.
   - Source of the integration path: `integration_dir()` gives `<worktree_root>/integration/<repo name>`. `worktree_root` comes from the ledger. `epic-driver status` does NOT print it (checked `status_data`). The run's `state.json` records it under `worktrees.integration.<repo name>`. The prompt says so, and says to confirm the branch with `rev-parse --abbrev-ref HEAD`.
4. LOW: "retry once after one second (a single `sleep 1` is allowed here)".
5. LOW, worker prompt: added "any other command that creates a commit or moves a ref (cherry-pick, revert, pull, am, merge, rebase, tag, branch, reset, stash)". A FAIL review still ends `DONE: <step>`, because the report carries the verdict. Reading report, card and epic-prompt paths outside the worktree is allowed; writing only in the worktree and at the report path (§1 and §2 item 5).
6. LOW, test. New tests:
   - `test_force_flags_are_only_prohibited`: a line with `--force`, `-f`, `--force-with-lease`, `--mirror` or a `+refspec` must contain never/no/not/forbidden, or sit under a "Forbidden" heading.
   - `test_no_push_without_dash_c`: a line with `git push` must have `git -C` or be a prohibition.
   - `test_worker_has_never_commit_rule`.
   - `test_closing_commands_carry_dash_c`.
   - The existing needle `git merge --no-ff` became `merge --no-ff --no-edit`.
7. LOW: `-f`, `--delete` and `:branch` refspecs named in the driver §3 and §7 bans and in the worker §2 and §4 bans.
8. LOW, test: `test_verdict_shaped_blocks_are_tagged` fails when an untagged fenced block has lines starting with a verdict verb.

## Line counts
- `epic-driver-driver.md`: 159 lines (was 149).
- `epic-driver-worker.md`: 91 lines (unchanged).
- Both are under 250.

## Mutation 4 (prompt edited to recommend `--force-with-lease`)
- Backup with `cp` to `/tmp/drv.bak`; restored from the copy; `diff` printed nothing in both runs.
- Variant A: a standalone line "Prefer `git -C ... push --force-with-lease ...` after a rebase." `test_force_flags_are_only_prohibited` FAILS, as required.
- Variant B: the same text spliced into the line that already says "Never `--force`...". The test PASSES. The check is per line, so a recommendation sharing a line with a prohibition word is not caught. This is a known limit.

## Full suite
`python3 tests/run_all_tests.py` ran in the foreground, last, after all edits.
- Summary: `Ran 3042 tests in 437.870s` / `OK (skipped=2)`
- Exit code: 0. (Baseline 3037; +5 new tests.)
- `TestRolePrompts` alone: 12 tests, OK.

## git status --short
```
 M tasks/50_epic_driver/architecture.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
?? docs/prompts/epic-driver-driver.md
?? docs/prompts/epic-driver-worker.md
```

## Questions for User

None.
