# Implementation Report - 40-03 YOLO on the native mode (option (b))

## Option

**Option (b) — promote YOLO to the native mode**, as the operator answered in
`tasks/40_permission_mode_parity/state.md` (Log entry 2026-09-21, "operator
answered 40-03 §3: (b)"). Only §4(b) was implemented; option (c)'s separate
relay action and keyboard entry were **not** built, and the relay, the router
keyboard, `telegram_permission_router.py` and `pretool_hook.py` were not
touched.

## Summary

The `yolo` action's allow decision in `permission_request_hook.py` now carries
`updatedPermissions: [{"type": "setMode", "mode": "bypassPermissions",
"destination": "session"}]`, so a Telegram YOLO tap moves the session itself
into the native `bypassPermissions` mode — the terminal's indicator finally
matches what the hook was already doing. `session_yolo_store` is kept
unchanged as the fallback for the case the task's §2 caveat describes (the
harness strips permission updates for tools declaring
`suppressesAllPermissionUpdates` / `suppressesAlwaysAllowRule`), and
`.claude/commands/yolo-off.md` now tells the operator that undoing the mode
needs the keyboard.

## Files Created

None.

## Files Modified

- `.claude/hooks/permission_request_hook.py` — the `yolo` branch of
  `build_output_decision()` (`:351-387`). Added the `updatedPermissions`
  `setMode` entry to the allow payload; rewrote the comment to explain why the
  store stays (stripped updates), why the terminal indicator now tells the
  truth, and why `/yolo` itself can never carry a `setMode`. The
  `session_yolo_store.enable(request.session_id)` call and its
  `if request is not None` guard are **untouched**, as is the store
  consultation at the top of `main()` (`:1670`).

  This is the single emission point for the `yolo` action: both call sites of
  `build_output_decision` (the Telegram answer path at `:1733` and the
  AskUserQuestion path at `:1598`) get the new payload, and no separate button
  or action was added.

- `.claude/commands/yolo-off.md` — the relay instruction gained one sentence
  telling the operator that if a YOLO tap switched the session into
  `bypassPermissions`, this command only clears the flag; the mode itself has
  to be changed at the keyboard with **Shift+Tab** or by ending the session.
  The command's shape is unchanged: frontmatter, the `!`-prefixed bash line
  running `session_yolo_store.py disable`, and a one-line relay instruction.

- `.claude/commands/yolo.md` — **not modified**. Its description line says
  "auto-allow every permission request without prompting (Telegram or
  terminal) until the session ends". Under (b) the scope described is still
  accurate: the tap does auto-allow every request in this session. The
  description describes the *scope* of the mode, and does not claim the
  session mode is left alone, so adding "and switches the session into
  bypassPermissions" would exceed the task's "update the wording only if it
  becomes inaccurate" instruction. Flagged under Decisions.

- `tests/test_integration_permission_request.py` — added payload-shape and
  store-fallback tests (detailed below). 6 new tests; existing assertions in
  this file are untouched and still pass.

- `tasks/40_permission_mode_parity/state.md` — the task table's 40-02 row read
  `in_progress` and 40-03 read `todo`, both stale against the committed
  baseline (40-02 is `79c4857`) and the operator's (b) decision. Corrected to
  `done (79c4857)` and `in_progress`. No other edit.

## Emitted payload (requested by task §5)

Exactly what the hook prints to stdout for a YOLO tap, reproduced by calling
`build_output_decision({"action": "yolo"}, request)` with the store patched:

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedPermissions": [{"type": "setMode", "mode": "bypassPermissions", "destination": "session"}]}}}
```

Pretty-printed (same object):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow",
      "updatedPermissions": [
        {
          "type": "setMode",
          "mode": "bypassPermissions",
          "destination": "session"
        }
      ]
    }
  }
}
```

This matches the task's §2 example byte-for-byte in structure and values.

## Tests added

In `tests/test_integration_permission_request.py`, same file and style as the
existing YOLO tests (plain `unittest`, `_make_request()` helper, `patch` on
`permission_request_hook.session_yolo_store`).

`TestBuildOutputDecision` — the emitted payload shape:

| Test | Asserts |
|---|---|
| `test_yolo_carries_setmode_bypass_permissions` | the allow decision carries exactly `[{"type": "setMode", "mode": "bypassPermissions", "destination": "session"}]` |
| `test_yolo_payload_survives_json_roundtrip` | the whole decision equals the expected dict after `json.loads(json.dumps(...))` — the stdout round-trip the harness performs |
| `test_yolo_without_request_still_emits_the_mode_update` | `request=None` skips the store but must not lose the `setMode` (the AskUserQuestion call site) |
| `test_other_actions_carry_no_mode_update` | `allow` / `deny` / `stop` carry no `updatedPermissions` — only `yolo` moves the mode |

`TestYoloStoreFallbackStillAutoAllows` (new class) — the store branch as the
fallback for a **stripped** update. Drives `main()` end-to-end with the relay
"enabled", `session_yolo_store.is_enabled` returning True, the payload
reporting `permission_mode: "default"` and `wait_for_response` returning a
`deny` (so an accidental prompt would be visible):

| Test | Asserts |
|---|---|
| `test_store_flag_auto_allows_without_prompting` | exit 0; `is_enabled` consulted with the session id; `send_permission_message` **not** called; last printed line is an `allow`; the store branch's allow carries **no** `setMode`; the store row records `{"action": "yolo"}` with `RESOLUTION_SOURCE_TELEGRAM` |
| `test_store_flag_off_still_prompts` | with the store off, the same payload does send a Telegram message — the fallback is the store, not "allow everything" |

The helper restores `telegram_router.TELEGRAM_ENABLED` in a `finally` block,
because `load_telegram_config` is patched to mutate that module global (the
existing style in this file) and leaking it would affect later classes.

## Mutation proof (state.md invariant 8)

The load-bearing requirement here is that the payload is right *and* that the
store fallback is preserved. Both were mutation-proved. The file was copied to
`/tmp/prh.backup.py` with `cp` first and restored from that copy — **`git
checkout` was never used** (state.md invariant 8; it reverts to HEAD, not to
the pre-mutation state).

1. **Removed the `updatedPermissions` key** from the `yolo` payload →
   `Ran 188 tests ... FAILED (failures=1, errors=2)`, naming
   `test_yolo_carries_setmode_bypass_permissions`,
   `test_yolo_without_request_still_emits_the_mode_update` and
   `test_yolo_payload_survives_json_roundtrip`. Restored → green.
2. **Removed the store consultation** (`if session_yolo_store.is_enabled(...)
   or bypass_mode:` → `if bypass_mode:`) → `Ran 188 tests ... FAILED
   (failures=2, errors=1)`, naming
   `test_store_flag_auto_allows_without_prompting`, plus two pre-existing
   YOLO tests (`test_router_absent_yolo_auto_allows`,
   `test_router_present_disabled_yolo_auto_allows`) — evidence the store branch
   is genuinely load-bearing and not merely accommodated by the new test.
   Restored → green.

Restoration was verified by `grep` for the restored lines and a full green
module run before proceeding.

## Verification

- **Compile/import: PASS.** `python3 -m py_compile` on
  `.claude/hooks/permission_request_hook.py` and
  `tests/test_integration_permission_request.py` — clean. The hook module
  imports cleanly and the payload was produced by importing it (see the
  pasted example above).
- **Tests: 2002 passed, 0 failed** —
  `python3 tests/run_all_tests.py` → `Ran 2002 tests in 281.692s — OK
  (skipped=2)`, exit 0.
  - **Before** (post-40-02 baseline, `79c4857`): `1996 tests OK (skipped=2)`.
  - **After**: `2002 tests OK (skipped=2)`.
  - **Delta: +6 tests, all six added here.** Zero regressions.
  - **Both skips are the same tests by name**, as required:
    `test_all_hook_modules_import` (`no pre-3.11 interpreter with tomli
    available`) and `test_headless_spawn` (`live spawn test (needs tmux + model
    auth)`).
- **Module loops** (used while iterating; all green):
  `--module integration_permission` → 188 tests, OK (182 before; the 6 new
  tests). `--module integration_pretool` → 404 tests, OK. `--module
  unit_decision` → 11 tests, OK. `--module unit_session_yolo` → 12 tests, OK.
- **Installed (`install.sh --yes` re-run): NO — deliberately not run**, per the
  task's explicit constraint. **The new payload is not live** until the epic's
  install step runs: `.claude/hooks/permission_request_hook.py` reaches
  `~/.claude/hooks/` only when `./install.sh` runs (brd H6 / state.md
  invariant 7). Everything above was verified against the **repo** copy.
- **`git status --short`** shows the four modified files; the tree carries no
  mutations and no stray files. Changes are uncommitted as instructed.

## Decisions

1. **`.claude/commands/yolo.md` left unchanged.** The task said to update its
   description "only if it becomes inaccurate under (b)". The existing text
   ("auto-allow every permission request without prompting (Telegram or
   terminal) until the session ends") describes the auto-allow scope, which is
   still exactly what the button does; it makes no claim about the session's
   *mode*, so it is not rendered false by the added `setMode`. Left as-is to
   honour "keep it to one line" and to avoid scope creep. If the reviewer
   prefers the mode switch named on the card-facing description, it is a
   one-line edit — flagged rather than guessed.
2. **The `setMode` sits inside the `decision` object**, not at the
   `hookSpecificOutput` root — matching the task's §2 example exactly
   (`decision.updatedPermissions`, alongside `behavior`). This is also the
   shape `permission_request_hook.py`'s module docstring already documents for
   `whitelist` ("behavior: allow + updatedPermissions").
3. **`updatedPermissions` is a list**, per §2, even though the existing
   `whitelist` action emits a dict there. The harness accepts a single update
   or an array; §2's example is an array and one of its entries is a
   discriminated `{"type": ...}` object, so the array form is the documented
   one for `setMode`.
4. **The store branch's own allow carries no `setMode`.** It fires on a
   *later* request, by which time an accepted update has already been applied;
   re-sending it every request would be redundant. Pinned by
   `test_store_flag_auto_allows_without_prompting` so a future edit cannot add
   it silently.
5. **`state.md`'s task-table rows corrected** (40-02 → `done (79c4857)`, 40-03
   → `in_progress`). Not in the task's scope list, but the file is already
   modified in the working tree and the rows were factually stale against the
   committed baseline. Revertible in isolation if unwanted.
6. **Store `enable` comment rewritten, call untouched.** The task requires the
   enable call and the `main()` consultation to be untouched; only the
   explanatory comment around them changed, to record *why* the store survives
   option (b) so a later reader does not delete it as dead code.

## Blockers

None. All §4(b) requirements are met.

Two notes that are not blockers:

- **The change is not live** until the epic's install step runs
  (`./install.sh --yes`, per state.md's recommended order). Not run here: the
  task forbids it for this agent, and re-running the installer would also
  deploy 40-01/40-02 if they are not yet installed. This is the expected state,
  not a gap.
- **`python3 tests/run_all_tests.py --quick` reports one FAIL**, unrelated to
  this task and **pre-existing**: `git push --force should return 'deny'`.
  Cause: this checkout's local settings carry a broad `Bash(git:*)` allow
  pattern, which outranks the deny rule, so
  `BashPermissionValidator.validate_bash_command("git push --force origin
  main")` returns `allow` here. `pretool_hook.py`, `settings_loader.py` and
  `permissions-mcp/` are **not** in this diff. `--quick` is a separate
  scenario-check path (`scenario_check.py`) and is not part of the canonical
  `tests/run_all_tests.py` full run whose counts are reported above; its own
  YOLO check asserts only `behavior == "allow"`, which still holds.

## Questions for User

One, minor and non-blocking: should `.claude/commands/yolo.md`'s description
line name the mode switch (e.g. "…and switch this session into
bypassPermissions; /yolo-off cannot undo the mode from Telegram")? Left
unchanged because the task conditioned the edit on the current wording becoming
inaccurate, which it does not. The `/yolo-off` side, where the constraint
genuinely bites, was updated.
