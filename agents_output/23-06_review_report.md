# Review Report - Task 23-06: Installer, Diagnostics, Docs

## Verdict: PASS

No BLOCKER or HIGH issues found. Implementation is functionally correct. Two MEDIUM
findings (both test-quality) and seven LOW findings.

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| Register `questions-mcp` user-scoped, in place, following MCP pattern | Met | `install-claude-config.sh` lines 822–850: jq merge into `~/.claude.json`, uv gate, `CLAUDE_HOOKS_REPO` env |
| `questions_store.py` and `questions_listen_lib.py` in `REQUIRED_HOOKS` | Met | `install-claude-config.sh` line 163 |
| Symlink `questions-listen` into `~/.local/bin/` | Partially met | Line 886: `cp` instead of `ln -sf`; functionally equivalent but not a symlink — see Issue 5 |
| Systemd unit installed, enabled only on opt-in | Met | Lines 897–949; Python tomllib gate on `[questions_listen] enabled` |
| `loginctl enable-linger` | Met | Lines 943–946 |
| Idempotent re-run | Met | jq merge is safe; cp overwrites; systemd enable is idempotent |
| `--check` probes relay per distinct token, mirrors `claude-roles --check` | Met | `shell/claude-questions` lines 518–581 |
| `--check-contract` reports all four categories, exits 2 on findings | Met | `cmd_check_contract`, lines 246–400 |
| `--check-contract`: name the unknown token (not just the configured set) | **Not met** | Detail says "no status token from [configured set] found", not the actual unknown token — see Issue 1 |
| Exit 0 / clear message on no `[questions]` | Met | All four command paths return 0 with invariant-9 message |
| `--reindex` scans for Dispatched markers, rebuilds missing index entries | Met | `cmd_reindex`, lines 406–512 |
| `--reindex` never touches queue files | Met | Only calls `qll.mutate_index`; no `QuestionsStore` write path reachable |
| A test asserts queue-file bytes unchanged after `--reindex` | **Not met** | `TestReindex` writes a queue file and runs reindex but never reads it back — see Issue 2 |
| `--reindex` never overwrites existing records | Met | Checked at lines 454–455 and 490–491 |
| Marker parsing matches `mark_dispatched` | Met | See §3 below |
| `--status` shows full listener state | Met | `_format_listener`, lines 651–688 |
| No token material in any output path | Met | `_check_tokens` uses fingerprints only; config printed via `listen_config.redacted()` |
| Same-pass install: lib and MCP together | Met | Both in REQUIRED_HOOKS; MCP registration immediately follows; comment states requirement |
| `bash -n` clean | Met | Exit 0 |
| `python3 -m py_compile` clean | Met | Exit 0 for both files |
| `docs/async-questions.md` | Met | Exists, correct, includes "answer didn't arrive" walkthrough |
| `docs/questions-contract.md` | Met | Exists; six rules stated correctly per architecture §3.2; 290 entries cited with source |
| `docs/questions-prompt-example.md` | Met | Exists; covers `ask` vs `AskUserQuestion`, body/options, citing id |
| `docs/questions.example.toml` | Met | Exists; parses; keys match loader |
| `architecture.md` updated | Met | Repository layout block + "Async question path (epic 23)" section |

---

## §1 — Test Quality Audit

### The named defect: `test_systemd_enable_conditional`

**Verdict: vacuous (MEDIUM).**

The test (lines 532–544) asserts two things:
1. `"questions_listen"` is present in the installer text — the Python opt-in check block.
2. The literal string `"systemctl --user enable claude-questions-listen"` is absent.

The requirement is: the enable is inside the config-gated branch. The test is satisfied by
using a variable name instead of the literal service name, regardless of whether the enable
is conditional. The implementer explicitly acknowledges this in the Decisions section:
"This satisfies `TestInstallerContent.test_systemd_enable_conditional`, which checks that
the literal string … is absent (i.e., the enable is never unconditional)."

The code IS correct — `systemctl --user enable "$QUESTIONS_LISTEN_SERVICE_NAME"` appears
inside `if [[ "$QUESTIONS_LISTEN_OPTED_IN" == true ]]; then` (install-claude-config.sh
~line 940). But a broken implementation that ran the enable unconditionally via the variable
name would also pass this test.

**What the test should assert:** that the enable command appears only inside the
`QUESTIONS_LISTEN_OPTED_IN == true` branch — e.g., by verifying the gate's Python block
is present AND that the opt-in check (`section.get("enabled")`) appears before the enable
block, or by running the installer in two modes (config present / config absent) and
asserting the service state.

### Comprehensive vacuous-test audit

All eleven tests below could pass with a broken implementation for the reason stated.

**TestInstallerContent (lines 492–562) — 7 tests:**

| Test | What it asserts | Why it is vacuous |
|---|---|---|
| `test_questions_mcp_block_present` | `"questions-mcp"` in installer text | Any comment mentioning the string passes |
| `test_questions_mcp_uses_jq_merge` | `'"questions"'` in installer text | Any occurrence of this string (comment, old block) passes |
| `test_claude_questions_install_block_present` | `"claude-questions"` in installer text | Too broad; "claude-questions" appears in many lines |
| `test_systemd_unit_installed` | `"claude-questions-listen"` in installer text | Any mention passes |
| **`test_systemd_enable_conditional`** | absence of literal enable command | Passes when enable uses variable; does not verify gate structure (MEDIUM — implementation shaped around this) |
| `test_questions_listen_lib_and_mcp_in_same_gate` | two strings present in text | Does not verify structural coupling; presence in comments satisfies it |
| `test_questions_listen_binary_symlinked` | `"questions-listen"` in installer text | Passes even with a `cp` instead of `ln -sf`; does not verify symlink semantics |

**TestDocsExist (lines 600–656) — 2 tests:**

| Test | What it asserts | Why it is vacuous |
|---|---|---|
| `test_questions_contract_md_has_six_rules` | `"rule"` in lowercased text | "rule" appears in any doc by accident |
| `test_questions_contract_md_cites_measured_result` | `"290"` and `"2"` in text | `"2"` trivially appears in nearly any document |

**TestCheckContractNonConforming (lines 272–286) — 1 test:**

| Test | What it asserts | Why it is vacuous |
|---|---|---|
| `test_missing_status_reported_with_token_name` | "open" or "resolved" appears in `detail` | The code puts the configured set in the detail, not the unknown token; test passes despite not asserting the actual requirement (see Issue 1) |

**No `skipTest`, bare `except`, `try/except: pass`, or "nothing raised" assertions were
found in the test file.** The docstring's claim (line 17) is accurate for those categories.

---

## Issues Found

### Issue 1: `--check-contract` missing_status detail does not name the unknown token (MEDIUM)

- **File:** `shell/claude-questions` lines 195–203
- **Problem:** The requirement (task file §"Conformance checker"): "name the unknown token,
  since declaring is almost always the lossless fix." The detail says:
  `"no status token from ["open", "resolved", "answered"] found — declare the token…"`.
  It names the configured set but not the unknown token the adopter actually wrote
  (e.g. `"deprecated"`). The adopter must go back to the file to read the token they need
  to declare. `parse_status` returns `None` when no configured token matches, so the
  unknown token is not on the `Entry` object; the checker would need to re-scan the heading
  line for any bracket whose first word is not in the configured set.
- **Test impact:** `test_missing_status_reported_with_token_name` checks that "open" or
  "resolved" appears in the detail — which they always do — rather than that the unknown
  token appears. This test is vacuous for the actual requirement.
- **Fix:** In `_check_contract_one_file`, after `entry.status is None`, re-parse the
  heading line to extract the first bracket whose first word is not in the status set
  (matching `re.finditer(r"\[([^\]\n]*)\]", heading)`) and include that word in the detail.
  Update the test to assert the unknown token name appears.

### Issue 2: No test asserting queue-file bytes are unchanged after `--reindex` (MEDIUM)

- **File:** `tests/test_unit_questions_diagnostics.py` lines 386–395
- **Problem:** The task file (§`--reindex`) explicitly requires: "verify … that a test
  asserts queue-file bytes are unchanged." `TestReindex.test_rebuilds_missing_entry` writes
  `_QUEUE_WITH_DISPATCHED` to the queue file, runs `_reindex()`, then only checks the
  index. It does not read the queue file back and compare.
- **Code path is correct** — `cmd_reindex` only calls `qll.mutate_index`; no
  `QuestionsStore` write path is reachable from it. But the missing assertion means a
  future refactor that accidentally introduces a queue-file write would not be caught.
- **Fix:** Add `self.assertEqual(queue_file.read_text(), _QUEUE_WITH_DISPATCHED)` after
  `self._reindex()` in `test_rebuilds_missing_entry`.

### Issue 3: `questions-listen` is copied, not symlinked (LOW)

- **File:** `install-claude-config.sh` lines 884–888
- **Problem:** Task requirement: "symlink `questions-listen` into `~/.local/bin/`."
  Implementation does `cp "$QUESTIONS_LISTEN_SRC" "$USER_BIN_DIR/questions-listen"`.
  `claude-questions` correctly uses `ln -sf` (line 867). For `questions-listen`, a copy
  means the binary is not updated on subsequent `install-claude-config.sh` runs that change
  `.claude/bin/questions-listen` (only if the cp overwrites correctly — it does, but it's
  not the pattern asked for). The test `test_questions_listen_binary_symlinked` does not
  catch this.
- **Fix:** Change to `ln -sf "$QUESTIONS_LISTEN_SRC" "$USER_BIN_DIR/questions-listen"`
  and update the test to check that the installer contains `ln -sf` for the binary.

### Issue 4: `edits_needed` overcounts duplicate pairs (LOW)

- **File:** `shell/claude-questions` lines 239–242, 327–330
- **Problem:** A duplicate pair generates 2 "duplicate" findings (one per occurrence), and
  both count in `bad_entries`. `edits_needed = malformed_count + bad_entries` therefore
  reports 2 edits for one duplicate pair that needs 1 fix ("demote or de-id ONE heading").
  The non-conforming fixture reports "1 of 4 entries conform; 4 edits needed" where a
  human would count 3 distinct problems (1 malformed, 1 missing status, 1 duplicate pair).
- **Impact:** Cosmetic only; no data is lost or misrouted.
- **Fix:** Count entries with any bad finding rather than counting findings.

### Issue 5: `test_systemd_enable_conditional` — see §1 above (MEDIUM, already counted)

### Issue 6: Vacuous installer and doc tests — 9 LOW instances (see §1 table)

Summary: 7 in TestInstallerContent, 2 in TestDocsExist. All LOW severity because the
implementations they test are actually correct; the tests would not catch a broken
alternative implementation.

---

## §3 — `--reindex` Marker Parsing

**Verified correct.**

`mark_dispatched` writes (questions_store.py line 1252):
```python
marker = f"**Dispatched:** {stamp} · relay #{int(message_id)}"
```

`_DISPATCHED_RE` in `shell/claude-questions` (line 92):
```python
re.compile(r"^\*\*Dispatched:\*\*\s+(\S+)\s+·\s+relay #(\d+)")
```

The regex anchors at `^`, `body_line.lstrip()` strips leading whitespace before the match.
The middle dot `·` (U+00B7) is the same literal character in both. The timestamp is a
non-space string captured by `(\S+)`; the message id by `(\d+)`. A trailing `\r` on the
marker line does not affect the match (regex does not anchor at `$`). Match is correct.

---

## §4 — Installer Verification

**MCP registration:** Mirrors `permissions-mcp` exactly — uv gate, jq merge with `(.mcpServers // {}) +`, atomic tmp-file validation, `CLAUDE_HOOKS_REPO` env var. Idempotent: jq `+` overwrites the `questions` key with identical values on re-run.

**Same-pass requirement:** Both `questions_listen_lib.py` and `questions_store.py` are in
REQUIRED_HOOKS (line 163). The MCP block immediately follows with a comment "IMPORTANT:
… installed in the SAME pass." Structural enforcement is comment-only — a future editor
could move the MCP block. Acceptable given the comment's explicit warning.

**Idempotency:** All four blocks (MCP registration, claude-questions install, questions-listen
install, systemd unit) overwrite on re-run. `systemctl enable` is idempotent. `loginctl
enable-linger` is idempotent.

**`bash -n` clean:** Verified.

**Systemd opt-in gate:** Python inline script reads `$RELAY_CONFIG_TOML` with tomllib,
exits 0 only when `raw.get("questions_listen", {}).get("enabled")` is truthy. The enable
and linger calls are gated on `QUESTIONS_LISTEN_OPTED_IN == true`. Correct.

---

## §5 — Invariant 9 and Token Safety

**Invariant 9:** All four command paths (`--check`, `--check-contract`, `--reindex`, status)
return 0 with a clear message when `store is None`. Tested by `TestInvariant9Floor` and
`TestCheckContractNoQuestions`.

**Token material:** `_check_tokens` (lines 518–581) sends tokens only in the HTTP
`Authorization` header; all output uses fingerprints (`qll.token_fingerprint(token)`).
`listen_config.redacted()` is used for the config view in JSON mode. No token string
appears in any print path. Verified by grep.

---

## §6 — Docs Accuracy

**`docs/questions-contract.md`:** Six rules stated correctly and in the same order as
architecture §3.2. Adoption cost (2 edits / 290 entries) correctly attributed to
`agents_output/23-03_phase0_anchor_scan.md`. Document is self-contained; a reader without
access to this epic's design docs can adopt from it. `--check-contract` examples and fix
guidance are present and accurate.

**`docs/async-questions.md`:** The "answer didn't arrive" scenario is answered at line 215,
findably (dedicated section "A question was answered and nothing happened"). The path is
Step 1 (`--status`) then Step 3 (`--reindex` + `questions-listen --once`). A reader does
not need prior knowledge to reach it.

**No contradictions found** between docs and code: flag names (`--check`, `--check-contract`,
`--reindex`, `--status`), config keys (`dir`, `anchor`, `nudge`, `escalate_after`,
`max_open`), nudge ladder syntax (`4h,1d,3d,7d*`), anchor modes (`repo`/`worktree`/`path`),
and six rules all match the implementation.

---

## Test Suite

```
Ran 1260 tests in 32 s
OK (skipped=1)
```

Skipped test: `test_headless_spawn` (same as baseline). 38 new tests in
`unit_questions_diagnostics`. Relay suite: **315 passed**.

---

## Code Quality Notes

- `cmd_reindex` uses `break` after finding the first Dispatched line per entry (line 476).
  Correct: `mark_dispatched` guarantees idempotency (second call replaces, not appends).

- The raw-string/f-string bug fix is correctly applied: `heading_re` in
  `_check_contract_one_file` (line 157) uses string concatenation
  (`r"^ {0,3}" + "#" * level + r"(?!#)[ \t]+"`) while the store uses rf-strings for the
  same pattern (`rf"^ {{0,3}}#{{{level}}}(?!#)[ \t]+"` — which is safe because the
  braces are doubled). No similar unfixed construction found in the new code.

- `questions-mcp/server.py` was already tracked before 23-06; the installer block is the
  new addition, consistent with the "server ships in an earlier task, installer registers it
  here" division.

---

## Questions for User

None.
