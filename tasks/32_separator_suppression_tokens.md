# Task 32 — Two tokens switch the separator scanner off, hiding every command after them (bug)

**Status:** todo · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 1
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
  X=curl` allows. Not yet filed.
- **NUL fail-open** — a NUL in a redirect target raises out of
  `_resolve_target_path`; `main()`'s blanket except turns it into `sys.exit(0)`,
  an allow. Not yet filed.
- `(( a & b ))` mis-splits toward `ask` (harmless, task 31 §8.8).
