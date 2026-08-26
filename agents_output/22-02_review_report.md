# Review Report - 22-02 External Decisions Reach the Wait Loop

## Verdict: PASS

No BLOCKER or HIGH issues found. One MEDIUM test-quality gap (case 3 does not
prove the source gate in isolation) and two LOW notes.

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| `RESOLUTION_SOURCE_AGENT = "agent"` constant | met | `permission_state_store.py:74` |
| `PermissionRequest.actor_agent: Optional[str] = None` | met | `permission_state_store.py:97-101` |
| `AuditEntry.actor_agent: Optional[str] = None` | met | `permission_state_store.py:134` |
| `update_request_state` accepts `actor_agent`, guarded write | met | `permission_state_store.py:355, 451-452` |
| `resolution_source is None` inference does not claim agent writes | met | comment + unchanged inference block at `permission_state_store.py:389-403` |
| `from_dict` tolerates old rows without `actor_agent` | met | `PermissionRequest` is a `@dataclass` with default=None; `from_dict` already filters unknown keys (verified: `test_row_written_before_this_change_loads_with_actor_agent_none`) |
| Relay-path loop: new arm after unchanged `RESOLVED_TERMINAL` arm | met | `permission_request_hook.py:363-376`; RESOLVED_TERMINAL arm is identical to pre-22-02 diff |
| Source gate: `resolution_source == RESOLUTION_SOURCE_AGENT` | met | `permission_request_hook.py:368` |
| State gate: `_EXTERNALLY_DECIDABLE_STATES = {allow, deny, stop}` | met | `permission_request_hook.py:414-419` |
| REPLY and WHITELIST excluded | met | comment at `permission_request_hook.py:404-413`; set confirmed by `test_reply_and_whitelist_are_not_externally_decidable` |
| `_finalize_agent_decision`: patch then cancel, best-effort | met | `permission_request_hook.py:422-450`; `finalize_message` reused; `try/except Exception` wraps entire body |
| `_finalize_agent_decision` non-fatal: exception does not cost decision | met | `try/except` at `permission_request_hook.py:439`; caller does `return current.decision` on the line after the call — the return is OUTSIDE the try block |
| Attribution text `🤖 <action> by agent <actor_agent>` | met | `permission_request_hook.py:445-447`; verified in test case 1 `assertIn("🤖 allow by agent…")` |
| `render_permission_body` extracted, `send_permission_message` delegates to it | met | `telegram_permission_router.py:472-527`; text composition logic unchanged |
| AskUserQuestion group loop: no external-decision check, comment only | met | diff adds only a docstring comment at `permission_request_hook.py:910-919`; no behavior change |
| Long-poll chunk size unchanged | met | `chunk = min(25, …)` at `permission_request_hook.py:375`; untouched |
| No new bespoke writers (invariant 6 / H7) | met | diff contains no new `open()`/JSONL-write paths; `actor_agent` written inside existing `LOCK_EX` block at `permission_state_store.py:449-452` |
| `_wait_state_store_only` untouched; passes agent-sourced allow through | met | no diff on that function; proved by test case 5 |
| `install-claude-config.sh` not re-run (manager instruction) | met | explicitly noted in implementation report §Verification |
| 6 required test cases | met | see test-quality section below |
| No edits outside the 4 permitted files + tests | met | `git diff --name-only` shows exactly `permission_state_store.py`, `permission_request_hook.py`, `telegram_permission_router.py`, `test_integration_permission_request.py`, `test_unit_state_store.py` |

---

## Issues Found

### Issue 1: Test case 3 does not isolate the source gate [MEDIUM]

- **File:** `tests/test_integration_permission_request.py` — `test_relay_loop_ignores_a_terminal_row_written_by_another_source`
- **Problem:** The test sets the row to state `EXPIRED` with `source=timeout`. `EXPIRED` is not in `_EXTERNALLY_DECIDABLE_STATES`, so the *state* gate alone would reject this row even if the source gate (`resolution_source == RESOLUTION_SOURCE_AGENT`) were removed entirely. The test therefore does not prove the source gate is load-bearing.

  The task spec says: "Gate on `resolution_source == 'agent'`, not on any-terminal-with-decision … expiry writes EXPIRED and 22-05's compaction touches old rows; the source gate keeps the loop from adopting anything an agent did not write." The concern is about compaction writing `ALLOW`/`DENY`/`STOP` with a non-agent source. That case is unexercised.

- **Fix:** Add a variant (or replace the EXPIRED case) that uses state `ALLOW` with `resolution_source=RESOLUTION_SOURCE_TIMEOUT` (or any non-agent source). That row would pass the state gate but must fail the source gate. With the fix, a broken implementation that omits the source check would fail the test; currently it would pass.

### Issue 2: `_finalize_agent_decision` defined after its first call site [LOW]

- **File:** `permission_request_hook.py:370` (call), `permission_request_hook.py:422` (definition)
- **Problem:** Python resolves names at call time (not definition time) for module-level functions, so this is not a runtime error. However the function is called within `wait_for_response` at line 370, which is defined before `_finalize_agent_decision` at line 422. This is an unusual ordering in this codebase (other helpers are defined before their callers) and makes `wait_for_response` harder to read without scrolling down.
- **Fix:** Move `_EXTERNALLY_DECIDABLE_STATES` and `_finalize_agent_decision` above `wait_for_response`, or at minimum above the first call site. Cosmetic only; no functional change needed.

### Issue 3: `test_relay_loop_adopts_agent_deny_and_finalizes` patches at wrong level [LOW]

- **File:** `tests/test_integration_permission_request.py` — case 2
- **Problem:** Case 2 patches `self.hook.finalize_message` (i.e., the name `finalize_message` in the `permission_request_hook` module's own namespace). `_finalize_agent_decision` calls `finalize_message` directly (it is imported at the top of `permission_request_hook.py`), so the patch target `self.hook.finalize_message` is correct for the mock to intercept the call. This is fine. However, the test does NOT patch `edit_message_text`/`remove_inline_buttons` individually, and it does patch `self.hook.finalize_message`, which means the real Telegram calls are bypassed. This is the right layering, but it differs from case 1 (which patches at the lower level and verifies the patch-then-cancel order). Case 2 only verifies that `finalize_message` was called once with the right arguments, not that patch precedes cancel.
- **Fix:** Low-priority; case 1 already covers the ordering. No change strictly required.

---

## Code Quality Notes

1. **Consistency of the source-gate proof across test cases.** Cases 1, 2 verify positive path; case 3 verifies negative path but only via the state gate (see Issue 1). Case 4 is the most thorough: it drives both orderings of the race with real store writes. Case 5 and 6 cover the store-only path and schema compat. The suite is good overall.

2. **`_EXTERNALLY_DECIDABLE_STATES` placement.** Currently defined after `wait_for_response` (which uses it). Python resolves at call time so it works, but the `_WAIT_STATE_STORE_ONLY`-style convention in this file is to keep constants and helpers near the top of their section. See Issue 2.

3. **Fallbacks in `_finalize_agent_decision`** (`action → "decided"`, `actor → "unknown"`) are sensible for the best-effort path and match the non-fatal convention.

4. **`render_permission_body` extraction** follows the established `render_question_body` pattern and keeps `send_permission_message` unchanged in behavior. The `import html as _html` local import moved into `render_permission_body`; `send_permission_message` no longer imports it directly, which is correct since it no longer interpolates HTML itself.

5. **Test counts:** baseline 931 → 941 after 22-02 (+10). Observed: `Ran 941 tests in 30.570s OK (skipped=1)`.

---

## Questions for User

None.
