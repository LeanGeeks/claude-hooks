# Review Report - 41-01 (pass 9): the gate reads the real target

## Verdict: PASS

0 BLOCKER, 0 HIGH, 0 MEDIUM, 2 LOW (cosmetic / equivalent mutant; neither a flip nor a hole).
py_compile of the three changed files OK, modules import. Full suite foreground, nothing else running: `Ran 2052 tests`, `OK (skipped=2)` (/tmp/41-01-review9-suite.log). `git status --short` identical before and after everything (/tmp/41-01-review9-status-before.txt).

## Review-8 issue: verification

Review-8 Issue 1 (`2<.. > /dev/stderr/../zz`): FIXED. `_passes_through_process_link` (pretool_hook.py:61) is called in `_is_redirect_target_allowed` (:1054, after the exact-sink and `/dev/tcp` returns, before realpath) and `_unresolved_target_allowed` (:1152, on the literal directory part). It walks the path lexically (empty and `.` dropped, `..` pops) and refuses any further segment after `/dev/fd/<seg>`, `/dev/std{in,out,err}`, `/proc/{self,thread-self,<pid>}`.

Bash oracle (/tmp/41-01-review9/oracle.py; scratch /dev/shm/r9; the validator process has fds 0-2 dup'd onto a file under /tmp so a realpath through them would land inside the roots; every gate "allow" is actually run in bash with cwd = workspace, V empty): 696 commands = 29 prefixes x 4 tails (`/../zz`, `/../../zz`, `/x`, `/`) x 6 operator forms (`>`, `>>`, `&>`, `1<.. >`, `2>`, quoted). Prefixes: `/dev/fd/{0,2}`, `/dev/std{in,out,err}`, `/dev/./stderr`, `/dev//fd/2`, `/dev/../dev/stderr`, `/proc/self/{fd/2,cwd,root}`, `/proc/thread-self/{fd/2,cwd}`, `/proc/<pid>/{fd/2,cwd,root}`, `/proc/self/../self/cwd`, `/proc/self/./cwd`, `/proc//self/cwd`, `/proc/self/task/1/cwd`, `/proc/self/fd/../cwd`, workspace symlinks `ln1 -> /proc/self/cwd` and `ln2 -> /dev/fd`, and unresolvable `/dev/fd/$V`, `$V/dev/fd/2`, `/proc/self/$V`, `/dev/std$V`, `/proc/$V/cwd`. Result: every resolved and unresolvable per-process-link form is refused. The only 24 allows are `ln1/x`, `./ln1/x`, `ln1/` (workspace symlink to /proc/self/cwd, with no `..`), all of which bash wrote inside the workspace: 0 harmful. (Allow of a bare symlink to /proc/self/cwd is resolved by realpath in the hook's cwd; it only diverges if the command first `cd`s, which is the accepted in-command-state family and out of scope; identical at HEAD.)

Still allowed (with echo/ls/grep allowlisted): `2>/dev/null`, `ls 2>/dev/null`, `&>/dev/null`, `>&2`, `> /dev/fd/2`, `> /dev/fd/1`, `2>/dev/stderr`, `>/dev/tty`, `>/dev/stdout`, `2>&1`, `(ls 2>/dev/null) | grep a`, `( (ls 2>/dev/null) )`, `echo $(ls 2>/dev/null)`, `> a.log 2>&1`. (Under an echo-only allowlist the `(ls ...) | grep a` row asks only because ls/grep are not allowlisted.)

## Completeness Check

- D1-D4, task table: unchanged since pass 8; suite green; met.
- Must-allow rows from fix reports 4/5/6, clean workspace, allowlist echo/ls/grep/date: `./$V`... all as in review 8: `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `case_$(date)`, `./$(echo showcase)`, `./out_*.txt`, `out_$((1 + 1)).txt`, `out_$[ 2 * 3 ].txt`, `"out_$V".txt`, `/tmp/.$i.log`, `/tmp/jit_$i.txt`, `>> ./$V` allow; `> $V/../x` asks. (Note: a workspace that still holds symlinks, e.g. my leftover ln1/ln2, makes `./$V` ask by design, R5.)

## HEAD-vs-change fuzz

30000 generated redirect commands (review-8 generator plus `/proc/self/`, `/proc/self/cwd/`, `/dev/stderr/`, `/dev/fd/2/`, `/proc/thread-self/`, `cwd`, `fd`, `root` pieces), HEAD copy via `git archive HEAD .claude/hooks` under /tmp/41-01-review9/head vs change, same seed: ask->allow 298, allow->ask 9693, 0 exceptions. Each of the 298 flips run in bash x 21 values of V in a /dev/shm fixture, snapshot diff: 0 writes outside the roots or into a protected path. No harmful flip.

## Issues Found

### Issue 1: [severity: LOW]
- **File:** .claude/hooks/pretool_hook.py:57 (`_PROCESS_LINK`) / tests
- **Problem:** `/dev/fd/[^/]+` vs `/dev/fd/[0-9]+` is an equivalent mutant (mutation m6 survives): a non-numeric `/dev/fd/<x>` is not a link, and such targets are refused anyway by the roots / R1 checks. Harmless over-breadth, untested.
- **Fix:** optional; use `[0-9]+` or leave.

### Issue 2: [severity: LOW]
- **File:** .claude/hooks/pretool_hook.py:~1026 (docstring of `_is_redirect_target_allowed`)
- **Problem:** cosmetic carry-over from review 8: the line "Allowed: the write-safe /dev sinks (and an exact `/dev/fd/<digits>`), the" exceeds the wrap width; the docstring does not mention the new per-process-link refusal (it is documented in the in-code comment and helper docstring).
- **Fix:** optional rewrap and one sentence.

## Code Quality Notes

- Full pretool_hook.py diff re-read top to bottom: no dead code, docstrings match code, fail-closed posture kept (`except -> False`), no duplicated tests (`test_41_process_link_*` appear once; sinks test overlaps lightly with `test_dev_sinks_and_fd_allowed`, acceptable).
- The test points the process fds 0-2 at a /tmp file so the old code could not pass by accident; it restores and closes fds and removes its temp dir.

## Mutation proofs

Protocol: `git status --short` saved before; `cp` backup /tmp/41-01-review9-mut/pretool_hook.py.bak; one edit per run; target `cd tests; python3 -m unittest test_integration_pretool.TestRedirectTargets` (62 tests, baseline OK); restored by `cp` and `diff` identical after each; final `git status --short` identical. No git state-changing command used.

| # | Mutation | Result |
|---|---|---|
| m1 | resolved branch: `if _passes_through_process_link(target)` -> `if False` | `test_41_process_link_paths_are_refused` FAILS |
| m2 | unresolvable branch: `if _passes_through_process_link(directory)` -> `if False` | same test FAILS |
| m3 | helper never reports a link (`if stack and ...` -> `if False`) | same test FAILS |
| m4 | drop `thread-self` from the pattern | same test FAILS |
| m5 | `..` no longer pops the stack | same test FAILS |
| m6 | `/dev/fd/[^/]+` -> `/dev/fd/[0-9]+` | survives (equivalent, Issue 1) |

## Questions for User

None
