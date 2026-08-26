# Task 25 — Parser: incomplete shell-builtin allowlist

**Filed by:** daily permission reviewer 2026-08-26 (queue entry 20260826T135656Z_2b0e6439)  
**Source:** fixture-agent @ hyppie-flow, session fixture-session-cccc3333

## Reported command

```
trap 'rm -f temp/review.lock' EXIT; python3 tests/run_all_tests.py
```

## Reproduction (confirmed 2026-08-26)

```
$ printf '%s\n' '{"command": "trap '\''rm -f temp/review.lock'\'' EXIT; python3 tests/run_all_tests.py"}' \
    | CLAUDE_HOOK_REPLAY=1 python3 .claude/hooks/pretool_hook.py --dry-run

Decision: ask
Reason: Not in allowlist — review before approving: `trap 'rm -f temp/review.lock' EXIT`
Sub-commands: ["trap 'rm -f temp/review.lock' EXIT", 'python3 tests/run_all_tests.py']
```

Sub-command split is correct. The validator sees two sub-commands, checks each
against patterns, and blocks on `trap` — which has no pattern.

## Root cause

The validator handles POSIX shell builtins one pattern at a time. Currently
allowlisted: `set`, `export`, `source`, `read`, `for`, `done`, `fi`, `break`.
Not allowlisted (and therefore asking): `trap`, `shift`, `local`, `wait`,
`umask`, `ulimit`, `getopts`, `return`, `continue`, `unset`, `readonly`, and
others.

`eval` is deliberately absent from this list — it executes arbitrary strings
and should stay in the ask/human tier regardless.

## Why this is structural

Patching `Bash(trap:*)` alone would fix this one report but leave the other
missing builtins to surface case-by-case. The right fix is a curated
"known-safe shell builtins" category in the parser with an explicit exclusion
list (`eval`, `exec`, maybe `source` when the argument is a variable).
Deciding which builtins are safe to auto-allow requires human judgment about
the threat model; the reviewer cannot make that call unilaterally.

## Proposed shape of fix

1. In `.claude/hooks/pretool_hook.py` (or the sub-command classifier), add a
   set `SAFE_BUILTINS` covering the already-allowlisted ones plus the safe
   missing ones. Exclude `eval`, `exec`, and any builtin that can load or
   execute external code.
2. A sub-command whose head token is in `SAFE_BUILTINS` is treated as `allow`
   without a pattern lookup.
3. Add test cases in `tests/` for `trap`, `shift`, `local`, `eval` (should
   still ask), and `exec` (should still ask).
4. Remove the individual `Bash(set:*)`, `Bash(export:*)`, etc. pattern entries
   that are now redundant — or leave them for the transition period and note
   they can be pruned later.

## Do not

- Widen `Bash(crontab:*)` → `Bash(crontab -l:*)` (already handled separately)
- Add `Bash(trap:*)` or `Bash(eval:*)` to the allowlist as a workaround —
  that hides the bug and grants more than the structural fix would.
