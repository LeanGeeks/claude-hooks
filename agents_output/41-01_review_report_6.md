# Review Report - 41-01 (pass 6): the gate reads the real target

## Verdict: FAIL

1 HIGH, 1 MEDIUM, 2 LOW. py_compile and import pass. Full suite (foreground, nothing else running):
`Ran 2046 tests`, `OK (skipped=2)` (/tmp/41-01-review6-suite.log). Mutation proofs: 6 killed, 2 survived (see below).
Tested copy: repo working tree (uncommitted, base HEAD 05b9fdd), default mode, nothing installed. All bash proofs ran in
scratch workspaces under /dev/shm with HOME patched; no real path was written. `git status --short` identical before and after
every mutation and at the end.

## Review-5 issues: verification

- HIGH 1 (R5 wildcard): FIXED. `ln${V:-k}s`, `./ln$(echo k)s`, `l"$(echo nks)"`, `es$(echo c)`, `./h${V:-c}` ask beside the
  fixture symlinks (rows in `_REFUSED_BY_RULE_41["R5"]`, both cwds); `V=ln?s; ./$V` asks. Mutation of the wildcard line is killed (22 failures).
- HIGH 2 (case-paren): FIXED for `case` and the other early ends the fixer listed (`#`, `${..)}`, `"$(..)"`, `<<`, `$( (subshell)`).
  `rd$(case x in x) echo ;; esac)/../.claude/settings.json` now yields the sentinel and asks. See Issue 1 for a sibling that is not closed.
- LOW test isolation: FIXED. `~/.claude/bash_manual_confirm.log` stat before and after
  `cd tests; env -u CLAUDE_MANUAL_CONFIRM_LOG python3 -m unittest test_integration_pretool` (443 tests, OK): both `20:32:19.88 14852393`, unchanged.
  (The log did grow later during this session; that is this session's own installed hook, not the tests.)

## Completeness Check

- Task table: every row is covered by a test (walked it against the diff; the six-test folding kept all rows). met.
- Must-allow rows (item 4): `./$V`, `.$V`, `x$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `out_$(date).txt`, `/tmp/.$i.log`,
  `./out_*.txt`, `echo $(ls 2>/dev/null)`, `(ls 2>/dev/null) | grep a`, `( (ls 2>/dev/null) )`, `echo k >> ./$V`, `showcase`,
  `case_$(date)`, `./$(echo showcase)` all `allow` from a clean workspace root and from its `sub/` (my own run, /dev/shm/r6clean).
  In a workspace that holds an outward symlink `./$V` correctly asks. met.
- HEAD-vs-change (item 3): 40000 generated redirect commands x 3 cwds, HEAD copies vs change: ask->allow 1410, allow->ask 10909.
  Every ask->allow flip was run in bash against a scratch workspace with 4 values of V: 0 writes outside the roots or into a
  protected path. Breakdown: `$'x'`/`$"x"` quoting (405), a lone `$` or `$` + non-expansion char that is a literal file name (e.g.
  `> $`, `> $~`; bash writes the file `$`), `$[ … ]` words (HEAD allows the same real escape, see Issue 1), constructs that complete a
  non-existent name (13 residual rows, all oracle-benign). No flip is outside the paren row and the quoting rows in a harmful way. met.
- Adversarial oracle fuzz (item 2): ~60000 random words (pieces: literals, `..`, `~`, quotes, `$'..'`, `${V:-}`, `$(..)`, backticks,
  `$(( ))`, `$[ ]`, case/comment/`${V:-)}` spellings, globs, extglobs, brace expansion, process substitution, `\<newline>`) x 5 operators x
  cwd {ws, ws/sub, workspace=HOME}, fixture with symlinks into `.claude`, `~/.claude`, outside dir, a symlinked file and dir, run in bash for
  every `allow`. Hits: only the `$[ … ]` class (Issue 1). Held: `$'..'`, `$"..."`, `${V/…}`, brace/glob/extglob, `~user`/`~+`/`~-`,
  `>&$V`, `<>`, `>|`, `exec 3>`, quoted metacharacters, backslash-newline, cwd symlink, HOME workspace.

## Issues Found

### Issue 1: [severity: HIGH, pre-existing (HEAD allows the same), not a flip] A legacy arithmetic `$[ … ]` containing blanks or `<>&|` cuts the target word

- **File:** .claude/hooks/bash_command_parser.py `_dollar_expansion_end` / `_read_redirect_word` (no `$[` case; the `[` is read as a glob character, then a blank ends the word)
- **Problem:** bash treats `$[expr]` as one word (arithmetic expansion). The reader sees `$` literal, `[` glob, and stops at the first blank, so the target is a
  prefix and the rest, including `]/../…`, is dropped. Verified in bash (workspace has a real directory `r0`, symlink `es5 -> ../outside/f`):
  ```
  echo k > r$[0 + 0]/../.claude/settings.json     target 'r$[0'   allow   bash overwrote ws/.claude/settings.json
  echo k > r$[ 0 ]/../../home/.claude/settings.json target 'r$['  allow   bash overwrote ~/.claude/settings.json
  echo k > r$[ 0 ]/../../outside/f                 target 'r$['   allow   bash wrote outside the roots
  echo k > es$[2 + 3]                              target 'es$[2' allow   writes through symlink es5 to outside/f
  ```
  The random oracle fuzz hit it 4 times in 3 seeds (e.g. `echo k >| es$[1 +1]`). HEAD gives the same allow (token-based), so it is not a flip,
  but it is exactly the defect brd §1.3 and task part A name ("the path bash will write to, or reported unresolvable — never shorter") and it
  needs no symlink at all. Review 5 listed `$[…]` as held; it holds only without blanks and `<>&|`.
- **Fix:** in `_dollar_expansion_end` (or in `_read_redirect_word`) handle `$[`: scan to the matching `]` (nesting `[`/`]`) and treat it as an
  EXPANSION part (as `$((…))`), or fail closed with the unproven sentinel when a `$[` is met. Add rows to `_EARLY_END_41`/R-table:
  `r$[0 + 0]/../.claude/settings.json`, `es$[2 + 3]`, `r$[1|0]/x`. Not part of the three out-of-scope command-matching items.

### Issue 2: [severity: MEDIUM] the arithmetic guard's balanced-paren sub-condition in `_unproven_paren_end` is unpinned by any test

- **File:** .claude/hooks/bash_command_parser.py:2207 (`command[end - 2:end] == '))' and depth == 0 and …`)
- **Problem:** mutation `depth == 0` -> `True` leaves `TestRedirectTargets` green (56 tests OK). It changes behaviour (`rd$((echo a) | (cat))/x` goes from sentinel to
  an ordinary expansion) but I could not construct an input where the reader's end is then wrong (400k random bodies, bash -n as the oracle for
  where the substitution really ends: 0 early ends). So it is either dead defence or an unpinned one; either way nothing in the tests says which.
- **Fix:** add a row that only this condition refuses (`rd$((echo a) | (cat))/x` expecting the sentinel, and the comment form `$((echo a) #))\n)`),
  or drop the sub-condition if it is intentionally redundant with the `))`/regex checks.

### Issue 3: [severity: LOW] dead or overlapping code left from the fix passes

- `_read_redirect_word` returns `(target, end)` but its only caller uses `read[0]`; the `end` element (and the three `return …, end` forms) is dead.
- The `UNREAD_REDIRECT_TARGET` branch in `_scan_write_targets` is documented as unreachable; acceptable as a fail-closed guard, but no test reaches it.
- The `~+`/`~-`/`~N` -> unresolvable rule in `_read_redirect_word` is held only by a second, independent refusal (R2 refuses any `~…` that `expanduser` leaves
  unexpanded): mutating the parser rule leaves the suite green (mutation 7). Pin it with a parser-level assert (`unresolvable` is True) or note the overlap.
- Duplicate coverage: `test_41_anchored_variable_leaf_still_allowed`, `test_41_lenient_fallback_unexpandable_user_refused`,
  `test_41_lenient_fallback_refuses_dotdot_anywhere` restate rows now in `_MUST_ALLOW_41` / `_REFUSED_BY_RULE_41` (they differ only by asserting the target text).

### Issue 4: [severity: LOW] state.md and the 41-02/41-03 task files still carry the old names

`_literal_prefix_inside_allowed_roots` / `anchor_len` remain in the shared-edit-point table (the Log already notes it). No code or docstring in the hooks mentions them. Orchestrator item only.

## Code Quality Notes

- Read top to bottom: the `_unresolved_target_allowed` docstring matches the code (R0-R6, R5 wildcard text, residual wording). `RedirectTarget` docstring matches `_read_redirect_word`.
  No unused imports (`RedirectTarget` is used by the hook), `_strip_quotes` removal leaves no caller. `_judge_redirect_word` and the trap-handler path agree.
- `_UNPROVEN_PAREN_RE` is deliberately over-inclusive (a quoted `"case"` also asks); accepted by the fixer, fine.
- Fail-closed posture preserved: `_judge_redirect_word` and R5 filesystem work `except -> False`. Only R0-R4 sit outside the inner `try`, covered by the outer handler.
- The `sub/` semantics (roots = cwd) are today's; rerun the sub rows after 41-02.

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-01-review6-status-before.txt); backups `cp` to /tmp/41-01-review6-mut/*.bak; one replacement per run;
test run = `cd tests; python3 -m unittest test_integration_pretool.TestRedirectTargets` (56 tests; direct run is isolated, see above);
restore with `cp`, `diff` identical every time; `git status --short` identical after. No git state-changing command.

| # | Mutation | Result |
|---|---|---|
| 1 | R5 wildcard: `pattern.append('.*')  # a construct` -> `pattern.append('')` | KILLED: 22 failures (R5 rows `ln${V:-k}s`, `l"$(echo nks)"`, … both cwds) |
| 2 | `_unproven_paren_end`, `<<` alternative removed | KILLED: `test_41_other_early_ends…` (`rd$(cat <<EOF…`) |
| 3 | `_unproven_paren_end`, `${…)}` alternative disabled | KILLED: 3 failures (`${V:-)}`, `${V//)/x}`, `>(echo ${V:-)})`) |
| 4 | `_unproven_paren_end`, arithmetic `depth == 0` -> `True` | SURVIVED (Issue 2) |
| 5 | R2: `if '..' in directory.split('/')` -> `if False` | KILLED: 2 failures (R2 rows `sub/../x$V`, both cwds) |
| 6 | task-table row (inner redirect of a substitution): `targets.extend(self._scan_write_targets(t_val, _depth + 1))` -> `pass` | KILLED: 7 failures (`$(echo x > /etc/passwd)`, backticks, depth-cap, plain substitution) |
| 7 | `~+`/`~-` unresolvable rule (`login[:1] in ('+','-')` -> `in ()`) | SURVIVED (R2 refuses the unexpanded `~+/x` anyway; Issue 3) |

Mutations 1, 2/3, 5, 6 cover the required set (R5 wildcard, one `_unproven_paren_end` sub-condition other than `case`, R2, one task-table row).

## Questions for User

None.
