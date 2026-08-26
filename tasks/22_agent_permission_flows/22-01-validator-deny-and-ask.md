# 22-01 — Validator: deny denies, ask asks

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §1 finding 6, §2 D1/D2, §4 H1 ·
[state.md](./state.md) invariant 1

## Goal

Bring the hook's semantics in line with Claude Code's native evaluation order
(deny → ask → allow, verified against the docs 2026-08-26):

1. A sub-command matching a **deny** pattern hard-denies the whole command —
   no prompt, no PermissionRequest, reason names the match.
2. A sub-command matching an **ask** pattern forces a prompt even when every
   sub-command is also allowlisted. `permissions.ask` is currently ignored by
   the entire stack.

This task defines the tier vocabulary 22-03's decide tool enforces: after it,
"matches an ask pattern" is the machine-readable marker for *human-only*.

## Scope

### 1. `SettingsLoader` — load and merge `permissions.ask`

`.claude/hooks/settings_loader.py`. Three touch points, all symmetric with the
existing allow/deny handling:

- `_normalize_to_modern_format` (currently `:102-137`): carry
  `permissions.ask` through. There is **no legacy-format equivalent** — do not
  invent one; legacy files simply contribute an empty ask list.
- `_merge_settings` (`:139-171`): union base + override ask lists, deduped
  ordered, same as allow/deny.
- The self-test `__main__` block: print the ask count alongside allow/deny.

### 2. Validator — check ask between deny and allow

`.claude/hooks/pretool_hook.py`, `BashPermissionValidator`:

- `__init__` (`:413-428`): load `ask_patterns` next to allow/deny.
- `_check_single_command` (`:1088`): match the same candidate variants
  (basename, builtin-alias) against ask patterns via the existing
  `_matches_pattern`, and return `matched_ask_patterns` + an `asked` flag next
  to `allowed`/`denied`. The no-op/local-function/workspace-binary early
  returns stay as they are — a command that runs nothing cannot be ask-listed
  into a prompt (add `'asked': False` to those returns).
- The decision block (`:521-551`) becomes, in precedence order:

  1. `any_denied` → **`decision = 'deny'`**, reason
     `"Matches a denied pattern: " + _format_command_list(denied_cmds)`.
     This is the D1 flip — today this branch returns `'ask'` with the comment
     "let PermissionRequest handle it or user decide" (`:525-529`). Delete
     that comment; it is the old policy.
  2. `any_asked` → `decision = 'ask'`, reason
     `"Matches an ask pattern: " + _format_command_list(asked_cmds)`.
     **The reason prefix is load-bearing:** 22-03 re-classifies at decide time
     with this same validator, and 22-06 verifies the string reaches Telegram.
     Keep it stable.
  3. disallowed redirect targets → `'ask'`, unchanged reason (`:531-534`).
  4. all allowed → `'allow'`; else → `'ask'` "Not in allowlist …" — both
     unchanged.

  Ask must outrank allow (a fully-allowlisted command with one ask-matched
  sub-command prompts) and lose to deny. That matches native order.

### 3. Hook output — emit the deny decision

`main()` (`:1371-1398`) currently emits only `allow`/`ask`. Add the deny arm:

```python
elif result['decision'] == 'deny':
    output = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': result['reason'],
        }
    }
    print(json.dumps(output))
    sys.exit(0)
```

`permissionDecisionReason` flows back to the model — this is H1's mitigation,
so the reason must name the matched sub-command(s), which
`_format_command_list` already does. `log_manual_confirmation` fires on every
non-allow decision (`:1367-1368`) and needs no change: hard denies keep landing
in `bash_manual_confirm.log` with `decision: "deny"`, which is what the daily
reviewer (22-05) watches for false positives.

The replay path (`replay_from_log`, `:320`) prints decisions for testing —
add the deny arm there too so replayed logs show the new mapping.

### 3b. Installer merge — `permissions.ask` must propagate

`install-claude-config.sh` Step 5 (`:640-651`) extracts **only** allow/deny
from the project config and replaces the global `permissions` object wholesale
with `{allow, deny}`. Left as is, this breaks D2 at user scope twice over: a
repo-level `permissions.ask` never reaches the global file, and any `ask` key
already in the global file is silently **erased on every install**. Extract
`ASK_TOOLS=$(jq '.permissions.ask // []' ...)` beside the other two and carry
`ask: $ask` in the merged object (no legacy-format fallback — legacy has no
ask equivalent, same as §1).

### 4. Downstream string check

`_unallowlisted_bash_parts` / `_format_non_whitelisted`
(`telegram_permission_router.py` / `permission_request_hook.py:423`) render
"matches a denied pattern" into auto-deny notes. After D1, deny matches no
longer reach PermissionRequest, so that branch goes quiet naturally — **do not
remove it** (settings can change between request creation and the sweep), but
extend whatever helper enumerates non-allowlisted parts to also name
ask-matched parts, so the auto-deny note and the Telegram body can say *why*
this request needed a human.

One knock-on to record, not to fix: the Telegram **Whitelist** button on an
ask-matched request writes an *allow* pattern, and ask outranks allow — so the
same command asks again next time. That is the semantics of ask working as
intended (a one-time allow plus a pattern that never silences the prompt);
accepted for v1. If it confuses in practice, the router can suppress the
button when the reason carries the ask prefix — a later one-liner, not this
task.

## Testing

`tests/run_all_tests.py` drives the suites; the pretool/whitelist suites live
under `tests/` (grep for the existing `validate_bash_command` cases). Re-measure
the baseline count first and report before/after.

New cases, minimum:

| # | Settings | Command | Expect |
|---|---|---|---|
| 1 | deny `Bash(curl:*)` | `echo hi && curl http://x` | decision `deny`, reason names `curl http://x` |
| 2 | deny `Bash(curl:*)`, allow `Bash(echo:*)` | `echo hi` | `allow` (flip breaks nothing adjacent) |
| 3 | ask `Bash(git push:*)`, allow `Bash(git:*)` | `git push origin main` | `ask`, reason prefix `Matches an ask pattern:` |
| 4 | ask `Bash(git push:*)` | `git status` | not asked (word-boundary rules hold for ask too) |
| 5 | deny + ask both match | — | `deny` wins |
| 6 | ask in workspace file, allow in global | — | merged: ask honored (loader union) |
| 7 | legacy-format file present | — | ask list empty, no crash |
| 8 | hook-level: deny decision emits `permissionDecision: "deny"` with reason | — | JSON shape above |

Existing whitelist/pretool tests that assert the old deny→ask mapping must be
updated to the new expectation — count and name them in the report.

## Done criteria

1. `python3 -m py_compile` clean on both files; `python3 tests/run_all_tests.py`
   green with the cases above; before/after counts reported.
2. Manual, reported with literal output: with a scratch workspace whose
   `.claude/settings.json` denies `Bash(curl:*)` and asks `Bash(git push:*)`,
   `echo '{"tool_name":"Bash","tool_input":{"command":"true && curl http://x"},"cwd":"<ws>"}' | python3 .claude/hooks/pretool_hook.py`
   prints the deny JSON; the `git push` equivalent prints the ask JSON with the
   ask-pattern reason.
3. Installer check (§3b): with a scratch `$GLOBAL_CONFIG` containing a stale
   `ask` key and a project config carrying a new one, the Step 5 jq produces
   the project's ask list — show the jq output, no full install needed.
4. **Not live until installed:** note that `./install-claude-config.sh` must
   re-run for the hook copy under `~/.claude/hooks/` to change (the standing
   reinstall rule). Run it only if the epic manager asks.
5. No edits outside `settings_loader.py`, `pretool_hook.py`, the helper named
   in §4, `install-claude-config.sh` Step 5, and tests.
