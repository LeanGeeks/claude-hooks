# Review Report 2 - 41-01 The redirect gate reads the target bash will write to

Reviewed the uncommitted working tree against base 05b9fdd (repo copy, permission_mode default, nothing executed). Both hooks byte-compile and import. Probes: working tree vs a HEAD copy under /tmp/41-01-review2-head, workspace /var/tmp/rv41b/ws/sub with HOME patched, symlinks into `.claude/` and /etc, plus ~1,500 randomly composed targets.

## Verdict: FAIL

Both pass-1 HIGHs and the LOW are fixed, and there is no HEAD-ask to now-allow regression (fuzz diff: only the intended `( (ls 2>/dev/null) )` rows flipped ask to allow). One new HIGH: the fixer's D4 `.claude` rule is incomplete. A construct glued before `.claude` in the same segment still reaches the protected files.

## Completeness Check

- Pass-1 Issue 1 (`..$V`): fixed. `/tmp/..$V/x`, `"/tmp/..$V"`, `..$V/x`, `./..$V/x`, `/tmp/..$(echo)/x`, `sub/..$V/..$V/etc/x` all ask; extra forms `/tmp/'..'$V/x`, `/tmp/\..$V/x`, `/tmp/$'..'$V/x`, `/tmp/.\.$V/x` ask too. Test and mutation below.
- Pass-1 Issue 2 (protected path completed after a construct): PARTIALLY fixed, see Issue 1 below. `.claude/settings$V.json`, `.cl$V/settings.json`, `.claude/$(echo hooks)/x`, `.claude/s*.json`, `.c*/settings.json`, `.claude"$V"/x`, `".claude"/settings.json`, `~/.claude$V/x`, `~/.cl$V/x` and `V=.claude; echo x > $V/settings.json` (constant resolved) all ask.
- Pass-1 Issue 3 (extglob anchor): fixed, `+(/)etc/x` and `!(a)/etc/x` no longer anchor.
- D4 consistency with invariant 2 and D3/D4: the `.claude`-segment refusal for unresolvable targets is consistent with invariants 2 and 4 in direction (it only turns allow into ask). It does not break the documented allow rows: `./$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`, `./out.$V.log`, `/tmp/.hidden_$V`, `.claude/settings.local.json.bak`-style resolved names all still allow. Cost: `/tmp/.$i.log`, `.$V` now ask (accepted by the fixer; D3 is about `..`, not dotfiles, so this is a small, safe friction, noted only).
- Whole diff re-read fresh: `_read_redirect_word`, `_scan_write_targets`, `_judge_redirect_word`, `_is_redirect_target_allowed(literal=)`, `_literal_prefix_inside_allowed_roots`, trap-handler site. Posture: `_judge_redirect_word` fails closed (only converts allow to ask). `_strip_quotes` removal has no remaining callers. `extract_write_redirect_targets` shape unchanged.
- Adversarial probes (all behave per brd or ask): mixed quoting (`"/etc"/x`, `""/etc/x`, `'/etc/x'y`, `\/etc/x`, `"$HOME"/.bashrc`), `$'/etc/x'`, `$'\x2f..'` (unresolvable ask), `${V:-/etc/x}`, `${V:-/etc}/x`, `${V:=x}/../../etc/x`, `$((1))/../..`, `~root/x`, `~root$V/x`, `~nosuch/x`, `~+`, `~-`, `~1`, quoted `'~'/x` (allow, literal), `<>`, `>|`, `&>`, `&>>`, `>&`, `2>`, `3>`, `{fd}>`, nested `$(echo $(echo x > /etc/passwd))`, `> >(cat > /etc/x)`, `> <(...)`, `( (ls 2>/dev/null) )`, symlink `cl/settings.json` and `hc/settings.json` (resolved, ask). Brace expansion in a redirect word is not performed by bash 5.3.9 here (`> ./{.claude,}/settings.json` is "ambiguous redirect"), so those allows are not an escape.

## Issues Found

### Issue 1: [severity: HIGH] A construct glued before `.claude` in one segment bypasses D4

- **File:** /data/sync/work/leangeeks-ai/claude-hooks/.claude/hooks/pretool_hook.py `_literal_prefix_inside_allowed_roots` (the segment loop and `if '.claude' in segments`)
- **Problem:** the check compares whole segments against `.claude` (exact) or a construct-led segment whose literal head is a prefix of `.claude`. A segment that begins with the construct and ends in the literal `.claude` has an empty head, which the loop skips (`if head == seg or not head: continue`), and it is not equal to `.claude`. All of these return `allow` (HEAD allowed them too, so not a regression, but they defeat D4/invariant 4 and the fixer's claimed fix of pass-1 Issue 2):

  | Command | Decision | Bash writes (V empty) |
  |---|---|---|
  | `echo x > ./$V.claude/settings.json` | allow | `<ws>/.claude/settings.json` (verified in bash 5.3.9, rc 0, file content changed) |
  | `echo x > ./$V.claude/hooks/x.py` | allow | a hook file |
  | `echo x > ./$V.claude/settings.local.json` | allow | the local settings |
  | `echo x > a/$V.claude/...`, `./${V}.claude/...`, `./"$V".claude/...`, `./$(echo).claude/...` | allow | same class |
  | `echo x > /tmp/$V.claude/settings.json` | allow | `/tmp/.claude/...` (not protected, harmless) |

  `echo x > "$V".claude/x` asks only because there is no literal anchor. The same one-token trick pass 1 reported as Issue 2 therefore still works with the construct moved to the front of the segment.
- **Fix:** replace the head/prefix heuristics with one rule: for a segment containing a construct, take its literal pieces (constructs removed, in order) and refuse if they are non-empty and could be completed to `.claude`, i.e. `re.fullmatch('.*'.join(map(re.escape, pieces)), '.claude')` (with `.*` at the construct positions at the ends). That covers `$V.claude`, `.cl$V`, `.claude$V`, `.c*` and still allows `./$V`, `x$V`, `out_$(date)`, `out.$V.log`. Add tests for `./$V.claude/settings.json` and `./$V.claude/hooks/x.py`.

### Issue 2: [severity: LOW] D4 friction on unrelated dotfiles

- **File:** same function
- **Problem:** `/tmp/.$i.log` and `.$V` now ask (dots-only head rule). Not a security issue; Issue 1's regex rule would remove it (pieces `.` and `.log` do not complete to `.claude`) while keeping `..$V` refused through the separate `..` rule.
- **Fix:** optional, with the Issue 1 fix.

## Code Quality Notes

- The unit-test mutation that replaces only `prefix = os.path.expanduser(prefix)` survives, because the following `if prefix.startswith('~'): return False` also refuses. This is defence in depth, not a test gap; mutating the whole block fails 2 tests.
- `./$(echo ..)/settings.json` and `/tmp/$(echo ..)/x` allow (a segment that is entirely a construct); this is the residual D3 explicitly accepts. Mentioned only.
- `x$V` symlink-named paths into `.claude` (`cl$V/settings.json` allow when `cl` is a symlink to `.claude`) are the same accepted residual as `x$V`; the fixer documented it.
- Pre-existing and not made worse: tokenizer `$'\''` hiding later commands (logged in state.md).
- Tests: 29 `test_41_*` tests in `TestRedirectTargets` exercise the real validator with outside-/tmp targets; assertions check decision, target and kind. The gap is the `$V.claude` form.

## Questions for User

None.

## Mutation proofs

Protocol: `git status --short` saved before (`/tmp/41-01-review2-status-before.txt`); backups in /tmp/41-01-review2-mut/*.bak made with `cp`; each mutation applied by a single-occurrence string replace, targeted tests run with `python3 -m unittest test_integration_pretool.TestRedirectTargets.<test>`; restored with `cp` from the backup and `filecmp` confirmed identical every time. No git checkout/restore/stash/add/commit. git status after matches before; test file unchanged (diff identical to its backup).

| # | Fix / row | Mutation | Result |
|---|---|---|---|
| M1 | fixer Issue 1 (`..$V`) | dots check `if set(head) == {'.'}:` to `if False:` | `test_41_dotdot_glued_to_an_expansion_is_refused` fails 6 subtests |
| M2 | fixer Issue 2 (`.claude` segment) | `if '.claude' in segments:` to `if False:` | `test_41_protected_path_completed_after_a_construct` fails 4 subtests |
| M3 | fixer Issue 2 (prefix of `.claude`) | `if '.claude'.startswith(head):` to `if False:` | same test fails 2 subtests (`.c*`, `.cl$V`) |
| M4 | fixer Issue 3 (extglob) | `text[-1] in '@+!'` branch disabled | `test_41_extglob_prefix_is_not_an_anchor` fails |
| M5 | named row `.claude/settings.local.json` | resolved-branch protected check to `if False:` | `test_41_protected_settings_inside_the_workspace` (3 subtests) and `test_41_user_claude_dir_protected_when_home_is_the_workspace` fail |
| M6 | named row inner redirect | CMD_SUBST recursion disabled in `_scan_write_targets` | `test_41_inner_redirect_of_a_target_substitution_is_scanned` (3 subtests) and `..._process_substitution_target` fail |
| M7 | named row `~/.bashrc$V` | `expanduser` line only | SURVIVED (redundant guard, see notes); whole `startswith('~')` block disabled: `test_41_lenient_fallback_expands_the_tilde` and `..._unexpandable_user_refused` fail |
| M8 | named row `"$HOME"/..` / quoted substitution | double-quoted expansion recorded as literal | `test_41_quoted_substitution_target` fails |

## Full suite

`python3 tests/run_all_tests.py`: Ran 2039 tests in 272s, OK (skipped=2). Log: /tmp/41-01-review2-suite.log. A green suite does not cover Issue 1.
