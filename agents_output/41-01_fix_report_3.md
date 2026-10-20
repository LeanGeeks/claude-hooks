# Fix Report 3 - 41-01 (pass 3)

## Orchestrator decision applied
The per-segment `.claude` heuristics (length floor, regex "can be completed to `.claude`", literal `.claude` segment check) in `_literal_prefix_inside_allowed_roots` are replaced by one rule. An unresolvable target is refused when (a) its source text contains `.claude` anywhere (literal, inside `${V:-.claude}`, `$(echo .claude)`, after quote removal), or (b) its text with every construct deleted contains `.claude` (`.cl$V"aude"`, `.cla${V}ude`, `.c*laude`). The `..` rule, dots-only rule, `~` expansion, home rule, literal-prefix containment are kept; the dots-only check is now a small loop that is the only per-segment logic left.

## Issue 1 (HIGH, `.$V.claude`) and Issue 2 (MEDIUM, construct-only segment / `.claude` inside a construct)
- Change: `.claude/hooks/pretool_hook.py` `_literal_prefix_inside_allowed_roots` (segment loop + the single `.claude` rule, ~lines 1117-1140). Segment regex, `len<2` skip for `.claude`, and the `'.claude' in segments` check removed.
- Parser change (needed by the `.cl$V"aude"` row): `.claude/hooks/bash_command_parser.py` new `_brace_before_glued_name`, used by both `add_expansion` call sites of `_read_redirect_word`. Quote removal turned `.cl$V"aude"` into `.cl$Vaude`, which names variable `Vaude` (resolved empty, allowed). A bare `$NAME` followed by a quote/backslash and a word character is now emitted as `${NAME}`; other spellings unchanged (`./"$V".claude/s` still `./$V.claude/s`).
- Tests (`tests/test_integration_pretool.py`, TestRedirectTargets): `test_41_claude_in_an_unresolvable_target_is_refused` (22 spellings incl. every one from passes 2/3, x workspace root and `/ws/project/sub`, refusals outside /tmp); `test_41_quote_removal_keeps_the_variable_name_apart`; `test_41_dot_claude_text_beside_a_construct_asks_even_when_harmless` (`./$V.claude.bak`). Old `test_41_construct_glued_before_dot_claude_is_refused` replaced; two rows (`.cl$V/settings.json`, `.c*/settings.json`) dropped from `test_41_protected_path_completed_after_a_construct`, and `.cl$V$W`, `.cl*ude` rows dropped, because they are now the accepted residual (no `.claude` text).
- Must-still-allow (`test_41_dotfile_names_with_a_construct_stay_allowed`, both cwds): `/tmp/.$i.log`, `.$V`, `./.$V.log`, `./$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `out_$(date).txt`, `./my$V_notes`.

## Issue 3 (LOW)
Docstring of `_literal_prefix_inside_allowed_roots` now lists what is covered and states the residual explicitly. Test `test_41_construct_value_supplying_dot_claude_is_the_documented_residual` pins it.

## Mutation proofs
`git status --short` before == after; backup /tmp/41-01-fix3-mut/pretool_hook.py.bak; restored with `cp`, `diff` identical. No git state-changing commands.
- (a) `if '.claude' in target or ...` -> `if False or ...`: TestRedirectTargets FAILED (4): `./${V:-.claude}/settings.json`, `./$(echo .claude)/settings.json`, both cwds. (Plain-text `.claude` spellings are also caught by (b), so only the in-construct ones isolate (a).)
- (b) drop the `masked.replace(MARK,'')` clause: FAILED (6): `.cl$V"aude"/settings.json`, `.cla${V}ude/settings.json`, `./.c*laude/settings.json`, both cwds.

## Accepted residual (brd D3, 41-03 reclassifies)
A construct whose expanded VALUE alone supplies `.claude`, `/` or `..`, with no `.claude` text visible in the source, is out of the gate's reach: `./$V/settings.json` and `./$V` with V=.claude/settings.json, `.cl$V` / `.cl$V$W` (the value supplies the rest), `.$V.x` or `./$V.bak` with V containing `/` or `../`. Listed in the function docstring; the first three pinned by a test.  Glob rows are NOT residual: see rule (c) below.

## Suite
`python3 tests/run_all_tests.py > /tmp/41-01-fix3-suite.log`: see totals line below.
Ran 2044 tests, OK (skipped=2) before the follow-up; final totals in the follow-up section. `git status --short` after the full suite is identical to before.



## Follow-up: glob rule (c)
Orchestrator rejected the glob residual. Added rule (c) in `_literal_prefix_inside_allowed_roots` (pretool_hook.py ~1148-1154): the target with non-glob constructs deleted and globs kept (`_mask_constructs(..., keep_globs=True)`, new parameter; `import fnmatch` added) is split on `/`; any segment containing `*`, `?` or `[` for which `fnmatch.fnmatchcase('.claude', seg)` is true refuses. Docstring updated.
- `.claude/set*.json`, `.claude/h*/x` are already caught by (a) (`.claude` visible); confirmed by tests, no extra code.
- Tests: glob spellings added to the 22-row subTest table (now 29 rows x 2 cwds): `.c*/settings.json`, `.cl*ude/hooks/x`, `.cl?ude/settings.json`, `.[c]laude/settings.json`, `.cl*ude/settings.json`, `.claude/set*.json`, `.claude/h*/x`. Must-allow: `./out_*.txt`, `/tmp/x*`; new `test_41_glob_away_from_dot_claude_still_asks_outside_the_roots` (`/etc/x*` asks). Over-match note: a bare `*` segment (`./*/x`) matches `.claude` and asks.
- Mutation (c): `if any(...)` -> `if False and any(...)`: FAILED (10), the five non-`.claude`-text glob rows x 2 cwds. Backup /tmp/41-01-fix3-mut/c.bak, restored by cp + diff, `git status --short` same.
- Final suite: `/tmp/41-01-fix3-suite.log`: Ran 2045 tests, OK (skipped=2).

## Questions for User
None.
