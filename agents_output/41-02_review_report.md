# Review Report - 41-02 Allowed write roots mirror Claude Code's

## Verdict: FAIL

One HIGH issue (the orchestrator-scoped deny bypass by the `rm` / workspace-binary shortcuts). Everything else in the task table is met. Counts: BLOCKER 0, HIGH 1, MEDIUM 0, LOW 3.

## Verification done
- `py_compile` of all hooks, MCP lib/server, both touched test files: OK. Modules import.
- Full suite once, foreground: `Ran 2113`, `OK (skipped=2)` (baseline 2073, +40 all in `test_unit_write_roots`). `/tmp/41-02-review-suite.log`.
- `~/.claude/bash_manual_confirm.log` mtime/size identical before and after the touched modules and before/after the full suite (22:56:44, 15041024 bytes). Earlier changes of that file during the review come from the live installed hook on my own Bash calls, not from the tests.
- `git status --short` identical before/after all mutation work.
- Own oracle under /dev/shm with patched HOME (`/dev/shm/rv/oracle.py`, `t1..t4.py`), about 200 probes. Results below.

## Completeness Check
- Project root via 41-05's `resolve_settings_root` (not re-derived; `compute_write_roots` takes the caller's value): met. `web/src` + `../index.html` rows pass (subprocess tests; oracle `> ../x` OK).
- Memory dir `dirname(transcript_path)/memory`, strict shape (`_transcript_key`, pretool_hook.py:~591): met. Oracle: None/int/''/relative/`.json`/subagent depth/`..`/outside projects/NUL give no root and no crash.
- Worktree case: met. Registration read from `git -C <main> worktree list --porcelain` (`project_key.list_worktrees`), self-check with `encode_cli_project_key`, main != root, failure gives no root. Oracle with a real repo + worktree: main key memory allowed in auto, ask in default; unregistered `.git`-file pretender asks; key mismatch asks; subdir cwd works. `_run_git` catches OSError/SubprocessError/timeout and returns None, so no crash and no root.
- `autoMemoryDirectory` / `autoMemoryEnabled` / `additionalDirectories` from `~/.claude/settings.json` only (`_user_settings`): met. The three existing lists are untouched (SettingsLoader unchanged). Project/local files never read (M-projsettings mutation proves it).
- Mode gating in `compute_write_roots`, validator mode-unaware: met (pretool_hook.py `if mode in DEFERRING_MODES and settings_root`). `main()` passes the payload mode; the validator takes `extra_roots` only.
- Carve-out: only memory-shaped roots (`realpath == ~/.claude/projects/<key>/memory`); sibling, own key outside `memory/`, `memory/../`, `~/.claude/settings.json`, `~/.claude/hooks/` stay protected. Met (oracle).
- One computation, on the row: `permission_request_hook` calls the same helper (permission_request_hook.py:~1612) and stores `write_roots` (store field after `settings_root`, optional, old rows give None and no roots). Router passes `request.write_roots`, MCP `classify` passes `row.write_roots`, `find_deny_collision` unchanged. Met. Validator re-sanitises row roots (absolute, realpathed).
- `replay_from_log`: project root + additionalDirectories, no memory root, help text says so. Met.
- Workspace-binary execution: `_is_workspace_binary` untouched; oracle: binary in project root (cwd = subdir), memory dir and additionalDirectories root are not workspace binaries (ask). Met.
- Fail-open: `main()` and the PermissionRequest hook keep their outer `except Exception` paths. `compute_write_roots` guards each part. Met, with LOW 3.
- architecture.md: PreToolUse row and a new "Write roots" subsection; no stale "workspace, /tmp" claim remains for the redirect gate or `rm`. Met.
- Tests: temp dirs under /var/tmp, fake HOME, real git repo and worktree, runner-isolation env via setdefault; no real-log writes. Table rows all have a test. Met.
- `rm` change (protected path or a directory holding one now refused by the shortcut) is reasonable and consistent with invariant 4.

## Issues Found

### Issue 1: [severity: HIGH]
- **File:** `.claude/hooks/pretool_hook.py` `_check_single_command`, `_is_workspace_binary` branch (~line 2016) and `_is_workspace_rm` branch (~line 2029); both return `allowed` before `_match_deny_and_ask` / `_tier_override_result`.
- **Problem:** Verified. A `Bash(rm:*)` deny with `rm /tmp/zz`, `rm <project root>/zz`, `rm <own memory>/zz` or `rm <additionalDirectories root>/zz` returns `decision: allow` (`matched_allow_patterns == ['workspace_rm']`). A denied workspace binary (`Bash(<root>/bin/t:*)` deny) is also `allow`. The ask tier is skipped the same way. Pre-existing at HEAD for cwd and /tmp, but 41-02 widens the `rm` shortcut to new roots, and state.md invariant 1 says deny is unconditional.
- **Fix:** Call `_tier_override_result(cmd)` before both shortcuts (as the other shortcuts already do, per the comment near line 242); return its result when it is not None. Add tests: deny `Bash(rm:*)` and `rm` in cwd, /tmp, project root, memory dir, additionalDirectories root; a denied workspace binary; and ask-tier equivalents.

### Issue 2: [severity: LOW]
- **File:** `.claude/hooks/pretool_hook.py` `_realpath_inside_allowed_roots` (`resolved.startswith(root + os.sep)`) with an `additionalDirectories` entry of `/`.
- **Problem:** `root + os.sep` is `//`, so a `/` entry matches only `/` itself. Safe direction (over-broad `/` gives no extra access), but it does not mirror Claude Code. Not a security issue.
- **Fix:** Use `root.rstrip(os.sep) + os.sep`, as `_path_within` already does. If fixed, `~/.claude` and `<root>/.claude/...` protection still holds; I confirmed it does for `~/` (home as root): `~/.claude/settings.json`, `hooks/`, sibling memory, `rm -rf ~/.claude`, `rm -rf <proj>/.claude`, unresolved `~/.claude/$V` all ask. Add a test for `/` when fixing.

### Issue 3: [severity: LOW]
- **File:** `.claude/hooks/pretool_hook.py` `compute_write_roots` / `_user_settings`.
- **Problem:** A pathological `~/.claude/settings.json` (deeply nested JSON) raises `RecursionError` out of the helper (the `json.load` catch is `(OSError, ValueError)`). `main()` fails open on it (exit 0, no output), so the gate is skipped; the docstring says each part fails closed. Self-inflicted and exotic.
- **Fix:** Catch `Exception` in `_user_settings` and return `{}`.

### Issue 4: [severity: LOW]
- **Problem (recorded limits, not failures):** (a) the row's `write_roots` is trusted like `settings_root` (H6 family); (b) worktree registration via an allowlisted `git worktree add` (H6 family); (c) a memory dir or key dir that is a symlink to a sibling's `memory` passes the shape check (`realpath` is memory-shaped), but creating such a symlink needs a write under `~/.claude`, which is protected; (d) a user-set `autoMemoryDirectory` that points at another project's `.claude/` or a sibling memory dir becomes a plain root, or a carved-out root for the sibling case. That is user-configured from the user-only file, so by design. Its own `.claude/settings*.json` and `hooks/` stay protected only relative to the roots; a foreign project's `.claude/hooks/` is not.

## Security probe results (oracle, auto mode, fake HOME)
- Allowed as intended: own memory file, `~/` spelling, additionalDirectories root file, rm in own memory and extra root, unresolved `mem/$V.md`, `mem/x$V`.
- Asked: sibling memory, own key outside `memory/`, `memory/../`, `~/.claude/settings.json`, `~/.claude/hooks/x`, `<proj>/.claude/settings{,.local}.json`, `<proj>/.claude/hooks/*`, `<mem>/.claude/settings.json`, `<mem>/.claude/hooks/*`, `<extra>/.claude/settings*.json` and `hooks/`; symlinks inside memory to `~/.claude/settings.json`, `/etc/passwd`, sibling memory, `~/.claude/`; symlinks inside an extra root to outside and to `<proj>/.claude`; memory dir symlinked outside; `rm -rf <memory>`, `rm -rf <extra>`, `rm` through a symlink to protected files, `rm <sibling>`, `rm mem/../x`; unresolved `mem/$V`, `mem/.claude$V`, `mem/.claude/x$V`, `<proj>/.claude/$V`, `~/.claude/s*`, sibling/settings unresolved under `~`.
- Over-broad roots: `additionalDirectories` of `/`, `~/`, `~`, `~/.claude`, `~/.claude/projects`, relative; `autoMemoryDirectory` of `/`, `~`, `~/.claude`, `~/.claude/projects`, `<key dir>`, `<proj>/.claude`, relative, `~root`, non-string. None gives access to `~/.claude/settings.json`, `~/.claude/hooks`, the project's `.claude/settings.json` or `hooks`. The helper drops entries inside `~/.claude`; entries containing it keep `~/.claude` protected via the always-present `dirs` entry. Decision: over-broad roots keep `~/.claude/` and `<root>/.claude/` protected, as required.
- Workspace binary: memory/extra/subdir-of-project binaries are not auto-allowed.

## Mutation proofs
Protocol: `git status --short` saved before (`/tmp/41-02-review-status.before`); `cp pretool_hook.py /tmp/41-02-review-mut/pretool_hook.py.bak`; string mutation, targeted test, restore by `cp`, `cmp` identical, test passes again; `git status --short` after matches. No git command other than `status` and `diff` was used. Driver `/tmp/41-02-review-mut/drive.py`.

| Row | Mutation | Result |
|---|---|---|
| sibling memory (first try, one layer: memory root = `~/.claude/projects` only, or shape only) | SURVIVED, because two layers each refuse (not a root, and still protected) | n/a: mutated both layers |
| sibling memory | `_memory_root_for_key` returns the projects dir and `is_memory_shaped` accepts any path under projects | KILLED: `test_sibling_memory_asks`, `test_own_key_outside_memory_asks` |
| `~/.claude/settings.json` | `~/.claude` entry removed from protected dirs | KILLED: `test_claude_settings_json_asks_even_when_home_is_a_root` |
| `~/.claude/settings.json` | carve-out applies to every path under `~/.claude` | KILLED: same test |
| unregistered worktree | registration check replaced with `if False` | KILLED: `test_worktree_not_listed_by_the_repo_its_git_file_names` |
| project-settings additionalDirectories | helper also merges `<root>/.claude/settings.json` additionalDirectories | KILLED: `test_entry_only_in_project_or_local_settings_is_ignored` |
| mode gating | memory root added in every mode | KILLED: `test_own_memory_write_asks_in_default_until_h5_is_verified` |

## Code Quality Notes
- The plain `~/.claude/settings.json` row is protected by two independent layers, so only the home-as-root variant is mutation-sensitive. The implementer already documents this.
- Reason text still says "outside the workspace, /tmp, or /dev/null" while roots are wider. Deferred to 41-03 per the task.
- Live checks owed to 41-04: `transcript_path` in Bash PreToolUse (not observed locally; the helper degrades to no memory root), H5 in default mode, and a worktree live check. The implementer's report covers these.

## Questions for User
None.
