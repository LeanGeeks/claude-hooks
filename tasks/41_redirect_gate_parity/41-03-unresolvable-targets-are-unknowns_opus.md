# Task 41-03 — An unresolvable write target is an unknown, not a risk

**Status:** todo · **Depends on:** 41-01, 41-02 · **Implementer:** opus
(gate + router + permissions MCP tiering; amends an epic 40 invariant)
**Read first:** [brd.md](./brd.md) §1.1, §1.2 (variable row), D3, D4, D6 ·
[state.md](./state.md) invariants 2, 3, 5 · epic 40 state.md invariant 4 and
brd §4.3 · `build_pretool_output` (`pretool_hook.py:452-500`) · 41-01 and 41-02
reports

Runs after 41-02 because the split below asks "is the literal anchor inside the
allowed roots", and 41-02 changes those roots.

## Decision (operator, 2026-10-20): option (a)

181 of the 365 refused targets in the brd window are unresolvable with no
literal anchor (`> "$f.out"` in a loop, `x=$(mktemp); > $x`, `> "$S/.env"`).
They become **unknowns**: `ask_kind: 'unknown'`, so `build_pretool_output`
prints nothing in `DEFERRING_MODES` and `ask` everywhere else — exactly like an
unknown command. The same applies to command-substitution and brace-expansion
targets: after this task, `echo hi > $(echo ~/.bashrc)` in an auto-mode
session is decided by the harness's pipeline, not by this hook. In
`bypassPermissions` the cost is the audit row only — `permission_request_hook`
auto-allows every request there (`:1653-1692`), so the gate never blocked in
that mode (brd §1.1).

Recorded alternatives, not to be re-proposed without new evidence: (b) only
`$name` / `${name}` targets become unknowns, substitutions stay risk; (c) no
change.

## The split

Every write target falls in exactly one of three outcomes. Evaluate on the
target as 41-01 returns it, after `_expand_constants`; "resolves" means
`_resolve_target_path` produced a path (it applies `expanduser` and
`expandvars` with the hook's environment — so `$HOME/.bashrc` resolves).

| Outcome | Rule | `ask_kind` |
|---|---|---|
| **allowed** | resolves inside the allowed roots and is not protected (41-01 D); or unresolvable with a literal anchor inside the roots and no literal `..` (the lenient rule, 41-01 C) | — |
| **outside** | resolves outside the roots; **or** hits a protected path; **or** unresolvable with a non-empty literal anchor outside the roots (`/etc/$V`, `~/.bashrc$V`); **or** unresolvable with a literal `..` segment; **or** an unexpandable `~user` prefix | `risk` |
| **unresolved** | unresolvable **and** the literal anchor is empty (`$out`, `"$S/.env"` after quote removal, `$(…)`, `{a,b}`, the `TRUNCATED_SUBSTITUTION_TARGET` marker) | `unknown` |

A resolved `..` (`> ../index.html`) is judged on its realpath like any other
resolved target; the `..` rule applies to unresolvable targets only.

Put the outcome decision in one function next to `_is_redirect_target_allowed`
and have the validator call it, so the rule lives in one place.

## Decision precedence in `validate_bash_command` (`:709-757`)

1. any sub-command denied → `deny` (unchanged)
2. any `permissions.ask` match → `ask` / risk (unchanged)
3. any **outside** target → `ask` / risk — wins even when unresolved targets
   are also present
4. all sub-commands allowed, no outside target → `allow` if there are no
   unresolved targets, else `ask` / **unknown**
5. otherwise (unknown sub-commands) → `ask` / unknown (unchanged)

The `decision` vocabulary stays `allow` / `deny` / `ask` (epic 40 invariant 2).

## Result shape, reason, tiering

- `redirect_targets` keeps meaning "targets refused as escaping" — fill it
  from the **outside** outcome only.
- Add `unresolved_redirect_targets` (additive key, list). Keep both keys
  present on every result for a stable shape.
- **Reason string:** keep today's wording byte-identical when only the old
  outcomes are involved. For an unresolved-only ask use a reason that does not
  claim the write escapes, e.g. "Write target cannot be resolved — review: `…`".
  The confirm log's `reason` changes with it; 41-04's tally filters on
  `emitted`.
- **Permissions MCP tiering** (`permissions_mcp_lib.py:326-340`): a redirect
  ask is human-only today because its reason starts with
  `REDIRECT_REASON_PREFIX` (`:107`). An unresolved-target ask must stay
  **human-only** too — an agent must not be able to approve a write whose
  destination nobody can read. Tier off the structured keys
  (`redirect_targets` or `unresolved_redirect_targets` non-empty → human-only),
  keeping the prefix check as a fallback for older results. Test both.

## The card (`telegram_permission_router.py`)

- `BashAnnotations` (`:379-422`): add `unresolved_redirect_targets`, normalise
  it like `redirect_targets` in `__post_init__`, and count it in `empty`
  (`:413-421`) — otherwise an unresolved-only card shows no annotation.
  `_bash_annotations` (`:424-495`) copies it off the validator result.
- In a **deferring mode**, an unresolved-only breakdown must not claim the hook
  asked (epic 40 brd H4). The existing harness-flagged heading only exists
  inside `if ann.unknown:` (`:631-655`) and says "not on the allowlist either",
  which is false for an allowlisted command with an unresolved target. Add a
  branch for that case with its own wording (e.g. "🤖 The harness flagged this
  call — the session's mode resolved it"), and list the targets under a
  neutral heading such as "📝 Write targets the hook could not resolve".
  When the command also has unknown sub-commands, keep the existing heading
  and add the neutral 📝 section.
- In a **non-deferring mode**, a card whose targets are all in the old outcomes
  stays **byte-identical** (epic 40 brd §4.3); an unresolved target is listed
  under the neutral heading, not under "Writes outside the workspace".

## Docs and the amended invariant

- Amend epic 40 state.md invariant 4 in place to: *"A `permissions.ask` match
  and a write redirect that resolves — or whose literal anchor lies — outside
  the allowed roots, or hits a protected path, still emit `ask` in every mode.
  A write target the gate cannot resolve and that has no literal anchor is an
  unknown (epic 41, 41-03)."* Add a dated line to epic 40's Log saying so.
- `architecture.md:112`: the PreToolUse row's description of the risk gate.

## Tests

`build_pretool_output` level in `auto`, `bypassPermissions` and `default`, plus
validator level:

| Command | Outcome | auto / bypass | default |
|---|---|---|---|
| `for f in a b; do echo x > "$f.out"; done` | unresolved | silent | ask |
| `x=$(mktemp); echo y > $x` | unresolved | silent | ask |
| `echo hi > $(echo ~/.bashrc)` | unresolved | silent | ask |
| `echo y > "$S/.env"` | unresolved | silent | ask |
| `echo y > {/etc/x,}` | unresolved | silent | ask |
| `echo k >> $HOME/.bashrc`, `>> "${HOME}"/.claude/settings.json` | outside (resolves) | ask | ask |
| `echo k >> ~/.bashrc$V` | outside | ask | ask |
| `echo k >> /etc/$V` | outside | ask | ask |
| `echo k >> .$V/../../etc/x` | outside | ask | ask |
| `echo k >> ~/.bashrc` | outside | ask | ask |
| `echo x > .claude/settings.local.json` | outside (protected) | ask | ask |
| `echo a > "$f" ; echo b > /etc/x` | outside wins | ask | ask |
| `echo a > ./$V` | allowed | allow | allow |
| cwd = `<root>/sub`, `echo a > ../x` | allowed (resolved `..`) | allow | allow |
| unresolved target + a `permissions.ask` match | risk wins | ask | ask |
| unresolved target + a denied sub-command | deny | deny | deny |

Plus: the deferring-mode card for an allowlisted command with an unresolved
target uses the new heading and the neutral 📝 section; `BashAnnotations.empty`
is false for it; the default-mode card for an outside-only command is
byte-identical to today's; the permissions MCP tiers an unresolved-only row
human-only.

Mutation-prove: an outside target still asks in `bypassPermissions`; "outside
wins over unresolved"; the MCP human-only tier for an unresolved row.

## Done when

- Tables pass; suite green against the baseline, skips by name.
- `agents_output/41-03_implementation_report.md` lists, for every row of the
  first table, the hook's printed output per mode.
