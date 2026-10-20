# Fix Report 4 - 41-01 (pass 4): one structural rule for unresolvable targets

## Summary

The spelling heuristics in the lenient fallback (dots-only segments, `.claude` with constructs deleted, the glob segment `fnmatch`, construct masking) are gone. One structural rule, R0-R6, now reads an unresolvable word from a parts list the parser records. The list says what is literal, what was quoted, and what is a construct or glob. The rule vouches only when the directory part is pure literal text inside a root and not under `.claude/`, and the final segment checks out on disk. All three review-4 HIGHs ask from the workspace root and from a subdirectory. Full suite: `Ran 2041 tests`, `OK (skipped=2)`.

## The rule as implemented

`.claude/hooks/pretool_hook.py`:
- `_judge_redirect_word` (:912). Literal words and words that resolve are judged on their realpath, as before. Every other word goes to `_unresolved_target_allowed`, with constants put into its EXPANSION parts. A word "does not resolve" when it is unresolvable, or when `_resolve_target_path` returns None (for example an undefined `$V`). It still refuses on any exception.
- `_is_redirect_target_allowed` (:997). Now handles resolved targets only, and returns False for a target that does not resolve. Its only string caller outside the gate is the truncation-sentinel test, which expects False.
- `_protected_write_paths(unresolved=False)` (:962) and `_is_protected_write_path` (:989). A resolved target keeps the narrow D4 list. An unresolvable one gets the whole `<root>/.claude/` for every root. `~/.claude` is a single entry at :985, with a comment marking it as the place where 41-02 carves out the memory dir.
- `_unresolved_target_allowed(parts)` (:1035). The word is split at the last `/` in LITERAL text (:1087): D is the directory part, F the final segment.
  - **R6** (:1080): refuse when `.claude` appears anywhere in the text, or `claude` appears inside the source of a non-literal part.
  - **R0** (:1084): the word must begin with a LITERAL or TILDE part, i.e. the literal anchor is not empty. This rule is not in your R1-R6 list; see "Interpretation" below.
  - **R1** (:1100): D must hold only LITERAL parts, plus a leading TILDE. A TILDE in F (`~`/`~user` with no `/` after it) refuses.
  - **R2** (:1107, :1126): expand the tilde, and refuse a `~user` that does not expand. Refuse a literal `..` segment in D. realpath(D), joined onto the cwd, must lie inside a root.
  - **R4** (:1114): no construct or glob part in F may have `/`, `..` or a backslash in its source. The backslash covers `$'\x2f'` and `${V/x/\/}`. No LITERAL piece of F may hold `..`, and F with its constructs taken as empty must not be `..` (`.$V.`).
  - **R3** (:1128): realpath(D) must not be protected, using the unresolvable set.
  - **R5** (:1131): with no glob in F, `lexists(D/leaf)` is checked, where leaf is F with constructs as empty. With a glob in F, a regex is matched over `os.listdir(realpath(D))`, dotfiles included: `*` becomes `.*`, `?` becomes `.`, and `[`, `{` or an extglob group becomes `.*` to the end of F. Every match must realpath inside a root and not be protected. Any exception refuses, and that includes listing a directory that does not exist.
  - The docstring states the accepted residual.

`.claude/hooks/bash_command_parser.py`:
- `RedirectTarget` (:16) gains `parts`: a tuple of `(kind, source)` pairs, kinds `LITERAL`, `TILDE`, `EXPANSION`, `SUBST`, `GLOB`. `anchor_len` is removed (no remaining use).
- `_read_redirect_word` (:2201) builds `parts` instead of a character list. Unquoted `*?[{` (:2395) and an extglob group together with its `@`/`+`/`!` prefix (:2377) are GLOB parts. `{` is a GLOB, not a SUBST, because bash does perform brace expansion on a redirect word that comes out as one word. I checked this: `echo hi > {lnks,}` wrote through the `lnks` symlink.
- `_unread_target` (:2171) builds the two sentinel targets as a single SUBST part.
- `_brace_before_glued_name` (:2421) is **kept, reimplemented on parts**. R0-R6 do not need it, but the resolved path does: `_expand_constants` and `expandvars` read the text. Without it, `V=/etc/x; Vaude=/tmp/y; echo hi > $V"aude"` resolves to `/tmp/y` and is allowed, while bash writes `/etc/xaude`. It now braces a bare `$NAME`/`$N` whenever a literal starting with a word character follows it. That also closes `$V''aude`, `$V$'aude'`, `$V\aude` and `$1"0"`, which the previous source-lookahead version missed.

## Interpretation (R0)

You wrote "D empty → the cwd". Taken alone, that would allow `> "$LOG"`, `> $x` and `> $(echo ~/.bashrc)`. That breaks invariant 2, brd D6 and the existing `test_anchorless_variable_target_prompts`. So I kept today's rule: the word must start with literal text or a tilde. Empty D still means the cwd for `x$(echo y)`, `.$V` and `out_$(date).txt`.

## Removed

From `pretool_hook.py`:
- `_literal_prefix_inside_allowed_roots`, replaced by `_unresolved_target_allowed`. **Renamed**: state.md's shared-edit-points table and the 41-02/41-03 files refer to the old name.
- `_mask_constructs` and `_CONSTRUCT_MARK`.
- The dots-only segment rule, rule (b) (`.claude` with constructs deleted) and rule (c) (glob-segment `fnmatch`).
- `import fnmatch`. R5 uses `re`.

From `bash_command_parser.py`: `RedirectTarget.anchor_len`, and the `anchor_len` and `literal_meta_at` bookkeeping.

From the tests: ten per-spelling tests (`dotdot_glued…`, `dotted_names…`, `protected_path_completed…`, `claude_in_an_unresolvable_target…` with `_CLAUDE_SPELLINGS_41`, `dotfile_names…`, `glob_away…`, `dot_claude_text_beside…`, `construct_value_supplying…`, `other_unresolvable_names…`, `extglob_prefix…`). Their spellings are now rows in the new table.

## Tests (`tests/test_integration_pretool.py`, TestRedirectTargets)

- `setUpClass` (:1720) builds a real workspace under `/var/tmp`, so it is outside `/tmp` and an allowed root cannot mask a refusal. Fixtures sit in `ws/` and again in `ws/sub/` (pointing one level up): `lnk -> .claude`, `lnk2 -> .claude/hooks`, `lnks -> .claude/settings.json`, `esc -> /etc/hostname`, and a plain file `out_1.txt`. These tests skip if `/var/tmp` is not usable; it is here.
- `_REFUSED_BY_RULE_41` (:1752) with `test_41_unresolvable_targets_refused_by_rule`: 81 spellings filed under R0-R6, each run from `ws` and `ws/sub`, each must give ask/risk with a target listed.
  - Every spelling from review passes 2-4 is in it, including `..${V:-/}etc/x`, ``..`echo /et`c/x``, `..${V:=/}etc/x`, `.${V:-claude}/settings.json`, `.cl${V:-aude}/…`, `.$(echo claude)/…`, `.c${V-la}ude/hooks/x.py`, `/tmp/${V:-..}/x`, `lnk/setti$(true)ngs.json`, `lnk/${V:-}settings.json`, `ln*/settings.json`, `l${V}nk2/x.py`, `./lnk${V}s`, `./ln*s`, `{lnks,}`.
  - The task table's ask rows are also in it.
  - `/tmp` appears only where the `/tmp` prefix is the point.
- `_MUST_ALLOW_41` (:1807) with `test_41_unresolvable_targets_that_stay_allowed`: all ten documented rows plus eight more (18), from both cwds.
- Also added:
  - `test_41_construct_in_a_directory_position_asks` (pins the behaviour change).
  - `…documented_residual`.
  - `test_41_filesystem_error_in_the_leaf_check_refuses`.
  - `test_41_word_parts_record_what_was_quoted` (parser level).
  - `test_41_quote_removal_keeps_the_variable_name_apart`, extended with the new glue cases and the `$V"aude"` constant row, which must ask.
- The task-table tests are unchanged and pass.

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-01-fix4-mut/status-before.txt), file backed up with `cp` to /tmp/41-01-fix4-mut/, one exact replacement, targeted test run, then restored with `cp` and checked with `diff` (clean every time). `git status --short` afterwards matched the saved copy. No git state-changing command was used. Driver: /tmp/41-01-fix4/mut.sh. Unless noted, the test is `test_41_unresolvable_targets_refused_by_rule`, and "failures" are failing subtests.

| Mutation | Result |
|---|---|
| R0 `parts[0]` must be LITERAL/TILDE → dropped | FAILED 6 (R0 rows) |
| R1 → `False and any(...)` | FAILED 40 (R1 rows) |
| R2 roots check → `if False` | FAILED 8 (R2 x6, R3 rows from the sub cwd x2) |
| R2 `..` in D → `if False` | FAILED 2 (`sub/../x$V`) |
| R2 unexpanded `~user` → `if False` | FAILED 2 (`~nosuchuser41/$V`) |
| R3 → `if False` | FAILED 2 (`lnk2/$V`, `lnk/x$V`) |
| R3 whole-`.claude` set → narrow list | FAILED 2 |
| R4 construct source `/`,`..`,`\` → `elif False` | FAILED 14 |
| R4 literal `..` in F → `if False` | FAILED 2 |
| R4 leaf `..` → `if False` | FAILED 2 (`sub/.$V.`) |
| R5 all matches skipped | FAILED 20 |
| R5 lexists branch → `[]` | FAILED 8 |
| R5 glob listdir → `[]` | FAILED 12 |
| R5 `[`/`{`/extglob superset → literal | FAILED 6 |
| R5 `*`/`?` → literal | FAILED 6 |
| R6 `.claude` text | FAILED 6 |
| R6 `claude` in a construct | FAILED 4 |
| parser `{`,`*`,`?`,`[` as SUBST, not GLOB | FAILED 8 (R5 rows) |
| parser glued-name brace → no-op (`test_41_quote_removal…`) | FAILED 7 |
| over-refusal: R5 refuses every match (`…that_stay_allowed`) | FAILED 4 |

Two mutations survived at first: R2 `..` and the R4 leaf `..`. In every existing row, the roots check or R5 also refused. I added `sub/../x$V` and `sub/.$V.`, which only those rules refuse, and both then failed. The R5 glob mutations were re-run after the last edit to that block.

## HEAD vs change fuzz

- **Method**: HEAD copies (`git show HEAD:`) in /tmp/41-01-fix4/head, the pass-3 tree in /pass3, this change in /new. Corpus of 12,048 commands (/tmp/41-01-fix4/fuzz.txt): 20 anchors x 25 constructs x 16 tails, 4,000 random two-construct words, five operators, and the hand rows. Each command was run against all three versions from the fixture workspace (/var/tmp, HOME patched) and from its `sub/`. No version raised an error.
- **HEAD ask → now allow: 53 commands.**
  - `(ls 2>/dev/null) | grep a`: the intended D2 fix. `( (ls 2>/dev/null) )` is covered by the tests.
  - 52 rows with an ANSI-C string that has no escapes, `$'x'`: `$'x'/../etc/x`, `..$'x'/x`, `/tmp/..$'x'/../etc/x`, `$'x'.claude/settings.json` and so on. HEAD read `$'x'` as an unresolved `$`. It is the literal `x`, so the gate now resolves the real path, which stays inside the roots and off the protected list. Same class the pass-4 reviewer accepted.
  - No other flips.
- **Bash oracle**: every target the new code allows (3,423 judgements) was expanded with real bash (`set -- WORD`, extglob on, V and friends unset, same fixtures). Each single-word result was checked against the roots and the protected paths. None is bad. The two oracle hits are `D=/tmp; echo hi > $D/x$V`, where the oracle does not run the `D=/tmp` assignment; bash writes `/tmp/x`.
- Pass-3 ask → now allow: 13 rows. Examples: `/tmp/*`, `"."$V.\`echo x\`.`, `sub/'*'/${V:-}`. These were over-matches of the old dots and glob rules; the oracle confirms each lands inside a root.

## Accepted residual (brd D3)

The gate cannot read the runtime VALUE of a construct confined to F. `./$V` with `V=../../x` or `V=.claude/settings.json`, and `x$(cmd)` whose output holds `/` or `..`, are allowed. `$(cmd)` output (`base64 -d`, `printf %s%s . .`) can produce `/` or `..` without either appearing in the source; R4 only covers what is visible in the source. This is stated in the docstring and pinned by `test_41_construct_value_in_the_final_segment_is_the_documented_residual`.

Also out of reach, and true of resolved targets too: a symlink created by an earlier statement in the same compound command (`ln -s .claude d; echo > d/settings$V.json`). The gate judges the disk before anything runs. The allowlist governs `ln`.

## Behaviour change (allowed before, ask now in default mode)

1. **A construct or glob in a directory position**, even under a literal anchor inside a root: `out/$name/x.log`, `./"$dir"/x`, `logs/$(date +%F)/run.log`, `/tmp/$V/x`, `/tmp/$(mktemp -u)/x`, `./*/x`, `./$V/settings.json`, `.cl$V/settings.json` (R1).
2. **A construct in F whose source holds `/`, `..` or a backslash**, which hits common parameter operators: `/tmp/${f##*/}.out`, `./${f%/*}.log`, `${V:-a/b}`, `$(dirname …)` with a `/` argument, `$'\t'` (R4).
3. **An unresolvable target anywhere under `<root>/.claude/`**: `.claude/agents/$name.md`, `.claude/commands/x_$i.md`. Resolved ones such as `.claude/agents/x.md` still follow the narrow D4 list (R3).
4. **A leaf (constructs as empty, or glob matches) that exists and is a symlink out of the roots or into `.claude`**: `./lnk$V` (R5).
5. **An unquoted glob in F under a directory that does not exist yet** (`newdir/out_*.txt`): listing fails, and a filesystem error refuses.
6. **`..` in F's literal text** (`x..$(date)`, `out..$V`), and `claude` inside a construct (`${V:-claude}`). Literal `claude` beside a construct (`claude_$(date).log`) is still allowed; I did not add an adjacency rule because R1 and R5 already cover that case structurally.

## For 41-03

- Rows 1-4 and 6 above, plus the R0 class, all have a literal anchor inside a root and are not protected, yet now ask. Classify them `unknown` (silent in deferring modes) per D6.
- Keep `risk` for:
  - R2 refusals: anchor outside the roots, literal `..` in D, unexpandable `~user`.
  - R3 refusals, where D is under `.claude`.
  - R5 matches that resolve outside the roots or into a protected path.
  - The R6 `.claude` text.
- The outcome function belongs beside `_unresolved_target_allowed`. It is the rule that knows which R failed, so it can return the reason instead of a bool.
- The function was renamed from `_literal_prefix_inside_allowed_roots`, and `RedirectTarget.anchor_len` no longer exists: use `parts`.

## Verification

- `python3 -m py_compile` passes on both hooks, and both import (the suite imports them).
- Full suite (`python3 tests/run_all_tests.py > /tmp/41-01-fix4-suite.log`): **`Ran 2041 tests`, `OK (skipped=2)`**. Same two skips (`test_all_hook_modules_import`: no pre-3.11 interpreter; `test_headless_spawn`: live spawn). The count went from 2045 to 2041 because ten per-spelling tests were folded into six table tests.
- `git status --short` matches the start of this pass, apart from your own edits to state.md, which I did not touch.
- Installed: no (`./install.sh` not run; repo copy tested).

## Side effect to note

Early on I ran `python3 -m unittest test_integration_pretool` directly, outside the runner. That appended 84 test entries (`session_id: ""`, `workspace: /tmp/tmp…`, 2026-10-20T15:3x) to the real `~/.claude/bash_manual_confirm.log`. `test_case17` flags exactly this when the module runs outside the runner. I did not edit the log. 41-04's tally should drop those entries, or the tally window should start after them.

## Questions for User

None.
