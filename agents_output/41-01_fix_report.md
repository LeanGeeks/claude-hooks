# Fix Report - 41-01

## Issue 1 (HIGH) `..` glued to an expansion
- Change: `.claude/hooks/pretool_hook.py` `_literal_prefix_inside_allowed_roots`. The target is split whole into segments. A literal `..` segment is refused. A segment with a construct (`$`, backtick, `*`, `?`, `[`) whose literal head is only dots is also refused (`..$V`, `.$V.`, `.$V`). This restores the HEAD refusal and also covers `.$V./x`.
- Tests: `test_41_dotdot_glued_to_an_expansion_is_refused` (7 commands, including `/tmp/..$V/x` and `..$V/x`). `test_41_dotted_names_without_traversal_stay_allowed` guards `/tmp/.hidden_$V` and `./out.$V.log`.
- Mutation: the dots check replaced with `if False` failed 6 subtests. Restored with cp and diff.
- Accepted friction: `/tmp/.$i.log` (head `.` then a construct) now asks.

## Issue 2 (HIGH) Protected path completed after a construct
- Decision (no need to ask the user): an unresolvable target is refused when it has a `.claude` segment. It is also refused when a segment has a construct whose literal head is a non-empty prefix of `.claude` (`.c*`, `.cl$V`). This keeps invariant 2: the gate does not vouch for a path it cannot read to its end. The cost is that unresolvable targets under `.claude/` now ask, which is rare and safe.
- Change: same function. The docstring rules are updated.
- Tests: `test_41_protected_path_completed_after_a_construct` (6 commands: `.claude/settings$V.json`, `.cl$V/settings.json`, `.claude/$(echo hooks)/x`, `.claude/s*.json`, `.c*/settings.json`, `V=;` form). `test_41_other_unresolvable_names_in_the_workspace_stay_allowed` is the allow-side guard.
- Mutations: removing the prefix check failed 2 subtests. Removing the `.claude` segment check failed 4 subtests. Both restored with cp and diff.
- Residual: a symlink named with a construct that leads into `.claude` (`lnk$V/settings.json`) cannot be read; this is the same class as the accepted `x$V` residual.

## Issue 3 (LOW) Extglob prefix anchor
- Change: `.claude/hooks/bash_command_parser.py` `_read_redirect_word`, `(` branch. When the char before the group is `@`, `+` or `!`, `anchor_len` is set one char earlier. `?` and `*` already set the anchor through the glob branch.
- Test: `test_41_extglob_prefix_is_not_an_anchor` (`+(/)etc/x`, `!(a)/etc/x`).
- Mutation: the check replaced with `if False` failed that test. Restored with cp and diff.

## Verification
- py_compile OK on both hooks. `git status --short` before and after the mutations is identical. No forbidden git ops. Backups are in /tmp/41-01-fix-mut.
- TestRedirectTargets: 49 tests OK.
- Full suite (`python3 tests/run_all_tests.py`, log /tmp/41-01-fix-suite.log): Ran 2039 tests, OK (skipped=2). That is 2034 plus 5 new tests.

## Questions for User
None. The review's question on D4 (literal prefix only versus any `.claude` segment) was resolved conservatively; revert the `.claude` segment rule if the user prefers the documented residual.
