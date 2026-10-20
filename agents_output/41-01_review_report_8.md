# Review Report - 41-01 (pass 8): the gate reads the real target

## Verdict: PASS

0 BLOCKER, 0 HIGH, 0 MEDIUM, 1 LOW (pre-existing, identical at HEAD, not a flip).
py_compile of the three changed files OK. Full suite foreground, nothing else running: `Ran 2050 tests`, `OK (skipped=2)` (/tmp/41-01-review8-suite.log). `git status --short` identical before and after all mutations and at the end.

## Review-7 issues: verification

- MEDIUM 1 (`/dev/fd/` prefix): FIXED. `_is_redirect_target_allowed` uses `target in SAFE_REDIRECT_DEVICES or re.fullmatch(r'/dev/fd/[0-9]+', target)`; anything else falls to normal resolution. Bash oracle (/dev/shm scratch, 146 candidate spellings x 5 operators: `/dev/fd/../…`, `/dev/fd/$V`, `/dev/fd/${V}`, `/dev/null/../…`, `/dev/stdout|stderr|tty/..`, `/dev//fd/…`, `//dev/fd`, `/dev/./fd`, `/dev/fd/N/../…`, `/dev/tcp/../…`, `/dev/shm/../fd/…`, `/dev/null$V`): 165 gate allows run in bash, 0 wrote outside the roots. (The `/dev/stderr/../…` allows exist only because the gate's own fd 2 realpaths into /tmp here; bash gets ENOTDIR. See Issue 1 for the one way that becomes real.)
- Still allowed: `2>/dev/null`, `&>/dev/null`, `>&2`, `> /dev/fd/2`, `2>/dev/stderr`, `>/dev/tty`, `>/dev/stdout`, `2>&1`, `(ls 2>/dev/null) | grep a`.
- LOW 2 (temp dir leak): FIXED. `atexit.register(shutil.rmtree, ...)` after the mkdtemp. Direct run from `tests/` (`python3 -m unittest test_integration_pretool`, 447 OK): `~/.claude/bash_manual_confirm.log` mtime and size unchanged (14933584 bytes, same mtime), `/tmp/claude-hooks-tests-*` count 104 before and 104 after (no new leak). Note: `python3 -m unittest tests.test_integration_pretool` from the repo root cannot import (no `tests` package, pre-existing); I used the `tests/` form.

## Completeness Check

- Task table rows and D1-D4: unchanged since pass 7; suite green; met.
- Must-allow rows (fix reports 4/5/6 plus review 7 list), both cwds `ws` and `ws/sub` in a clean workspace: `./$V`, `.$V`, `x$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `out_$(date).txt`, `/tmp/.$i.log`, `./out_*.txt`, `>> ./$V`, `showcase`, `case_$(date)`, `./$(echo showcase)`, `out_$[ 1 + 1 ].txt`, `./out_$[ 2 * 3 ].txt`, `out_$((1 + 1)).txt`, `"out_$V".txt`, `/tmp/jit_$i.txt`, `> a.log 2>&1`, `echo $(ls 2>/dev/null)`, `( (ls 2>/dev/null) )` all allow; `> "$LOG"` and `> ~/x.txt` ask as designed.

## HEAD-vs-change fuzz

40000 generated redirect commands (review-7 templates and word generator, plus new `/dev/fd/`, `/dev/null`, `/dev/stderr`, `/dev//fd/`, `..` pieces), HEAD copies from `git show HEAD:<file>` under /tmp/41-01-review8/head vs change: ask->allow 435, allow->ask 11377, 0 exceptions. Each of the 435 flips run in bash x 21 values of V in a /dev/shm fixture: 0 writes outside the roots or into a protected path. No harmful flip.

## Issues Found

### Issue 1: [severity: LOW, pre-existing at HEAD, not a flip, same class as the in-command state holes]
- **File:** .claude/hooks/pretool_hook.py `_is_redirect_target_allowed` (`target in SAFE_REDIRECT_DEVICES`, then `realpath` of `/dev/stderr/../x`)
- **Problem:** `/dev/stdout|stderr/..` is realpathed in the hook's own process (its fd 2/1), not bash's. `echo k 2<.. > /dev/stderr/../zz` is `allow` (HEAD too) and bash writes the grandparent of cwd (verified: created `/dev/shm/zz` from a `/dev/shm/r8b/ws` workspace). Requires a command that first re-points fd 1/2 at a directory, so it is the same family as the accepted `cd` / in-command-state holes.
- **Fix:** optional, follow-up epic: refuse any target that starts with `/dev/std*`, `/dev/tty` or `/dev/null` followed by `/` instead of realpathing it.

## Code Quality Notes

- Full diff of pretool_hook.py read top to bottom: no dead code; docstrings of `_is_redirect_target_allowed`, `_judge_redirect_word`, `_unresolved_target_allowed` match the code. Cosmetic: the docstring line "Allowed: the write-safe /dev sinks (and an exact `/dev/fd/<digits>`), the" exceeds the surrounding wrap width.
- `test_41_dev_fd_is_exact_digits_only` is not duplicated elsewhere; it overlaps lightly with `test_dev_sinks_and_fd_allowed` (allow side only), acceptable.
- Fail-closed posture kept (`except -> False`).

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-01-review8-status-before.txt); `cp` backup to /tmp/41-01-review8-mut/pretool_hook.py.bak; one change per run; target `cd tests; python3 -m unittest test_integration_pretool.TestRedirectTargets` (60 tests); restore by `cp`, `diff` identical, final `git status --short` identical. No git state-changing command used.

| # | Mutation | Result |
|---|---|---|
| m1 | `re.fullmatch(r'/dev/fd/[0-9]+', target)` -> `target.startswith('/dev/fd/')` | `test_41_dev_fd_is_exact_digits_only` FAILS |
| m2 | `[0-9]+` -> `[0-9]*` | `test_41_dev_fd_is_exact_digits_only` FAILS (bare `/dev/fd/`) |
| m3 | drop the `_is_protected_write_path(resolved)` refusal in `_is_redirect_target_allowed` | `test_41_blank_bearing_constructs_in_a_directory_position_ask` and others FAIL |

## Questions for User

None
