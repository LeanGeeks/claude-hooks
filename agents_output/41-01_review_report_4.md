# Review Report - 41-01 The gate reads the real target (pass 4)

## Verdict: FAIL

Compiles and imports; full suite 2045 OK (skipped=2), working tree identical before/after. Three HIGH findings remain: one is a HEAD-ask -> allow regression, two are bypasses of the "one rule" for unresolvable targets that the task asked me to probe.

## Completeness Check

- A. whole-word scan, trailing `)` (D2), recursion into substitutions in the target word: met (`_read_redirect_word`, `_scan_write_targets`; must-allow and paren rows verified).
- B. quote removal, unresolvable flags, public `extract_write_redirect_targets` shape kept: met.
- C. lenient fallback: partially met. `..` anywhere in the segments is refused only when the `..` is a standalone/only-dots segment after masking constructs; HEAD's prefix check was lost (Issue 1). `~` handling met.
- D. protected paths: partially met. Settings/hooks protected for resolved targets; unresolvable targets leak via Issues 2 and 3.
- Must-allow rows (verified in a real workspace outside /tmp, patched HOME): `./$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `out_$(date).txt`, `/tmp/.$i.log`, `.$V`, `echo $(ls 2>/dev/null)`, `./out_*.txt` all allow. Intended paren flips `(ls 2>/dev/null) | grep a`, `( (ls 2>/dev/null) )` ask -> allow.
- `_brace_before_glued_name`: called only from `_read_redirect_word` (two sites). The tokenizer, `_tokenize_with_quotes`, router and command matcher are untouched; `extract_write_redirect_*` is used only by pretool_hook.py. `_strip_quotes` was deleted and has no other user. No impact on command matching.

## Method

HEAD copies of all hooks under /tmp/h4/old, working tree under /tmp/h4/new, 13,565 adversarial commands (quoting, `$'..'`, `$".."`, escapes, brace, `${V:-}`/`${V/}`, arithmetic, backslash-newline, globs plus constructs, symlinks lnk->.claude and lnk2->.claude/hooks in the workspace, wrappers, all operators) plus 2,140 protected-target-focused ones. Workspace /var/tmp/h4ws (not /tmp), HOME=/var/tmp/h4home. Result: 189 HEAD-ask -> allow, all but the 4 below are correct (sane `$'x'`, quoted `~`, `$"x"` reads; verified against real bash expansion). As an oracle I expanded every new-`allow` target with real bash (V and i unset) and checked the realpath against protected paths / roots: 176 hits, all HEAD-allow except 4 (Issue 1).

## Issues Found

### Issue 1: [severity: HIGH] HEAD `ask` now `allow`: `..` followed by a construct supplying `/`
- **File:** .claude/hooks/pretool_hook.py `_literal_prefix_inside_allowed_roots` (the `segments = masked.split('/')` check replaced HEAD's `'..' in prefix.split('/')`)
- **Problem:** `echo hi > ..${V:-/}etc/x`, `` echo hi > ..`echo /et`c/x ``, `..${V:=/}etc/x`, `..${U:-/et}${V:-}c/x` write `../etc/x`. HEAD: ask. Now: allow. Masking joins `..` and the construct into one segment `..<mark>etc` that is neither `..` nor only dots. (Bare `..$V` is still refused; only the construct-with-visible-`/` form leaks.)
- **Fix:** keep the literal-prefix check as well: refuse when `'..' in prefix.split('/')` and when the prefix's last segment ends with `..` (or `.` followed by a construct whose text contains `/`); more generally refuse a segment whose literal pieces contain `..` adjacent to a construct. Add these four rows to the tests.

### Issue 2: [severity: HIGH] `.claude` or `..` completed by construct CONTENT split across the construct boundary (rules a-c miss it)
- **File:** .claude/hooks/pretool_hook.py `_literal_prefix_inside_allowed_roots`
- **Problem:** Rules (a)-(c) test `.claude` in the full source, and in the text with constructs deleted. A default/echo value that is visible in the source but only completes the name together with the literal text beside it is not caught. All verified `allow` (bash with V unset writes the protected path):
  - `echo hi > .${V:-claude}/settings.json`, `.cl${V:-aude}/settings.json`, `.$(echo claude)/settings.json`, ``.`echo claude`/settings.json``
  - `.c${V-la}ude/hooks/x.py`, `.claud${V-e/setting}s.json`, `.c"$(echo laude/settings.js)"on`, `.${V:-claude/hooks}/x.py`, `/var/tmp/h4ws/.cla`echo ude`/settings.json`
  - escapes outside the roots: `/tmp/${V:-..}/x`, `/tmp/${V:=../etc}/x`, `.${V:-./e}tc/x`, `/tmp/$(echo ..)/x`
  The text is visible in the source, so this is not the brd D3 value-only residual. (`.${V:-claude}` and the `..` rows were already `allow` at HEAD, but the task asks for exactly this class.)
- **Fix:** add a (d) rule: build a second reading where each construct is replaced by the literal text of its default/arguments (strip `${V:-`, `${V-`, `${V:=`, `${V/x/` wrappers, `$(echo ` / backtick `echo`/`printf` heads, quotes) and re-run the `.claude`, `..` and root checks on it; or simply refuse any unresolvable target whose constructs contain `claude`, `..` or `/` text plus a surrounding literal `.`/segment. Over-refusal is the stated trade-off.

### Issue 3: [severity: HIGH] Workspace symlink into `.claude/`: the `.claude` text is not in the source, so the rule never fires
- **File:** .claude/hooks/pretool_hook.py `_literal_prefix_inside_allowed_roots` (protected check covers `base` and `anchored` only; `settings.json`/`settings.local.json` are file-only entries)
- **Problem:** with `lnk -> .claude` in the workspace (agent can create it): `echo hi > lnk/setti$(true)ngs.json`, `lnk/sett${V:-}ings.json`, `lnk/${V:-}settings.json`, `lnk/settings.jso${V}n`, `ln*/settings.json`, `l"$V"nk/settings.json` all allow and write `.claude/settings.json`. With `lnk2 -> .claude/hooks`: `l${V}nk2/x.py`, `ln$(true)k2/x.py`, `ln"$V"k2/x.py` allow (a name split by a construct is not realpath-resolvable as one). Only `lnk2/<construct-first>` is refused (`base` is protected). Fully resolved `lnk/settings.json` is correctly refused.
- **Fix:** for unresolvable targets, treat the whole `<root>/.claude` directory as protected for the `base`/`anchored` check (refuse `base == <root>/.claude` or under it), and also realpath the construct-deleted text and the glob-expanded segments (`fnmatch` against actual directory entries, or refuse a glob/construct-split first segment whose directory contains a symlink) and refuse if the result is protected.

## Code Quality Notes

- Test gaps: add rows for each Issue 1-3 spelling, from both cwds (workspace and workspace/sub).
- The docstring states "Covered: the literal text, the text inside constructs, and the text with constructs deleted"; Issues 1-2 show combined readings (literal + construct content) are not covered, so the docstring over-claims.
- `./$V/settings.json`, `./$V`, `.cl$V` allowed: accepted D3 residual, not reported.
- Parser change is above the tokenizer; no tokenizer change.

## Questions for User

None.

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-01-review4-status-before.txt); `cp` to /tmp/41-01-review4-mut/*.bak; mutate; targeted test (`cd tests; python3 -m unittest test_integration_pretool -k <name>`); restore with `cp`; `diff` identical; `git status --short` after is identical to before.

| Mutation | Test | Result |
|---|---|---|
| rule (a): drop `'.claude' in target` | test_41_claude_in_an_unresolvable_target_is_refused | FAILED (failures=4) |
| rule (b): drop `'.claude' in masked.replace(MARK, '')` | same | FAILED (failures=4) |
| rule (c): glob-segment `fnmatchcase` loop disabled (`if False:`) | same | FAILED (failures=10) |
| `_brace_before_glued_name` returns `src` unchanged | test_41_quote_removal_keeps_the_variable_name_apart | FAILED (failures=1) |

All four restored byte-identical (`diff` clean). Full suite after restoration: `Ran 2045 tests ... OK (skipped=2)` (/tmp/41-01-review4-suite.log).
