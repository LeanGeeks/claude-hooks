# Task 36 — Two narrow parser cleanups carried out of task 32

**Status:** todo · **Type:** cleanup · **Created:** 2026-08-28 · **Rev:** 1
**Priority:** low — both are `ask`-direction only and neither can launder a deny
**Suggested worker:** one implement → review → fix loop; genuinely small
**Scope:** `.claude/hooks/bash_command_parser.py`, `tests/test_integration_pretool.py`,
this file. No behaviour change toward `allow` is permitted.
**Read first:** §1 and §2 · [task 32](./32_separator_suppression_tokens.md) §16
**Origin:** task 32's round-9 review (the round that passed and landed as
`3e7baa9`), which recorded both as carry-forward rather than blockers.

## 1. MEDIUM — `<<` inside arithmetic is a shift operator, and now costs a prompt

`bash_command_parser.py:1431` emits the declined-delimiter marker whenever
`_parse_heredoc_delim` declines — including when `_tokenize_with_quotes` is
re-tokenizing an **arithmetic interior** on behalf of `_scan_arith` (`:883`).
Inside `$(( ))` and `(( ))`, `<<` is a **left-shift operator** and bash opens no
heredoc at all.

Measured regression against both HEAD and round 8:

```
echo $((1 << $(echo 3)))              HEAD allow  R8 allow  now ask   bash prints 8
echo $(( 1 << `echo 3` ))             HEAD allow  R8 allow  now ask   bash prints 8
(( x = 1 << $(echo 2) )) ; echo $x    HEAD allow  R8 allow  now ask   bash prints 4
N=$(( 4096 << $(echo 1) )) ; echo $N  HEAD allow  R8 allow  now ask   bash prints 8192
```

9 of 12 realistic shift idioms flip to `ask`.

**Why it is low priority.** The direction is `ask`, never `allow`, so nothing is
bypassable. The underlying mis-modelling is **pre-existing** — HEAD already
mis-lexes `echo $(( 1 << 2 ))` as containing a heredoc operator — and task 32
only made the consequence visible as a prompt. It appears in **zero** of the
2221 real commands harvested from `~/.claude/bash_hook_debug.log` and the
permission store.

**Fix:** thread an "in arithmetic" flag so the emission is suppressed there, or
stop modelling `<<` as a heredoc operator inside arithmetic at all. Prefer
whichever is provable from the tokenizer's structure rather than a special case.

**Also correct, in the same edit:** task 32 §16.1's construction claim and the
emission-site comment at `bash_command_parser.py:1432` both say the flag is
`True` *only* for delimiters "bash DOES open a body for". That is over-broad —
in an arithmetic carrier the flag fires where bash opens nothing.

## 2. LOW — the `declined=False` boundary is narrower in code than in prose

`_parse_heredoc_delim` (`bash_command_parser.py:573`) returns `declined=True`
for `<<$'X` and `<<$"X` — an **unterminated** `$`-quote — because the `$'`/`$"`
branch returns before the unterminated-quote check at `:630`.

bash syntax-errors on these and runs **nothing** (`bash -n` →
``unexpected EOF while looking for matching `'``), so the marker costs a prompt
on text bash never executes. That is precisely the case §16.1 and the docstring
say the flag avoids: *"`False` for … an unterminated or line-spanning quote"*.

`test_declined_is_false_for_every_other_refusal` covers only plain `'` and `"`,
so **its corpus cannot express the counterexample** — the same "property test
that cannot fail" pattern task 32 exists to name, one level down. Fixing the
test matters as much as fixing the code.

**Fix:** a two-line termination check before the `$'`/`$"` branch returns, or
correct the prose to match the code. Either is acceptable; say which and why.

## 3. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`, with or
  without a pathspec. Mutation-test in `/tmp` copies. Create no files in the
  repo root.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`.
- **No cell may move toward `allow`.** Task 32 landed with 0 toward-allow moves
  across 22,652 standing cells, 1120 laundering cells, 2996 fuzz cells and 2221
  real commands. Re-run those corpora and hold that at 0 — it is the property
  the whole task was for.
- A shadowed command does **not** disarm a redirect: `cat > f <<'X'` truncates
  even with `cat` shadowed. Strip redirects or use an overlay; never replay
  logged traffic verbatim.
- Never use an oracle whose success token also appears in the input.
- Full suite green with counts. Baseline: **1525 ran, OK, 1 skipped**
  (`test_headless_spawn`).

## 4. Done criteria

1. §1 and §2 fixed, or explicitly resolved as documentation with the reasoning.
2. Tests that would have caught each — in particular a `declined` corpus that
   can express the §2 counterexample.
3. Both over-broad statements corrected in place (task 32 §16.1 and the
   emission-site comment).
4. Corpora re-run, 0 toward-allow confirmed; suite green with counts.
5. Implementation log here.
