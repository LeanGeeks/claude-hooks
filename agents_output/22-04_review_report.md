# Review Report — 22-04 Allowlist writers + the proposal queue

## Verdict: PASS

No BLOCKER or HIGH issues found.

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| `resolve_project_key` in `project_key.py`, worktrees → main checkout | **met** | `.claude/hooks/project_key.py` lines 76–116; `--git-common-dir` → parent when basename is `.git` |
| One project-identity function, every caller imports it | **met** | `permissions_mcp_lib.py:78-82` imports from `project_key`; `permission_queue.py` docstring explicitly says it does not compute keys |
| Queue format: `~/.claude/permission-queue/<key>/<ts>_<8hex>.json`, atomic, move-to-processed | **met** | `permission_queue.py` lines 128–177; dot-prefixed tmp, same-dir rename |
| `settings_writer.py` generalises the inline router writer, parameterised by file + list | **met** | `settings_writer.py`; router delegates at `telegram_permission_router.py:908-914` |
| `allowlist_add` MCP tool: validate → deny-collision → own-write or enqueue | **met** | `permissions_mcp_lib.py:980-1141` |
| `report_parser_issue` always enqueues to claude-hooks key | **met** | `permissions_mcp_lib.py:1175-1176`; no own-workspace shortcut |
| New tools in `.claude/settings.json` allow list | **met** | `.claude/settings.json:88-89` |
| `settings_writer.py` + `project_key.py` in `REQUIRED_HOOKS` | **met** | `install-claude-config.sh:163` |
| H6 note in every user-scope result | **met** | `permissions_mcp_lib.py:1069` appends `H6_NOTE` to `notes` list in the returned dict (live tool output) |
| Telegram Whitelist button unchanged (settings.local.json, allow) | **met** | `telegram_permission_router.py:908-914` passes `LOCAL_SETTINGS`, `"allow"` |
| 9 test cases, real temp git repos + worktrees | **met** | `tests/test_unit_allowlist_queue.py`; cases 1-9 present |
| Suite green, counts reported | **met** | Ran locally: **1020 passed, 0 failed, 1 skipped** (baseline was 984, +36) |

---

## Issues Found

None at BLOCKER or HIGH.

### Issue 1: LOW — case 4 does not verify the caller's own key queue is empty

- **File:** `tests/test_unit_allowlist_queue.py:339-342`
- **Problem:** The test asserts `caller_repo/.claude` does not exist (filesystem level), and that `queue_entries(caller_key) == []`. Both are present, so this is adequately covered. Flagging only because the absence of a settings file is slightly weaker than asserting no write happened at all — but the queue check for the caller's own key is the meaningful assertion and it is there.
- **Fix:** No change needed; the combination of both assertions is sufficient.

### Issue 2: LOW — permission_queue.py third module: sensible factoring, not scope creep

- **File:** `permissions-mcp/permission_queue.py`
- **Observation:** The task's scope clause named two new hook modules (`project_key.py`, `settings_writer.py`) and did not name this module. The implementer created it as a third module in `permissions-mcp/`.
- **Judgment:** This is **sensible factoring**. The queue IO (directory layout, atomic write, filename generation, processed-move) is a distinct concern from pattern validation and business logic. Separating it allows 22-05's drain side to `import permission_queue` without pulling in the full MCP lib. Keeping it in `permissions-mcp/` (not hooks) means it does not need to be in `REQUIRED_HOOKS`, which is correct — it is not a hook. The module explicitly states it does not compute project keys, deferring to `resolve_project_key` from callers, so it does not duplicate the identity function (invariant 7 preserved).

---

## Verification of Specific Points

**1. Invariant 4 / D6 — provenance rule (most important).**
Every write path verified: `allowlist_add` at line 1071 writes only `own_root` (the caller's `resolve_workspace_root`), which is always the caller's own checkout. Foreign targets (line 1123) call `permission_queue.enqueue` only. `permission_queue.py` performs no filesystem operations on the target checkout. Grep for `git ` in non-`project_key` modules: only string literals in docstrings/messages, no subprocess invocations. No git edit/commit/push/checkout anywhere outside `project_key.py`. **CLEAR.**

**2. Invariant 7 — one project-identity function.**
`resolve_project_key` exists only in `project_key.py`. All callers (`permissions_mcp_lib.py:78-82`, and `permission_queue.py` which explicitly defers) import from there. No hand-rolled `/`→`-` encoding or `realpath`-as-key found elsewhere in the diff. **CLEAR.**

**3. D7 worktree behavior.**
Test case 1 (`test_worktree_and_main_checkout_share_one_key`) uses a real temp repo and a real `git worktree add` via `_git(repo, "worktree", "add", ...)`. Both paths produce the same key. The `--git-common-dir` → `.git` basename condition is at `project_key.py:92-94`. The fallback for non-`.git` basenames is at line 89 (`root = _realpath(key)`). Case 5 test at line 343 uses a worktree caller targeting a foreign worktree and confirms the entry lands under the main checkout's key (line 368-372 verifies the worktree path key is different and has no queue dir). **CLEAR.**

**4. Refusals before any disk effect.**
Order in `allowlist_add`: parse_pattern → scope check → rationale check → deny-collision check → then (and only then) write or enqueue. All three refusal paths return before any disk operation. `find_deny_collision` reads files but does not write. Deny collision refusal at line 1056 includes `result["deny_entry"] = collision` (the offending entry named). Collision check uses `_build_validator(settings_dir)` → `SettingsLoader` (merged settings). **CLEAR.**

**5. Atomicity.**
`settings_writer.py:179-183`: `temp_path = path.with_suffix(".json.tmp")` — same directory as destination, rename is same-filesystem. `permission_queue.py:140-144`: `temp_path = target_dir / f".{name}.tmp"` — same directory. Both clean up on exception. **CLEAR.**

**6. Router delegation byte-compatible.**
`telegram_permission_router.py:908-914` passes `settings_filename=settings_writer.LOCAL_SETTINGS` and `list_name="allow"`. Return is `result.ok` (bool), identical to the old signature. Behavior is byte-identical for well-formed files. Malformed-input corner differs only in not raising `TypeError` (now returns `False`) — a safe improvement. The new test `test_router_writes_local_settings_and_never_versioned` (visible in grep output line 250 of the report) confirms the router still writes `settings.local.json`. **CLEAR.**

**7. H6 note in actual tool output.**
`H6_NOTE` at line 829 is appended to `notes` at line 1069, which lands in the returned dict's `"notes"` key — the live tool response, not a docstring. Case 8 test asserts `any("install-claude-config.sh" in note for note in result["notes"])`. **CLEAR.**

**8. `report_parser_issue` always enqueues.**
`permissions_mcp_lib.py:1175-1185`: resolves `repo = claude_hooks_repo()`, then `target_key = resolve_project_key(repo)`, then `enqueue`. No own-workspace shortcut. **CLEAR.**

**9. Test isolation.**
`AllowlistQueueTestCase` sets `CLAUDE_PERMISSION_QUEUE_DIR` to a temp dir at line 100 (`"HOME": str(self.home)` and the queue dir env). After running the full suite, `~/.claude/permission-queue/` does not exist. **CLEAR.**

**10. Test quality.**
- Case 3 dedupe: reads the actual settings file and asserts `count == 1` (line 237-238) — cannot pass against a broken deduper.
- Case 4: asserts both that the target checkout has no `.claude/` and that the caller's own key queue is empty (line 337-342) — covers both sides of D6.
- Cases 6/7: assert `result["reason"]` contains specific text (`"deny → ask → allow"`, `"not a permission pattern"`, `"rationale is required"`) — not just that an exception was raised. **CLEAR.**

**11. Scope.**
Only `permissions-mcp/`, new hook modules, router delegation, `.claude/settings.json`, `install-claude-config.sh`, and tests were modified. No stray files. `REQUIRED_HOOKS` correctly lists `settings_writer.py` and `project_key.py`. The two new `mcp__permissions__*` grants are at lines 88-89 of `.claude/settings.json`. **CLEAR.**

---

## Code Quality Notes

- `resolve_workspace_root` (a second function in `project_key.py`) is correctly documented as distinct from identity: it returns the worktree's own top-level, used as the write target, not the queue key. This is correct and necessary for invariant 4.
- `permission_queue.py` dot-prefixes temp files (`.foo.json.tmp`) so a concurrent glob on `*.json` never sees them — a nice defensive touch.
- `SettingsWriteResult` dataclass is a clean API improvement over the old bare `bool` return; callers get `added` to distinguish "written" from "already there".
- The deny-collision logic documents why it uses `BashPermissionValidator._matches_pattern` rather than `validate_bash_command` (the full path short-circuits before consulting the deny list for some shapes). The reasoning is sound.

## Questions for User

None.
