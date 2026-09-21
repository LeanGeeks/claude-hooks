# Fix Report - Task 40-03 (option (b)): the two LOW doc-accuracy issues

Scope: `agents_output/40-03_review_report.md`'s "Issues Found" — exactly Issue 1
and Issue 2, nothing else. Both were **actionable**; neither turned out to be a
false positive on re-reading. All changes stay uncommitted in the shared working
tree; no repo-wide git command and no `./install.sh` was run.

Baseline: `79c4857` (40-02, committed; `HEAD == 79c4857`).

## Issue 1 — LOW — `yolo.md`'s "Disable with /yolo-off" is a weakened reversal claim

- **Status: ACTIONABLE, fixed.**
- **File:** `.claude/commands/yolo.md:2` (frontmatter `description`, one line).
- **Fix applied:** appended the mode caveat to the existing sentence, adapting the
  reviewer's suggested wording so the description stays truthful about *which*
  path is one-way (a Telegram tap, not `/yolo` itself, which can never carry a
  `setMode` — task §2):

  ```
  Disable with /yolo-off — note that a Telegram YOLO tap also moves the session
  into bypassPermissions, which only Shift+Tab at the keyboard (or ending the
  session) can leave.
  ```

  (written as a single physical line inside the frontmatter `description:`, per
  the task's "keep it to one line" constraint).
- **Constraint compliance:** `yolo.md`'s shape is untouched — same two frontmatter
  keys (`description`, `allowed-tools`), the `!`-prefixed
  `session_yolo_store.py enable "${CLAUDE_SESSION_ID}"` line byte-identical, the
  relay-instruction body line byte-identical, file still 7 lines. Verified by
  `yaml.safe_load` on the frontmatter (parses; `description` carries no newline)
  and by `git diff -- .claude/commands/yolo.md` (one changed line, no added line).
- **Scope note:** the reviewer marked this "follow-up, not a blocker" because
  §4(b) names only `yolo-off.md`. Taken here because this agent's brief is to
  resolve the two LOW issues, and because it is the same class of fix as the
  `yolo-off.md` edit the task *did* require — the warning currently lives only in
  the file the operator must already have disabled to read. No behaviour changes:
  the text is documentation read by the model relaying `/yolo`'s output.
- **No test pins this string** (`grep -rn "Disable with /yolo-off"` over the repo
  hits only `yolo.md` itself), so the edit carries no suite impact.

## Issue 2 — LOW — the module docstring's action-mapping table omits `yolo`

- **Status: ACTIONABLE, fixed.** (Pre-existing, not introduced by 40-03 — confirmed
  against `git show 79c4857:...:20-27`, same table, same omissions.)
- **File:** `.claude/hooks/permission_request_hook.py:20-31` (module docstring).
- **Fix applied:** added the `yolo` row and corrected the `whitelist` row. The
  `whitelist` line is **relabelled, not deleted**: it is not merely stale, it is
  inverted. `build_output_decision` has no `whitelist` branch at all (the branches
  are `allow`/`deny`/`stop`/`yolo`/`reply`/`answer`/`deny_mixed_roles`, then the
  unknown-action `return None`), and `relay_answer_to_decision` never emits it
  (`:1207` gates on `("allow", "deny", "stop", "yolo")`) — so it falls to the
  terminal like any unknown action, and `yolo` is now the one action that
  actually emits `updatedPermissions`. The corrected row names the real writer
  (`process_whitelist_update`, settings files) so a reader grepping for "who
  emits `updatedPermissions`" lands on `yolo`.

  Final table rows (`:20-31`):

  ```
  - yolo      -> behavior: "allow" + updatedPermissions (setMode
                 bypassPermissions, session), and flags the session in
                 ``session_yolo_store`` as the fallback
  ...
  - whitelist -> *not* a decision this hook builds; it has its own writer path
                 (settings files, ``process_whitelist_update``) and falls through
                 to the terminal like any unknown action
  ```

  `whitelist` was kept last so the docstring's ordering matches the function's
  branch order for the real actions. The `resolved_terminal` row was left as the
  reviewer found it: the reviewer named only the `whitelist` line for change, and
  `resolved_terminal` *is* a real `RequestState` value in this epic's store
  (`_ACTION_TO_STATE`-adjacent; the docstring means the store's terminal
  resolution, not a `build_output_decision` action) — changing it would be scope
  expansion beyond the worklist.
- **Runtime impact: none.** Docstring only. `build_output_decision`'s body is
  byte-identical to the pre-fix working tree (the diff hunk for the docstring ends
  at `:32`; the next hunk is the pre-existing 40-03 comment block at `:357`).

## Files changed by this fix pass

| File | Δ | What |
|---|---|---|
| `.claude/commands/yolo.md` | +1/−1 | description gains the mode caveat (Issue 1) |
| `.claude/hooks/permission_request_hook.py` | +7/−1 | docstring table: `yolo` row added, `whitelist` row corrected (Issue 2) |

No other file in the working tree was touched by this pass; the 40-03
implementation's four files (`permission_request_hook.py` payload/comment,
`yolo-off.md`, `tests/test_integration_permission_request.py`,
`state.md`) are otherwise as the reviewer saw them.

## Verification

- **Byte-compile (required — a `.py` file was touched):**
  `python3 -m py_compile .claude/hooks/permission_request_hook.py` → exit 0.
- **Module (required):**
  `python3 tests/run_all_tests.py --module integration_permission` →
  **Ran 188 tests in 11.736s — OK**, matching the review's baseline count for
  this module (188 after the 6 new tests; 182 before).
- **Full canonical suite:** `python3 tests/run_all_tests.py` →
  **Ran 2002 tests in 322.558s — OK (skipped=2)**, exit 0, both skips the named
  `test_all_hook_modules_import` and `test_headless_spawn`, matching the
  review's baseline (2002 / skipped=2) exactly. (Canonical runner only; pytest
  never used.)
- **Payload unchanged by this pass (direct check):** importing the edited module
  and calling `build_output_decision({"action": "yolo"}, req)` with
  `session_yolo_store.enable` patched produces a `json.dumps` string **equal** to
  the review's byte-for-byte expectation, and `enable` is still called with the
  session id (`call('sess-abc')`). This is the positive control that the
  docstring edit changed no executable line.
- **`yolo.md` shape:** frontmatter parses via `yaml.safe_load`; two keys;
  `description` has no embedded newline; file is 7 lines; the `!`-bash line and
  the relay-instruction line are unchanged.
- **Diff hygiene:** `git status --short` shows the same five modified files as
  before this pass (`yolo-off.md`, `yolo.md`, `permission_request_hook.py`,
  `state.md`, `test_integration_permission_request.py`), no untracked files, no
  mutations left behind. Uncommitted, as the brief requires.

## Residual concerns

1. **`yolo.md` was outside §4(b)'s named scope list.** The fix here is
   documentation-only and matches the reviewer's own suggested remedy, but if the
   manager prefers to hold scope strictly to §4(b), this one line is revertible in
   isolation without touching the rest of 40-03.
2. **The claim in the new `yolo.md` sentence is not machine-verified by this
   pass.** That a session in `bypassPermissions` leaves it only via Shift+Tab or
   session end is the same claim the task's §3 premise and the already-shipped
   `yolo-off.md:7` rest on, and the reviewer reports having verified Shift+Tab
   reaches `default` by hand. Proving the caveat end-to-end remains 40-04's live
   six-mode pass, as the review recommends.
3. **`whitelist`'s docstring row is now correct but the underlying path is still
   write-only from this repo's perspective** — `process_whitelist_update` has no
   in-repo caller (`grep` finds only its definition), consistent with it being
   driven by the epic-22 permissions MCP. The corrected wording says so rather
   than implying this hook emits it.
4. The reviewer's Code Quality notes 1–7 and notes 3/5 (the narrower
   `test_other_actions_carry_no_mode_update` loop; the two distinct pre-existing
   `--quick` failures) were deliberately **not** actioned — they are not in the
   Issues list and are explicitly outside this pass's worklist.
