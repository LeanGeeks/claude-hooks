# Implementation Report - 41-05 Settings follow the project root

## Summary

One resolver, `project_key.resolve_settings_root(cwd, env=None)` (`CLAUDE_WORKSPACE_DIR` -> `CLAUDE_PROJECT_DIR` -> `resolve_workspace_root(cwd)`, debug-logged on the last branch), now decides where the hook reads `.claude/settings*.json` and where the Whitelist button writes. The root is stored on the permission row as `settings_root`; the router card validator, the Whitelist write and the permissions MCP classifier use the row's value (old rows fall back to `resolve_workspace_root(row.cwd)`). The validator's `workspace_dir` stays the payload `cwd`.

## Files Created

- tests/test_unit_settings_root.py — 19 tests: the task table, resolver order, replay, row round-trip, Whitelist write location, card annotations, MCP classification.

## Files Modified

- .claude/hooks/project_key.py — `resolve_settings_root`, `settings_root_for_row`, debug logger.
- .claude/hooks/permission_state_store.py — optional `settings_root` field on `PermissionRequest` (after `owner_host`) and `create_request(settings_root=None)`. Follows the additive-field precedent (`permission_mode`): `from_dict` filters to known fields, `to_dict` is `asdict`, no schema version.
- .claude/hooks/pretool_hook.py — `main()` builds `SettingsLoader(settings_root)`, validator `workspace_dir` still `cwd`; debug-logs `CLAUDE_PROJECT_DIR` and the root. `replay_from_log`: `CLAUDE_WORKSPACE_DIR` first; else per-entry root from the logged `workspace` with `env={}`; one validator per distinct (workspace, root).
- .claude/hooks/permission_request_hook.py — resolves the root, debug-logs `CLAUDE_PROJECT_DIR`, passes `settings_root` to the permission-row `create_request` (AskUserQuestion child rows untouched).
- .claude/hooks/telegram_permission_router.py — `_bash_annotations` and `process_whitelist_update` use `settings_root_for_row(request.settings_root, request.cwd)`.
- permissions-mcp/permissions_mcp_lib.py — `_build_validator(cwd, settings_root=None)`; `classify` passes the row's root; `find_deny_collision` unchanged (no root -> reads the dir it is given).
- tests/run_all_tests.py — registers `unit_settings_root`.
- architecture.md — settings_loader line and a new "Settings root (epic 41)" subsection.

## Verification

- Compile/import: PASS (py_compile of all hooks, MCP lib, new test).
- Tests: full suite 2071 run, OK (skipped=2) — `python3 tests/run_all_tests.py`. Repo copy only.
- Installed (install.sh re-run): no (forbidden by the brief).

## Testing

- Baseline re-measured before the first edit at HEAD 10e2a0b: Ran 2052, OK (skipped=2).
- Final: Ran 2071 (+19 new), OK (skipped=2). No regressions.
- Skips by name (unchanged): `test_unit_python_compat.TestImportUnderOldInterpreter.test_all_hook_modules_import`, `test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`.
- Table coverage: all 10 rows. The redirect-gate row runs under /var/tmp because /tmp is itself an allowed redirect root (skips if /var/tmp is absent).

## CLAUDE_PROJECT_DIR observations

Not observable without installing: owed after install. Evidence found: the current agent process environment has no `CLAUDE_PROJECT_DIR` or `CLAUDE_WORKSPACE_DIR` (that is a Bash-tool child, not a hook process, so it says nothing about hooks). `~/.claude/bash_hook_debug.log` and `permission_state_debug.log` contain 0 occurrences (the lines did not exist before). After install, with `CLAUDE_HOOK_DEBUG=1`, each PreToolUse (`bash_hook_debug.log`) and PermissionRequest invocation logs `CLAUDE_PROJECT_DIR: <value or None>` and `Settings root: <path>`.

## Stray settings.local.json files

Survey under /data/sync/work of `.claude/settings.local.json` whose directory is below its git top level (read-only, nothing moved):
- /data/sync/work/dr_remi/hipaa_form/frontend-react/.claude/settings.local.json (git top /data/sync/work/dr_remi/hipaa_form) — the known candidate.
- /data/sync/work/asafechoice/formdr_replacement.bak-oct-14-2025/frontend-react/.claude/settings.local.json (git top /data/sync/work/asafechoice/formdr_replacement.bak-oct-14-2025) — a backup copy.
Whether the button or a launch there created them is unknown. Operator decides.

## Mutation proofs

Protocol: status before/after identical; `cp` backup, break, targeted test, restore with `cp` + `diff` (clean). No git restore/checkout used. (The "before" snapshot was taken after my edits, so "same" means the mutation round left the tree as it was.)
1. Whitelist write location (router: `update_settings_local_json(request.cwd, ...)` instead of the row root): `test_whitelist_tap_writes_at_stored_settings_root` ERROR and `test_whitelist_tap_on_old_row_writes_at_git_top_level` FAIL.
2. First table row (pretool `main()`: `SettingsLoader(workspace_dir)` instead of the root): `test_project_dir_settings_json_allows_from_subdir`, `..._settings_local_json_...`, `test_git_top_level_fallback_without_project_dir`, `test_worktree_uses_its_own_settings_...` FAIL.

## Live checks owed

- brd H1: after install, observe `CLAUDE_PROJECT_DIR` in PreToolUse and PermissionRequest hook env, main session and worktree session (debug log lines above). If absent, the git-top-level fallback applies; note that fallback follows `cd` into a nested repo.
- Whitelist-from-subdir end-to-end on the installed copy (41-04).

## Decisions

- Old rows in the MCP classifier also fall back to `resolve_workspace_root(row.cwd)` (same as the router) rather than raw `cwd`, so both processes agree. Behavior change only for old rows whose cwd is below a git top level.
- `CLAUDE_*_DIR` values are `abspath`'d, not realpath'd.
- Replay entry without a logged `workspace` uses `os.getcwd()`; with no env, `CLAUDE_PROJECT_DIR` never leaks into replay (tested).
- Added helper `settings_root_for_row` so the three row consumers share the fallback.

## Blockers

None (the H1 live check is owed, not a blocker, per the orchestrator's note).

## Questions for User

None.

## For 41-02

Resolver: `project_key.resolve_settings_root(cwd, env=None) -> str` (import it in `pretool_hook.py`; `main()` calls it as `settings_root = resolve_settings_root(workspace_dir)`). Row field: `PermissionRequest.settings_root: Optional[str]` (JSON key `settings_root`), set via `create_request(..., settings_root=...)`; read with `project_key.settings_root_for_row(row.settings_root, row.cwd)`. Add `write_roots` next to it the same way. `_build_validator(cwd, settings_root=None)` in the MCP lib is where `write_roots` would also be threaded.
