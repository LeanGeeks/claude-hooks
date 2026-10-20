# Fix Report - 41-02 (review: HIGH 1, LOW 3)

Files: `.claude/hooks/pretool_hook.py`, `.claude/hooks/settings_loader.py` (one word), `tests/test_unit_write_roots.py` (+9 tests). Not committed.

## HIGH - deny/ask bypass by the workspace-rm / workspace-binary shortcuts
- Change: `pretool_hook.py` `_check_single_command`, new `_tier_override_result(cmd)` gate (~line 2025) directly before the `_is_workspace_binary` and `_is_workspace_rm` shortcuts; a deny or ask match returns before either. Shortcuts otherwise untouched.
- Tests (`TestDenyAskBeatShortcuts`): baseline rm allowed; `Bash(rm:*)` deny gives deny under cwd, project root, memory dir (auto mode), additionalDirectories, /tmp; ask rule on rm gives ask under each root; workspace binary allow/deny/ask; deny reaches the hook output as `deny`.
- Mutation (gate removed): 4 tests FAIL (rm deny, rm ask, binary, hook output). Restored, diff clean.

## LOW - `/` in additionalDirectories matched nothing
- Change: `root + os.sep` replaced by `root.rstrip(os.sep) + os.sep` in `_realpath_inside_allowed_roots` (~1195), `_is_workspace_binary` (~1118), `_is_path_inside_workspace` (~1178). `_path_within` already did it. The other `+ os.sep` uses are `/tmp` (fixed string).
- Tests: `/` is a root (redirect and rm allowed); `~/.claude/settings.json`, `~/.claude/hooks/`, `<proj>/.claude/settings.json`, `<proj>/.claude/hooks/`, `rm -rf <proj>/.claude` all still ask with `/` as a root.
- Mutation (rstrip removed at 1195): `test_slash_entry_is_a_root` FAILS. Restored.

## LOW - RecursionError escaped `compute_write_roots`
- Change: `compute_write_roots` wraps `_user_settings()` in `try/except Exception` and returns only the project root (no additionalDirectories, no memory roots) on failure. Also `settings_loader.py:97` now catches `RecursionError`: the live hook subprocess still failed open because SettingsLoader (same file) raised the same error in `main()`; with the fix the gate runs (empty allow list, so the redirect asks).
- Test: `test_pathological_user_settings_fail_safe_and_the_gate_runs` (200000 nested `[`): roots == [project root], and the hook subprocess returns `ask` for `echo x > /etc/...`.
- Mutations: guard narrowed to ZeroDivisionError: test ERRORS; loader RecursionError removed: test FAILS. Restored.

## LOW - recorded limits
- Added the cheap guard: a user `autoMemoryDirectory` containing a `.claude` path component that is not memory-shaped gives no memory root (`_memory_roots`). Test `test_auto_memory_directory_under_a_foreign_claude_dir_is_no_root`; mutation (guard `if False`) FAILS. Restored.
- No code for tampered rows and `git worktree add` (H6 family, accepted/recorded). Symlinked memory dir: needs a write under `~/.claude`, which stays protected, so no extra guard; the realpath shape check already applies.

## Verification
- Protocol: `git status --short` before and after the mutations identical; backups in /tmp/41-02-fix-mut; no git ops besides status.
- Full suite (foreground): `Ran 2122 tests`, `OK (skipped=2)` (baseline 2113, +9). Log /tmp/41-02-fix-suite.log.

## Questions for User
None.
