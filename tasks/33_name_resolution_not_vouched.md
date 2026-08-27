# Task 33 — The validator vouches for a *name*, not for what the name runs (bug)

**Status:** todo · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 1
**Priority:** high — live on `main`, but see §2: it grants no privilege a bare
command does not already have, which is why it ranks below 32 and 30
**Suggested worker:** implement → review → fix loop, background agents
**Scope:** `.claude/hooks/pretool_hook.py`, `tests/`, this file. Likely also a
design decision recorded in `architecture.md`.
**Read first:** §1 · §3 (why the obvious fix is wrong) ·
[task 28](./28_safe_builtins_bypass.md) §5 (the rule that closed *text
substitution*; this is the sibling class it deliberately does not close)
**Origin:** task 28's round-5 reviewer, as INFO 2, while judging whether the
`$`-rule closed the trap class. Independently reproduced by the manager.

## 1. The defect

Task 28 closed *text substitution into a command*: any handler whose raw text
carries a `$` or a backtick now asks, because the validator cannot model the
environment a template expands in. It does **not** close the sibling class —
**what a name resolves to when it finally runs.**

Measured, scratch fixture confirmed via `SettingsLoader.load_all_settings()`
(`deny: ['Bash(curl:*)']`, `allow: ['Bash(echo:*)','Bash(printf:*)']`):

```
X=echo; f(){ $X http://e/x; }; trap f EXIT; X=curl   -> allow
X=echo; f(){ $X http://e/x; }; X=curl; f             -> allow
curl http://e/x                                      -> deny   (control)
```

`f` is a locally-defined function, so `local_function`
(`pretool_hook.py:1385`) treats the call as a no-op — correctly, the body is
validated separately. But the body is validated **at its definition offset**,
where `X=echo`. Bash expands `$X` in the body at **call** time, so `curl` runs.

Confirmed decisively against real bash by the reviewer:
`X=printf; f(){ $X > W; }; trap f EXIT; X=/usr/bin/id` writes `id` output to
`W`, not printf's.

The same shape covers every other name-resolution channel, all of which were
measured equal to their bare-command baselines: `source lib.sh`, `./x`,
`~/bin/evil`, PATH resolution, and cwd-dependent globs.

## 2. Severity, stated honestly

**This is not a `trap` escalation and not a task-28 regression.** The bare form
allows identically, and every measured channel gives the same verdict with and
without the trap. The validator has always vouched for names this way; task 28
simply made it visible by closing the louder class next door.

So the exposure is: *an agent that can define a function (or write a file, or
set PATH) can arrange for a denied command to run under an allowed name.* An
agent can trivially do all three. It ranks below task 32 (which hides commands
outright with no setup) and task 30 (which defeats `deny` on the direct path),
but it means epic 22 invariant 1 is not truly whole even after those land.

## 3. Why the obvious fix is wrong

"Re-validate the function body at the call offset" does not work: there may be
many call sites, the body may be redefined, and the binding may come from
outside the command entirely (the environment, a sourced file, an earlier tool
call). The validator has no model of run-time state and should not grow a bad
one — that is exactly the mistake task 28 made four times before replacing
enumeration with a property.

Candidate directions, to be chosen in a Phase 0 rather than assumed:

1. **Follow task 28's precedent.** A function body containing a `$` that the
   validator cannot resolve at *every* reachable call site cannot be vouched
   for — ask. Provable from the text, no run-time model required.
2. **Narrower:** ask only when a name used in the body has any assignment
   anywhere in the command other than the one already used. Cheaper, but this
   is the enumeration road task 28 abandoned; if chosen, say why it will not
   fail the same way.
3. **Accept and document.** Record in `architecture.md` that the validator
   vouches for names, not for resolution, and that deny patterns are advisory
   against an adversarial author. Legitimate if the cost of (1) is too high —
   but it must be an explicit decision, not the current silence.

**Phase 0 is required**, with the false-ask cost of (1) measured against the
harvested corpus (`~/.claude/bash_hook_debug.log*`,
`~/.claude/permission_requests.jsonl`) before any code is written. Function
definitions are ordinary in real agent commands; a rule that prompts on all of
them is not shippable.

## 4. Tests

Watched to fail first, output quoted. At minimum §1's three rows, the
`trap`-free variant, and the reviewer's real-bash witness shape. Plus a
regression floor: ordinary function definitions with literal bodies
(`cleanup(){ rm -f /tmp/x; }; cleanup`) keep whatever verdict Phase 0 chooses,
and the 240-case oracle stays green.

## 5. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`.
  Mutation-test in `/tmp` copies.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`.
- Differential sweep over the harvested corpus. Per task 31's lesson, the claim
  to prove is **"every move toward allow is bash-faithful"**, verified against
  real bash — not "nothing moved toward allow".
- Ground-truth with `set -T` + a `DEBUG` trap reading `$BASH_COMMAND`; inert
  payloads only (`_assert_probe_safe` refuses non-inert commands).

## 6. Done criteria

1. Phase 0 recorded in this file: direction chosen, with the measured false-ask
   cost that justified it.
2. Implementation, or an explicit accept-and-document outcome in
   `architecture.md`.
3. §4's tests; full suite green with counts. Baseline at filing: **1393 ran,
   OK, 1 skipped**.
4. Implementation log here.
5. A sentence in `tasks/22_agent_permission_flows/state.md`'s Log stating
   whether invariant 1 is finally whole.

## 7. Related, pre-existing, cheap to fold in

`trap(){ :; }; trap 'curl x' EXIT` decides **deny**, because the
`SAFE_BUILTINS` head-token check fires before the function-def no-op check —
bash would call the user's no-op function. Contrived, errs toward blocking,
found by task 28's round-5 review. Fix it here or record it as accepted.
