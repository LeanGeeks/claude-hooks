# Review Report - 41-01 (pass 7): the gate reads the real target

## Verdict: PASS

0 BLOCKER, 0 HIGH, 1 MEDIUM (pre-existing, not a flip, not 41-01's change), 2 LOW.
py_compile of all hooks and the test module OK; modules import. Full suite (foreground, nothing else running):
`Ran 2049 tests`, `OK (skipped=2)` (/tmp/41-01-review7-suite.log). `git status --short` identical before and after every mutation and at the end.
Tested copy: repo working tree (uncommitted, base HEAD 05b9fdd), nothing installed. All bash proofs ran in /dev/shm scratch workspaces with HOME patched.

## Review-6 issues: verification

- HIGH 1 (`$[ ... ]` cut the word): FIXED. `_dollar_expansion_end` reads `$[`, `_scan_legacy_arith` nests `[`/`]`, `_unproven_paren_end` has a `$[` branch (proven only when closed on `]`, balanced `[]` and `()`, arithmetic-only body) else the unproven sentinel. Oracle: `r$[0 + 0]/../.claude/settings.json`, `r$[ 0 ]/../../home/.claude/settings.json`, `es$[2 + 3]` etc. all ask now (HEAD copy: bash overwrites, allow). Mutation proofs below.
- MEDIUM 2 (`depth == 0` unpinned): FIXED, now `_balanced(body, '()')`; `test_41_arithmetic_shaped_subshell_pair_is_refused` kills the mutation (m5).
- LOW 3 dead code: FIXED. `_read_redirect_word` returns `Optional[RedirectTarget]`, caller uses it directly; `UNREAD_REDIRECT_TARGET` branch is reached by `test_41_an_unreadable_operand_is_refused_not_dropped`; `~+`/`~-` unresolvable rule is pinned at parser level (m6 killed); the three overlapping tests are gone, rows folded into `test_41_lenient_fallback_reports_the_real_target`.
- LOW 4 stale names in state.md / 41-02 / 41-03: still orchestrator's (not touched).

## Completeness Check

- Task table rows: all covered by tests (walked the test names against the table); met.
- Must-allow (item 4), run from clean root and from `sub/` in /dev/shm/r7clean: `./$V`, `.$V`, `x$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `out_$(date).txt`, `/tmp/.$i.log`, `./out_*.txt`, `echo $(ls 2>/dev/null)`, `(ls 2>/dev/null) | grep a`, `( (ls 2>/dev/null) )`, `>> ./$V`, `showcase`, `case_$(date)`, `./$(echo showcase)`, `out_$[ 1 + 1 ].txt`, `./out_$[ 2 * 3 ].txt`, `out_$((1 + 1)).txt`, `"out_$V".txt`, `/dev/null`, `&>/dev/null`, `> a.log 2>&1` all allow. `~/x.txt` and `$(pwd)/x` ask as designed. met.
- Spec C/D (fallback, protected paths): met (mutation m4 on `settings.local.json` killed).

## Adversarial probing (item 2): 0 hits in scope

Bash oracle = real redirect run in /dev/shm fixture (ws, ws/sub, HOME=workspace; symlinks into `.claude`, `~/.claude`, outside dir/file; V in 21 values), hit = gate `allow` and bash wrote outside the roots, to `<root>/.claude/{settings.json,settings.local.json,hooks}` or anywhere in `~/.claude`.
- Templated random fuzz (/tmp/41-01-review7/o7.py): ~60 command templates (compound `{ ; } >`, `( ) >`, `while/if/for ... done >`, function-definition redirect, `exec > / 2> / 5> / 5<>`, `coproc`, here-strings, `2>&1 >`, `>&`, `&>`, `&>>`, `>|`, `<>`, `N>`, `{fd}>`, double redirects, redirect then `;`/`&&`/`|`/`&`/`)`, case body, heredocs, `$(…)`/backtick/`>(…)` wrappers) x the review-6 word generator (incl. `$[…]`), 8 seeds, ~110000 commands, ~45000 gate allows run in bash: 1 flagged, `echo k >(cat > lnk/~-)` writing `ws/.claude/~-` -- not a finding: a resolved target is protected only for settings.json / settings.local.json / hooks per brd D4, and `.claude/<other>` is allowed by spec.
- Recursive word-end generator (/tmp/41-01-review7/gen.py): `r` + construct (`$[ … ]`, `$(( … ))`, `$( … )` over ~35 command bodies incl. `case`, `#)` comments, heredocs with `)`, nested `${U:-)}`, quotes, backticks, `${U:-…}`) + `/../.claude/settings.json` or `/../../outside/f`, `bash -n` filtered, 5 seeds, ~50000 valid commands: 0 allows at all in the change (every dir-position construct asks), 0 hits. Same generator on the HEAD copy: hits (e.g. `echo k > r$( echo 0)/../.claude/settings.json` overwrote ws/.claude/settings.json), so it is sensitive.
- Hand lists (~130 commands, both cwds): `>&file`, `>&$V`, `2>&file`, `N>&M-`, `&>`, `&>>`, `>|`, `<>`, `0<>`, `{fd}>`, `exec 3>`/`3<>`/`>`, here-strings, multiple redirects in either order, `{ }`/`( )`/`while`/`if`/function/coproc redirects, comments, trailing `;`/`&`/`|`/`)`, malformed/unterminated operands, `>(…)`, `<(…)` operands, quoting/backslash/`$'…'`/`$"…"` spellings of a symlink to settings.json: 0 hits.

## HEAD-vs-change fuzz (item 3)

60000 generated redirect commands (templates above) x 3 cwds, HEAD copies (`git show HEAD:<file>` under /tmp/41-01-review7/head) vs change: ask->allow 767, allow->ask 14665, 0 exceptions. Each of the 767 ask->allow flips run in bash x 21 values of V: 0 writes outside the roots or into a protected path. No harmful flip.

## Issues Found

### Issue 1: [severity: MEDIUM, pre-existing at HEAD and unchanged by 41-01, not a flip]
- **File:** .claude/hooks/pretool_hook.py `_is_redirect_target_allowed` (`target.startswith('/dev/fd/')`)
- **Problem:** the `/dev/fd/` prefix whitelist is not a path check. `echo k > /dev/fd/../../../etc/x` is `allow` (HEAD too) and bash writes `/etc/x` (verified: `/dev/fd/../../../dev/shm/r7clean/zz` created the file). `/dev/fd/$V` likewise. Already noted in review 1 as "unchanged"; it is exactly the "never a different path" contract (brd §1.3) so it belongs in the follow-up epic with the tokenizer items, or in 41-02.
- **Fix:** allow only `re.fullmatch(r'/dev/fd/\d+', target)`; anything else under `/dev/fd/` goes through the normal resolution.

### Issue 2: [severity: LOW]
- **File:** tests/test_integration_pretool.py:33 `_tmp_store = Path(tempfile.mkdtemp(...))` at import: a new temp dir per direct run, never removed. Harmless; `run_all_tests.py` already isolates. Optional: `atexit` rmtree.

### Issue 3: [severity: LOW, orchestrator]
- state.md and the 41-02/41-03 task files still say `_literal_prefix_inside_allowed_roots` / `anchor_len`.

Also known and out of scope (unchanged): the gate judges relative targets against the payload cwd, so `cd /etc && echo k > x` is `allow` at HEAD and now (review 1 note).

## Code Quality Notes

- Diff read top to bottom as new: no dead code found; `_read_redirect_word` docstring, `RedirectTarget` docstring, `_unresolved_target_allowed` R0-R6 text, `_dollar_expansion_end` and `_scan_legacy_arith` docstrings match the code. `_judge_redirect_word` and the trap-handler path agree. Fail-closed posture kept (`except -> False`).
- `_UNPROVEN_PAREN_RE` is deliberately over-inclusive (a quoted `"case"` asks); acceptable.
- No duplicate tests spotted among the 40 `test_41_*` methods; the former duplicates are folded.

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-01-review7-status-before.txt); `cp` backup to /tmp/41-01-review7-mut/*.bak; one replacement per run; target = `cd tests; python3 -m unittest test_integration_pretool.TestRedirectTargets` (59 tests); restore by `cp`, `diff` identical, `git status --short` identical, every time. No git state-changing command used.

| # | Mutation | Result |
|---|---|---|
| m1 | `$[` branch of `_unproven_paren_end` -> `return False` (never refuse) | KILLED, 8 failures (`r$[ 0`, `$(echo ])`, `"]"`, `${V:-]}`, `;`, `\]`, `r$[ [ ]`) |
| m1b | `$[` scan in `_dollar_expansion_end` disabled | KILLED, many failures (`_WHOLE_WORD_41` rows, dir-position rows both cwds) |
| m2 | R0 (`parts[0][0] not in (LITERAL, TILDE)` check dropped) | KILLED (`test_41_an_unreadable_operand…`, `…case_inside_a_target_substitution…`, `$(case …)` alone, etc.) |
| m3 | R4 (`'/' in source or '..' in source or '\\' in source` -> False) | KILLED (R4 rows `x${V/x/\\/}`, `x${V:-/../../etc/x}`, `.c"$(echo laude/settings.js)"on`, R1 row) |
| m4 | task-table row / D4: `settings.local.json` protected path removed | KILLED (`test_41_protected_settings_inside_the_workspace`, `echo x > .claude/settings.local.json`) |
| m5 | `$((` branch `_balanced(body,'()')` -> True (review-6 MEDIUM) | KILLED (`test_41_arithmetic_shaped_subshell_pair_is_refused`) |
| m6 | `~+`/`~-` parser rule `login[:1] in ('+','-')` -> `in ()` (review-6 LOW) | KILLED (`test_41_directory_stack_tilde_is_unresolvable_in_the_parser`, 4 failures) |

## Questions for User

None.
