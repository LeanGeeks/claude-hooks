
# Implementation Report - 41-03 An unresolvable write target is an unknown, not a risk

## Summary

Every write-redirect target now has one of three outcomes, decided in one function, `BashPermissionValidator._redirect_target_outcome`:

- **allowed**.
- **outside**: `ask_kind: 'risk'`, so the hook asks in every mode.
- **unresolved**: `ask_kind: 'unknown'`, so the hook prints nothing in `DEFERRING_MODES` and asks in every other mode.

The R0–R6 checks (`_unresolved_target_refusal`, formerly `_unresolved_target_allowed`) now return the code of the rule that refused the target. The outcome function maps that code through one set, `UNRESOLVED_REFUSALS = {R0, R1, R4-slash}`; any other code means outside. Other changes:

- The validator applies the task's precedence. Results gain the `unresolved_redirect_targets` key.
- The Telegram card has a neutral heading for unresolved targets and an honest harness line for deferring modes.
- The permissions MCP tiers a row human-only when either target list is non-empty. The old reason-prefix check stays as a fallback.
- Epic 40 invariant 4 and `architecture.md` are amended.

## Files Created

None. The tests went into the existing suites.

## Files Modified

- `.claude/hooks/pretool_hook.py`
  - Outcome constants and `UNRESOLVED_REFUSALS` (:98-124).
  - `_redirect_target_outcome` replaces `_judge_redirect_word` and sits next to `_is_redirect_target_allowed` (:1400).
  - `_unresolved_target_refusal` replaces `_unresolved_target_allowed` (:1466): same rules, now reporting a code.
  - `validate_bash_command`: decision block and result shape (:1037-1134).
  - The trap-handler gate uses the outcome function; any target that is not allowed makes the trap not allowed, as before.
  - Docstrings and comments updated.
- `.claude/hooks/telegram_permission_router.py`
  - `BashAnnotations.unresolved_redirect_targets`: normalised in `__post_init__` and counted in `empty`.
  - `_bash_annotations` copies the key from the validator result.
  - `render_permission_body` gains the new deferring-mode heading (:680) and the neutral section (:711).
- `permissions-mcp/permissions_mcp_lib.py`: `_classify_command` tiers off the two structured keys, with the prefix check as a fallback (:362-368). The comment at :101 is updated.
- `architecture.md`: the PreToolUse row (:113) and a new "Write-target outcomes (epic 41, task 41-03)" subsection.
- `tasks/40_permission_mode_parity/state.md`: invariant 4 amended in place with the task's wording, plus a dated Log line.
- `tests/test_integration_pretool.py`:
  - New class `TestUnresolvedTargetsAreUnknowns` (9 tests).
  - Existing 41-01 tests reclassified (details under Decisions).
  - New helper `_assert_unresolved`.
  - `_assert_no_refusal` also checks the new key.
- `tests/test_integration_permission_request.py`: 7 card tests in `TestPermissionModeAnnotation`.
- `tests/test_unit_permissions_mcp.py`: 4 tier tests in `TestDecideTier`.

I did not touch `tasks/41_redirect_gate_parity/state.md`; its diff is the orchestrator's uncommitted edit.

## Verification

- **Compile and import: PASS.** `py_compile` passed on the three changed modules. `permissions_mcp_lib` imports cleanly from `permissions-mcp/`.
- **Tests: 2149 run, 0 failed, `OK (skipped=2)`.** Command: `python3 tests/run_all_tests.py`; log at `/tmp/41-03-suite.log`.
- **Installed: no**, as instructed. Everything was tested against the repo copy.

## Testing

- **Baseline:** measured before the first edit at HEAD 2df3c12: `Ran 2129 tests`, `OK (skipped=2)` (`/tmp/41-03-baseline.log`).
- **Final:** `Ran 2149 tests`, `OK (skipped=2)`. The extra 20 tests are 9 in pretool, 7 for the card and 4 for the MCP.
- **Skips, by name, the same two as the baseline:**
  - `test_unit_python_compat.TestImportUnderOldInterpreter.test_all_hook_modules_import`: no pre-3.11 interpreter with tomli.
  - `test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`: needs `AMUX_SPAWN_LIVE_TEST=1`.
- **Equivalence check of the allowed set (not committed as a test):** I ran HEAD's `_unresolved_target_allowed` and the new `_unresolved_target_refusal(...) is None` side by side on the same validator setup over 59,792 random parts lists. The workspace was real, with `.claude/`, symlinks into `.claude`, outward symlinks and globs. **0 mismatches** (8,323 allowed). The refactor changes how a target is classified, never whether it is allowed.
- **Real `~/.claude/bash_manual_confirm.log`:** no entry from these runs. The only grep hit for `4103` was a timestamp in an older, unrelated entry. Growth during the run came from live sessions.

## Printed output per mode

These rows come from running the repo hook as a subprocess (`pretool_hook.py`, JSON payload on stdin). Setup:

- A real workspace under `/var/tmp` holding `.claude/`, `sub/`, and `links/lns -> /etc/hostname`.
- Project settings: allow echo, mktemp and `git:*`; ask on `git push:*`; deny `rm:*`.
- `CLAUDE_PROJECT_DIR=<root>`, `HOME=<scratch home>`, an isolated `CLAUDE_MANUAL_CONFIRM_LOG`, and none of `V`, `f`, `x`, `S`, `name`, `dir` set in the environment.

"nothing" means the hook printed nothing. Reasons are quoted exactly. Two abbreviations:

- **R-outside:** `Redirects output outside the workspace, /tmp, or /dev/null — review the write target: `
- **R-unres:** `Write target cannot be resolved — review: `

### The task's table

| Command | Outcome | auto | bypassPermissions | default |
|---|---|---|---|---|
| `for f in a b; do echo x > "$f.out"; done` | unresolved | nothing | nothing | ask — R-unres `` `$f.out` `` |
| `x=$(mktemp); echo y > $x` | unresolved | nothing | nothing | ask — R-unres `` `$x` `` |
| `echo hi > $(echo ~/.bashrc)` | unresolved | nothing | nothing | ask — R-unres `` `$(echo ~/.bashrc)` `` |
| `echo y > "$S/.env"` | unresolved | nothing | nothing | ask — R-unres `` `$S/.env` `` |
| `echo y > {/etc/x,}` | unresolved | nothing | nothing | ask — R-unres `` `{/etc/x,}` `` |
| `echo k >> $HOME/.bashrc` | outside | ask — R-outside `` `$HOME/.bashrc` `` | same | same |
| `echo k >> "${HOME}"/.claude/settings.json` | outside | ask — R-outside `` `${HOME}/.claude/settings.json` `` | same | same |
| `echo k >> ~/.bashrc$V` | outside | ask — R-outside `` `~/.bashrc$V` `` | same | same |
| `echo k >> /etc/$V` | outside | ask — R-outside `` `/etc/$V` `` | same | same |
| `echo k >> .$V/../../etc/x` | outside | ask — R-outside `` `.$V/../../etc/x` `` | same | same |
| `echo k >> ~/.bashrc` | outside | ask — R-outside `` `~/.bashrc` `` | same | same |
| `echo x > .claude/settings.local.json` | outside (protected) | ask — R-outside `` `.claude/settings.local.json` `` | same | same |
| `echo a > "$f" ; echo b > /etc/x` | outside wins | ask — R-outside `` `/etc/x`; write target cannot be resolved: `$f` `` | same | same |
| `echo a > ./$V` | allowed | allow | allow | allow |
| cwd `<root>/sub`: `echo a > ../x` | allowed (resolved `..`) | allow | allow | allow |
| `echo a > "$f"; git push origin main` | ask match wins | ask — `` Matches an ask pattern: `git push origin main` `` | same | same |
| `echo a > "$f"; rm -rf build` | deny | deny — `` Matches a denied pattern: `rm -rf build` `` | same | same |

### The orchestrator's rule → outcome rows

| Command | Outcome | auto | bypassPermissions | default |
|---|---|---|---|---|
| `echo k > .claude/$V` | outside (R3) | ask — R-outside `` `.claude/$V` `` | same | same |
| `echo k > links/ln${V}s` (`lns -> /etc/hostname`) | outside (R5) | ask — R-outside `` `links/ln${V}s` `` | same | same |
| `echo k > $(case x in x) echo ;; esac)` (UNPROVEN sentinel) | outside | ask — R-outside `` `$__unproven_substitution_end__` `` | same | same |
| `echo k > out.txt` with `_redirect_operand_start` patched to None (UNREAD sentinel, in-process via `build_pretool_output`) | outside | ask — R-outside `` `$__unread_redirect_operand__` `` | same | same |
| `echo k > out/$name/x.log` | unresolved (R1) | nothing | nothing | ask — R-unres `` `out/$name/x.log` `` |
| `echo k > "$dir"/x` | unresolved (R0) | nothing | nothing | ask — R-unres `` `$dir/x` `` |
| `echo k > $(mktemp)` | unresolved (R0) | nothing | nothing | ask — R-unres `` `$(mktemp)` `` |
| `echo k > {/etc/x,}` | unresolved (R0) | nothing | nothing | ask — R-unres `` `{/etc/x,}` `` |
| `echo k > ./out_${V:-a/b}` | unresolved (R4-slash) | nothing | nothing | ask — R-unres `` `./out_${V:-a/b}` `` |
| Truncation marker (target nested `$(` 65 deep; in-process) | unresolved | nothing | nothing | ask — `` Not in allowlist — review before approving: `__unparsed_nested_substitution__` `` |

`/etc/$V`, `~/.bashrc$V` and `.$V/../../etc/x` are in the first table.

In `bypassPermissions`, a printed `ask` is an audit row only: `permission_request_hook` auto-allows it (brd §1.1).

The truncation row reads "Not in allowlist" because the parser also reports the truncated command as an unknown sub-command. Its target is in `unresolved_redirect_targets`.

## Rule → outcome map

Mapping: `pretool_hook.py:124` `UNRESOLVED_REFUSALS = frozenset({'R0', 'R1', 'R4-slash'})`, applied at `:1459`. Any code not in the set is outside.

Codes, in evaluation order. A code marked "held back" is remembered while the rest run, and only returned if no risk code fires; any risk code returns at once.

| Code | `pretool_hook.py` line | When | Outcome |
|---|---|---|---|
| `sentinel` | :1556 | `UNREAD_REDIRECT_TARGET`, `UNPROVEN_SUBSTITUTION_TARGET` | outside |
| `R1-tilde` | :1558 | a tilde-prefix that is not the first part (cannot happen with today's parser; defensive) | outside |
| `R6` | :1567 | `claude` in any construct's source | outside |
| `R2-dotdot` | :1571 | a `..` segment in the literal text, each construct read as one opaque character (`$V/../x`, `.$V/../../etc/x`) | outside |
| `R4-dotdot` | :1573 | `..` in any construct's source (`$(cd ..; pwd)/x`) | outside |
| `R6` / `R0` | :1576 | the word starts with a construct: `R6` if `.claude` is in the text (`$V.claude/x`), else `R0` | outside / **unresolved** |
| `R1` | :1603 | a construct or glob before a later literal `/`. Held back. | **unresolved** |
| `R2-user` | :1606, :1612 | `~user` that does not expand, or a tilde-prefix with no `/` after it | outside |
| `R2-dotdot` | :1614 | the tilde-expanded directory has a `..` segment | outside |
| `R2-link` | :1616 | the directory goes through `/dev/fd/N`, `/proc/self`, etc. | outside |
| `R4-dotdot` | :1621, :1628 | a literal piece of the segment holds `..`, or the segment with its constructs taken as empty is `..` | outside |
| `R4-escape` | :1623 | a construct in the segment has a backslash | outside |
| `R4-slash` | :1625 | a construct in the segment has `/` (no `..`, no backslash). Held back. | **unresolved** |
| `R2-outside` | :1632 | realpath of the literal directory is outside the roots | outside |
| `R3` | :1635 | realpath of the literal directory is protected (`<root>/.claude/`, `~/.claude/` minus the memory carve-out) | outside |
| `R6` | :1641 | `.claude` in the text (the directory's text is exempt inside the own memory dir) | outside |
| `R5` | :1681 | a matched entry on disk realpaths outside the roots or into a protected path | outside |
| `error` | :1685 | filesystem error (e.g. a final-segment glob in a directory that cannot be listed) | outside |
| — | :1682 | nothing refused: the held-back code, or None | as held / allowed |

Outcome function (`:1400-1464`):

- fd-pipe words, and a resolving word inside the roots → allowed.
- A literal word refused by `_is_redirect_target_allowed`, or a resolvable word that resolves outside or protected → outside (:1435, :1447).
- An exception while judging → outside (:1464).

## Mutation proofs

Protocol:

- `git status --short` was saved before (`/tmp/41-03-mut-status-before.txt`) and after (`/tmp/41-03-mut-status-after.txt`); the two are identical.
- The three files were backed up to `/tmp/41-03-mut/*.bak`.
- Each mutation was a single-string replace, followed by the targeted test run, then a restore with `cp` and a clean `diff` against the backup.
- No git commands besides `git status`/`git diff`/`git show HEAD:` (read-only).

| # | Mutation | Targeted test | Result |
|---|---|---|---|
| M1 | outside branch sets `ask_kind='unknown'` | `test_41_03_task_table` | FAIL: 8 rows (`('ask','unknown') != ('ask','risk')`) |
| M1b | `build_pretool_output` silences every ask in `bypassPermissions` | `test_41_03_task_table` | FAIL: 9 rows, `'silent' != 'ask'` (every outside row plus the ask-pattern row). **An outside target still asks in bypassPermissions.** |
| M2 | `elif outside_targets and not unresolved_targets:` | `TestUnresolvedTargetsAreUnknowns` | FAIL: `test_41_03_outside_wins_over_unresolved` and the `"$f" ; /etc/x` table row. **Outside wins over unresolved.** |
| M3 | MCP drops the `unresolved_redirect_targets` check | `unit_permissions_mcp` | FAIL: 3 (`test_unresolved_target_refuses_as_human_only`, `test_unknown_command_with_unresolved_target_is_human_only`, structured-keys test). **The human-only tier holds for an unresolved row.** |
| M3b | MCP drops the `redirect_targets` check | `unit_permissions_mcp` | FAIL: 1 (structured-keys test, neutral reason) |
| M3c | MCP drops the prefix fallback | `unit_permissions_mcp` | FAIL: 1 (structured-keys test, old-shape result) |
| M4 | `R2-outside` added to `UNRESOLVED_REFUSALS` | `TestUnresolvedTargetsAreUnknowns` | FAIL: 4 (`/etc/$V`, `~/.bashrc$V` in the risk rows and the table). **Risk-vs-unknown mapping.** |
| M5 | `R1` removed from `UNRESOLVED_REFUSALS` | same | FAIL: `out/$name/x.log` unknown row |
| M5b | `R4-slash` removed | same | FAIL: `./out_${V:-a/b}` |
| M6 | R0 ignores a visible `.claude` | same | FAIL: `$V.claude/x` |
| M7 | no global `..` check on construct text | same | FAIL: `$(cd ..; pwd)/x` |
| M8 | no R5 in a directory position | same | FAIL: `./$V/x` |
| C1 | router: drop the new deferring-mode heading branch | `integration_permission` | FAIL: 3 (auto, bypassPermissions, dontAsk) |
| C2 | `BashAnnotations.empty` ignores unresolved targets | same | FAIL: 2 |
| C3 | router: drop the neutral 📝 section | same | FAIL: 7 |

## Decisions

1. **How R-checks report the rule.** `_unresolved_target_refusal(parts) -> Optional[str]` returns a code, and `UNRESOLVED_REFUSALS` is the only list of unknown codes. The refusal logic still lives in one function, so there is no parallel implementation. To keep risk ahead of unknown, the unknown codes are held back:
   - R0 returns only after the visible-text checks that apply to an anchorless word: `claude` / `..` in construct text, a literal `..` segment, `.claude` in the text.
   - R1 and R4-slash return only after R2–R6.

   The fuzz showed the allowed set is unchanged.
2. **R5 in a directory position (an extension, flagged for review).** The orchestrator lists "an R5 filesystem match" as risk. Before this task, R5 only ever saw a final segment, because R1 stopped earlier. For an R1 word I now run the same R5 matcher on the first segment that holds a construct. That segment is traversed as a directory, so every matching entry counts (not only symlinks), and a missing directory matches nothing. The effect:
   - `.cl${V:-aude}/settings.json` and `./"$dir"/x`, run from a root that contains `.claude/`, are **outside**, because the matcher names `.claude`.
   - From a subdirectory with no such entry, the same words are unresolved.
   - `out/$name/x.log` stays unresolved, even when `out/` does not exist yet (the `mkdir -p` pattern).

   If you want R1 to be unknown without exception, delete the `in_directory` branch at `:1658`. M8 shows which test would need changing.
3. **R0 words skip R2–R5.** There is no anchor to judge, so they get only the visible-text checks. In particular `{lnks,}` (brace expansion naming an escaping symlink) is now unresolved. It was risk under 41-01.
4. **Tilde forms.** `~$V/x`, `~+/x` and `~-/x` reach the gate as a single SUBST part (the parser's contract), so they are R0, i.e. unresolved. Only a real TILDE part that fails to expand (`~nosuchuser/$V`, `~nosuch`) is "unexpandable ~user" → outside.
5. **Mixed reason.** When outside and unresolved targets appear together, the reason is today's outside wording plus `; write target cannot be resolved: …`. Outside-only reasons are byte-identical to before. In the deny and ask-pattern branches both target lists are `[]`, as `redirect_targets` was before. When an unknown sub-command and an unresolved target appear together, the reason is the unchanged "Not in allowlist", and the target goes in `unresolved_redirect_targets` so the card lists it and the MCP keeps the row human-only.
6. **Existing 41-01 tests reclassified.** `_REFUSED_BY_RULE_41` now asserts the outcome for each row and each cwd through `_UNKNOWN_SINCE_41_03`. Rows that became unknown:
   - R0 in both cwds: `"$LOG"`, `$V.log`, `$(mktemp)`.
   - R1 rows that are unknown from `sub/` only: `.cl…` / `.c…` / `.c*` / `.[c]laude`.
   - Unknown in both cwds: `.claud${V-e/setting}s.json`, `.${V:-./e}tc/x`, `+(/)etc/x`, `!(a)/etc/x`, `{/etc/x,}`, `~±/x`, `~$V/x`, `r$[1|0]/x`, `.c"$(echo laude/settings.js)"on`, `{lnks,}`.

   Every other row is still risk. `test_41_substitution_target_is_kept_in_source_form`, `test_41_quoted_substitution_target`, `test_41_unmodelled_expansions_are_unresolvable` and `test_41_construct_in_a_directory_position_asks` now assert the new outcomes.
7. **Card wording.**
   - The new heading is `🤖 <b>The harness flagged this call — the session's mode resolved it</b>`.
   - The neutral section is `📝 <b>Write targets the hook could not resolve:</b>`, shown in every mode.
   - The harness line appears only in a deferring mode with no unknown, asked, denied or outside entries. With unknown sub-commands, the existing heading stays and the 📝 section is added.

## Residual risk this task accepts (decision (a), for the record)

Spellings that build `..` or `.claude` across a literal/construct boundary are now **silent in deferring modes**: `.claud${V-e/setting}s.json`, `.c"$(echo laude/settings.js)"on`, `.${V:-./e}tc/x`, and `.cl${V:-aude}/settings.json` from a directory with no `.claude` entry. In `default` they still ask, and none of them is ever `allow`. Under (a) they are no worse than `> "$P"` with `P` assigned at runtime, which is unresolved by design.

## Live checks owed (41-04)

- After `./install.sh --yes`, in a live **auto** session:
  - `for f in a b; do echo x > "/var/tmp/$f.out"; done` and `x=$(mktemp); echo y > $x` should raise no prompt and no card. `~/.claude/bash_hook_debug.log` (with `CLAUDE_HOOK_DEBUG=1`) should show `DEFERRING to the pipeline`, and the confirm log should show `emitted: null` with reason `Write target cannot be resolved — …`.
  - `echo k >> ~/.bashrc$V` must still prompt, on the terminal and on Telegram.
- A harness-raised card for an allowlisted command with an unresolved target should show the new 🤖 line and the 📝 section.
- The 41-04 tally filters on `emitted`, not on the reason (the reason wording changed for unresolved-only asks).
- Not verified: anything on the installed copy. Every check in this report ran against the repo copy.

## Blockers

None.

## Questions for User

None. Decision 2 (R5 in a directory position) is flagged for the reviewer's call, not for the user.
