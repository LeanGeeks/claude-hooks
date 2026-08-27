# Task 30 — `workspace_binary` / `workspace_rm` / `local_function` also bypass deny and ask (bug)

**Status:** todo · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 1
**Priority:** **critical** — this one runs arbitrary code, not shell state
**Suggested worker:** implement → review → fix loop, background agents
**Scope:** `.claude/hooks/pretool_hook.py`, `tests/test_integration_pretool.py`,
this file. No settings edits, no installer change.
**Read first:** §1 (the measurements) · [task 28](./28_safe_builtins_bypass.md)
(same defect class, already fixed for `safe_builtin` — copy its shape) ·
`tasks/22_agent_permission_flows/state.md` invariant 1 and `brd.md` §2 D1/D2
**Origin:** found by the implementer of task 28 as residual risk 4, then
**independently verified by task 28's reviewer** with a fixture confirmed loaded
through `SettingsLoader.load_all_settings()`.

## 1. The defect

Task 28 fixed the `safe_builtin` shortcut so it can no longer skip the deny and
ask tiers. **Three sibling shortcuts in the same function still can.** All three
return `allowed: True` above `_tier_override_result`:

| Shortcut | Line |
|---|---|
| `local_function` | `pretool_hook.py:1385` |
| `workspace_binary` | `:1398` |
| `workspace_rm` | `:1411` |

**Line numbers refreshed 2026-08-27** after tasks 28 (`9a48e3e`) and 31
(`b2f9ee8`) landed; the `:1326/:1332/:1345` anchors in the original filing are
stale. Re-anchor by the `matched_allow_patterns` marker string, not by number.

Measured in a scratch workspace, fixture verified:

| settings | command | decides | should be |
|---|---|---|---|
| `deny: ['Bash(rm:*)']` | `rm ./file.txt` | **allow** | deny |
| `deny: ['Bash(rm:*)']` | `rm -rf ./file.txt` | **allow** | deny |
| `deny: ['Bash(./scripts/evil.sh:*)']` | `./scripts/evil.sh --now` | **allow** | deny |
| `deny: ['Bash(deploy:*)']` | `deploy(){ echo hi; }; deploy prod` | **allow** | deny |
| `ask: ['Bash(release:*)']` | `release(){ echo hi; }; release v1` | **allow** | ask |

Control, confirming the shortcut is what does it: `rm /etc/passwd` → `deny`
(outside the workspace, so `workspace_rm` never fires).

## 2. Why this is worse than task 28's defect

Task 28's payload was shell-state builtins — `unset`, `umask`, `shift`. This
one's payload is **arbitrary code**. `workspace_binary` means: an agent that can
write a file into the workspace can then execute it, and **no `deny` or `ask`
entry anywhere will stop it**. Writing a file into the workspace is ordinary,
unremarkable, allowlisted agent behaviour.

It also compounds with task 28's `trap` work: `trap "rm ./file.txt" EXIT` allows
even under `deny: ['Bash(rm:*)']`, because the handler's sub-command reaches
`workspace_rm` before the tier check. Task 28 deliberately did not touch this.

This is the same **epic 22 invariant 1** violation as task 28
(*"no path — hook, MCP, or reviewer — downgrades a deny match to a prompt"*),
on paths task 28 does not cover. Until this lands, that invariant is still false.

## 3. The fix

Apply task 28's §2.1 shape to all three: run the deny/ask tier check **before**
each shortcut returns `allowed: True`. Task 28 built `_tier_override_result` /
`_pattern_candidates` / `_match_deny_and_ask` for exactly this; **import and
reuse them — do not write a second matcher.** Two matchers that disagree are a
new bypass, which is the trap task 28's reviewer checked for specifically.

Watch for the string-mismatch bug task 28's reviewer found as its MEDIUM 1: the
tier check must be fed the **same reduced command** the normal pattern lookup
uses, or prefixes (`if`, `while`, `env`, `time`, `nohup`, `timeout 5`, …) will
evade the new gate exactly as they evaded that one.

Land task 28 first — this builds directly on its helpers.

## 4. Tests

In `tests/test_integration_pretool.py`, beside task 28's
`TestSafeBuiltinsTierOrdering`. Every one **watched to fail first**, quoted in
the report:

1. Each row of §1's table decides correctly after the fix.
2. Deny beats ask beats allow for a workspace binary, a workspace `rm` and a
   local function.
3. Prefixed forms are gated too — at minimum `if ./scripts/evil.sh`,
   `env ./scripts/evil.sh`, `time rm ./file.txt`.
4. **Regression floor:** with no deny/ask patterns present, all three shortcuts
   still allow exactly as they do today. This is the H2 failure mode — an
   over-tightened matcher that blocks ordinary workspace work is a worse
   outcome than the bug, and `workspace_binary` is on the hot path of every
   agent that runs a project script.
5. `rm` outside the workspace still denies via the normal path.

No `skipTest`, no bare `except`. Verify every scratch fixture actually loaded
through `SettingsLoader.load_all_settings()` before trusting a result.

## 5. Constraints

- **Shared checkout.** No repo-wide git operations — never `git checkout`,
  `stash`, `reset` or `restore`, with or without a pathspec. `cp` files aside
  for mutation tests, or work in a `/tmp` tree built with `git archive HEAD`.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`; say in
  the report which copy was tested.
- Full suite green, before/after counts reported.

## 6. Done criteria

1. All three shortcuts consult deny and ask before allowing.
2. §4's tests present and green, with the report naming which were watched to
   fail and what they printed.
3. Full suite green, counts reported.
4. This file updated with an implementation log.
5. A sentence in `tasks/22_agent_permission_flows/state.md`'s Log recording that
   invariant 1 is whole again — it has been false on these paths since the
   invariant was written.
