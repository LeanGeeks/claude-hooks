# Review Report - 41-01 The redirect gate reads the target bash will write to

Reviewed uncommitted working tree against base 05b9fdd (repo copy; permission_mode default; nothing executed). Both changed hooks byte-compile and import.

## Verdict: FAIL

One HIGH regression (a fail-open that HEAD did not have) and one HIGH (protected-path bypass through an unresolvable middle segment). Everything in the task table passes and is mutation-proven.

## Completeness Check

All table rows probed directly against the validator, outside /tmp (HOME patched, workspace /var/tmp/rv41/ws, with symlinks into home/etc).

- A. Whole target word (`_read_redirect_word`, `_scan_write_targets`): met. `$(echo ~/.bashrc)`, `"$(echo /etc/x)"`, `x$(echo y)/../../../etc/x` ask/risk in source form; `x$(echo y)` and `/tmp/out_$(date +%s).txt` allow; trailing `)` stripped only when unbalanced (`(ls 2>/dev/null)` -> `/dev/null`, `$(echo x)` and `"a)"` keep it); inner redirects of target substitutions (`$(...)`, backtick, `"$(...)"`, `>(...)`) scanned; depth cap marker kept.
- B. Quote removal: met. `"/etc"/x`, `""/etc/x`, `'/etc/x'y`, `\/etc/x`, `"$HOME"/.bashrc`, `~/.bashrc"$V"`, `'~'/x` (literal, allow), `{/etc/x,}`, `~-/x`, `~+/x`, `/etc/x*` all behave per table. Extra: `$'/etc/x'` -> `/etc/x` ask; `$'\x2f…'` unresolvable ask; `${x:-/etc/x}`, `${x:-/etc}/x`, `$((1))/../..`, `~root/x`, `~nosuch/x`, `~$V/x`, `[/]etc/x`, `/et[c]/x`, `/{etc,}/x` all ask. Operators `<>`, `>|`, `&>`, `&>>`, `>&`, `2>`, `3>`, `12>>`, `{fd}>`, heredoc forms, subshell/brace-group/`$(...)`/backtick/process-subst nesting, `${x:-$(… > /etc/x)}`: all ask with the right target.
- C. Lenient fallback: PARTIALLY met. Tilde expansion, `~user` refusal and `..`-anywhere refusal work for explicit `..` segments, but see Issue 1 (regression: `..$V`).
- D. Protected paths: met for resolved targets and for a literal prefix inside the protected area (`.claude/settings.json`, `.claude/settings.local.json`, `.claude/hooks/*`, `../.claude/…` from `sub`, `~/.claude/*` with workspace=$HOME, symlinks `lnk/`, `sub/cl/settings.json`, `./.claude/./settings.local.json`, `.claude//hooks//a`, `.claude/hooks/../hooks/a`, `"$PWD/.claude/settings.json"`). PARTIALLY met for unresolvable targets whose protected path is completed after the anchor: see Issue 2.
- Not in scope respected: refused targets remain `ask_kind: risk`; public `extract_write_redirect_targets` shape unchanged; sibling `extract_write_redirect_words` added; `_strip_quotes` removal has no other callers (grepped in hook files).
- Tests: 24 new `test_41_*` tests in `TestRedirectTargets`; table rows covered, but nothing covers the `..$V` form (Issue 1).

## Issues Found

### Issue 1: [severity: HIGH] Regression - `..` glued to an expansion is no longer refused

- **File:** /data/sync/work/leangeeks-ai/claude-hooks/.claude/hooks/pretool_hook.py `_literal_prefix_inside_allowed_roots`, the line `if '..' in target.split('/'):`
- **Problem:** HEAD checked `'..' in prefix.split('/')`, where the prefix is cut at the first `$`/backtick, so a segment `..$V` produced the prefix segment `..` and was refused. The new check splits the whole target, so the segment is `..$V` (not equal to `..`) and the target is allowed. When `$V` is unset/empty (or a `$(printf)` yields nothing) bash writes through the parent directory. Verified HEAD vs working tree with the same probe harness (HEAD copy in /tmp/41-01-review-head):

  | Command | HEAD | now | bash writes (V empty) |
  |---|---|---|---|
  | `echo x > /tmp/..$V/x` | ask | **allow** | `/x` |
  | `echo x > "/tmp/..$V"` | ask | **allow** | `/` (dir) |
  | `echo x > ..$V/x` | ask | **allow** | `<ws>/../x` |
  | `echo x > ./..$V/x` | ask | **allow** | `<ws>/../x` |
  | `echo x > /tmp/..$(echo)/x` | ask | **allow** | `/x` |
  | `echo x > sub/..$V/..$V/etc/x` | ask | **allow** | outside ws |

  This is the exact class the task's item C says to tighten ("refuse when any literal path segment is `..`"); the change loosened it. No test covers it (the new `..` test only has an explicit `/../` segment).
- **Fix:** keep both checks: refuse when `'..' in target.split('/')` OR when any segment, cut at its first `$`/backtick, equals `..` (e.g. `any(re.split(r'[$`]', seg, 1)[0] == '..' for seg in target.split('/'))`); also treat a segment whose literal part ends in `.` right before an expansion as suspect if you want to close `.$V./x` (pre-existing at HEAD, same accepted class). Add tests for `/tmp/..$V/x` and `..$V/x` (target outside /tmp).

### Issue 2: [severity: HIGH, per the review brief's definition] Protected paths can be reached when the construct sits inside the protected path

- **File:** /data/sync/work/leangeeks-ai/claude-hooks/.claude/hooks/pretool_hook.py `_literal_prefix_inside_allowed_roots` (protected check covers `base` and `anchored` = the literal prefix only)
- **Problem:** the task wording ("refuse when its literal prefix already points into one of these") is implemented literally, so any construct before the path completes bypasses D4 and the agent can still rewrite its own settings/hooks without a prompt: `echo x > .claude/settings$V.json`, `> .cl$V/settings.json`, `> .claude/$(echo hooks)/x`, `> .claude/s*.json`, `> .c*/settings.json` all return `allow` (bash writes `.claude/settings.json` when `$V` is empty, or the glob matches). Also `V=; echo x > .claude/settings$V.json` (empty constant is not folded). Same residual class as `x$V` that D3 accepts, but here the goal of D4 (agent cannot widen its own bounds) is defeated by a one-token trick. Note `.claude/hooks/$V` and `.claude/settings.local.json$V` ARE caught.
- **Fix:** in the lenient path, refuse any unresolvable target whose literal text contains a `.claude` path segment or a segment that is a prefix of `.claude` followed by a construct (simplest conservative rule: refuse if `'.claude'` appears as a segment, or any segment begins with `.` and contains a construct before completing). Alternatively, ask the operator whether D4 should be limited to the literal prefix and document the residual; if so downgrade to a documented risk.

### Issue 3: [severity: LOW] Extglob prefix characters are treated as a literal anchor

- **File:** /data/sync/work/leangeeks-ai/claude-hooks/.claude/hooks/bash_command_parser.py `_read_redirect_word`
- **Problem:** `echo x > +(/)etc/x` and `!(a)` yield anchor_len=1 (`+`/`!` literal) and are allowed. Bash needs `shopt -s extglob` and an existing matching file for this to matter, and without extglob it is a syntax error, so practically unreachable. Mention only.
- **Fix:** set anchor_len to 0 when the `(` immediately follows `@ + ! ? *`.

## Code Quality Notes

- Pre-existing, NOT made worse by 41-01: the tokenizer bug with `$'\''` hiding later commands. Verified at HEAD (HEAD parser/pretool copy in /tmp/41-01-review-head): `echo $'\'' ; echo y > /etc/x` returns `allow` at HEAD and in the working tree alike. Out of scope for 41-01.
- Also pre-existing and unchanged: `/dev/fd/../../etc/x`, `/dev/fd/$V` allow (prefix whitelist); `/tmp/$(echo ..)/x` allow (accepted residual); `cd /; echo x > x` is judged against the payload cwd; `tee file` is not a redirect and is not in this gate.
- `/tmp` is an allowed root, so D4 also refuses `/tmp/.claude/settings.json` and `/tmp/.claude/hooks/*`; harmless.
- `_judge_redirect_word` fails closed on exceptions (refuse); a good posture, only turns an allow into a prompt.
- `fd_pipe` words (`> >(cmd)`) are returned as allowed with the inner redirects scanned separately; verified `> >(cat > /etc/x)` asks on `/etc/x` and `> >(cat) /etc/x` allows (the `/etc/x` is an argument there, correct).
- Test-quality: tests exercise the real validator with patched HOME and keep refusal targets outside /tmp; assertions check decision, ask_kind and target. Gap: nothing for `..$V` (Issue 1) or a protected path completed after a construct (Issue 2).

## Questions for User

- Issue 2: is D4's "literal prefix only" intentional (accepting `.claude/settings$V.json` as residual risk), or should an unresolvable target with a `.claude` segment always be refused?

## Mutation proofs

Protocol: `git status --short` saved before (`/tmp/41-01-status-before.txt`); both hook files copied to `/tmp/41-01-review-mut/*.bak`; each mutation run against `test_integration_pretool.TestRedirectTargets` (44 tests, green before mutating); restored with `cp` from the backup and `diff`-confirmed identical. No git checkout/restore/stash/add/commit used. After all mutations `git status --short` matched the BEFORE output (diff empty) and both hook files are byte-identical to the backups.

| # | Row | Mutation | Failing tests | Restore |
|---|---|---|---|---|
| M1 | `$(echo ~/.bashrc)` | scanner returns the substitution's inner text as the target (old behaviour) | `test_41_substitution_target_is_kept_in_source_form`, `test_41_quoted_substitution_target`, `test_41_parens_that_belong_to_the_word_stay` | restored by `cp` + diff identical (my helper's first restore step failed on an arg bug, so I restored manually immediately; diff identical, verified) |
| M2 | inner-redirect | skip CMD_SUBST tokens inside the target word (old `continue` before recursion) | `test_41_inner_redirect_of_a_target_substitution_is_scanned` (3 subtests), `test_41_inner_redirect_of_a_process_substitution_target`, `test_41_target_substitution_past_the_depth_cap_is_refused` | identical |
| M3 | `"/etc"/x` (and `'/etc/x'y`) | single-quote content dropped; then double-quote content dropped | M3: `test_41_quote_removal_finds_the_real_path` (`'/etc/x'y`), `test_41_quoted_tilde_is_literal`; M3b: `test_41_quote_removal_finds_the_real_path` (`"/etc"/x`), `test_41_parens_that_belong_to_the_word_stay`, `test_constant_assignment_outside_root_still_prompts` | identical |
| M4 | `~/.bashrc$V` | no tilde expansion in the lenient fallback | `test_41_lenient_fallback_expands_the_tilde`, `test_41_lenient_fallback_unexpandable_user_refused`, `test_41_quoted_variable_glued_to_home_path` | identical |
| M5 | `.$V/../…` | `..` check back to prefix-only | `test_41_lenient_fallback_refuses_dotdot_anywhere`, `test_41_glued_substitution_with_traversal` | identical |
| M6 | `.claude/settings.local.json` | protected-path check removed from the resolved branch | `test_41_protected_settings_inside_the_workspace` (3 subtests), `test_41_user_claude_dir_protected_when_home_is_the_workspace` | identical |

git status before/after: identical (`M .claude/hooks/bash_command_parser.py`, `M .claude/hooks/pretool_hook.py`, `M tasks/41_redirect_gate_parity/state.md`, `M tests/test_integration_pretool.py`).

## Full suite

Ran 2034 tests in 294.7s, OK (skipped=2) - matches the implementer (baseline 2010 + 24 new). Log: /tmp/41-01-review-suite.log. A green suite does not cover Issues 1 and 2.
