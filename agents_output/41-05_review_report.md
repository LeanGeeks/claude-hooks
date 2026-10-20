# Review Report - 41-05 Settings follow the project root

## Verdict: PASS

(0 BLOCKER, 0 HIGH, 1 MEDIUM, 1 LOW.) py_compile of all 6 changed files OK; imports OK. Full suite: Ran 2071, OK (skipped=2). ~/.claude/bash_manual_confirm.log mtime unchanged before/after; git status identical before/after all mutations and the suite.

## Completeness Check

- Resolver `resolve_settings_root` in project_key.py: met. Order CLAUDE_WORKSPACE_DIR -> CLAUDE_PROJECT_DIR -> resolve_workspace_root(cwd); not resolve_project_root; debug log on fallback branch only.
- pretool main(): met. SettingsLoader(settings_root); validator workspace_dir still cwd. Inside the existing try (fail-open preserved). Test `test_redirect_gate_still_uses_cwd_as_its_root` keeps `> ../x` as ask.
- replay_from_log: met. Override first; else per entry from logged `workspace` with env={} (CLAUDE_PROJECT_DIR cannot leak); validators cached per (workspace, root). Log entries do write `workspace` (pretool_hook.py:451).
- settings_root on row: met. Dataclass field with None default; store is JSON lines, `from_dict` filters by dataclass fields so old rows load (tested); to_dict is asdict. Set only on permission-row create_request (hook :1640); AskUserQuestion child (:1345) untouched. No SQLite/schema version in this store, so no migration needed.
- Router card validator and Whitelist write use `settings_root_for_row(row.settings_root, row.cwd)`; old rows fall back to resolve_workspace_root(cwd): met.
- MCP `_build_validator(cwd, settings_root=None)`: met; classify passes row root; find_deny_collision unchanged (test). Note: classify now uses git top level for old rows rather than cwd (consistent with D8/"fall back"; slight behaviour change, acceptable).
- Fail-open: resolver calls are in main()'s try in both hooks; permission_request_hook imports project_key at top level but install.sh lists project_key.py for the permission-hooks group (install.sh:134, 264).
- architecture.md: met (settings_loader line + new "Settings root (epic 41)" section).
- Test table: all 10 rows covered by tests/test_unit_settings_root.py (19 tests, passing); module registered in run_all_tests.py. Isolation conventions kept (isolated state env, temp dirs, no real ~/.claude writes).
- Report items (observed CLAUDE_PROJECT_DIR, stray files): implementation concern, not re-verified here; hook now debug-logs CLAUDE_PROJECT_DIR for the live check after install.

## Issues Found

### Issue 1: [severity: MEDIUM]
- **File:** .claude/hooks/permission_request_hook.py:1649 (test gap in tests/test_unit_settings_root.py)
- **Problem:** Nothing tests that the PermissionRequest hook actually computes and stores `settings_root` on the row. Mutation M4 (delete the `settings_root=settings_root,` argument) leaves all 19 tests and thus the suite green. The whole D8 chain (router/MCP using the stored root) depends on this link; if broken, every row would silently use the git-top-level fallback.
- **Fix:** Add a test running permission_request_hook main with CLAUDE_PROJECT_DIR set and cwd=proj/sub (patched RelayClient / isolated store), asserting the created row's settings_root == proj.

### Issue 2: [severity: LOW]
- **File:** .claude/hooks/pretool_hook.py replay_from_log (`default_validator = validator_for({})`, `validator = default_validator`)
- **Problem:** Builds a validator from getcwd() up front only for the pattern-count banner; harmless but slightly redundant naming.
- **Fix:** Optional cleanup.

## Code Quality Notes
- Resolver uses os.path.abspath (not realpath) for env values; fine, matches MCP precedent.
- Env-var values of whitespace are treated as unset; good.

## Questions for User
None.

## Mutation proofs
Protocol followed: status saved before, cp backups in /tmp/41-05-review-mut, restored by cp, diff identical, git status identical after.
- M1 Whitelist write location (router `update_settings_local_json(request.cwd, ...)`): FAILED (1 failure, 1 error): test_whitelist_tap_writes_at_stored_settings_root, test_whitelist_tap_on_old_row_writes_at_git_top_level. Killed.
- M2 first row / CLAUDE_PROJECT_DIR read (removed from resolver loop): 3 failures: test_project_dir_settings_json_allows_from_subdir, ..._local_json_..., TestResolver.test_order. Killed.
- M3 old-row fallback (`return stored or cwd`): test_whitelist_tap_on_old_row_writes_at_git_top_level failed. Killed.
- M4 (extra) hook omits settings_root: tests stay green. SURVIVED -> Issue 1.
