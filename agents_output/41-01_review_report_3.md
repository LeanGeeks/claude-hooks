# Review Report - 41-01 The gate reads the real target (pass 3)

## Verdict: FAIL

1 HIGH, 1 MEDIUM, 1 LOW. Pass 2's HIGH (`./$V.claude/...`) and LOW (`/tmp/.$i.log`, `.$V`) are fixed for the forms they named, but the same root cause (a construct that expands to `/`) still glues `.claude` onto the workspace root.

## Completeness Check

- Compile/import: `python3 -m py_compile` on both hooks OK; suite imports them.
- Pass-2 HIGH, `$V.claude` / `${V}.claude` / `"$V".claude` / `$(echo).claude` / backtick / `.cl$V"aude"` / `.claude$V/...` / `.claude/set$V.json` / `.claude/hooks$V/x` / `$V/.claude/hooks/x` / `~/$V.claude/..`, from the workspace root and a subdirectory cwd (cwd = `ws/sub`): all ask. Met for the forms named.
- Pass-2 LOW (`/tmp/.$i.log`, `.$V`, `./.$V.log`, `./$V.claude.bak`): allow. Met.
- Documented allow rows (`./$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `echo $(ls 2>/dev/null)`, `(ls 2>/dev/null) | grep a`, `../index.html` asks from a subdirectory as D3 wants for a real escape): behave as documented, both cwds. Met.
- HEAD-vs-change fuzz: 1968 commands (23 `.claude` spellings x 5 tails x 8 prefixes x 2 quote forms, plus ~60 hand-picked) x 2 cwds, workspace in /dev/shm/rv3/ws (not under /tmp so /tmp being a root cannot mask results), HOME=/dev/shm/rv3/home. HEAD `ask` -> change `allow`: 36 per cwd. All benign on inspection: `(ls 2>/dev/null) | grep a` (the intended D2 fix), 5 `sub/../'$V'.claude/...` (single-quoted, a literal directory name), and 30 double-quoted `"~/.cla*/x"`-style targets (quoted tilde/glob is literal: bash writes `ws/~/.claude/x`, confirmed in bash). No HEAD ask -> allow regression.
- Ground truth in bash (printf of the target word with V in {'', '.', '..', 'x', '/', '.claude', 'claude', '.cl', 'hooks', 'settings', ws path, '../..', '/etc', home} x W in {'', 'x'}, files created under /dev/shm/rv3 only), every change-`allow` checked for a protected or out-of-root result. Findings below.

## Issues Found

### Issue 1: HIGH
- **File:** .claude/hooks/pretool_hook.py:1110-1131 (`_literal_prefix_inside_allowed_roots`, segment loop)
- **Problem:** The segment loop splits the masked target on `/`, but a construct can expand to something containing `/`, so `.claude` can begin after a construct in the middle of a segment. `.$V.claude/settings.json` has literal pieces `['.', '.claude']`; the regex `\..*\.claude` cannot match `.claude` (needs 8 characters), and the "dots only" rule does not apply, so it is allowed. With `V=/` bash writes `./.claude/settings.json`. Verified in bash in /dev/shm/rv3/ws: `V=/; echo hi > .$V.claude/settings.json` overwrote `ws/.claude/settings.json`. Same for `.$V.claude/settings.local.json`, `.$V.claude/hooks/x.py`, `./.$V.claude/...`, `.${V}.claude/hooks/x`, `"."$V.claude/hooks/x`, `.$(echo /).claude/x`, and from a subdirectory cwd (`sub/.claude/...`, which is that cwd's protected set). All gate `allow` (HEAD: also allow, so not a regression, but this is exactly the "construct glued before `.claude`" class pass 2 was asked to close). Probes: `ask` for `$V.$W.claude/x`, `$(echo .)$V.claude/x`, `` `echo /`.claude/x ``; `allow` for the ones above. A prefix that is not the root itself (`sub$V.claude`, `/dev/shm/rv3/ws$V.claude`) is fine (writes a non-protected `sub/.claude`, or asks on the dirname).
- **Fix:** Additionally refuse a construct-bearing segment when, for any construct boundary j >= 1, `'.*'.join(pieces[j:])` fullmatches `.claude` (the text after a construct that can supply a `/` can itself be a segment start). Equivalently treat a literal piece that starts with `.claude` after any construct as refusal. Over-refusal check: `.log`, `.txt`, `.claude.bak`, `_notes` do not fullmatch, so `/tmp/.$i.log`, `./$V.claude.bak`, `out_$(date).txt` stay allowed. Add `.$V.claude/settings.json`, `./.$V.claude/hooks/x`, `.$(echo /).claude/x` to `test_41_construct_glued_before_dot_claude_is_refused`.

### Issue 2: MEDIUM
- **File:** .claude/hooks/pretool_hook.py:1115-1121
- **Problem:** A segment that is only a construct (`len(literal) < 2`) is never judged, so `./$V/settings.json`, `./${V:-.claude}/settings.json`, and `./$(echo .claude)/settings.json` are `allow`; bash writes `ws/.claude/settings.json` (verified, `V` unset -> `.claude` via the default). This is the same shape as the documented `./$V` allow row, and D3 accepts that "a construct that expands to `../`" residual, but here the gate can read `.claude` inside the `${V:-.claude}` / `$(echo .claude)` text and still vouches. Invariant 3 and brd D4 list the settings files as asks in every mode. Whether `./$V/<leaf>` should ask is a design decision (it would cost `./$V/out.log` loop-variable writes, which 41-03 is about), but the cheap, in-scope part is: if the text inside a masked construct contains `.claude`, refuse.
- **Fix:** In `_mask_constructs` (or before it) refuse when any construct's source text contains `.claude`. Record the residual `./$V/settings.json` in the report/state as a known, accepted D3 gap, or decide with the user.

### Issue 3: LOW
- **File:** .claude/hooks/pretool_hook.py:1110
- **Problem:** `./$V.claude.bak` etc. allow, correct; but the same `/`-in-construct property lets `./$V.claude.bak` with `V=../..` write outside the roots (`/dev/shm/rv3/...claude.bak`), and `..$V.claude/x` with `V=/` writes `../.claude/x`. Both are the D3-accepted `../`-via-construct residual, not new. Noting so the report does not overstate "as a class".
- **Fix:** None required; keep the docstring honest that construct values containing `/` are out of the gate's reach and list what is covered.

## Code Quality Notes
- Parser diff (`_read_redirect_word`, `_scan_write_targets`, `RedirectTarget`) read fully; fuzz found no crash (no `ERR` decisions in 3936 judgements) and no quoting/brace/process-substitution `ask` -> `allow` regression. `_judge_redirect_word` fails closed on exceptions.
- `_mask_constructs` unbalanced input masks to the end, conservative.
- Tests are real (parser + validator, FakeLoader); the 15-form refusal test is a single test with subTests, so a failure shows the form.

## Mutation proofs
Protocol: `git status --short` saved before (/tmp/41-01-review3-status.before) and compared after (identical); `cp` backups in /tmp/41-01-review3-mut/; each mutation restored with `cp`, `diff` identical; no git state-changing command used. M1-M3 against the full `integration_pretool` module (438 tests; baseline OK), R1-R2 against `TestRedirectTargets` (51 tests; baseline OK).
| # | Change | Mutation (file:line) | Result |
|---|---|---|---|
| M1 | pass-2 regex refusal | pretool_hook.py:1130 `if re.fullmatch` -> `if False and re.fullmatch` | FAILED (failures=16): 14x `test_41_construct_glued_before_dot_claude_is_refused`, 2x `test_41_protected_path_completed_after_a_construct` |
| M2 | pass-2 length floor | :1120 `< 2` -> `< 0` | FAILED (2): `test_41_dotfile_names_with_a_construct_stay_allowed`, `test_41_anchored_variable_leaf_still_allowed` |
| M3 | pass-2 mask `$(`/`${` branch | :1032 `in '({'` -> `in '\x01'` | FAILED (5): 4x glued-before-`.claude` (incl. `$(echo a/b).claude`), `test_41_dotdot_glued_to_an_expansion_is_refused` |
| R1 | row `$(echo ~/.bashrc)` / unquoted substitution target | bash_command_parser.py:2355 `add_unresolvable` -> `add_literal` | FAILED (6), incl. `test_41_substitution_target_is_kept_in_source_form`, `$(echo)` glued forms, `.claude/$(echo hooks)/x` |
| R2 | row `.claude/settings.local.json` | pretool_hook.py:962 commented out | FAILED (1): `test_41_protected_settings_inside_the_workspace` (`echo x > .claude/settings.local.json`) |

## Suite
`python3 tests/run_all_tests.py > /tmp/41-01-review3-suite.log`: Ran 2041 tests, OK (skipped=2). Matches the fixer's 2041; `git status --short` after all mutations identical to before. The suite is green but does not cover Issue 1 (`.$V.claude`).

## Questions for User
Issue 2: should `./$V/<protected leaf>` (a lone construct segment before `settings.json`/`hooks/`) ask, or stay an accepted D3 residual like `./$V`?
