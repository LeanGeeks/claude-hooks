# Fix Report - 41-05

## Issue 1 (MEDIUM) - fixed
- Change: tests/test_unit_settings_root.py, new class `TestPermissionRequestHookStoresRoot` (before `TestMcpClassification`). Drives `permission_request_hook.main()` in-process (bypassPermissions path, so no Telegram; isolated state store; real `create_request` wrapped by a spy; no real ~/.claude writes) with cwd=proj/sub.
- Tests: `test_row_stores_project_dir_root_from_subdir` (CLAUDE_PROJECT_DIR=proj -> stored row.settings_root == proj, row.cwd == sub); `test_row_stores_git_top_level_without_project_dir` (git fallback).
- Mutation proof: deleted `settings_root=settings_root,` (permission_request_hook.py:1649). Both new tests FAILED (21 run, 2 failures). Restored by cp, diff identical, git status identical before/after.

## Issue 2 (LOW) - not changed
Optional cosmetic cleanup in pretool_hook.py replay_from_log; left as is (harmless).

## Suite
Ran 2073, OK (skipped=2). Log: /tmp/41-05-fix-suite.log

## Questions for User
None
