# Implementation Report - 41-02 Allowed write roots mirror Claude Code's

## Summary

The redirect gate and the `rm` shortcut now accept writes in more places than the payload `cwd` and `/tmp`. The new places are the project root (41-05's settings root), user-level `permissions.additionalDirectories`, and, in `DEFERRING_MODES` only, the session's own auto-memory directory. For a linked worktree whose registration and key encoding check out, that also includes the main checkout's memory dir. A user-level `autoMemoryDirectory` replaces the default memory dir, and `autoMemoryEnabled: false` removes it. All of these come from `~/.claude/settings.json` only.

One helper, `pretool_hook.compute_write_roots`, computes the roots. `main()` passes them to the validator. The PermissionRequest hook stores them on the row as `write_roots`, and the router card and the permissions MCP pass that stored value to the validator.

The memory dir is carved out of the `~/.claude/` protection consistently: for resolved targets and for the R3/R6 rules of unresolvable targets. Every root keeps its own `.claude/settings*.json` and `.claude/hooks/` protected, the memory dir included. Everything here was tested on the repo copy only.

## Files Created

- `tests/test_unit_write_roots.py`: 40 tests covering every task-table row plus the security edges. Everything runs under `/var/tmp` with a fake `HOME`. It uses a real temp git repo and `git worktree add`, and runs the hook both in-process and as a subprocess.

## Files Modified

- `.claude/hooks/project_key.py`
  - `encode_cli_project_key(path)`: the CLI's key encoding, where every non-`[A-Za-z0-9]` character becomes `-`.
  - `list_worktrees(main)`: runs `git -C <main> worktree list --porcelain`.
  - `encode_project_key` (the queue key) is unchanged.
- `.claude/hooks/pretool_hook.py`
  - New module-level helpers:
    - `compute_write_roots(payload, env=None, mode=None, settings_root=None)`
    - `_memory_roots`, `_worktree_memory_root`, `_transcript_key`, `_memory_root_for_key`
    - `is_memory_shaped`, `_user_settings` (a direct read of `~/.claude/settings.json`), `_user_dir`
    - `_claude_home`, `_claude_projects`, `_path_within`, `_sanitize_extra_roots`
  - `BashPermissionValidator.__init__(…, extra_roots=None)` sets `self.extra_roots` and `self.memory_roots`.
  - `_allowed_roots()` now adds the extra roots.
  - Carve-out in `_is_protected_write_path`: the `~/.claude` entry does not protect paths inside `self.memory_roots`. No other entry is exempt.
  - `_unresolved_target_allowed`: R6 moved after R3. When `realpath(D)` is inside the own memory dir, D's literal text is exempt from R6; the final segment F is not.
  - `_is_path_inside_allowed_rm_roots`: now refuses a protected path, or a directory that holds one.
  - `main()` debug-logs `transcript_path` and `Write roots`, and computes the roots from the payload mode. The validator stays mode-unaware.
  - `replay_from_log`: uses the helper with no mode and no transcript (so project root and `additionalDirectories` only). Its help text says so.
  - Comments and docstrings updated.
- `.claude/hooks/permission_request_hook.py`: calls `compute_write_roots(input_data, mode=permission_mode, settings_root=settings_root)` and passes `write_roots=` to `create_request`.
- `.claude/hooks/permission_state_store.py`: new optional `PermissionRequest.write_roots: Optional[List[str]]`, placed after `settings_root`, plus a `create_request(write_roots=None)` parameter. This is additive, following the same precedent as `settings_root`.
- `.claude/hooks/telegram_permission_router.py`: `_bash_annotations` passes `extra_roots=request.write_roots`.
- `permissions-mcp/permissions_mcp_lib.py`:
  - `_build_validator(cwd, settings_root=None, write_roots=None)`.
  - `classify` passes `row.write_roots`.
  - `find_deny_collision` is unchanged and passes no roots.
- `permissions-mcp/server.py`: docstring wording ("outside the allowed roots").
- `architecture.md`: the PreToolUse row (line 113) and a new "Write roots (epic 41, task 41-02)" subsection. The Settings-root paragraph no longer says the redirect roots are the cwd.
- `tests/test_unit_settings_root.py`: 41-05's `test_redirect_gate_still_uses_cwd_as_its_root` pinned the pre-41-02 behaviour. It is renamed to `test_redirect_gate_accepts_the_settings_root` and now expects `> ../x` from `proj/sub` to be `allow`, plus a new `> ../../x` (outside proj) case that must be `ask`.
- `tests/run_all_tests.py`: registers `unit_write_roots`.

## Verification

- Compile/import: PASS. Ran `py_compile` on all hooks, the MCP lib/server and both test files, and imported `pretool_hook`, `permission_request_hook`, `telegram_permission_router`, `project_key`, `permission_state_store` and `permissions_mcp_lib`.
- Tests: full suite `Ran 2113`, `OK (skipped=2)`. See Testing.
- Installed (`install.sh --yes` re-run): no, per the brief. The repo copy was tested.

## Testing

- Baseline, measured before my first edit at HEAD 6772917 (with only the orchestrator's `state.md` modified): `python3 tests/run_all_tests.py` gave `Ran 2073 tests`, `OK (skipped=2)` (/tmp/41-02-baseline.log).
- Final: `python3 tests/run_all_tests.py > /tmp/41-02-suite.log 2>&1`, run in the foreground, gave **`Ran 2113 tests`, `OK (skipped=2)`**. That is +40, all of them the new module. One existing test was rewritten (41-05's redirect row above), not removed.
- Skips by name (unchanged):
  - `test_unit_python_compat.TestImportUnderOldInterpreter.test_all_hook_modules_import`: no pre-3.11 interpreter with tomli.
  - `test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`: needs `AMUX_SPAWN_LIVE_TEST=1`.
- Real logs: no test row reached `~/.claude/bash_manual_confirm.log` or `~/.claude/permission_requests.jsonl` (grep for `s-write-roots` / `write-roots-` gives 0). The new module sets the runner's isolation env with `setdefault`, so it is isolated even when run directly.

Task table → tests (`tests/test_unit_write_roots.py`):

| Row | Test |
|---|---|
| own memory, auto → no ask | `TestMemoryRoot.test_own_memory_write_is_silent_in_auto` (in-process and hook subprocess, `~/` spelling too) |
| same, default → ask | `test_own_memory_write_asks_in_default_until_h5_is_verified` (also acceptEdits; root set per mode) |
| sibling memory → ask/risk | `test_sibling_memory_asks` (auto and bypass; subprocess) |
| `~/.claude/settings.json`, auto and bypass → ask/risk | `test_claude_settings_json_asks`, `test_claude_settings_json_asks_even_when_home_is_a_root` |
| own key outside `memory/` → ask | `test_own_key_outside_memory_asks` (also `memory/../`, `memory2/`) |
| registered worktree → main key memory, no ask | `TestWorktreeMemory.test_registered_worktree_writes_main_checkout_memory` (also from a worktree subdir; default asks) |
| `.git` file names a repo that does not list it → ask | `test_worktree_not_listed_by_the_repo_its_git_file_names` |
| self-check fails → ask | `test_encoding_self_check_failure_adds_no_worktree_root` |
| `web/src` + `ls > ../index.html` with `CLAUDE_PROJECT_DIR` → no ask | `TestProjectRoot.test_subdir_writes_up_to_the_project_dir_root` |
| no env, git repo → no ask | `test_subdir_writes_up_to_the_git_top_level` |
| no env, not a repo → ask | `test_no_env_not_a_repo_keeps_todays_behaviour` |
| global `additionalDirectories` → no ask | `TestAdditionalDirectories.test_global_entry_is_a_root` (also `rm`) |
| only in project/local → ask | `test_entry_only_in_project_or_local_settings_is_ignored` (subprocess, both files) |
| `autoMemoryDirectory` global → that dir, not the default | `TestAutoMemorySettings.test_user_level_auto_memory_directory_replaces_the_default` |
| `autoMemoryDirectory` project/local → ignored | `test_auto_memory_directory_in_project_or_local_settings_is_ignored` |
| `autoMemoryEnabled: false` → no memory root | `test_auto_memory_disabled_gives_no_memory_root` |
| `rm <own memory>/old.md` → allowed by the rm check | `test_rm_in_own_memory_is_allowed_by_the_rm_check` (`matched_allow_patterns == ['workspace_rm']`) |
| `<own memory>/tool` not a workspace binary | `test_memory_tool_is_not_a_workspace_binary` (auto, default, bypass) |
| malformed/missing `transcript_path` | `test_malformed_or_missing_transcript_gives_no_memory_root` (None, int, empty, relative, `.json`, subagent depth, `..`, outside projects, NUL; subprocess with no field) |
| router card with `write_roots` → 📝 does not list the memory target | `TestRowConsumers.test_router_card_does_not_list_the_memory_target` (annotations and rendered body; control row without `write_roots` lists it) |
| MCP classification; `find_deny_collision` with no row | `test_mcp_classification_uses_row_write_roots`, `test_find_deny_collision_without_a_row_is_unchanged` |

Security edges, beyond the table:

- The memory dir's own `.claude/settings*.json` and `.claude/hooks/` stay protected (`test_memory_dirs_own_claude_config_stays_protected`). The same holds for an `additionalDirectories` root and for the project root (`test_extra_roots_own_claude_config_stays_protected`, `test_project_root_config_stays_protected`).
- A symlink inside the memory dir is judged on its realpath, whether it points to `~/.claude/settings.json`, outside, or at the sibling memory dir. So is a symlink inside an extra root that points to `/etc` (`test_symlink_inside_memory_is_judged_on_its_realpath`).
- A memory dir that is itself a symlink out of `projects/` is not a root.
- Unresolvable targets get the carve-out in the memory dir only. `.claude` / `claude` in the final segment still refuses, and so do sibling, own-key and `..` targets.
- `rm` cannot reach protected paths through any root.
- Override values that would widen past memory are refused: `~/.claude`, `~`, `/`, `~/.claude/projects`, `~/.claude/hooks`, `~/.claude/agents`, a relative path, `~user`, a non-string.
- Roots on a tampered row that sit inside `~/.claude` never lift its protection.
- The PermissionRequest hook stores exactly the roots pretool computes (`TestPermissionRequestHookStoresRoots`).
- Replay: the project root applies and the memory root does not; the help text says so.

## transcript_path in PreToolUse

- **Bash PreToolUse on the current CLI: not observed. The live check is owed.** `~/.claude/bash_hook_debug.log` has 18 `Raw input` entries, all test-generated (`cwd: /tmp`, no `transcript_path`). `main()` now logs `transcript_path: <value>` and `Write roots: [...]` when `CLAUDE_HOOK_DEBUG=1`.
- Existing-log evidence (read-only, labelled as such):
  - `~/.claude/ask_user_question_debug.log` holds real **PreToolUse** payloads (`hook_event_name: "PreToolUse"`, tool `AskUserQuestion`, 2026-05-03, an older CLI) carrying `"transcript_path":"/home/anton/.claude/projects/-data-sync-work-leangeeks-ai-ai-playground/e8c79db4-….jsonl"`. That is the expected `~/.claude/projects/<key>/<id>.jsonl` shape.
  - `~/.claude/permission_request_debug.log` holds 172 `transcript_path` occurrences on PermissionRequest payloads, e.g. `/home/anton/.claude/projects/-data-sync-work-delaybuddy/1115b3b9-….jsonl`.
- If the field turns out to be absent in Bash PreToolUse, the memory root is simply not added (the "missing transcript" row). The project root and `additionalDirectories` do not depend on it.

## Key-encoding self-check pairs

- Source: a read-only scan of `~/.claude/projects/`. For each directory, I took the first `cwd` recorded in its first transcript and compared it with the directory name.
- Result: 322 of 322 pairs match `re.sub(r'[^A-Za-z0-9]', '-', path)`, with 0 mismatches. The non-alphanumeric characters seen in those paths are `_`, `.` and `-`.
- Pairs used in `TestKeyEncoding.test_observed_pairs`:
  - `/data/sync/work/dr_remi` → `-data-sync-work-dr-remi`
  - `/data/sync/work/dr_remi/hipaa_form` → `-data-sync-work-dr-remi-hipaa-form`
  - `/data/sync/Config/bash/ram_stat` → `-data-sync-Config-bash-ram-stat`
  - `/data/sync/work/delaybuddy/.worktrees/delay-buddy-form--w1-01` → `-data-sync-work-delaybuddy--worktrees-delay-buddy-form--w1-01`
  - `/data/sync/work/activecdn/acdn-workspace/skynet` → `-data-sync-work-activecdn-acdn-workspace-skynet`
- The longest observed key is 117 characters. No truncated or hashed long key was observed. For such a key the self-check fails, and no worktree root is added.
- The H2 example is consistent with this encoding. The session `-data-sync-work-tradingtips-leads-platform--064-qa01` maps to `/data/sync/work/tradingtips/leads-platform--064-qa01`, and its main key `-data-sync-work-tradingtips-leads-platform` exists.

## H5 status

**Unverified.** The memory root is added only when the payload mode is in `DEFERRING_MODES` (`auto`, `bypassPermissions`, `dontAsk`). In `default`, `acceptEdits` and `plan` a memory write still asks. This is pinned by the test, and mutation M5 below kills it. 41-04 must verify whether Claude Code writes memory without a prompt in `default` mode before the gating is lifted. Lifting it is a one-line change in `compute_write_roots` (`if mode in DEFERRING_MODES and settings_root:`).

## Mutation proofs

- Protocol:
  - Ran `git status --short` before.
  - Copied the file with `cp .claude/hooks/pretool_hook.py /tmp/41-02-mut/pretool_hook.py.bak`.
  - Applied the exact string mutation and ran the targeted test with a fresh `PYTHONPYCACHEPREFIX` (to avoid stale `.pyc`).
  - Restored by `cp`, byte-compared it with the backup (identical every time), and re-ran the test (passes every time).
  - Ran `git status --short` after; it was identical to before for every batch.
  - No git command other than `status` was used.
- Driver: `/tmp/41-02-mut/mut_final.py`. Logs: `/tmp/41-02-mut/mutation_log_final.txt`, `mutation_log_2.txt`.
- All final runs are on the final code.

| # | Row | Mutation | Result |
|---|---|---|---|
| M1 | sibling memory | memory root = `~/.claude/projects`, and any path under it counts as memory-shaped | KILLED: `test_sibling_memory_asks` |
| M2 | `~/.claude/settings.json` | carve-out skips the `~/.claude` entry for every path, not only those inside the memory dir | KILLED: `test_claude_settings_json_asks_even_when_home_is_a_root` |
| M2' | `~/.claude/settings.json` | `~/.claude` entry dropped from the protected dirs | KILLED: same test |
| M3 | unregistered worktree | main checkout's `worktree list` registration not checked | KILLED: `test_worktree_not_listed_by_the_repo_its_git_file_names` |
| M4 | project-settings `additionalDirectories` | roots read from `<root>/.claude/settings.json` when the user file is empty | KILLED: `test_entry_only_in_project_or_local_settings_is_ignored` |
| M5 | H5 / default mode | memory root added in every mode | KILLED: `test_own_memory_write_asks_in_default_until_h5_is_verified` |
| M6 | self-check | encoding self-check skipped | KILLED: `test_encoding_self_check_failure_adds_no_worktree_root` |
| M7 | R6 carve-out | exemption widened from D's text to the whole word | KILLED: `test_unresolvable_target_in_memory_gets_the_carve_out` |
| M8 | rm | protected / holds-protected refusal removed | KILLED: `test_rm_never_reaches_protected_paths_through_a_root` |
| M9 | helper drops `additionalDirectories` inside `~/.claude` | drop removed | KILLED: `test_entries_inside_claude_home_are_dropped` |
| M10 | carve-out limited to memory-shaped roots | any extra root carves out | KILLED: `test_row_roots_inside_claude_home_never_lift_its_protection` |

**About the plain `~/.claude/settings.json` row.** With the default config, two independent layers make it ask: it is outside every root, and it is protected. So no single mutation flips it. The home-as-root variant, which uses `additionalDirectories: ["~/"]`, removes the first layer, and M2/M2' prove the second.

**One mutation survived on an earlier draft, and the code was removed.** That draft filtered non-memory roots inside `~/.claude` in the validator's sanitizer. The filter had no observable effect, because such a root is still protected; the M10 test now pins that. The filter was deleted rather than kept as dead code.

**M9 was confirmed directly as well.** A memory-shaped root the validator is handed, such as a sibling's memory dir, is trusted and carved out (`allow`). So the helper's drop of `additionalDirectories` entries inside `~/.claude` is load-bearing.

## Live checks owed

These need `./install.sh --yes`, after 41-03, and belong to 41-04:

1. A Bash PreToolUse payload on the installed CLI carries `transcript_path`. Check with `CLAUDE_HOOK_DEBUG=1`: the `transcript_path:` and `Write roots:` lines in `~/.claude/bash_hook_debug.log`, for a main-checkout session and for a worktree session.
2. Brd H5: Claude Code writes memory without a prompt in `default` mode. If it does, lift the mode gate.
3. Acceptance 3 in a live auto-mode session:
   - `echo x >> <own memory dir>/MEMORY.md` is silent;
   - `echo x >> ~/.claude/settings.json` asks;
   - `echo x >> <another project's memory dir>/MEMORY.md` asks.
4. Brd H2 live: in a registered worktree session in auto mode, a write to the main checkout's `<key>/memory/` is silent.
5. Acceptance 4: after a `cd` into a subdirectory, `> ../file` inside the project is silent. This depends on H1 (`CLAUDE_PROJECT_DIR`) or the git fallback.
6. A real card for a row with `write_roots` does not list a memory-dir write under 📝 (acceptance 6, 41-02 part).

## Decisions

- **Project root is in `write_roots`.** The helper's output includes the project root, so the router and the MCP need only the row's `write_roots`. Old rows get none (cwd and `/tmp`), per the task. The helper takes `settings_root` from the caller (the value `main()` / the PermissionRequest hook already resolved with `resolve_settings_root`), so the root is not resolved a second way.
- **Where the helper lives.** It is in `pretool_hook.py`, beside `DEFERRING_MODES`. The PermissionRequest hook imports it (same `permission-hooks` feature). The git shell-outs live in `project_key.py`, per that module's rule. No new module, so no `install.sh` change.
- **Which memory roots get the carve-out.** Only memory-shaped roots (`realpath == ~/.claude/projects/<key>/memory`) are carved out of `~/.claude`.
  - The helper drops every `additionalDirectories` entry inside `~/.claude` (invariant 4: "never `~/.claude`").
  - A transcript-derived memory dir must still have that shape after realpath, so a symlinked memory dir that leads elsewhere is not a root.
  - An `autoMemoryDirectory` override may sit outside `~/.claude` (a plain root, no carve-out). It is refused if it equals or contains `~/.claude`, or if it sits inside `~/.claude` without the memory shape.
  - An invalid override (relative, `~user`, non-string) gives no memory root rather than falling back to the default.
- **Path-form rules.** `additionalDirectories` accepts `/…` and `~/…` only, per the task; a bare `~` is ignored.
- **R6 placement.** R6 moved after R3, and D's literal text is exempt only when `realpath(D)` is inside the own memory dir. Without that exemption the R3 carve-out was unreachable: every spelling of a memory path contains `.claude`. With no memory roots the refused set is identical to before, and 41-01's 449-test `test_integration_pretool` passes unchanged.
- **`rm` behaviour change (invariant 4 applied to `rm`).** The `rm` shortcut now also refuses:
  - a protected path, e.g. `rm .claude/settings.local.json` in the workspace, which was previously auto-allowed;
  - a directory that holds one, e.g. `rm -rf .` in a root, `rm -rf /tmp`, `rm -rf ~` when home is a root, or `rm -rf <memory>`.

  These now fall through to normal pattern matching, which may still allow them via a `Bash(rm:*)` allow rule. Without this, a new root such as `additionalDirectories: ["~/"]` would have let the shortcut delete `~/.claude/settings.json`.
- **Reason text unchanged.** The ask text is still "Redirects output outside the workspace, /tmp, or /dev/null". The permissions MCP matches its prefix (`REDIRECT_REASON_PREFIX`), and card wording is 41-03's. It is now looser than the real root set.
- **Cost.** In a deferring mode, a session whose transcript key passes the self-check costs one cached `git rev-parse --git-common-dir` per PreToolUse and PermissionRequest invocation. Only real linked worktrees add a `git worktree list`.
- **`CLAUDE_CONFIG_DIR` is not honoured** (per the brief: no new `CLAUDE_*` env reads). A session using it gets no memory root, which is the safe direction.

## Blockers

None. The Bash-PreToolUse `transcript_path` observation and H5 are owed live checks, not blockers, per the brief.

## Questions for User

None.

## Findings for the orchestrator (pre-existing, not fixed)

1. **The workspace `rm` shortcut, and the workspace-binary shortcut, run before deny matching.** In `_check_single_command`, `_is_workspace_rm` and `_is_workspace_binary` return `allowed` before `_match_deny_and_ask`. Measured on the repo copy: with a `Bash(rm:*)` deny and target `/var/tmp`, `rm /var/tmp/zz` returns **`allow`**. That breaks invariant 1 (deny is unconditional) at HEAD. 41-02 widens the `rm` shortcut's reach to the new roots (project root, `additionalDirectories`, memory dir in deferring modes). Suggested follow-up: consult `_tier_override_result` (deny/ask) before both shortcuts.
2. **The row's `write_roots` is trusted the same way `settings_root` is.** A memory-shaped entry on a tampered row gets the carve-out. The state file is protected from the redirect gate (`~/.claude/`), but not from allowlisted file-writing commands (brd H6).
3. **Worktree registration is only as strong as the main checkout's `.git`.** An agent that can run an allowlisted `git worktree add` inside another repo's checkout (H6 family) could register itself there. Recorded, not addressed.

## For 41-03

- **Roots helper:** `pretool_hook.compute_write_roots(payload, env=None, mode=None, settings_root=None) -> List[str]`. It returns realpaths: project root, user `additionalDirectories`, and (in `DEFERRING_MODES`) memory roots.
- **Validator argument:** `BashPermissionValidator(..., extra_roots=...)`. Inside the validator they are `self.extra_roots`, and the carve-out subset is `self.memory_roots`.
- **Row field:** `PermissionRequest.write_roots: Optional[List[str]]` (JSON key `write_roots`), set through `create_request(..., write_roots=...)`. The router passes `extra_roots=request.write_roots`. The MCP calls `_build_validator(cwd, settings_root, write_roots)`, and `classify` passes `row.write_roots`.
- **"Is the literal anchor inside the allowed roots"** should be asked inside `_unresolved_target_allowed`, the same way R2/R3 do today:
  - compute `base = os.path.realpath(os.path.join(self.workspace_dir, directory))`, where `directory` is the tilde-expanded literal D with no `..`;
  - "anchor inside" is `self._realpath_inside_allowed_roots(base)`;
  - "protected" is `self._is_protected_write_path(base, unresolved=True)`.

  Both read `self._allowed_roots()`, which already includes the extra roots and applies the memory carve-out. Do not re-derive roots.
- **R6 position:** R6 now runs after R3, and is exempt for D's text only when `self._inside_memory_root(base)`. An outcome function beside `_unresolved_target_allowed` should keep R6 a **risk** refusal.
