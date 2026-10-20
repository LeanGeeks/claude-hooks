# Review Report 2 - 41-05 Settings follow the project root

## Verdict: PASS

## Completeness Check
- MEDIUM from pass 1 fixed: met. tests/test_unit_settings_root.py `TestPermissionRequestHookStoresRoot` drives `permission_request_hook.main()` in-process (bypassPermissions, no Telegram), real `create_request` via spy, reads the stored row back with `get_request` and asserts `settings_root` (CLAUDE_PROJECT_DIR case and git-top-level case; row.cwd stays the subdir).
- Isolation: state/audit/debug/archive/manual-confirm-log env vars point to a temp dir (setdefault, as sibling modules). `~/.claude/bash_manual_confirm.log` stat identical before/after (mtime 22:06:17.06, 15027101 bytes).
- Rest of diff (re-read): resolve_settings_root order WORKSPACE_DIR > PROJECT_DIR > git top level; pretool hook, replay (env={} per entry), router annotations and Whitelist write, MCP classify all use the row/resolved root; additive `settings_root` field loads as None on old rows with `settings_root_for_row` fallback; project_key.py is already installed with permission-hooks, telegram and MCP groups (install.sh 134/264/328/1165). Fail-open: resolve_settings_root is inside the hook's existing try in both hooks; no posture change. No other SettingsLoader or settings-write callers missed (grep). architecture.md updated.
- Compile: py_compile of all changed files OK.

## Issues Found
None new. (Pass 1 LOW in replay_from_log accepted by orchestrator, not re-reported.)

## Code Quality Notes
Debug log in permission_request_hook is placed inside the main try, so a resolve failure fails open as before.

## Mutation proofs
Git status saved before and after; identical both times. Backups in /tmp/41-05-review2-mut/, restored by cp, diff identical.
1. Deleted `settings_root=settings_root,` from the `create_request` call in permission_request_hook.py: test_unit_settings_root 21 run, 2 failures (both new hook-path tests, `None != proj`). Restored.
2. In telegram_permission_router.py `process_whitelist_update`, replaced `settings_root_for_row(...)` with `request.cwd`: 21 run, 1 failure + 1 error (whitelist write lands in subdir, settings.local.json missing at proj). Restored.

## Suite
`python3 tests/run_all_tests.py`: Ran 2073, OK (skipped=2). Log /tmp/41-05-review2-suite.log.

## Questions for User
None
