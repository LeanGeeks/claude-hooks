# Fix Report — 22-02 External Decisions Reach the Wait Loop

## Summary

Three findings from the review addressed. No regressions.

---

## Finding 1 (MEDIUM): Source gate not isolated by any test

**New test:** `test_relay_loop_ignores_an_externally_decidable_state_with_non_agent_source`
**File:** `tests/test_integration_permission_request.py` — inserted after case 3

The test creates a row in state `ALLOW` (which IS in `_EXTERNALLY_DECIDABLE_STATES`)
carrying `decision={"action": "allow"}` but with `resolution_source=RESOLUTION_SOURCE_TELEGRAM`.
The loop must time out returning `None`, and `finalize_message` must not be called.

Without the source gate (`resolution_source == RESOLUTION_SOURCE_AGENT`), this test
fails because the state gate alone would let a telegram-sourced ALLOW through.
The existing case 3 (EXPIRED + source=timeout) remains untouched.

---

## Finding 2 (LOW): Helper defined after its call site

**File:** `.claude/hooks/permission_request_hook.py`

Moved `_EXTERNALLY_DECIDABLE_STATES` and `_finalize_agent_decision` from after
`wait_for_response` (formerly ~lines 406–447) to immediately before it (~lines 326–372).
The duplicate block was removed. The helpers now appear before the function that calls them,
matching the file's existing layout convention.

---

## Finding 3 (LOW): Patch-before-cancel order not asserted in case 2

**File:** `tests/test_integration_permission_request.py` — `test_relay_loop_adopts_agent_deny_and_finalizes`

Replaced the `patch.object(self.hook, "finalize_message")` mock (which only
verified the call, not ordering) with the same call-recording pattern used in
case 1: patch `tpr.edit_message_text` and `tpr.remove_inline_buttons` separately,
record `("edit", message_id)` / `("cancel", message_id)` in a list, then assert:
- `[c[0] for c in calls] == ["edit", "cancel"]` — patch strictly before cancel
- Both ops target message_id 99

---

## Verification

```
$ python3 -m py_compile .claude/hooks/permission_request_hook.py \
    .claude/hooks/permission_state_store.py \
    .claude/hooks/telegram_permission_router.py \
    tests/test_integration_permission_request.py \
    tests/test_unit_state_store.py
compile: PASS

$ python3 tests/run_all_tests.py
Ran 942 tests in 30.866s
OK (skipped=1)
```

Baseline before this fix: 941. New count: **942** (+1 for the new source-gate test).
No failures, no regressions.

---

## Blockers

None.
