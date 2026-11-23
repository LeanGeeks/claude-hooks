# 50-13 fix report

## Item 1: F-3 operator decision (trust the state root, no per-repo gate)
- .claude/hooks/epic_driver_lib.py `trust_folders`: now returns only the workspace and the driver state root. The
  per-repo entries and the `worktree_root` entry are removed. `trusted_by`, `trust_refusal` and gate 4 in `enable` are
  unchanged (exit 3 with fix text).
- tests/test_unit_epic_driver.py: removed `test_gate_4_checks_worker_repos`; replaced `test_trust_folders_of_a_ledger`
  with a test that expects only workspace + state root.
- docs/epic-driver.md: §3 enable list and §4 "Folder trust" say trust the workspace and the state root once, which
  covers the worker worktrees. They say that if a worktree still shows the dialog, `startup_dialog` reports it.
- tasks/50_epic_driver/architecture.md §18.1 gate 4: aligned. Worker repos are not gated; open point noted.
- tasks/50_epic_driver/50-12-signoff-evidence.md: the preamble note no longer says to trust each repo. The check 9 note
  is now an open point to observe live: after the state root is trusted, does a new worktree still show the dialog?
  Fallback: the `startup_dialog` alarm.

## Item 2: LOW notes
1. worktree_root over-requirement: fixed by removing it. Gate 4 now covers exactly the workspace and the state root,
   as the brief says.
2. `trusted_by` abspath/realpath leniency: fixed. It uses realpath only. The "worktree's own key accepted" leniency
   is left. It is harmless now that worker repos are not gated, and a git worktree's own key is a valid trust
   key in the non-rootOnly case.

## Suite
python3 tests/run_all_tests.py: Ran 3100 tests in 601.433s, OK (skipped=2)
