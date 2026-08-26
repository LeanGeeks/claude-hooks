# Review Report - 22-03 Permissions MCP: read + decide

## Verdict: PASS

No BLOCKER or HIGH issues found. All ten required cases are tested, all epic
invariants verified, and the test suite is green at 984 (942 + 42 new).

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| Server skeleton — uv single-file, `MCPServer("permissions")`, 4 tools | **met** | `permissions-mcp/server.py:1-5,37` |
| Caller identity resolved once at startup from process env | **met** | `server.py:40`, `lib.py:134-152` |
| Fail-closed on missing `CLAUDE_CODE_SESSION_ID` (read ok, decide refused) | **met** | `lib.py:130-132,701-707` |
| `list_permission_requests` with tier/decidable per row | **met** | `lib.py:484-507` |
| `get_permission_request` with full row + live classification | **met** | `lib.py:510-524` |
| `permission_history` joins store + bash_manual_confirm.log | **met** | `lib.py:588-641` |
| `decide_permission_request` — action ∈ {allow,deny,stop} | **met** | `lib.py:692-787` |
| Guard: refuse exactly `session == caller AND agent_id is None` | **met** | `lib.py:345-368` |
| Guard: same-session subagent row (agent_id set) is decidable | **met** | `lib.py:361` |
| Tier: one `classify()` called by both list and decide | **met** | `lib.py:227-337`; `lib.py:416,747` |
| Tier uses `BashPermissionValidator` re-run against row's cwd | **met** | `lib.py:176-186,282-337` |
| No regex/prose matching of stored reason string | **met** | `lib.py:90` only `REDIRECT_REASON_PREFIX` used on **fresh** run output |
| Tier: ask pattern → human-only | **met** | `lib.py:303-320` |
| Tier: redirect escape → human-only | **met** | `lib.py:321-323` |
| Tier: fresh deny → human-only with "predates a settings change" | **met** | `lib.py:304-311` |
| `AskUserQuestion` rows → listed as question, never decidable | **met** | `lib.py:240-245` |
| Non-Bash rows decidable unless ask pattern names the tool | **met** | `lib.py:258-277` |
| H4 lag note in `decide` tool description | **met** | `server.py:141-143` |
| H3 wording: "decision recorded", not "tool ran" | **met** | `lib.py:103-107` |
| `update_request_state` returns `None` → lost race, reported honestly | **met** | `lib.py:764-772` |
| `decision={"action": ...}`, `resolution_source=RESOLUTION_SOURCE_AGENT` (constant) | **met** | `lib.py:760-763`; import at `lib.py:63` |
| `actor_agent` non-empty on success | **met** | `lib.py:761` |
| reason required and reaches audit log | **met** | `lib.py:717-722`; `lib.py:655-689` |
| Audit entry uses store's `AuditEntry` + `_append_audit_log` (invariant 6) | **met** | `lib.py:60-61,671` |
| `get_requests(states, since)` in `permission_state_store.py` under its lock | **met** | `permission_state_store.py:849+88 lines` |
| No bespoke JSONL parser outside the store module | **met** | grep confirmed none in `lib.py` |
| `permission_history` uses `get_requests`, not a direct file read | **met** | `lib.py:603` |
| Installer registers `permissions` beside `context-usage` with UV gate | **met** | `install-claude-config.sh` diff |
| `env: {"CLAUDE_HOOKS_REPO": "$SCRIPT_DIR"}` present | **met** | installer diff |
| Four `mcp__permissions__*` entries in `.claude/settings.json` | **met** | `settings.json` diff |
| 22-01 step-5 ask handling not disturbed | **met** | `run_all_tests.py` diff: only one line added |
| 42 new tests, all 10 required cases covered | **met** | test file lines 154-842; `Ran 42 tests … OK` |
| Tests use scratch dirs, env-var store overrides, no network/real bot | **met** | `test_unit_permissions_mcp.py:37-41,75-89` |
| Scope: no edits outside the 5 allowed targets | **met** | `git status` — only `permission_state_store.py`, `.claude/settings.json`, `install-claude-config.sh`, `tests/run_all_tests.py` modified; `tasks/23_async_questions/` is untracked pre-existing work (not introduced by this task) |
| Compile/import clean | **met** | `python3 -m py_compile` → OK |
| Tests: 984 passed, 0 failed, 1 pre-existing skip | **met** | `Ran 984 tests in 31.443s` (observed) |
| Installer not run — explicitly manager-approved; substitute stdio exercise reported | **met** | implementation report §Blockers |

---

## Issues Found

None at BLOCKER or HIGH severity.

### Issue 1 [LOW] — `tasks/23_async_questions/` created outside this task's scope

- **Files:** `tasks/23_async_questions/` (untracked, 7 files)
- **Problem:** This task's scope is `permissions-mcp/`, `permission_state_store.py`, `install-claude-config.sh`, `.claude/settings.json`, and tests. The `tasks/23_async_questions/` directory was introduced by the implementer (it does not appear in earlier commits) and is not relevant to 22-03.
- **Fix:** The directory is untracked and will not be committed with this task's work. It should either be removed or committed separately in its own task. It does not affect any code here, so this is a cleanliness note rather than a correctness issue.

### Issue 2 [LOW] — `_append_audit_log` is a private store function

- **File:** `permissions-mcp/permissions_mcp_lib.py:61`
- **Problem:** `_append_audit_log` is a module-private function (leading underscore). Calling it from outside `permission_state_store.py` is technically crossing the privacy boundary, even though the implementer noted this is the intentional invariant-6 path (the reason entry uses the store module's own writer). The `decide_permission_request` function itself also explains why the public `update_request_state` cannot carry the free-text reason.
- **Fix:** Either expose a thin public wrapper (`append_agent_decision_reason`) or document that the private function is part of a stable semi-public surface for trusted callers. For now this is low-risk because the tests cover the round-trip and nothing prevents the store from adding such a wrapper later.

---

## Code Quality Notes

1. **One-classifier property is well-enforced.** `classify()` is the only place that calls `BashPermissionValidator.validate_bash_command`. Both `summarize_row` (list path) and `decide_permission_request` (decide path) reach it through `classify()`. The `test_classifier_is_the_imported_validator` test patches the validator and asserts the verdict moves with it — this test would fail if there were a second inline implementation.

2. **Guard is correctly row-shaped (invariant 3).** `guard_row` refuses only the exact shape `row.session_id == identity.session_id and row.agent_id is None`. `test_guard_is_not_widened_to_all_same_session_rows` exercises all three shapes (own main-agent refused, own subagent allowed, foreign main-agent allowed) against the function directly.

3. **Decision contract with 22-02 verified end-to-end.** Case 10 (`TestEndToEndWithWaitLoop`) is not a shape assertion — it parks the real `wait_for_response` loop (patching only the relay transport), fires the real `decide` MCP function mid-loop, and asserts that the loop returns `{"action": "allow"}` and that `build_output_decision` produces `{"behavior": "allow"}`. The Telegram edit-and-cancel flow is also confirmed. The two negative cases (deny path, and refused-decide-leaves-loop-parked) complete the coverage.

4. **Fail-closed on missing identity is clean.** `CallerIdentity.can_decide` returns `bool(self.session_id)`. An empty string coerces to `None` in `resolve_caller_identity` (`or "").strip() or None`), so an empty env var is indistinguishable from an absent one — no falsy-string escape path.

5. **`get_requests` pure-reader design is sound.** The function does not mark lapsed-pending rows expired (unlike `get_all_pending_requests`). `list`'s default `state="pending"` still takes the `get_all_pending_requests` branch, preserving the expiry side-effect for the path that had it. The docstring explicitly notes this distinction.

6. **`REDIRECT_REASON_PREFIX` check is justified.** The implementer's Decision 2 explains why: the redirect escape is the one branch the validator reports as an `ask` with no `matched_ask_patterns`, so the fresh reason string is the only available discriminator. Critically, it is checked against the **fresh** validator output (not the stored reason), so the invariant is preserved. A test (`test_redirect_escape_refuses_as_human_only`) confirms the wording.

7. **Test isolation is solid.** `PermissionsMCPTestCase.setUp` creates a fresh scratch HOME and workspace directory for every test, clears `SettingsLoader._cache` before and after, and patches `HOME` and `CLAUDE_MANUAL_CONFIRM_LOG` via `patch.dict` in every classify/decide call. The module-level `setdefault` (lines 38-40) means the isolated store is established before any import, and `run_all_tests.py` conftest does the same for the shared run.

---

## Questions for User

None.
