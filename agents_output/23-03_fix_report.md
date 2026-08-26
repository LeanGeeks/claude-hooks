# Fix Report — 23-03 Queue-file engine (LOW issues)

## Summary

All three LOW issues from the review are closed. Three new tests were added (+3 over the
1117 baseline). No logic was reverted or re-implemented. No commit was created.

---

## Issue 1 — `_has_answer_marker` docstring (documentation only, no behaviour change)

**File:** `.claude/hooks/questions_store.py`, function `_has_answer_marker` (around line 1382
pre-fix; shifts slightly after Issues 2 and 3).

Added a **Precondition** paragraph to the existing docstring explaining:
- The function scans the whole target file, so correctness requires `message_id` to be
  unique per qid *within the file*.
- The relay architecture guarantees this: one relay message carries exactly one qid, so no
  two entries in the same queue file can share a `message_id`.
- 23-05 additionally narrows the search to a single `rel_path` from the index, further
  reducing (the already-impossible) window.

No code change. The precondition was always true; it is now explicit.

---

## Issue 2 — Narrowed per-line guard to bracketed status tokens only

**File:** `.claude/hooks/questions_store.py`, lines 572–591 (module-level regex + `_needs_guard`).

### What changed

- **Removed** `_STATUS_LEAD_RE = re.compile(r"^[\[\*_>\-\s]*([A-Za-z0-9_]+)")` (matched
  both `[open]…` and `Open the file…` — over-broad).
- **Added** `_STATUS_BRACKET_RE = re.compile(r"^\[([A-Za-z0-9_]+)")` — matches only lines
  whose stripped content begins with `[word`, the exact shape rule 3 parses.
- Updated the one call site in `_needs_guard` to use `_STATUS_BRACKET_RE`.

### What this means for callers

| Input stripped content | Before | After |
|---|---|---|
| `[open] see below` | `\[open] see below` (guarded) | `\[open] see below` (guarded) ✓ |
| `[OPEN]` | guarded | guarded ✓ |
| `[resolved 2026-08-13] …` | guarded | guarded ✓ |
| `Open the file by clicking…` | `\Open the file…` (broken) | `Open the file…` (clean) ✓ |
| `Resolved: proceed` | `\Resolved: proceed` (broken) | `Resolved: proceed` (clean) ✓ |
| `Done it.` (when `done` in status set) | `\Done it.` (broken) | `Done it.` (clean) ✓ |

All other guarded prefixes (`#`, `<!--`, `**`, fence runs, setext rules) are unchanged.

### Tests updated

- `test_no_produced_line_forges_structure` — narrowed the "forged status token" assertion
  from `stripped.lower().lstrip("[*_ ").startswith(token)` to
  `stripped.lower().startswith(f"[{token}")`, which is the actual risk shape (rule 3 reads
  bracketed tokens, not bare words).
- `test_every_format_override` — updated the `assertIn("    \\Done it.", text)` assertion
  to `assertIn("    Done it.", text)` because `Done it.` is a bare word, not a bracketed
  token, and must no longer receive a backslash.

### New tests added (inside `TestAnswerEscaping`)

- `test_bare_status_word_is_not_backslash_guarded` — asserts that `"Open the file by
  clicking the button."` and `"Resolved: proceed with the JWT approach."` produce lines
  with four-space indent only (no backslash).
- `test_bracketed_status_token_is_still_guarded` — asserts that `"[open] see below"`,
  `"[OPEN] caps variant"`, and `"[resolved 2026-08-13] free-text suffix"` all produce
  lines with `\\[` prefix, confirming the guard still fires for the bracketed shape.

### Hostile-answer property test (`test_parse_resolves_identically_after_a_hostile_answer`)

Confirmed still passes: `HOSTILE_ANSWER` contains `resolved: a bare status word`, which
after the fix receives no backslash but is still 4-space indented. `parse_entries` never
reads indented lines as headings (requires `^ {0,3}#…`), so re-parsing yields the same
two entries, same ids, same order, no new entry. ✓

---

## Issue 3 — Section-aware git-config parse in `_is_bare`

**File:** `.claude/hooks/questions_store.py`, function `_is_bare` (around line 771 pre-fix).

### What changed

- Added module-level `_GIT_SECTION_RE = re.compile(r'^\[([A-Za-z0-9_-]+)(?:\s+"[^"]*")?\]')`
  to parse git section headers, including `[core]` and `[core "sub"]` subsections.
- Rewrote the loop body in `_is_bare` to:
  1. Track `in_core: bool` — True only while inside a `[core]` or `[core "sub"]` section.
  2. Skip `bare = …` lines outside `[core]`.
  3. Accumulate the last value within `[core]` (instead of returning on first match),
     implementing git's own last-value-wins rule.
- Added a docstring paragraph documenting these three properties.

Handles: `[core]`, `[core "sub"]`, inline whitespace, `#` and `;` comments, last-value-wins.

### New test added (inside `TestAnchorResolution`)

- `test_bare_in_non_core_section_is_not_treated_as_bare` — creates a primary checkout
  (with `[core]\n\tbare = false`), then appends `[branch "main"]\n\tbare = true` to the
  git config. Asserts that `resolve_anchor` returns the repo root with mode `"repo"` (not
  the worktree fall-through that would fire on a false-positive bare detection).

---

## Verification

```
python3 -m py_compile .claude/hooks/questions_store.py \
    tests/test_unit_questions_store.py
→ clean (no output)

python3 tests/run_all_tests.py
→ Ran 1120 tests in 32.4s   OK (skipped=1)
  (baseline 1117/1; +3 are this fix's: test_bare_status_word_is_not_backslash_guarded,
   test_bracketed_status_token_is_still_guarded, test_bare_in_non_core_section_is_not_treated_as_bare)

/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
→ 315 passed in 6.93s   (unchanged — no relay file touched)
```

---

## Diff reviewed (per implementer prompt Step 6)

Changes are confined to:
- `.claude/hooks/questions_store.py`: two module-level constants changed/added, one function
  body replaced (`_is_bare`), one docstring extended (`_has_answer_marker`). No duplicate
  definitions, no dead code, no `print` / TODO / stub.
- `tests/test_unit_questions_store.py`: two assertions updated (`test_no_produced_line_forges_structure`,
  `test_every_format_override`), three new test methods added. All new tests assert
  observable I/O, not implementation details.

---

## Blockers

None.
