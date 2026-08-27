# Task 35 — Invert the parser's default: what it cannot decompose must ask, not allow

**Status:** todo · **Type:** architecture · **Created:** 2026-08-27 · **Rev:** 1
**Priority:** high — this is the task that ends the series; the individual doors
are symptoms
**Suggested worker:** **Phase 0 first, by the manager or a strong model.** Do not
start implementing from this document.
**Read first:** §1 (the pattern) · §2 (the rule) · §3 (Phase 0 — mandatory) ·
[task 28](./28_safe_builtins_bypass.md) §5 (the same move, one level down) ·
[task 32](./32_separator_suppression_tokens.md) §§8–11 (six doors, in order)
**Origin:** the manager and operator, 2026-08-27, after task 32's sixth
consecutive review round found a seventh bypass of the same shape.

## 1. The pattern, stated once

Six review rounds across tasks 31 and 32 found six distinct ways to hide a
command from the validator:

| # | Door | Found by |
|---|---|---|
| 1 | `&` is not a separator | task 31 filing |
| 2 | an unmatched `[[` suppresses separators to end of string | task 31 review |
| 3 | a mid-word `#` eats the rest of the line | task 31 review |
| 4 | a `KEY=VALUE` prefix (and any keyword) reopens command position | task 32 review 1 |
| 5 | constructs that advance without flushing a word (`2>&1`, `$(…)`, `> tgt`) | task 32 review 2 |
| 6 | operators the table did not know (`>|`, `1>&`) | task 32 review 3 |
| 7 | heredocs the tokenizer cannot terminate; `esac<x` in pattern mode | task 32 review 4 |

Each fix was correct. Each was followed by another door. Two of them were
*introduced* by a previous round's fix. The reviewers' verdicts moved from
"I could not find another" to "the axis has been moved, not closed."

**They are all the same defect.** `.claude/hooks/bash_command_parser.py` models
bash's lexer, `pretool_hook.py` decides from that model, and wherever the model
is wrong the decision defaults to **allow**. A permission gate whose failure
mode is "permit" converts every modelling gap into a silent bypass. The gaps are
not going to run out: bash's grammar is large, the parser is ~2100 lines of
hand-written approximation, and `main()` even turns a *crash* into a soft allow
(`tasks/34_nul_byte_fail_open.md`).

Note the two shipped precedents for the fix direction, both in this repo:

- **Task 28** replaced four rounds of "enumerate every way bash can rebind a
  name" with one property provable from the grammar (`$` or backtick in a raw
  trap handler → ask). The series ended.
- **Task 32 round 3** replaced a hand-mutated `at_cmd_start` flag with a
  derivation over the emitted token stream. That was right, and it is why
  doors 6 and 7 were findable at all — but it left the *default* untouched.

This task applies the same move to the default itself.

## 2. The rule

> Where the parser cannot confidently decompose a command into sub-commands, the
> validator returns **ask**, never allow.

"Cannot confidently decompose" must be an explicit, positive signal the parser
raises — not an exception, not a sentinel, not silence. Candidate triggers,
every one of them a live bypass today or recently:

- an operator or metacharacter not in `BASH_OPERATOR_TOKENS`;
- a heredoc whose terminator the tokenizer does not find, including `<<-`'s
  tab-stripped form and a quoted delimiter containing non-word characters;
- unbalanced quotes, or quote state still open at end of input;
- nesting past the recursion bound in `_scan_arith` / `_scan_paren_subst` /
  `_scan_backtick`;
- a `case` construct whose pattern/body state does not close;
- any internal invariant violation that today raises and is swallowed.

Two hard requirements:

1. **Ask, not deny.** Epic 22 H1: a false deny hard-blocks with no human
   rescue. Ask is the safe direction and preserves the operator's ability to
   approve. (Note `pretool_hook.py`'s exit-0-with-no-output path hands back to
   the native prompt, which is *also* not a hard block — see task 34 §3 before
   assuming ask is the only option.)
2. **The signal must be legible.** The prompt has to say *why* — "this command
   contains a construct the validator cannot parse" — or the operator learns
   nothing and approves reflexively.

## 3. Phase 0 — mandatory, measure before designing

**Do not write the rule before knowing what it costs.** Heredocs and unusual
constructs appear in real traffic; a gate that prompts on all of them will be
switched off, which is worse than the bug.

Deliverables, in this order:

1. **Enumerate the confidence signals.** Walk the tokenizer's paths and list
   every point where it currently proceeds without knowing it is right. Task 32
   round 3's `_RESERVED_WORD_POSITION_PATHS` (58 rows, derived from the
   tokenizer's own branches) is the model for how to do this, and task 32 round
   4's operator sweep is the model for how to check it against bash rather than
   against ourselves.
2. **Measure the false-ask cost** against the harvested corpus —
   `~/.claude/bash_hook_debug.log` plus all rotated `.gz` plus
   `~/.claude/permission_requests.jsonl`, ~2100 distinct real commands. Report:
   how many would newly ask, broken down by trigger, with the ten most common
   shapes quoted verbatim. **This number decides the design.**
3. **Judge each trigger separately.** Some will be free (nobody writes
   `esac<x`). Some will be expensive (heredocs are ordinary). A trigger whose
   cost is high may deserve real parsing work instead of a prompt — that is a
   legitimate outcome for that trigger, decided on evidence.
4. **Recommend a design** with the measurement attached, including what happens
   to the `SAFE_BUILTINS` / `workspace_binary` / `local_function` shortcuts,
   which currently return allow *above* the tier check
   ([task 30](./30_workspace_shortcuts_tier_bypass.md)).

## 4. Relationship to the open tasks

- **[30](./30_workspace_shortcuts_tier_bypass.md)** (`workspace_binary` etc.
  outrank deny/ask) and **[33](./33_name_resolution_not_vouched.md)** (the
  validator vouches for a name, not for what it runs) are the *same* allow-by-
  default defect at the decision layer rather than the lexing layer. This task
  may subsume or reshape both — Phase 0 should say which, and the operator has
  chosen to keep 30 and 33 sequenced ahead of it for now.
- **[34](./34_nul_byte_fail_open.md)** owns `main()`'s blanket
  `except Exception: sys.exit(0)`. That is the crash-shaped instance of this
  same default and should be decided consistently with whatever this task
  concludes. Coordinate; do not decide it twice.
- **[32](./32_separator_suppression_tokens.md)** must land first. Its operator
  table, token-stream derivation and real-bash sweeps are the foundation this
  builds on, and its `known_gap` column is the beginning of §3 item 1's list.

## 5. What would make this task succeed

Not "no more doors" — nobody can promise that. The success condition is
**a door that opens a prompt instead of a hole**: after this lands, a construct
the parser does not understand must produce a visible ask, and a reviewer should
be able to demonstrate that by feeding the parser something deliberately
malformed and watching it prompt rather than allow.

The falsifiable acceptance test: take the six doors in §1's table, re-spell each
one in a form the *current* parser still cannot decompose, and confirm every one
asks.

## 6. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`, with or
  without a pathspec. Mutation-test in `/tmp` copies. Create no files in the
  repo root.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`.
- Full suite green with counts. Baseline when this was filed: **1460 ran, OK, 1
  skipped** (`test_headless_spawn`), pending task 32 round 5.
- Every claim about bash's behaviour measured against bash, with inert payloads
  only (`_assert_probe_safe` refuses non-inert commands).
- Per task 31 §8.9: prove **"every move toward allow is bash-faithful"**; never
  assert that none occurred.
