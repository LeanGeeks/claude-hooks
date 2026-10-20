# 41-01 fix report, pass 7

## Issue 1 (MEDIUM): /dev/fd/ prefix whitelist
- Change: .claude/hooks/pretool_hook.py `_is_redirect_target_allowed` (~line 1019-1022): `target.startswith('/dev/fd/')` replaced by `re.fullmatch(r'/dev/fd/[0-9]+', target)`; docstring updated. Anything else under /dev/fd/ now goes through normal resolution / the unresolvable path.
- Other /dev sinks: SAFE_REDIRECT_DEVICES is already a set with exact `in` match (/dev/null, /dev/stdout, /dev/stderr, /dev/tty), so no change. NET_REDIRECT_PREFIXES (/dev/tcp/, /dev/udp/) stays a prefix by deliberate operator opt-in (documented trade-off; bash treats those names as socket hosts, not paths).
- Test: tests/test_integration_pretool.py `test_41_dev_fd_is_exact_digits_only` (refuses /dev/fd/../../../etc/x, ../../etc/x, /dev/fd/3/../.., /dev/fd/$V, /dev/fd/, 3x, -1; allows /dev/fd/2, /dev/fd/10, `2>/dev/stderr`, `>&2`). Existing `2>/dev/null` and `/dev/fd/3` tests still pass.
- Mutation: restoring `startswith('/dev/fd/')` makes the new test fail; restored with cp, diff clean, git status identical.
- Oracle: in a /dev/shm scratch dir, `bash -c 'echo k > /dev/fd/../../../dev/shm/<scratch>/zz'` created zz (bash does write it); the gate now refuses that form.

## Issue 2 (LOW): temp dir leak
- Change: tests/test_integration_pretool.py: `import atexit` and `atexit.register(shutil.rmtree, str(_tmp_store), ignore_errors=True)` directly after the mkdtemp; env setdefault isolation untouched.
- Verification: direct `python3 -m unittest test_integration_pretool` (from tests/; 447 tests OK) left ~/.claude/bash_manual_confirm.log mtime unchanged and /tmp/claude-hooks-tests-* count unchanged. Mutation (atexit line removed) leaked one dir (102 -> 103); restored, no further leak. Note: 103 dirs now remain in /tmp (102 older leaks plus the one from the mutation run), not removed by me.
- Note: `python3 -m unittest tests.test_integration_pretool` from the repo root fails to import (no tests package); pre-existing, run from tests/.

## Suite
python3 tests/run_all_tests.py: Ran 2050 tests, OK (skipped=2). Log /tmp/41-01-fix7-suite.log.

## Questions for User
None
