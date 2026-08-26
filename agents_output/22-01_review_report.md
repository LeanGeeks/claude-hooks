# Review Report — 22-01: Validator deny denies, ask asks

## Verdict: PASS

No BLOCKER or HIGH issues found. All 10 check points verified.

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| §1 `_normalize_to_modern_format` carries `permissions.ask`; legacy contributes empty list | met | `settings_loader.py:125-128, 138` |
| §1 `_merge_settings` unions ask lists with dedup | met | `settings_loader.py:171-180` |
| §1 `__main__` prints ask count | met | `settings_loader.py:218` |
| §2 `BashPermissionValidator.__init__` loads `ask_patterns` | met | `pretool_hook.py:435` |
| §2 `_check_single_command` returns `asked`/`matched_ask_patterns` | met | `pretool_hook.py:1241-1251` |
| §2 All early returns carry `'asked': False` (no-op, local-function, workspace-binary, workspace-rm) | met | `pretool_hook.py:1146-1196` (4 early-return dicts each augmented) |
| §2 Decision block: deny → ask → redirect-escape → allow → unknown | met | `pretool_hook.py:530-561` |
| §2 D1 flip: deny now returns `'deny'` not `'ask'` | met | `pretool_hook.py:538-540` |
| §2 Stable reason prefix `"Matches a denied pattern: "` | met | `pretool_hook.py:540` |
| §2 Stable reason prefix `"Matches an ask pattern: "` | met | `pretool_hook.py:543-546` |
| §3 `main()` deny arm emits correct JSON + `sys.exit(0)` | met | `pretool_hook.py:1413-1427` |
| §3 `log_manual_confirmation` fires before deny arm (fires for all non-allow) | met | `pretool_hook.py:1399-1401` |
| §3 `replay_from_log` deny arm added | met | `pretool_hook.py:388-396` |
| §3b Installer extracts `ASK_TOOLS` and carries `ask: $ask` in merged object | met | `install-claude-config.sh` (diff shows ASK_TOOLS extraction and merged object) |
| §4 "matches a denied pattern" branch NOT removed | met | `permission_request_hook.py:430-431` |
| §4 Ask-matched parts named in auto-deny note | met | `permission_request_hook.py:432-433` |
| §4 `_unallowlisted_bash_parts` returns 3-tuple; all callers updated | met | `telegram_permission_router.py:370, 504`; `permission_request_hook.py:425` |
| Fail-open: except branches intact in all helpers | met | `telegram_permission_router.py:409-412`; `permission_request_hook.py:426-428` |
| Tests: 8 new cases (TestDenyAndAskSemantics) | met | `tests/test_integration_pretool.py:1482-1592` |
| Tests: 3 existing deny→ask tests updated to `deny` | met | lines 72, 105, 567 |
| Tests: before 923 / after 931 (`+8`) | met | verified by running `python3 tests/run_all_tests.py` → 931 passed, 0 failed, 1 skipped |
| Done criterion 4: not-live note present | met | implementation report final section |
| Scope: only allowed files changed | partially met (see Issue 1 below) |

---

## Issues Found

### Issue 1: `state.md` status not updated to `done` [LOW]

- **File:** `tasks/22_agent_permission_flows/state.md` (diff)
- **Problem:** The implementer updated task 22-01's status from `todo` to `in_progress` but not to `done`. The task is implemented and green. This leaves the board showing stale status.
- **Fix:** Set status to `done` in `state.md`. Note: `state.md` is not in the strictly-allowed file list (§5 of the task), but updating task status is a standard housekeeping expectation on completion; low-severity editorial miss.

---

## Code Quality Notes

**Precedence order (check point 1):** Verified at `pretool_hook.py:530-561`. The block is: `any_denied` → `any_asked` → `disallowed_targets` → `all_allowed` → unknown. A fully-allowlisted command with one ask-matched sub-command truly returns `ask` (case 3 test confirms). Deny beats ask (case 5 test confirms).

**Stable reason prefixes (check point 2):** Both prefixes are string literals, not f-strings. `"Matches a denied pattern: "` at line 540, `"Matches an ask pattern: "` at line 545. No drift risk.

**Early-return `'asked': False` (check point 3):** All four early-return paths in `_check_single_command` (control-prefix no-op, local-function, workspace-binary, workspace-rm) include `'asked': False` and `'matched_ask_patterns': []`. No KeyError risk at call site.

**Deny arm in `main()` and `replay_from_log` (check point 4):** Both present. JSON shape matches task spec (`permissionDecision: "deny"`, `permissionDecisionReason`). `log_manual_confirmation` fires before the if-chain (for all `!= 'allow'` decisions), so deny decisions land in `bash_manual_confirm.log`.

**`SettingsLoader` (check point 5):** `_normalize_to_modern_format` initializes `ask: []`, only extends if key present (no crash on missing key). Legacy format silently contributes empty ask. `_merge_settings` unions base+override ask with `_unique_ordered`. Fully symmetric with allow/deny.

**Installer §3b (check point 6):** `ASK_TOOLS=$(jq '.permissions.ask // []' ...)` extracted from project config. Merged object is `{allow: $allowed, deny: $disallowed, ask: $ask}` — replaces the old 2-key object. An existing global `ask` key is no longer erased.

**§4 downstream helper (check point 7):** `_unallowlisted_bash_parts` returns `(denied, unknown, asked)`. The `denied` branch was not removed. Ask-matched sub-commands now appear in both Telegram body and auto-deny note. Fail-open `except` at lines 409-412 and 426-428 return `([], [], [])` / `""` respectively.

**Fail-open posture (check point 8):** All error paths return the empty tuple or empty string. No new error path was wired to a deny decision. The deny path is reached only when a pattern explicitly matches — it is policy, not exception handling.

**Test quality (check point 9):** All 8 cases exercise real validator code (not mocked). Case 7 tests `SettingsLoader._normalize_to_modern_format` directly. Case 8 spawns a subprocess with a real scratch workspace, verifying the full hook pipeline end-to-end. Assertions are meaningful — they check `decision`, `reason` prefix, and exact sub-command text. None could pass against a broken implementation. The three updated tests now assert `deny` with `assertIn("denied", result["reason"].lower())`. Test count verified independently: 931 passed, 0 failed, 1 skipped.

**Scope (check point 10):** Files changed: `settings_loader.py`, `pretool_hook.py`, `telegram_permission_router.py`, `permission_request_hook.py`, `install-claude-config.sh`, `tests/test_integration_pretool.py`, `tests/test_integration_permission_request.py`, `tests/scenario_check.py`, `tasks/22_agent_permission_flows/state.md`. All are either required by the task or test/supporting files that needed updating. The `state.md` status edit is the only item not explicitly authorized by §5's file list — minor, not a real scope issue.

---

## Questions for User

None.
