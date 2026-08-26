# Implementation Report — 22-01: Validator deny denies, ask asks

## Summary

Implemented D1 (deny hard-blocks) and D2 (honors `permissions.ask`) across
`settings_loader.py`, `pretool_hook.py`, `telegram_permission_router.py`,
`permission_request_hook.py`, and `install-claude-config.sh` Step 5. A deny-matched
sub-command now emits `permissionDecision: "deny"` from the hook; an ask-matched
sub-command emits `permissionDecision: "ask"` with the stable reason prefix
`"Matches an ask pattern: ..."`. The installer now propagates `permissions.ask`
from the project config into the global settings file instead of silently erasing it.

## Files Modified

- `.claude/hooks/settings_loader.py` — `_normalize_to_modern_format`: carries `permissions.ask` through (legacy files contribute empty ask list); `_merge_settings`: unions ask lists with dedup; `__main__` block: prints ask count.
- `.claude/hooks/pretool_hook.py` — `BashPermissionValidator.__init__`: loads `ask_patterns`; `_check_single_command`: checks ask patterns between deny and allow, returns `asked`/`matched_ask_patterns`; adds `'asked': False` to every early-return path; decision block: deny now emits `decision='deny'`, new `any_asked` branch emits `decision='ask'` with stable prefix; `main()`: adds deny arm; `replay_from_log`: adds deny arm.
- `.claude/hooks/telegram_permission_router.py` — `_unallowlisted_bash_parts`: now returns 3-tuple `(denied, unknown, asked)`; call site updated to unpack `asked` and render "Matches an ask pattern" section in the Telegram body.
- `.claude/hooks/permission_request_hook.py` — `_format_non_whitelisted`: unpacks 3-tuple from `_unallowlisted_bash_parts`; names ask-matched parts in the auto-deny note.
- `install-claude-config.sh` Step 5 — extracts `ASK_TOOLS=$(jq '.permissions.ask // []' ...)` alongside allow/deny; carries `ask: $ask` in the merged permissions object.
- `tests/test_integration_pretool.py` — updated 3 tests asserting old deny→ask mapping to assert `deny`; added `TestDenyAndAskSemantics` with all 8 required test cases; `_FakeLoader` accepts `ask=` kwarg; `_validator` helper accepts `ask=` kwarg.
- `tests/test_integration_permission_request.py` — updated 6 call sites and assertions that depended on the old 2-tuple return value of `_unallowlisted_bash_parts`.
- `tests/scenario_check.py` — updated scenario 4 to assert `deny` not `ask`.

## Verification

### Compile / import

```
python3 -m py_compile .claude/hooks/settings_loader.py .claude/hooks/pretool_hook.py \
  .claude/hooks/telegram_permission_router.py .claude/hooks/permission_request_hook.py
# → COMPILE OK (no output)
```

### Tests — before / after

- **Before** (baseline, unmodified repo): 923 passed, 0 failed, 1 skipped.
- **After** (with all changes): 931 passed, 0 failed, 1 skipped.
- Net new tests: +8 (all 8 from the task's Testing table in `TestDenyAndAskSemantics`).

**Existing tests that asserted old deny→ask mapping (3 tests updated, all renamed/reworded):**

| Test (old name) | File | Change |
|---|---|---|
| `test_denied_command_returns_ask` | `test_integration_pretool.py` | renamed to `test_denied_command_returns_deny`; asserts `deny` |
| `test_absolute_path_respects_deny_list` | `test_integration_pretool.py` | asserts `deny` (was `ask`) |
| `test_timeout_without_duration_does_not_bypass` | `test_integration_pretool.py` | updated: `reboot` now expects `deny`; `rm` not in project deny list so stays `ask` — assertion now checks "not allow" |

Command used: `python3 tests/run_all_tests.py`

### Done criterion 2 — literal hook output

Scratch workspace with `deny: ["Bash(curl:*)"]` and `ask: ["Bash(git push:*)"]`:

```
=== Test 1: deny — compound with curl ===
$ echo '{"tool_name":"Bash","tool_input":{"command":"true && curl http://x"},"cwd":"<ws>"}' \
  | CLAUDE_WORKSPACE_DIR=<ws> CLAUDE_HOOK_DEBUG=0 python3 .claude/hooks/pretool_hook.py

{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Matches a denied pattern: `curl http://x`"}}

=== Test 2: ask — git push with ask pattern ===
$ echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"},"cwd":"<ws>"}' \
  | CLAUDE_WORKSPACE_DIR=<ws> CLAUDE_HOOK_DEBUG=0 python3 .claude/hooks/pretool_hook.py

{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "Matches an ask pattern: `git push origin main`"}}
```

### Done criterion 3 — installer jq check

Scratch global config containing stale `ask: ["Bash(old-ask:*)"]`; project config
carrying `ask: ["Bash(git push:*)"]`. After Step 5 jq:

```
=== Project config ask extraction ===
ASK_TOOLS: [
  "Bash(git push:*)"
]

=== Merged result (Step 5 jq) ===
{
  "permissions": {
    "allow": [
      "Bash(git:*)",
      "Bash(npm:*)"
    ],
    "deny": [
      "Bash(dd:*)"
    ],
    "ask": [
      "Bash(git push:*)"
    ]
  }
}
```

The project's ask list replaces the stale global one — no silent erase.

### Not live until installed (done criterion 4)

`./install-claude-config.sh` was NOT re-run per task instructions. The hook copy
under `~/.claude/hooks/` retains the pre-change version until the installer
re-runs. Changes are live only in the repo copy (`.claude/hooks/pretool_hook.py`)
used by direct invocation and the test suite.

## Decisions

- **`_unallowlisted_bash_parts` return arity extended from 2-tuple to 3-tuple.**
  Adding `asked` as a third element is backward-incompatible only for patched
  call sites (test mocks). All 6 test call sites in `test_integration_permission_request.py`
  were updated. The docstring explains the D1 context: deny-matched commands no
  longer reach `PermissionRequest`, so the `denied` bucket goes quiet naturally
  but is retained for settings-change robustness.
- **`'asked': False` added to all early-return paths in `_check_single_command`.**
  No-op builtins, local-function calls, and workspace binaries cannot be ask-listed
  into a prompt — the task explicitly requires this, and the field must exist so
  the decision block can iterate `validation_results` without a `KeyError`.
- **Legacy-format ask list is empty, not an error.** Consistent with the task
  statement "There is no legacy-format equivalent — do not invent one; legacy
  files simply contribute an empty ask list." Verified by test case 7.

## Blockers

None.

## Questions for User

None.
