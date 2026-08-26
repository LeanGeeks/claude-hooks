# Fix Report — 22-03 Issue 2: `_append_audit_log` privacy boundary

## What changed

### `.claude/hooks/permission_state_store.py` — line 197 (after existing `_append_audit_log`)

Added a thin public wrapper immediately after the private function:

```python
def append_agent_decision_reason(entry: AuditEntry) -> None:
    """Write an agent decision-reason audit entry through the store's lock/append protocol.
    ...
    """
    _append_audit_log(entry)
```

- Delegates entirely to `_append_audit_log`; no logic duplicated.
- Full docstring marks it as the sanctioned path for agent decision reasons (invariant 6).
- Type hint on parameter (`AuditEntry`) and return (`None`).

### `permissions-mcp/permissions_mcp_lib.py`

**Import** (line 66): replaced `_append_audit_log` with `append_agent_decision_reason`.

**Call site** in `_record_decision_reason` (line 671): replaced `_append_audit_log(` with `append_agent_decision_reason(`.

**Docstring** updated to say "via the store's public `append_agent_decision_reason` wrapper" and dropped the phrase "no reaching into private store internals".

## Wrapper name and signature

```python
def append_agent_decision_reason(entry: AuditEntry) -> None: ...
```

Located at: `.claude/hooks/permission_state_store.py` line 197.

## Audit entry shape — unchanged

`append_agent_decision_reason` calls `_append_audit_log(entry)` with no transformation.
`_append_audit_log` writes `json.dumps(entry.to_dict()) + '\n'` — identical to before.
The existing round-trip tests (`test_unit_permissions_mcp.py`) exercise the full
`decide_permission_request` → audit log → read-back path and confirm the fields
`action`, `reason`, `resolution_source`, `tier`, `matched_patterns`, `caller_session_id`,
`actor_agent` are all present. No schema changes.

## Verification

```
python3 -m py_compile .claude/hooks/permission_state_store.py  → OK
python3 -m py_compile permissions-mcp/permissions_mcp_lib.py   → OK
python3 -m py_compile permissions-mcp/server.py                → OK
python3 tests/run_all_tests.py                                 → Ran 984 tests in 32.091s  OK (skipped=1)
```

No regression. Count is identical to pre-fix baseline (984).

## Blockers

None.
