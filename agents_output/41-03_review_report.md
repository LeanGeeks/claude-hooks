# Review Report - 41-03 An unresolvable write target is an unknown, not a risk

## Verdict: FAIL

One MEDIUM (orchestrator Decision on R5 in a directory-position construct segment is not implemented). No BLOCKER or HIGH. Per the reviewer rules any open issue is reported; the MEDIUM is a small, local fix.

Compile/import: py_compile on pretool_hook.py, telegram_permission_router.py, permissions_mcp_lib.py OK. Full suite: Ran 2149, OK (skipped=2) (/tmp/41-03-review-suite.log). git status identical before/after all mutations and the suite.

## Completeness Check

- Three-outcome split in one function (`_redirect_target_outcome`, pretool_hook.py:1400) next to `_is_redirect_target_allowed`: met.
- Precedence deny > ask > outside > unresolved-only > unknown: met (pretool_hook.py:1060-1128). Outside wins when unresolved also present (card and reason show both; mutation-proven).
- Result shape: `redirect_targets` (outside only) and `unresolved_redirect_targets` present on every result: met. Reason byte-identical for outside-only (asserted in my HEAD-vs-new comparison); unresolved-only reason "Write target cannot be resolved — review: ..." claims no escape: met.
- build_pretool_output unchanged; unknown silent in auto/bypassPermissions/dontAsk, ask in default/plan/acceptEdits/None/bogus; outside asks in all (checked 8 mode values vs HEAD): met.
- Permissions MCP: tier off the two structured keys, prefix fallback kept (permissions_mcp_lib.py:361-368): met, mutation-proven.
- Card: BashAnnotations field + __post_init__ normalisation + `empty` + `_bash_annotations`: met. Deferring-mode new heading and neutral section: met. Non-deferring outside-only card byte-identical to HEAD's router (rendered with HEAD router copy over 9 commands x 6 modes: 42 SAME, only the mixed outside+unresolved cards differ, as intended): met.
- Epic 40 invariant 4 amended in place with the task's wording plus dated Log line; architecture.md PreToolUse row + new subsection: met.
- Test table rows (validator + auto/bypass/default): met; independently re-ran representative rows through HEAD/new, same outcomes as the report.
- Orchestrator Decision (directory-position segment of constructs only must be unknown): NOT met, see Issue 1.
- Tests keep isolation (isolated env vars set before import; real workspace under /var/tmp, removed in tearDownClass). The real ~/.claude/bash_manual_confirm.log grew only from live sessions; no entry with this task's markers.

## Issues Found

### Issue 1: [severity: MEDIUM]

- **File:** .claude/hooks/pretool_hook.py:_unresolved_target_refusal, the `if in_directory:` branch of the R5 block (names = [...regex.fullmatch(name)...], about :1658)
- **Problem:** the R5 matcher runs on a directory-position segment that consists only of constructs with no literal character. In a workspace that has a `.claude/` entry (the normal project root) the pattern `.*` matches `.claude`, so these stay risk, while `"$dir"/x` (no `./`) is unknown. Reproduced from a root holding `.claude/`:
  - `echo x > ./"$dir"/x` -> ask/risk, redirect_targets ['./$dir/x']
  - `echo x > ./$V/x` -> ask/risk
  - `echo x > ./$V/settings.json` -> ask/risk
  - `echo x > "$dir"/x` -> ask/unknown (the inconsistency); from `sub/` the `./` forms are unknown.
  Segments with literal characters or a glob (`.$V/x`, `.cl${V:-aude}/…`, `./.*/settings.json`) correctly stay risk from root, per the orchestrator decision.
- **Fix:** in the directory-position R5 branch, skip the filesystem match (return `held`) when the segment has no LITERAL part and no GLOB part (all parts are EXPANSION/SUBST constructs). Keep the match for segments with a literal character or an unquoted glob. Update `test_41_unresolvable_targets_refused_by_rule` rows `./$V/settings.json` (root becomes unknown) and the M8 test row `./$V/x`, and add `./"$dir"/x`, `./$V/x`, `out/$name/x.log` unknown from root.

## Invariant 2 fuzz (HEAD vs change)

HEAD copy of pretool_hook.py imported next to the working copy, same fake loader (echo, mktemp allowed), real workspace with `.claude/hooks`, `.claude/settings.json`, symlinks (`links/lns`, `lnk2`, `esc` -> /etc/hostname, `lnk` -> .claude/settings.json), cwd = root and root/sub, HOME fake, V/f/x/S/name/dir unset. Corpus: 41-01 `_REFUSED_BY_RULE_41` shapes + directory-position shapes + 45k distinct random fragment targets, plus the task's compound commands. 2 cwds x ~30k commands = 74,418 comparisons.
- same: 45,712. Changed: 28,706, ALL ('ask','risk') -> ('ask','unknown'). 0 flips from ask/deny to allow, 0 allow changes, 0 other changes. (INV-2 holds.)
- Of the risk -> unknown flips, I filtered for visible concrete escapes: `.claude` in text (0), `..` path segment in the construct-skeleton (0), literal `/etc`-style anchor outside the roots (0 real; the 7 hits are `//tmp/...`, inside /tmp), `..` or `claude` inside a construct (0 real). No HIGH flip.
- Shape-level flips (all consistent with the mapping): R0 words (`"$LOG"`, `$V.log`, `$(mktemp)`, `$V/x`), R1 under an inside literal directory (`out/$name/x.log`, `/tmp/$V/x`, `.cl${V:-aude}/…`, `ln*/settings.json`, `./*/x` from sub only), R4-slash (`.claud${V-e/setting}s.json`, `.${V:-./e}tc/x`), `{/etc/x,}`, `{lnks,}`, `+(/)etc/x`, `~$V/x`. Resolved/anchored escapes (`~/.bashrc$V`, `/etc/$V`, `.$V/../../etc/x`, `.claude/$V`, `$V/../x`, `~nosuchuser/$V`, `links/ln${V}s`) stayed risk.
- build_pretool_output for 12 commands x 8 modes: every outside/protected row ask in all modes incl. bypassPermissions; only unresolved-only rows went ask -> None, and only in auto/bypassPermissions/dontAsk.

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-03-review-mut/before.txt) and after (diff identical); `cp` backups of pretool_hook.py and permissions_mcp_lib.py; single-string replace; targeted tests; restored with `cp`, `diff` identical each time. No git write ops.

| # | Mutation | Targeted tests | Result |
|---|---|---|---|
| 1 | outside asks in bypassPermissions: build_pretool_output returns None in bypassPermissions for every ask | TestUnresolvedTargetsAreUnknowns | FAIL (outside_wins_over_unresolved, rule_to_outcome_risk_rows x many, task table) |
| 2 | outside wins over unresolved: `elif outside_targets and not unresolved_targets:` | TestUnresolvedTargetsAreUnknowns | FAIL 2 (test_41_03_outside_wins_over_unresolved, task_table row `"$f" ; /etc/x`) |
| 3 | MCP human-only tier for unresolved row: drop `fresh.get("unresolved_redirect_targets")` | unit_permissions_mcp TestDecideTier | FAIL 3 (structured keys test, unknown-command-with-unresolved, unresolved-refuses-human-only) |
| 4 | mapping, risk row: add `R3` to UNRESOLVED_REFUSALS | TestUnresolvedTargetsAreUnknowns + TestRedirectTargets | FAIL (`.claude/$V`, 41-01 R3 rows, protected-settings test) |
| 5 | mapping, unknown row: remove `R4-slash` from UNRESOLVED_REFUSALS | TestUnresolvedTargetsAreUnknowns | FAIL 1 (`./out_${V:-a/b}`) |

## Code Quality Notes

- `{lnks,}` (brace expansion naming a symlink that escapes) is now unknown (R0 skips R5). Consistent with the task table (`{a,b}` is unresolved) and the implementer's Decision 3; accepted residual, in default mode it still asks.
- Mixed outside+unresolved reason appends `; write target cannot be resolved: ...`; the old-outcome reasons are untouched.
- Unknown sub-command plus unresolved target keeps the "Not in allowlist" reason and puts the target in the new key (card + MCP human-only still right).
- Exceptions while judging a target fail toward outside (risk); fine.

## Questions for User

None.
