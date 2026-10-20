# Review Report - 41-01 (pass 5): the gate reads the real target

## Verdict: FAIL

Two HIGH issues (one new-in-spirit gap in R5, one pre-existing scanner gap that the
adversarial probe reached), one MEDIUM, two LOW. Both py_compile and import pass.
Full suite: `Ran 2041 tests`, `OK (skipped=2)`. Mutation proofs: all five killed.

Tested copy: repo (uncommitted tree, base HEAD 05b9fdd), permission_mode default.
Nothing installed. Probes ran against copies of the hooks under /tmp/41-01-r5/{new,head}
with a fixture workspace under /var/tmp/r5fix (outside /tmp), HOME patched into the fixture.
No real path was written; bash proofs ran in a throwaway dir under /var/tmp that I deleted.

## Completeness Check (R0-R6 as a structure)

- R0-R6 are implemented as stated, in one function (`_unresolved_target_allowed`,
  pretool_hook.py ~:1035). I traced every `return True`: the only one is the end of the
  `try` after R0, R1, R2, R3, R4, R5 and R6 have passed. There is no other path that vouches for
  an unresolvable word. Words that resolve are judged on realpath (`_is_redirect_target_allowed`),
  which returns False for anything that does not resolve.
- Fail-closed: `_judge_redirect_word` wraps everything in `except Exception -> False`; the R5
  filesystem work has its own `except -> False`; R0-R4 run before the `try` but are pure string
  operations and are covered by the outer handler. met.
- Parser: `RedirectTarget.parts`, `_read_redirect_word`, `_scan_write_targets`, `_brace_before_glued_name`
  read correctly. Recursion into target substitutions, D2 paren handling, quote removal, tilde
  handling and `$'x'`/`$"x"` handling all behave as documented (verified by ~25k fuzzed commands and
  a bash oracle, below). met.
- Must-allow rows (task item 4): all twelve allow, from the workspace root and from `sub/`.
  Run through the harness: `./$V`, `.$V`, `x$V`, `x$(echo y)`, `/tmp/out_$(date +%s).txt`,
  `out_$(date).txt`, `/tmp/.$i.log`, `./out_*.txt`, `echo $(ls 2>/dev/null)`,
  `(ls 2>/dev/null) | grep a`, `( (ls 2>/dev/null) )`, `echo k >> ./$V`. met.
- Task-table rows: I walked the table against the diff. Every row is covered by a test or a
  rule row (names below); no row was lost when ten tests became six. The six-test fold keeps the
  spellings as rows of `_REFUSED_BY_RULE_41`. met.
- Test hygiene (item 6): `tests/test_integration_pretool.py` does not isolate
  `CLAUDE_MANUAL_CONFIRM_LOG` itself; only `run_all_tests.py` and `tests/conftest.py` do.
  I used the runner only. See Issue 5.

## Issues Found

### Issue 1: [severity: HIGH] R5 does not see a symlink whose name is completed by a construct

- **File:** .claude/hooks/pretool_hook.py `_unresolved_target_allowed`, R5 block (`leaf = ... constructs taken as empty`, `names = [leaf] if ... lexists`)
- **Problem:** R5 looks up the leaf with every construct taken as EMPTY. A construct that
  supplies part of an existing symlink's name therefore never matches it. The value need not
  contain `/` or `..`, so it is not the accepted D3 residual (that is about `/` and `..`), and in
  several spellings the value is plainly visible in the source:
  ```
  # fixture: ws/lnks -> .claude/settings.json ; ws/esc -> /etc/hostname ; ws/lnk2 -> .claude/hooks
  echo k > ln${V:-k}s        allow   (bash writes .claude/settings.json: verified, file content replaced)
  echo k > ./ln$(echo k)s    allow
  echo k > l"$(echo nks)"    allow
  echo k > es$(echo c)       allow   (writes /etc/hostname's target)
  echo k > ./h${V:-c}        allow   (hc -> ~/.claude)
  V=ln?s; echo k > ./$V      allow   (the constant holds a glob so it is not substituted; bash globs $V to lnks)
  ```
  A targeted fuzz (20k words built by splitting existing symlink names around a construct, 4 cwds)
  produced 266 allows from the workspace root and 121 from `sub/` that a bash oracle shows land in
  `<root>/.claude/settings.json`, `.claude/hooks/`, `~/.claude`, or outside the roots (oracle files:
  /tmp/41-01-r5/or2_ws.json, or2_sub.json). HEAD allows the same words, so this is not a flip,
  but it is the exact hole R5 was written to close (`./lnk$V` is closed; `./ln${V:-k}s` is not).
  It needs a symlink already on disk (or one created earlier in the same command, which the gate
  already says it cannot see).
- **Fix:** in R5, when F has any construct (EXPANSION, SUBST) treat it like a glob: build the regex with the
  construct as `.*` and require every matching entry of realpath(D) to be inside a root and
  unprotected. Cost: `./$V` and `x$V` then ask only when the directory actually holds an escaping or
  protected entry, which is rare. If the orchestrator would rather call this D3 residual, the
  docstring and brd D3 must say "a construct may complete the name of an existing symlink", and the
  glob-valued constant (`V=ln?s`) deserves its own line. Add rows `ln${V:-k}s`, `l$(echo nks)`,
  `./h${V:-c}` to the R5 table.

### Issue 2: [severity: HIGH, pre-existing, not a flip] The paren scanner mis-ends a `$(…)` target that contains a `case` pattern, so the rest of the word is dropped

- **File:** .claude/hooks/bash_command_parser.py `_scan_paren_subst` (used by `_dollar_expansion_end` inside `_read_redirect_word`)
- **Problem:** `$(case x in x) echo ;; esac)` is balanced by counting parens, so the target word ends at the
  `)` after `x`. Bash parses the whole `case`, so the word continues. Verified in a scratch dir:
  ```
  echo k > rd$(case x in x) echo ;; esac)/../.claude/settings.json   -> allow (HEAD: allow too)
  ```
  bash overwrote `.claude/settings.json` (rd is an existing directory, so `rd/..` is the workspace).
  Also `$( … # )\n)` (a comment holding a `)`) and `$(echo ${V:-)})` end early (the last one needs a
  command that prints nothing to land on a real file; the scanning problem is the same).
  This is a gap in "scan the source string to the end of the shell word" (task part A) and it is
  inherited from the tokenizer, so I count it against the gate's goal, not against this diff.
- **Fix:** have `_scan_paren_subst` understand `case ... in pat) ... ;; esac`, comments and
  `${…}` inside `$(…)`; or fail closed by returning an unread-sentinel target when the scanner meets
  the word `case` (or `#`, or `${` containing `)`) inside a substitution that is part of a redirect word.
  A separate task is acceptable but it should be filed; it is the same class as the `$'\''` item the
  orchestrator already logged.

### Issue 3: [severity: MEDIUM] unquoted heredoc bodies are never scanned for write redirects

- **File:** .claude/hooks/bash_command_parser.py (tokenizer, heredoc body handling), out of 41-01's diff
- **Problem:** `cat <<EOF\n$(echo k >/etc/y)\nEOF` returns `allow` (also `>.claude/settings.json`,
  `>~/.claude/x`). An unquoted heredoc body does execute `$(…)`. HEAD allows the same, so it is
  pre-existing and unrelated to target-word reading. Listed because item 2 asked about heredoc lines.
- **Fix:** separate task: scan an unquoted heredoc body for `$(`/backticks like any word and recurse.

### Issue 4: [severity: LOW] the fixer's report undercounts the `$'x'` flip class

- **File:** agents_output/41-01_fix_report_4.md (HEAD vs change section)
- **Problem:** it says the 52 flips are all `$'x'`. In my corpora the ask-to-allow flips also include
  `$"k"` (locale-translated, no `$`), and `lnk$(true)ln`/`esc$(echo k).jsonnk`-style words whose
  literal piece names a symlink but whose completed name does not exist. Bash oracle over all new
  allows (24k + 19k words, 2 cwds, single-word results only): zero land outside the roots or in a
  protected path, except `.${V:-.}` which writes the directory `..` (cannot be written). So the flips
  are correct, but the HEAD-vs-change claim in the report is incomplete. Also `echo k > "~/.claude/x"`
  flips to allow (quoted tilde is the literal directory `~`, per the task row `'~'/x`): correct.
- **Fix:** none needed in code. Note for the report only.

### Issue 5: [severity: LOW] test hygiene

- **File:** tests/test_integration_pretool.py (no env isolation of its own)
- **Problem:** the module relies on `tests/run_all_tests.py` / `conftest.py` to point
  `CLAUDE_MANUAL_CONFIRM_LOG` at a temp file. Run directly with `python3 -m unittest`, it writes to the
  real `~/.claude/bash_manual_confirm.log` (state.md records 84 polluted entries from pass 4). I did not
  reproduce it, I only used the runner. Also, in three of my five mutation runs (while I was running
  CPU-heavy fuzz in parallel) `TestTrapHandlerAgainstRealBash` failed 8+ cases that are unrelated to the
  mutation; the unloaded full suite was green. Those tests are timing-sensitive under load.
- **Fix:** set the isolation env vars in the test module's `setUpModule` (setdefault to a temp dir).
  The trap tests: out of scope.

## Item 3: R0 and the `$'x'` flips

R0 (the word must begin with LITERAL or TILDE) is correct and needed: without it `> "$LOG"`, `> $x`,
`> $(echo ~/.bashrc)` have an empty directory part and would be vouched for. Mutation proof below. The `$'x'`
and `$"x"` flips are correct: `$'x'/../etc/x` is the path `x/../etc/x` and bash has no `x`; `$"V"` writes a file
named `V`; the oracle confirms every flipped command lands inside the roots and off the protected list.
No flip I found is a real escape. The only ask-to-allow flip outside the quoting classes is the paren row
`(ls 2>/dev/null) | grep a`.

## Adversarial probing: what I tried and what held

Held (no allow that bash lands outside): `$'…'`/`$"…"`, backslash-newline (in D and F), `${V/…/…}`, `${V#…}`,
`$((…))`/`$[…]`, brace expansion (`{a,b}`, `{/etc/x,}`, `x{,}`), glob classes, `.?`/`.??`/`.[.]`/`.*`
(dotfiles are listed by R5), `~+`/`~-`/`~user`/`~$V`, symlink loops, D that is a symlink to a file
(refused by R2/R3), cwd that is a symlink (identical decisions), workspace = HOME (`~/.claude` and
`.c*` refused), `&>`, `>|`, `<>`, `&>>`, `N>`, `{fd}>`, `>&path`, nested subshells, process substitution as
target and as host, fd forms inside `$(…)`, constants (`V=/etc; > $V/x`, `V=/etc/x; > $V"2"`), 700+ words
where a quoted or escaped metacharacter sits inside the target before `/../…`, and a 24k random word fuzz
with a bash oracle.

Broke (see Issues 1-3).

## Mutation proofs

Protocol: `git status --short` saved before (/tmp/41-01-review5-mut/status-before.txt); each source file
copied with `cp` to /tmp/41-01-review5-mut/*.bak; one exact replacement per run; the whole
`integration_pretool` module run through `tests/run_all_tests.py --module integration_pretool`; restore with
`cp`, `diff` identical every time; `git status --short` after is identical. No git state-changing command.
Driver: /tmp/41-01-review5-mut/run.sh; logs R0.log, R1.log, R3.log, R5.log, ROW.log.

| Mutation | Result |
|---|---|
| R0: `if not parts or parts[0][0] not in (LITERAL, TILDE)` -> `if False` | FAILED: R0 rows `"$LOG"`, `$V.log`, `$(mktemp)` from both cwds, and `test_anchorless_variable_target_prompts` |
| R1: dir-part construct check -> `if False and any(...)` | FAILED (45): R1 rows incl. `..\`echo /et\`c/x`, `..${U:-/et}${V:-}c/x`, and `test_41_construct_in_a_directory_position_asks` x5 |
| R3: `_is_protected_write_path(base, unresolved=True)` -> `if False` | FAILED: R3 rows `lnk2/$V`, `lnk/x$V` (from ws) and others |
| R5: `for name in names:` -> `for name in []:` | FAILED (29): `./+(l)nks`, `./e*c`, `./es${V}c`, `./l$(true)nks` etc, both cwds |
| Task-table row: remove `.claude/settings.local.json` from `_protected_write_paths` | FAILED: `test_41_protected_settings_inside_the_workspace` (`echo x > .claude/settings.local.json`) |
| (R1, R3, R5, row runs also show 8-21 `TestTrapHandlerAgainstRealBash` failures that are load-related, see Issue 5; the targeted tests above are the proof) | |

Five mutations (R0, R1, R3, R5, one task-table row), all killed; no mutant survived.

## Code Quality Notes

- Structure is sound. R0-R6 live in one place, the decision is a conjunction, and the parts list is
  the right substrate. Issue 1 is a missing wildcard step inside R5, not a structural flaw.
- `_judge_redirect_word` substitutes constants only into EXPANSION parts, but a constant that
  contains glob characters is not substituted at all, so `V=ln?s; > ./$V` reaches R5 as an empty
  construct. Same fix as Issue 1 (treat constructs as wildcard) covers it.
- Tests: the rule table runs each spelling from the workspace root and from `sub/` against real
  symlink fixtures outside /tmp. It asserts decision, kind, and a non-empty target list (not the exact
  target text), which is acceptable. It has no row for a construct that completes a symlink name
  (Issue 1) or a `case` inside a target substitution (Issue 2).
- `sub/` semantics: today the workspace root is the cwd; every `sub/` probe measures roots relative to `sub/`.
  41-02 will change this; rerun the `sub/` rows after it.

## Questions for User

None.
