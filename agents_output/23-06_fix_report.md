# Fix Report — Task 23-06 (Fixer pass)

## Changes per issue

### Issue 1 (MEDIUM) — `--check-contract` must name the unknown status token

**File:** `shell/claude-questions`

Added `_find_unknown_bracket_token(heading_line, status_set)` helper (after line ~109).
It iterates `re.finditer(r"\[([^\]\n]*)\]", heading_line)` and returns the first word
inside any bracket that is not in the configured status set, or `None` if no bracket
exists in the heading.

In `_check_contract_one_file`, category-2 block: for each entry with `status is None`,
the helper is called on `lines[entry.start]`. The detail now reads:
- bracket found: `no status token from […] found — found "deprecated" — declare it in …`
- no bracket: `no status token from […] found — no bracket in heading — add a [status] bracket…`

**Test rewritten:** `test_missing_status_reported_with_token_name` now asserts that
`"deprecated"` (the fixture's unknown token) appears in the finding detail.

---

### Issue 2 (MEDIUM) — assert `--reindex` leaves queue files byte-identical

**File:** `tests/test_unit_questions_diagnostics.py`

Added to `TestReindex.test_rebuilds_missing_entry` after `_reindex()`:

```python
self.assertEqual(
    queue_file.read_bytes(),
    _QUEUE_WITH_DISPATCHED.encode(),
    "--reindex must not modify the queue file (byte-identical check)",
)
```

Asserts exact bytes (not stripped or normalized).

---

### Issue 3 (LOW) — `questions-listen` symlink vs copy

**File:** `install-claude-config.sh` (questions-listen block)

Changed:
```bash
cp "$QUESTIONS_LISTEN_SRC" "$USER_BIN_DIR/questions-listen"
chmod +x "$USER_BIN_DIR/questions-listen"
```
to:
```bash
ln -sf "$QUESTIONS_LISTEN_SRC" "$USER_BIN_DIR/questions-listen"
```
Matches the sibling `claude-questions` pattern (line 867) and the task requirement.

**Test rewritten:** `test_questions_listen_binary_symlinked` now finds the line that has
both `ln -sf` and `"$USER_BIN_DIR/questions-listen"` — fails if a `cp` is used.

---

### Issue 4 (LOW) — `edits_needed` overcounts duplicate pairs

**File:** `shell/claude-questions`

Updated `_check_contract_one_file` to return a 4-tuple:
`(findings, total_entries, entries_with_bad, edits_for_bad)`

- `entries_with_bad`: count of entries (by unique `lineno`) with any bad finding — drives
  `conforming = total_entries - entries_with_bad` (unchanged semantics).
- `edits_for_bad`: distinct edits = one per missing-status entry + one per **unique qid**
  appearing in duplicate findings. For a Q-004 duplicate pair, this gives 1 (not 2).

Updated `cmd_check_contract` to accumulate `edits_for_bad` separately and compute:
`edits_needed = malformed_count + edits_for_bad`

Non-conforming fixture now reports: `1 of 4 entries conform; 3 edit(s) needed`
(was `4 edits needed`).

---

### Issue 5 (MEDIUM/LOW) — `test_systemd_enable_conditional` rewritten

**File:** `tests/test_unit_questions_diagnostics.py`

The old test asserted that the literal string `"systemctl --user enable claude-questions-listen"`
is absent. The enable uses a variable, so any implementation (conditional or not) passes.

The new test extracts the Python opt-in snippet from the installer text (between
`import sys, tomllib` and the `PYEOF` heredoc delimiter), writes it to a temp file,
and runs it via subprocess with two TOML fixtures:

1. `[questions_listen] enabled = true` → must exit 0 (opted in)
2. `[relay] host = "example.com"` (no section) → must exit non-0 (not opted in)

This actually tests the gating logic rather than grepping for a string.

---

### Issues 6 and remaining vacuous tests (LOW)

All 11 audited tests addressed. See table below.

---

## Test-quality audit table

| Test | Disposition | Reason |
|---|---|---|
| `test_missing_status_reported_with_token_name` | **Rewritten** | Now asserts `"deprecated"` (the unknown token) appears in the detail; old test checked for `"open"` or `"resolved"` which always appeared in the configured-set listing |
| `test_questions_mcp_block_present` | **Rewritten** | Now checks for `.mcpServers = (.mcpServers // {}) + {"questions"` — proves both the idempotent merge form and the server name; old test checked `"questions-mcp"` which appears in comments |
| `test_questions_mcp_uses_jq_merge` | **Deleted** | The rewritten `test_questions_mcp_block_present` already asserts the merge form and the server key; this test became fully redundant |
| `test_claude_questions_install_block_present` | **Rewritten** | Now checks `ln -sf` and `"$USER_BIN_DIR/claude-questions"` together; old test checked the string `"claude-questions"` which appears in dozens of lines including comments |
| `test_systemd_unit_installed` | **Rewritten** | Now checks for `ExecStart=%h/.local/bin/questions-listen` — the line that determines what the unit runs; old test checked `"claude-questions-listen"` which appears in comments and variable names |
| `test_systemd_enable_conditional` | **Rewritten** | Now runs the installer's Python opt-in snippet with both config states (opted-in exits 0, absent exits non-0); old test checked that a literal string was absent, which any variable-based enable satisfies |
| `test_questions_listen_lib_and_mcp_in_same_gate` | **Rewritten** | Now finds the `REQUIRED_HOOKS=` definition line and asserts both filenames appear on it; old test checked for each string anywhere in the file (comments satisfy it) |
| `test_questions_listen_binary_symlinked` | **Rewritten** | Now finds a line with both `ln -sf` and `"$USER_BIN_DIR/questions-listen"`; old test checked `"questions-listen"` which any mention satisfies, including log lines with a `cp` |
| `test_questions_contract_md_has_six_rules` | **Rewritten** | Now asserts `### Rule 1` through `### Rule 6` headings are all present; old test checked `"rule"` in lowercased text which any word "rule" anywhere satisfies |
| `test_questions_contract_md_cites_measured_result` | **Rewritten** | Now checks for the phrase `"2 edits across 290"` specifically; old test checked `"2"` separately which trivially appears in any document |
| (total audited = 10; brief said 11 — `test_systemd_enable_conditional` is counted both as MEDIUM and in the 9-broad-substring-check tally; this is the same test, not a separate one) | | |

---

## Mutation proofs

### `test_systemd_enable_conditional`

Mutation: copied `install-claude-config.sh` to a scratch file, changed
`sys.exit(0 if section.get("enabled") else 1)` → `sys.exit(0)` (always opts in).

Result: running the test's logic against the mutant confirmed exit code 0 even with a
TOML that has no `[questions_listen]` section. The test asserts `returncode != 0`,
so it **would fail**. Mutant not committed; scratch copy discarded.

### `test_rebuilds_missing_entry` byte-equality assertion

Mutation: copied `shell/claude-questions` to a scratch file, inserted a loop inside
`cmd_reindex` that writes `qf.read_text() + "\n"` to every queue file just before
returning.

Result: `queue_file.read_bytes()` was 141 bytes (original 140). The assertion
`assertEqual(queue_file.read_bytes(), _QUEUE_WITH_DISPATCHED.encode())` **would fail**.
Mutant not committed; scratch copy discarded. No `git checkout` used.

---

## Verification

- `python3 -m py_compile shell/claude-questions` → OK
- `python3 -m py_compile tests/test_unit_questions_diagnostics.py` → OK
- `bash -n install-claude-config.sh` → OK
- `python3 tests/run_all_tests.py` → **Ran 1259 tests in 32.6s, OK (skipped=1)**
  - Count drop: 1260 → 1259 because `test_questions_mcp_uses_jq_merge` was deleted
    (its requirement is now fully covered by the rewritten `test_questions_mcp_block_present`).
  - Skip: `test_headless_spawn` (unchanged).
- Relay suite: `/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q` → **315 passed**

---

## Blockers

None.
