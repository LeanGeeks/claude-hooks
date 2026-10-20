# Review Report 3 - Task 41-02 (allowed write roots mirror Claude Code), pass 3

## Verdict: PASS (0 BLOCKER, 0 HIGH, 0 MEDIUM, 1 LOW)

Reviewed the uncommitted diff over HEAD 6772917. py_compile of .claude/hooks/*.py, permissions-mcp/*.py and tests/test_unit_write_roots.py is clean. `git status --short` is identical before and after all mutations and the suite. Full suite: Ran 2129, OK (skipped=2) (/tmp/41-02-review3-suite.log).

## Completeness Check

1. `/` handling applies to additionalDirectories only: met.
   - pretool_hook.py `_base_roots()` (cwd, /tmp) uses the exact `root + os.sep` compare; only `self.extra_roots` use `_path_within`. `_is_workspace_binary` (L1123) and `_is_path_inside_workspace` (L1183) are back to the HEAD comparison. `compute_write_roots` skips a project root of `/` (L713).
   - Probe 1 (HEAD copies via `git show HEAD:` under /tmp/41-02-r3-head vs the working tree, imported): 19 commands x workspaces `/`, `/data/proj`, `/tmp`, plus 5 workspace-binary / inside-workspace probes each, 72 decisions in total. They include `rm /etc/foo`, `> /etc/foo`, `/usr/bin/curl`, `/tmp` vs `/tmpx`, `/data/proj` vs `/data/proj2`. Only one difference: cwd `/`, `rm -rf /` goes from HEAD allow to ask. That is a tightening (the `rm` check now refuses a directory that holds a protected path, D4), not a loosening. See Issue 1.
   - `rm /etc/foo`, `> /etc/foo`, `/usr/bin/curl`, `/tmpx`, `proj2` all ask at cwd `/`, as at HEAD. `/tmp` remains allowed.
   - Probe 2 (user `additionalDirectories: ["/"]`, computed through `compute_write_roots`): `/` is a root, so `/etc/foo`, `rm /etc/foo` and `/var/tmp/other/a` are allowed. `<root>/.claude/settings.json`, `settings.local.json`, `.claude/hooks/a`, `~/.claude/settings.json`, `~/.claude/hooks/a`, `~/.claude/projects/k/a`, `rm -rf <root>/.claude`, `rm -rf ~/.claude` and `rm ~/.claude/settings.json` all ask.
2. LOW fixes as described: met. The RecursionError catch stays (documented decision, covered by test_pathological_user_settings_fail_safe_and_the_gate_runs). The `_is_workspace_binary` rstrip gap is closed by TestCwdSlashIsNotARoot (M1/M2 below).
3. Confirm-log isolation test: met. test_case17 (tests/test_integration_pretool.py ~L2493) uses a uuid marker in the command. It asserts the marker lands in the isolated `CLAUDE_MANUAL_CONFIRM_LOG` and is absent from whatever the real log gains during the run (read-only, from the pre-run size). It also asserts the isolated path differs from the real one. It does not depend on the real log being quiet. Running case17 alone: real log size 15123852 before and after (stat). During the full suite the real log grew (15123852 to 15142081) from concurrent live sessions, and the suite still passed. The test never opens the real log for writing.
4. Whole-diff read: no regressions found.
   - write_roots plumbing is additive and None-safe: state store, permission_request_hook, router, MCP `_build_validator` (`find_deny_collision` passes none), and the replay CLI with its help text.
   - R6 moved after R3 and is relaxed only for the memory dir's own literal text.
   - architecture.md is up to date: the PreToolUse row and the new "Write roots" section match the code (cwd, /tmp, project root, additionalDirectories, memory in deferring modes, protected paths, `rm` protection, workspace-binary narrow, replay and `find_deny_collision`). One omission: it does not say that a project root of `/` is not a root. See Issue 1.
   - No dead code found. `_base_roots` is used by both `_allowed_roots` and `_realpath_inside_allowed_roots`.

## Issues Found

### Issue 1: severity: LOW
- **File:** .claude/hooks/pretool_hook.py `_is_path_inside_allowed_rm_roots` (~L1227) and architecture.md "Write roots"
- **Problem:** The `rm` check refuses a protected directory or one that holds a protected path, so `rm -rf /` from cwd `/` (and `rm -rf <root>` for a project root containing `.claude/settings.json`) changed from HEAD allow to ask. It is a tightening and consistent with D4, but "matches HEAD exactly" is not literally true for this case. The docs say it ("`rm` also refuses a protected path or a directory holding one") but do not call out the workspace-root case, nor that a project root of `/` is skipped.
- **Fix:** Optional: one sentence in architecture.md. No code change.

## Mutation proofs
Protocol: backup /tmp/41-02-review3-mut/pretool_hook.py.bak, mutate, `python3 tests/run_all_tests.py --module unit_write_roots` (baseline 56 OK), restore with `cp`, `diff` identical, `git status --short` identical to before. No git write ops.

| # | Mutation | Result |
|---|---|---|
| M1 | `_is_workspace_binary` uses `root.rstrip(os.sep) + os.sep` | FAILS 1: test_workspace_binary_outside_still_asks |
| M2 | `_is_path_inside_workspace` rstrip back | FAILS 1: same test |
| M3 | `_realpath_inside_allowed_roots` base roots (cwd, /tmp) use `_path_within` | FAILS 3: test_redirect_outside_still_asks (+_with_computed_roots), test_rm_outside_still_asks |
| M4 | `compute_write_roots` adds a project root of `/` (`if True:`) | FAILS 2: test_project_root_slash_is_not_a_write_root, test_redirect_outside_still_asks_with_computed_roots |
| M5 | `_path_within` loses the rstrip (additionalDirectories `/` stops working) | FAILS 3: test_slash_entry_is_a_root, test_slash_extra_root_still_works_from_slash, test_overrides_that_would_widen_past_memory_are_refused |

I did not mutation-test case17 with the log path pointed at the real log (it could write to it). Fix report 2's mutation with the path pointed at a different, non-real file is accepted.

## Code Quality Notes
- The RecursionError catch in settings_loader.py:97 matches the loader's existing JSONDecodeError/IOError posture (from pass 2, accepted).
- The cwd-`/` tests cover redirect, `rm`, workspace binary, `_is_path_inside_workspace`, `/tmp` and the `/` extra root.

## Questions for User
None.
