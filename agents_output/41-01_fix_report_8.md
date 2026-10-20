# 41-01 fix report 8

## Change
- .claude/hooks/pretool_hook.py:56-82 new module helper `_passes_through_process_link(path)`: walks the path lexically, segment by segment (empty and `.` dropped, `..` pops the preceding segment), and returns True if any further segment (`..` included, trailing `/` counts) follows a per-process link (`/dev/fd/N`, `/dev/stdin|stdout|stderr`, `/proc/self|thread-self|<pid>`).
- :1054 in `_is_redirect_target_allowed`: after the exact-sink / `/dev/tcp` returns, before any realpath, a target through such a link is refused. The exact sinks from fix 7 are untouched (they return earlier).
- :1152 in `_unresolved_target_allowed`: the literal directory part through such a link refuses.
- Note: `/proc/self/**` and `/proc/<pid>/**` are refused whole (superset of fd/cwd/root/task).

## Tests (tests/test_integration_pretool.py, TestRedirectTargets, 62 tests)
- `test_41_process_link_paths_are_refused`: 19 resolvable spellings (incl. `/dev//fd/2/..`, `/dev/./stderr/..`, `/dev/../dev/stderr/..`, `/proc/self/fd/2/..`, cwd/root, thread-self, real getpid) with and without `2<..`, plus 5 unresolvable (`$V`) spellings. It points the test process's fds 0-2 at a file under /tmp first, so realpath through the hook's own fds would land INSIDE the roots (otherwise the old code refused these by accident, because the fake workspace is outside).
- `test_41_process_link_sinks_still_allowed`: `2>/dev/null`, `&>/dev/null`, `>&2`, `> /dev/fd/2`, `2>/dev/stderr`, `>/dev/tty`.

## Mutation proof (cp backup, restored with diff, git status identical before/after)
m1 disable check in `_is_redirect_target_allowed` -> FAIL; m2 disable in `_unresolved_target_allowed` -> FAIL; m3 stop dropping `.` segments -> FAIL; m4 drop `thread-self` from the pattern -> FAIL. (An earlier draft that matched `/./` forms only after normpath was caught by the same test: `/dev/../dev/stderr/../zz` allowed, which drove the segment-walk design.)

## Oracle (bash, cwd /dev/shm/f8/ws, script /tmp/41-01-fix8-oracle.py runs both validator and real bash)
Refused (ask): `2<.. > X` for X in `/dev/stderr/../zz`, `/dev/stdout/../zz`, `/dev/fd/2/../zz`, `/dev//fd/2/../zz`, `/dev/./stderr/../zz`, `/proc/self/fd/2/../zz`, `/proc/self/cwd/../zz`, `/proc/thread-self/fd/2/../zz`, `/proc/self/root/../zz`. Bash did write `/dev/shm/zz` for the stderr/fd/proc-fd spellings, confirming the hazard.
Allowed: `2>/dev/null`, `&>/dev/null`, `>&2`, `> /dev/fd/2`, `2>/dev/stderr`, `>/dev/tty`.

## Suite
`python3 tests/run_all_tests.py`: Ran 2052, OK (skipped=2).

## Known limit
Lexical only: a symlink directory pointing at /dev or /proc (`/tmp/ln/stderr/..`) is not caught (same in-command-state family as accepted holes).

## Questions for User
None
