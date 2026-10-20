# Review Report 2 - Task 41-02 (allowed write roots mirror Claude Code), pass 2

## Verdict: PASS (0 BLOCKER, 0 HIGH, 1 MEDIUM, 2 LOW)

Pass 1's HIGH (deny/ask bypass) is fixed and mutation-proved. One new MEDIUM was found in the fixer's `/` containment change (loosens a `cwd == /` session). It is not a deny bypass. Fix recommended before commit.

Compile: `python3 -m py_compile` on .claude/hooks/*.py, permissions-mcp/*.py and tests/test_unit_write_roots.py is clean. Review was on the uncommitted diff over HEAD 6772917. `git status --short` is identical before and after all mutations and the suite. The real `~/.claude/bash_manual_confirm.log` was never touched by my probes (patched HOME and /var/tmp dirs). It does grow from other live sessions (see the suite note below).

## Completeness Check

1. Deny/ask before shortcuts, every root type: met.
   - `_check_single_command` calls `_tier_override_result(cmd)` at pretool_hook.py:2028, before `_is_workspace_binary` (2033) and `_is_workspace_rm` (2046).
   - Probe: 315 cases through `BashPermissionValidator`, HEAD (`git show HEAD:` copies under /tmp) vs working tree. The matrix:
     - Roots: cwd, project root (from a subdir), additionalDirectories, memory dir, /tmp.
     - Commands: `rm`, `rm -rf`, `rm --`, `rm -rf --`, `/bin/rm`, `command rm`, `env rm`, `sudo rm`, `echo hi | rm`, `(rm)`, `true && rm`, `cd d && rm x`, and `./tool.sh` as a workspace binary.
     - Rules: deny and ask written as `Bash(rm:*)` and `Bash(rm -rf:*)`.
   - Zero cases are weaker than HEAD. 0 HEAD-deny now allows; 0 HEAD-ask now allows.
   - Every difference is a HEAD allow becoming ask or deny, or a HEAD ask becoming deny (pipeline with an `rm:*` deny).
   - `Bash(rm -rf:*)` as deny does not match plain `rm x` (correct, pattern semantics). With no deny/ask rule, plain in-root `rm`, `rm -rf` and workspace binaries still allow under every root (no new friction). `/bin/rm` and `sudo rm` behave as at HEAD.
   - Note: HEAD already had the shortcuts above deny/ask even for cwd and /tmp. The fix tightens that too (intended, state.md invariant 1).
2. `/` root: met for additionalDirectories (see Issue 1 for the workspace-root side effect).
   - With `/` as an extra root, writes to /var/tmp/x/proj2 and /etc and `rm /etc/foo` are allowed. `<proj>/.claude/settings.json`, `<proj>/.claude/hooks/a`, `~/.claude/{settings.json,hooks/a,projects/k/a,other}`, `rm -rf <proj>/.claude`, `rm ~/.claude/settings.json` and `rm -rf ~/.claude` all still ask.
   - `/tmpx/f` (with root `/tmp`) and `proj2/f` (with root `proj`) still ask. `/tmpx` and `proj2` are not loosened.
3. Fail-safe: met.
   - `compute_write_roots` with an unreadable `_user_settings()` returns only the project root (pretool_hook.py ~L679-686). Subprocess probe: HOME with a 200000-deep `[` settings.json, project allow `echo`, deny `rm`.
   - HEAD: the hook crashes silently (rc 0, no output, so no gate at all).
   - New: the gate runs. `echo hi` is allowed by the project allow list, `rm x` is denied by the project deny list, and `echo x > /etc/foo` asks.
   - `settings_loader.py:97` now returns `{}` for a RecursionError, the same posture the loader already had for JSONDecodeError and IOError. Only the poisoned file's rules are lost (here the user file; project and other files still load). No HEAD-deny becomes allow, because HEAD never denied (it crashed to the no-decision path). See Issue 2.
4. Symlinked memory dir / autoMemoryDirectory-into-`.claude`: met.
   - A symlinked `projects/<key>/memory` realpaths to a non-memory-shaped dir, so no memory root.
   - `autoMemoryDirectory` of `~/.claude`, `~/.claude/hooks`, `<proj>/.claude/mem`, a relative path or `/` gives no root. `~/.claude/projects/<key>/memory` and an ordinary outside dir are accepted. The default mode adds no memory root.
   - A symlinked dir pointing at another key's memory still needs a write under `~/.claude`, which stays protected. Acceptable.
5. Whole-diff read for regressions: met, apart from Issue 1.
   - The write_roots plumbing (state store row field, permission_request_hook, router, MCP `_build_validator`) is additive and None-safe. The `rm` check now also refuses protected paths and dirs that contain one. The R6 relaxation applies only for the memory dir's own literal text.

## Issues Found

### Issue 1: severity: MEDIUM
- **File:** .claude/hooks/pretool_hook.py: `_realpath_inside_allowed_roots`, `_is_workspace_binary` (~1118), `_is_path_inside_workspace` (~1178). Applies to `rstrip(os.sep)`.
- **Problem:** The `root.rstrip(os.sep) + os.sep` change also applies to the payload workspace root. When the validator's `workspace_dir` is `/` (session launched from `/`), HEAD asked (`startswith('//')` never matched) and the new code allows. Probe with `BashPermissionValidator(..., '/')` and `Bash(echo:*)` allowed:

  | Command | HEAD | New |
  |---|---|---|
  | `echo x > /etc/foo` | ask | allow |
  | `rm /etc/foo` | ask | allow |
  | `/usr/bin/curl http://x` | ask | allow (workspace-binary shortcut) |
  | `/bin/ls` | ask | allow |

  The task only asked for `/` as an additionalDirectories root to work. This is an ask-to-allow loosening, not a deny bypass (deny and ask rules still win now). It needs payload cwd of `/` or an explicit user `/` root. Narrow trigger, so MEDIUM, not HIGH.
- **Fix:** Keep the rstrip only for `self.extra_roots` entries. `_is_workspace_binary` should revert to its original comparison, since it uses workspace and /tmp only; `_is_workspace_binary` also gets no rstrip, so a `/` extra root does not make every binary a workspace binary. In the allowed-roots loop, skip or special-case `workspace_dir == '/'`. Add a test: cwd `/` with no extra roots still asks for `> /etc/x`, `rm /etc/x` and `/usr/bin/x`.

### Issue 2: severity: LOW
- **File:** .claude/hooks/settings_loader.py:97
- **Problem:** The RecursionError catch is global. A deeply nested settings file (user-level deny rules included) now silently drops only that file's rules, with a stderr warning. HEAD crashed the hook instead (no gate). It matches the existing JSONDecodeError posture and was not asked for by the task, but the fixer documented it.
- **Fix:** None required. Optionally note it in architecture.md.

### Issue 3: severity: LOW
- **File:** tests/test_unit_write_roots.py
- **Problem:** Mutation 6 (below) shows the `_is_workspace_binary` rstrip is untested; the suite stays green with it reverted. The same gap hides Issue 1.
- **Fix:** Add the cwd-`/` regression test from Issue 1.

## Mutation proofs
Protocol: backups in /tmp/41-02-review2-mut/*.bak, `diff` identical after every restore (`cp`), `git status --short` identical before and after. No git write ops. Run with `python3 tests/run_all_tests.py --module unit_write_roots` (baseline 49 OK).

| # | Mutation | Result |
|---|---|---|
| M1 | `rm` shortcut ordered before the deny/ask gate (`None if self._is_workspace_rm(cmd) else _tier_override_result`) | FAILS 3: test_rm_ask_rule_still_asks_under_every_root, test_rm_deny_in_hook_output, test_rm_deny_wins_under_every_root |
| M2 | Same for `_is_workspace_binary` | FAILS 1: test_workspace_binary_deny_and_ask_win |
| M3 | Fail-safe guard narrowed `except Exception` to `except ZeroDivisionError` | ERRORS: test_pathological_user_settings_fail_safe_and_the_gate_runs |
| M4 | Loader `RecursionError` removed from the except tuple | FAILS: test_pathological_user_settings_fail_safe_and_the_gate_runs |
| M5 | `/` fix reverted in `_realpath_inside_allowed_roots` | FAILS: test_slash_entry_is_a_root |
| M6 | `/` fix reverted in `_is_workspace_binary` | SURVIVES (OK, 49 tests). Test gap, Issue 3 |

## Code Quality Notes
- `permission_request_hook.py` imports `pretool_hook` at module level. It imports cleanly, and `compute_write_roots` swallows its own errors.
- All user-sourced roots (additionalDirectories, autoMemoryDirectory) come from `~/.claude/settings.json` only. Memory roots apply in DEFERRING_MODES only. An additionalDirectories entry that is memory-shaped is carved out of `~/.claude` regardless of mode (user-configured, acceptable).
- The replay CLI applies project root and additionalDirectories only (documented).

## Full suite
`python3 tests/run_all_tests.py`: Ran 2122, FAILED (failures=1, skipped=2). The single failure is `test_integration_pretool...test_case17_confirm_log_respects_env_redirect`: it compares the real `~/.claude/bash_manual_confirm.log` size before and after, and the log grew 3884 bytes during the 5-minute run (15097980 to 15101864). The log also grew between my own stat calls (15081406 at start, 15106712 later) while no test of mine wrote it, so the growth is from other concurrent live sessions on this shared machine, not 41-02. Rerun alone: OK, and the log was unchanged across it. Log: /tmp/41-02-review2-suite.log. A quiet-machine rerun would confirm the full-suite OK.

## Questions for User
None.
