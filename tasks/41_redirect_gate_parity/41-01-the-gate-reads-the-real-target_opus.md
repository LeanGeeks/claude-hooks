# Task 41-01 — The redirect gate reads the target bash will write to

**Status:** todo · **Depends on:** none · **Implementer:** opus (security-sensitive
parser + gate change; this parser's history is a run of subtle fail-opens,
epics 25–36)
**Read first:** [brd.md](./brd.md) §1.2 (paren row), §1.3 (all five groups),
D1–D4 · [state.md](./state.md) invariants 2, 3, 6 · the docstrings of
`_is_redirect_target_allowed` and `_literal_prefix_inside_allowed_roots`
(`pretool_hook.py:912-972`) · the tokenizer around `$(`/backtick/process
substitution (`bash_command_parser.py:851-900`, `:1296-1340`)

## Goal

Every write target the gate checks must be either the path bash will write to,
or reported unresolvable — never shorter, never different. And the gate must
not let a redirect rewrite the hook's own settings. Five defect groups (brd
§1.3), every row of which returns `allow` today.

## Scope

### A. Find the whole target word (`_scan_write_targets`, `bash_command_parser.py:2051-2080`)

Tokens carry only a *start* offset, a `CMD_SUBST` value is the substitution's
inner text, and a quoted `"$(…)"` emits its `CMD_SUBST` (offset 5) *before*
the quoted `WORD` (offset 4) — so do not reassemble from tokens. Instead, take
the redirect operand's start offset and **scan the source string to the end of
the shell word**: stop at unquoted whitespace or an unquoted operator
character, keeping `'…'`, `"…"`, `$(…)`, `` `…` ``, `${…}` and `$((…))`
balanced. That span is the target word.

- **Trailing `)`** (D2): strip a `)` only when it is unbalanced within the word
  — i.e. it closes an enclosing subshell. `$(echo x)` keeps its paren;
  `(ls 2>/dev/null)` yields `/dev/null`.
- **Recurse into every substitution inside the word** — `$(…)`, backticks,
  `<(…)` / `>(…)` — for their own write redirects, then `continue`. Today the
  target branch `continue`s before the `CMD_SUBST` recursion (`:2063-2065`), so
  `echo hi > $(echo x > /etc/passwd)` is never scanned (brd §1.3 (ii)). Keep
  the `MAX_SUBSTITUTION_DEPTH` truncation marker behaviour.

### B. Quote removal, not quote stripping (brd §1.3 (iii))

Apply shell quote removal to the word (single quotes, double quotes, backslash
escapes, `$'…'` if present) while recording which characters were quoted. Then:

- the result is the literal target when the word has **no unquoted
  expansion**;
- an unquoted leading `~` / `~/` is tilde-expanded; a quoted one is literal;
- a `$` inside single quotes is literal; inside double quotes or unquoted it is
  a parameter expansion, left in the target for `_expand_constants` /
  `expandvars` as today (`"$HOME"/.bashrc` must reach the gate as
  `$HOME/.bashrc`, which then resolves to `~/.bashrc`);
- the word is **unresolvable** (returned in source form, so it starts with or
  contains a construct the gate refuses to vouch for) when it contains an
  unquoted command substitution, unquoted brace expansion (`{a,b}`, `{1..3}`),
  unquoted glob characters (`*`, `?`, `[`), or `~+` / `~-`.

Returning the target as `(text, offset)` stays the public shape
(`extract_write_redirect_targets`); if the gate needs the quoted/unresolvable
flags, add a sibling method rather than changing that signature — the router
and tests call it.

### C. The lenient fallback (`_literal_prefix_inside_allowed_roots`, `pretool_hook.py:941-972`)

Applies only to unresolvable targets (D3):

- refuse when **any** literal path segment of the target is `..` — not just
  the prefix's;
- expand an unquoted leading `~` / `~/` in the prefix before the containment
  check; refuse when the prefix starts with `~` and stays unexpanded
  (`~nosuchuser/…`);
- the docstring's promise does not change; make the code meet it.

A target that fully resolves is still judged on its realpath; `..` there is
fine (`> ../index.html` from a subdirectory resolves inside the project after
41-02).

### D. Protected paths (D4, brd §1.3 (v))

In `_is_redirect_target_allowed`, before the root checks: a target resolving
to `<root>/.claude/settings.json`, `<root>/.claude/settings.local.json`, or
under `<root>/.claude/hooks/` for any allowed root, or anywhere under
`~/.claude/` (41-02 later carves out the session's own memory dir), is refused.
For an unresolvable target, refuse when its literal prefix already points into
one of these. This makes `cat > .claude/hooks/x.py <<EOF` prompt in this repo
too — accepted; the Edit/Write tools are unaffected.

### Not in this task

Classifying refused targets into risk / unknown is 41-03. Here every refused
target stays `decision: 'ask'`, `ask_kind: 'risk'`, listed in
`redirect_targets`. Prefer fixing above the tokenizer; if the tokenizer must
change, say why in the report — it feeds command matching too.

## Tests (`tests/test_integration_pretool.py`, `TestRedirectTargets` `:1450`)

Parser level (`extract_write_redirect_targets`) and validator level (decision,
`ask_kind`, `redirect_targets`). `/tmp` is itself an allowed root, so a
temp-dir workspace masks any refusal whose target lands under `/tmp` — keep
every "should refuse" target outside `/tmp` (home via a patched `HOME`, `/etc`).

| Command | Expected decision |
|---|---|
| `echo hi > $(echo ~/.bashrc)` | ask / risk, target in source form |
| `ls >"$(echo /etc/x)"` | ask / risk |
| `echo hi > x$(echo y)/../../../etc/x` | ask / risk |
| `echo hi > x$(echo y)` | allow (anchored inside — accepted residual risk, as `x$V`) |
| `echo hi > /tmp/out_$(date +%s).txt` | allow (unchanged) |
| `echo hi > $(echo x > /etc/passwd)` | ask / risk (inner redirect scanned) |
| ``echo hi > `echo x > /etc/passwd` `` | ask / risk |
| `echo hi > "$(echo x > /etc/passwd)"` | ask / risk |
| `echo hi > >(cat > /etc/x)` | ask / risk |
| `echo x > "/etc"/x`, `> ""/etc/x`, `> '/etc/x'y`, `> \/etc/x` | ask / risk, target `/etc/x` (or `/etc/xy`) |
| `echo x > "$HOME"/.bashrc` | ask / risk |
| `echo x > ~/.bashrc"$V"` | ask / risk |
| `echo x > '~'/x` | per containment of literal `~/x` under the workspace (quoted tilde is literal) |
| `echo x > {/etc/x,}` | ask / risk (unresolvable) |
| `echo x > ~-/x`, `echo x > ~+/x` | ask / risk |
| `echo x > /etc/x*` | ask / risk |
| `echo k >> ~/.bashrc$V`, `>> ~/.ssh/authorized_keys$V` | ask / risk |
| `echo k >> .$V/../../etc/x` | ask / risk |
| `echo k >> ./$V` | allow (unchanged) |
| `echo x > .claude/settings.local.json`, `>> .claude/settings.json`, `> .claude/hooks/y` | ask / risk |
| cwd = `<ws>/sub`: `echo x > ../.claude/settings.local.json` | ask / risk |
| `echo x > ~/.claude/settings.json` with workspace = `$HOME` | ask / risk |
| `(ls 2>/dev/null) \| grep a`, `( (ls 2>/dev/null) )` | no redirect refusal |
| `(ls > /etc/x)` | ask / risk, target `/etc/x` |
| `echo x > $(echo a)` alone (inner text has no parens) | target keeps `$(echo a)` |
| `ls > "a)"` | target `a)` |
| `echo $(ls 2>/dev/null)` | unchanged: target `/dev/null`, allowed |
| substitution nested past `MAX_SUBSTITUTION_DEPTH` | unchanged (truncation marker, refused) |

Mutation-prove (state.md invariant 6) at least: `$(echo ~/.bashrc)`, the
inner-redirect row, `"/etc"/x`, `~/.bashrc$V`, `.$V/../…`, and
`.claude/settings.local.json`.

## Done when

- The table passes; full suite green against the baseline, skips accounted
  for by name.
- `agents_output/41-01_implementation_report.md` records: the baseline you
  measured on a clean tree **before** the first edit (the orchestrator copies
  it into state.md), and the before/after hook output for every brd §1.3
  command, run against the repo copy.
