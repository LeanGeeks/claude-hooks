# Review Report - 22-02 External Decisions Reach the Wait Loop (Pass 2)

## Verdict: PASS

No new issues. All three prior findings are resolved. Tests pass at the claimed count.

---

## Prior Findings — Resolution Status

### Finding 1 (MEDIUM): Source gate not isolated by a test → **RESOLVED**

A new test `test_relay_loop_ignores_an_externally_decidable_state_with_non_agent_source`
(case 3b) was added in `tests/test_integration_permission_request.py`.

The test creates a row in state `ALLOW` (which IS in `_EXTERNALLY_DECIDABLE_STATES`) with
`decision={"action": "allow"}` but `resolution_source=RESOLUTION_SOURCE_TELEGRAM`. The loop
must time out returning `None`, and `finalize_message` must not be called.

**The test is load-bearing.** If the source gate
(`current.resolution_source == RESOLUTION_SOURCE_AGENT`) were removed from the loop at
`permission_request_hook.py:412`, the state gate alone would pass the ALLOW row and the
loop would return `{"action": "allow"}` rather than `None` — the test would fail.

The original case 3 (`EXPIRED` + `source=timeout`) is unchanged and still present.

### Finding 2 (LOW): `_finalize_agent_decision` defined after its call site → **RESOLVED**

`_EXTERNALLY_DECIDABLE_STATES` (line 337) and `_finalize_agent_decision` (line 344) now
appear before `wait_for_response` (line 373). The call sites at lines 413 and 418 are both
inside `wait_for_response`. The ordering matches the file's convention of helpers-before-callers.
No duplicate definition left behind; confirmed by grep: only one definition of each symbol.

### Finding 3 (LOW): Case 2 did not assert patch-then-cancel order → **RESOLVED**

Case 2 (`test_relay_loop_adopts_agent_deny_and_finalizes`) now uses the same
call-recording pattern as case 1: patching `tpr.edit_message_text` and
`tpr.remove_inline_buttons` individually and asserting:
- `[c[0] for c in calls] == ["edit", "cancel"]` — patch strictly before cancel
- Both operations target message_id 99

---

## Regression Check

**Compile:** `python3 -m py_compile` on all five changed files → `OK`.

**Layout:** No leftover duplicate definition of `_EXTERNALLY_DECIDABLE_STATES` or
`_finalize_agent_decision`; both defined exactly once (lines 337 and 344).

**Loop invariants after the move:**
- `RESOLVED_TERMINAL` branch at line 407 still runs first, unchanged.
- Source gate (`resolution_source == RESOLUTION_SOURCE_AGENT`) plus state gate
  (`current.state in _EXTERNALLY_DECIDABLE_STATES`) both required — line 412–413.
- `_finalize_agent_decision` is wrapped entirely in `try/except Exception` (non-fatal);
  `return current.decision` at line 419 is outside the try block in the caller, so a
  Telegram failure cannot stop the decision being returned (fail-open posture preserved).
- No new bespoke store writers; `actor_agent` is written inside the existing `LOCK_EX`
  block.
- Scope limited to the four permitted files plus tests (`git diff --name-only` confirms).

**Tests:** `python3 tests/run_all_tests.py` → `Ran 942 tests in 30.919s OK (skipped=1)`.
Claimed count: 942 (+1 from baseline 941). Observed: **942**. Match confirmed.

---

## Code Quality Notes

No new concerns. The case 3b docstring correctly explains the compaction scenario
(22-05) that motivates the source gate, making the test self-documenting.

---

## Questions for User

None.
