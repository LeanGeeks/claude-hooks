# Review Report - Task 40-03 (option (b)): YOLO on the native mode

## Verdict: PASS

No BLOCKER, HIGH or MEDIUM issues. Two LOW (both documentation-accuracy, both
outside §4(b)'s scope list) and several code-quality notes. Every §4(b)
requirement is met, the emitted payload is byte-for-byte the task §2 shape, the
store fallback is intact and load-bearing, and I reproduced the implementer's
mutation proof myself with the `cp` protocol.

Base for the diff: `79c4857` (40-02, committed; `HEAD` and `79c4857` are the
same commit — `git status --short` shows exactly four modified files and no
untracked ones). No repo-wide git command was run; only
`git status --short`, path-scoped `git diff 79c4857 -- <path>`, and
`git show 79c4857:<path>`.

## Scope check

`git diff 79c4857 --stat` lists exactly four files, all in the expected set:

| File | Δ |
|---|---|
| `.claude/hooks/permission_request_hook.py` | +31/−8 |
| `.claude/commands/yolo-off.md` | +1/−1 |
| `tasks/40_permission_mode_parity/state.md` | +2/−2 |
| `tests/test_integration_permission_request.py` | +160 |

- **No option (c) signs.** `telegram_permission_router.py` is untouched
  (`_PERMISSION_ACTIONS` at `:295-304` still `allow/deny/stop/yolo`;
  `relay_answer_to_decision:1207` still `("allow","deny","stop","yolo")`); the
  relay server and `relay-server/` are untouched; no new action value exists
  anywhere (`grep -rn "updatedPermissions\|setMode"` across the repo returns the
  hook, its tests, `telegram_permission_router.py:1159` (pre-existing whitelist
  writer) and `tests/fixtures.py:265` — nothing new). No separate button.
- **`state.md` is status markers only.** The two changed lines are
  `40-02 in_progress → done (79c4857)` and `40-03 todo → in_progress`. The
  committed baseline itself carries the "(b)" Log entry
  (`git show 79c4857:tasks/40_permission_mode_parity/state.md:161`), so the
  implementer had no reason to write one and did not. No invariant, no Log
  entry, no prose outside the task table changed — within the manager's own
  status markers, not a substantive content change. Not a finding.
- **`install.sh` not run.** Per state.md invariant 7 the change is **not live**
  until `./install.sh --yes`. Nothing in this diff claims otherwise; wiring is
  already in place (`install.sh:130,132` deploy both
  `permission_request_hook.py` and `session_yolo_store.py` under
  `feature_permission-hooks`). Correctly reported as a non-gap.

## Completeness Check

### Step 2 — compile / import
`python3 -m py_compile .claude/hooks/permission_request_hook.py tests/test_integration_permission_request.py` → clean, exit 0. `import permission_request_hook` under `sys.path` = `.claude/hooks` → OK. No BLOCKER-class failure; review proceeded.

### §4(b) requirement 1 — `permission_request_hook.py` adds `updatedPermissions` to the `yolo` action's payload — **MET**

`.claude/hooks/permission_request_hook.py:373-387`; the new key is at `:378-384`.
The diff against `79c4857` is **two hunks, both inside `build_output_decision`** —
nothing else in a 2600-line file moved.

The exact stage, produced by importing the working-tree module and calling
`build_output_decision({"action": "yolo"}, <request>)` with `session_yolo_store.enable` patched (the implementer's pasted payload reclaims the same object; `json.dumps` uses the default separators `', '` / `': '`):

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedPermissions": [{"type": "setMode", "mode": "bypassPermissions", "destination": "session"}]}}}
```

This is **exactly** task §2's shape: `behavior: allow`, and
`updatedPermissions` = `[{"type": "setMode", "mode": "bypassPermissions",
"destination": "session"}]` — correct key name, correct array length (one),
correct three fields, correct spelling of `bypassPermissions`. Reproduced
independently, not taken from the report. `session_yolo_store.enable(session_id)`
still fires on the same call (observed `call('sess-abc')`).

### §4(b) requirement 2 — `session_yolo_store` stays as the fallback — **MET**

- `session_yolo_store.enable(request.session_id)` and its `if request is not
  None:` guard: `:370-372`, **untouched** by the diff (the hunk boundary ends at
  `:374`; the only change in the surrounding hunk is the comment block `:352-369`).
- Top-of-`main()` consultation `:1670` (`if session_yolo_store.is_enabled(session_id) or bypass_mode:`), `prune()` at `:1576`, import at `:105`: all untouched.
- The store branch's own allow (`:1681-1686`) carries **no** `setMode` — correct,
  and deliberately pinned by a test (see below).

I re-ran the implementer's mutation **#2** myself, cp protocol, no git:

```
cp .claude/hooks/permission_request_hook.py /tmp/prh.403review.backup.py   # md5 068143d1b7057ace63730a2835b4f0fd
# mutate :1670  ->  if bypass_mode:
python3 tests/run_all_tests.py --module integration_permission
  ERROR: test_router_present_disabled_yolo_auto_allows
  FAIL:  test_router_absent_yolo_auto_allows
  FAIL:  test_store_flag_auto_allows_without_prompting
  Ran 188 tests in 11.673s — FAILED (failures=2, errors=1)
cp /tmp/prh.403review.backup.py .claude/hooks/permission_request_hook.py
# md5 068143d1b7057ace63730a2835b4f0fd (identical); module re-run: Ran 188 — OK
# git status --short byte-identical to the pre-mutation list
```

This matches the implementer's named set exactly: the new class test **plus two
pre-existing YOLO tests** fail, which is the evidence that the store branch is
genuinely load-bearing and not merely accommodated by the new test.

### §4(b) requirement 3 — `.claude/commands/yolo-off.md` tells the operator the mode must be changed at the keyboard — **MET**

`.claude/commands/yolo-off.md:7`. Command shape preserved exactly: frontmatter
`description` unchanged, the `!`-prefixed bash line
(`python3 ~/.claude/hooks/session_yolo_store.py disable "${CLAUDE_SESSION_ID}"`)
unchanged, and the one-line relay instruction is still one line — the new
sentence is appended to it. It says the flag is all this clears and names both
reversals (Shift+Tab, session end). Verified by hand: **Shift+Tab does reach
`default` from `bypassPermissions`**, so the instruction is actionable, not a
dead end.

### §4(b) requirement 4 — tests: payload shape, and the store branch still auto-allows when the update is stripped — **MET**

Payload shape, `tests/test_integration_permission_request.py:83-142` (4 tests,
none vacuous):

| Test | Assertion | File:line |
|---|---|---|
| `test_yolo_carries_setmode_bypass_permissions` | `assertEqual` on the exact 3-key list | `:83-99` |
| `test_yolo_payload_survives_json_roundtrip` | whole-output `assertEqual` after `json.loads(json.dumps(...))` | `:101-119` |
| `test_yolo_without_request_still_emits_the_mode_update` | payload survives `request=None` | `:121-133` |
| `test_other_actions_carry_no_mode_update` | `assertNotIn("updatedPermissions", decision)` for `allow`/`deny`/`stop` | `:135-142` |

These are real value assertions, not "no exception thrown". The
"other actions" test is non-vacuous: I loaded the **baseline** module from
`git show 79c4857:...` into `/tmp/40-03_base/` (read-only) and diffed every
emitted payload against the working tree. `allow`, `deny`, `stop`, `reply`,
`answer`, unknown action and `None`-decision are **byte-identical**; only `yolo`
differs. That is stronger than the test itself (which covers 3 of the 6
non-yolo actions) — the gap is noted under Code Quality, not as a defect.

Store fallback, `tests/test_integration_permission_request.py:5269-5362` — and
**it is a genuine stripped-update simulation, not a payload-contains-setMode
test**:

- `_payload()` (`:5323`) sets `"permission_mode": "default"`, so the
  `bypass_mode` branch at `:1669` is **off** — the auto-allow can only come from
  the store.
- `wait_for_response` is patched to return `{"action": "deny"}` (`:5310`), so an
  accidental prompt would resolve as a deny and be visible; `send_permission_message`
  is asserted `assert_not_called()` (`:5342`).
- `is_enabled` is asserted called with the session id (`:5341`).
- The emitted allow is asserted to carry **no** `updatedPermissions` (`:5349-5352`) — the store branch is the fallback, not a second emitter.
- The row is asserted `RequestState.ALLOW` with `decision == {"action": "yolo"}`
  and `RESOLUTION_SOURCE_TELEGRAM` (`:5353-5357`).
- `test_store_flag_off_still_prompts` (`:5360-5362`) is the negative control: with
  the store off, the send **does** happen — so the fallback is the store, not
  "allow everything".
- I ran the class standalone (`python3 -m unittest -v
  test_integration_permission_request.TestYoloStoreFallbackStillAutoAllows`) →
  2 tests, OK.

### Test suite — **MET**

Canonical runner only, run by me in the working tree:

| Run | Result |
|---|---|
| `python3 tests/run_all_tests.py` | **Ran 2002 tests in 271.851s — OK (skipped=2)**, exit 0 |
| `--module integration_permission` | **188 OK** (11.6 s) |
| `--module unit_session_yolo` | 12 OK |
| `--module unit_decision` | 11 OK |
| `--module unit_state` | 59 OK |

Baseline arithmetic holds: `182 → 188` `def test_` in the module (+6), and the
six added names are exactly the six listed above — nothing was deleted to make
room. **Both skips are the same tests by name**:
`test_all_hook_modules_import` (*no pre-3.11 interpreter with tomli available*)
and `test_headless_spawn` (*live spawn test…*). Delta over post-40-02's 1996 is
**+6**, as expected.

## Issues Found

### Issue 1: [severity: LOW] `.claude/commands/yolo.md`'s "Disable with /yolo-off" is now a weakened reversal claim

- **File:** `.claude/commands/yolo.md:2` (unmodified by this diff)
- **Problem:** Under (b), `/yolo-off` no longer fully disables YOLO — it clears
  the store flag, but a session already in `bypassPermissions` keeps auto-allowing
  until the operator presses Shift+Tab or ends the session. The description line
  the operator reads *when enabling* still ends "Disable with /yolo-off", which
  is the reversal path that (b) breaks. The implementer's justification ("it
  makes no claim about the session's *mode*") is only half right: it makes a
  claim about *disabling*, and disabling is what changed. The warning lives only
  in `yolo-off.md`, i.e. the operator has to have already disabled to read it.
- **Fix (follow-up, not a blocker):** one line — append
  "…(a session moved to bypassPermissions needs Shift+Tab at the keyboard to
  leave the mode)". Task §4(b) names only `yolo-off.md`, so leaving `yolo.md`
  alone is scope-compliant; this is accuracy debt the manager can hand to
  40-04's doc touch or a one-line follow-up. The implementer already raised the
  same question in their report.
- Not raised above LOW because §4(b) lists exactly three artefacts and `yolo.md`
  is not one of them.

### Issue 2: [severity: LOW] The module docstring's action-mapping table still omits `yolo`

- **File:** `.claude/hooks/permission_request_hook.py:20-27`
- **Problem:** The table lists `allow / deny / stop / whitelist / reply /
  resolved_terminal`. `yolo` — the action this task changed — is absent, and
  `whitelist -> behavior: "allow" + updatedPermissions` is now doubly
  misleading: `build_output_decision` returns `None` for `whitelist`
  (`:463-465`), `relay_answer_to_decision` never emits it, and `yolo` is now the
  one action that actually emits `updatedPermissions`. A reader grepping the
  docstring for "who emits `updatedPermissions`" is sent to the wrong action.
- **Pre-existing:** verified against `git show 79c4857:...:18-28` — the same
  table, same omissions. The diff did not introduce it.
- **Fix:** add `- yolo -> behavior: "allow" + updatedPermissions (setMode
  bypassPermissions, session)` and drop or correct the `whitelist` line. Cosmetic;
  no runtime effect.

## Code Quality Notes

1. **The comment block (`:352-369`) is long but earns its place.** It records
   three non-obvious facts a future reader would otherwise have to re-derive:
   why the store survives (stripped updates), why the classifier edge matters (a
   refusal raises no prompt, so the hook is never consulted), and why `/yolo`
   cannot carry a `setMode` itself. Accurate against brd §1.4/§2.2 and task §2.

2. **The store branch deliberately does not re-send `setMode` (`:1681-1686`).**
   Documented at implementation-report Decision 4 and pinned by the new test.
   Worth the manager's awareness that this is the one place the strip-caveat
   bites: if the update was stripped on the tap, later requests are auto-allowed
   by the store but the session mode stays as it was, so the terminal keeps
   saying "manual mode". Re-sending the setMode from the store branch is
   *outside* §4(b)'s scope, so I am not asking for it — but it is the obvious
   next lever if 40-04's live verification shows the strip case in practice.

3. **`test_other_actions_carry_no_mode_update` (`:135-142`) covers 3 of the 6
   non-yolo actions.** `reply`, `answer` and the unknown-action fallback are not
   in its loop. I verified those three are byte-identical to baseline myself, so
   the requirement holds; the loop is just narrower than the claim. Cheap to
   widen to `("allow","deny","stop","reply","answer")` if wanted.

4. **`test_yolo_without_request_still_emits_the_mode_update` (`:121-133`) pins a
   path that cannot occur in production.** Both `handle_ask_user_question`
   returns are `answer` (`:1339-1345`) or `deny_mixed_roles` (`:1265`) — never
   `yolo` — so `build_output_decision(..., request=None)` can never see a yolo
   decision. Harmless and defensively useful (it pins the guard to the store
   call and not the payload), but it is not a regression guard for a live path.

5. **`--quick` (`tests/scenario_check.py`) is non-canonical and pre-existing-red,
   and the report slightly understates it.** `python3 tests/run_all_tests.py
   --quick` exits 1 with **one FAIL and one ERROR**:
   - FAIL: `git push --force should return 'deny'` (`:88-92`) — this checkout's
     own `.claude/settings.json:115` `Bash(git:*)` allow outranks the deny rule.
     `.claude/settings.json` is **not** in this diff.
   - ERROR: `check_decision` (`:176`) reads `decision["reason"]`, but the `reply`
     action has emitted `message` since the epic-40 era — a scenario-check bug,
     not a hook bug.
   Neither touches the 40-03 emission path. `scenario_check` runs only under
   `--quick` and is **not** in `TEST_MODULES` (`run_all_tests.py:39-64`), so the
   canonical 2002-test run never reaches it. Note that the implementer's report
   says the pre-existing `--quick` failure is the only one; there are two
   distinct pre-existing problems (a FAIL and an ERROR), and `:163-169` of that
   file still asserts only `behavior == "allow"` for yolo — it does not check the
   new `updatedPermissions`. Non-blocking; named so nobody later blames 40-03.

6. **Fail-open posture preserved.** No error path, `except` branch, exit code or
   return type changed. The `yolo` action still returns a dict (never `None`),
   so the `if output:` caller at `:1754` behaves as before.

7. **`yolo.md`'s description is still one line** (`:2`, 3 sentences on one
   line) — the shape constraint in the review brief holds, and it was not
   touched.

## Questions for User

None that block. One that the manager should settle, and which the implementer
also raised: **should `yolo.md`'s description gain the mode caveat?** (Issue 1.)
My recommendation is a one-line follow-up rather than a re-open of 40-03 —
§4(b)'s scope list does not include `yolo.md`, the operator-facing warning that
the task did require is present in `yolo-off.md:7`, and the correct place to
prove the caveat is 40-04's live six-mode pass.
