# Fix Report - 40-01 review issues

Date: 2026-09-21 · Scope: exactly the three LOW issues from
`agents_output/40-01_review_report.md`. No other code touched:
`.claude/hooks/pretool_hook.py` diff is unchanged (still 204+/61-), and the
working tree still contains only `pretool_hook.py`, `state.md` (manager's), and
`tests/test_integration_pretool.py` (now 331 → +30 lines of my fixes).

## Issue 1 — payload tests depended on the operator's global settings

- **Where:** `tests/test_integration_pretool.py`, class
  `TestPermissionModeParity` (was `WS = "/ws/project"` at :1576).
- **Chosen fix (different from both suggestions):** a class-level
  `setUpClass`/`tearDownClass` that seeds a temp workspace (mkdtemp
  `mode-parity-ws-`) with a minimal `.claude/settings.json` — allow
  `Bash(echo:*)`, deny `Bash(dd:*)` — and points `cls.WS` at it; the class
  docstring documents why. The review suggested "a temp dir seeded with a
  minimal settings.json (as case 11 already does)" — same direction, but I
  raised it to `setUpClass`/`tearDownClass` instead of per-test seeding so all
  ~20 payload tests of the class share one seeded workspace without each test
  growing `try/finally` scaffolding, and without switching to the repo
  workspace (which would still merge the tracked repo settings and could drift
  when someone edits that file; a seeded fixture pins exactly the patterns the
  cases assert).
- **Why not the review's other option** (reuse the repo workspace like
  `TestPreToolUseHookProcess`): the repo `.claude/settings.json` is a
  hand-maintained operator file with no `ask` patterns; seeding keeps the tests
  hermetic against both the global file (the review's complaint) and future
  edits to the repo allowlist.
- **Effect:** `SettingsLoader` still merges the global file underneath (it
  always merges global → workspace → local), but the tests now pass with an
  empty global file: the patterns cases 1/2/8/9/10 etc. rely on are supplied by
  the seed. Case 11 is unchanged (it already passes its own `workspace=`).
  Validator-level tests in the class keep using `_FakeLoader`, so only their
  `workspace_dir` changed (now an existing dir instead of a nonexistent one —
  irrelevant to those assertions). The other `/ws/project` constant
  (`TestWorkspaceRelativeBinary`, :1457) was NOT touched: it never loads
  settings (fake loader), so the review correctly scoped Issue 1 to this class
  only.

## Issue 2 — `test_case17` raised FileNotFoundError on machines without the real log

- **Where:** `tests/test_integration_pretool.py:1841-1855`
  (`test_case17_confirm_log_respects_env_redirect`).
- **Chosen fix (different from both suggestions):** branch on
  `real_log.exists()` instead of skipping or touching the file.
  - Log absent before the run → after the run assert the hook did NOT create
    it (`assertFalse(real_log.exists())`). This is strictly stronger than
    `skipTest`: on a log-less machine the H7 guarantee is "nothing appeared at
    the real path", and a silent skip would test nothing there. Touching the
    file was rejected because it would create a file in the operator's real
    `~/.claude/` from a test.
  - Log present (the common case on this machine) → unchanged behavior:
    size + mtime_ns asserted unchanged across a run that logs.
- **Effect:** the test asserts H7 meaningfully on every machine, ERROR-free,
  without skipping and without writing outside the sandbox.

## Issue 3 — inaccurate sentence in the implementation report

- **Where:** `agents_output/40-01_implementation_report.md`, Decisions
  section, the `_run_payload` bullet.
- **Changed:** reworded to the truth — the override points at a workspace
  seeded by `TestPermissionModeParity.setUpClass` (allow echo / deny dd), the
  payload tests exercise that workspace's settings, the global file is still
  merged underneath but no longer depended on, and a parenthetical notes the
  pre-fix `/ws/project` state. No code change (as the review instructed).

## Verification

- **Byte-compile:** `python3 -m py_compile tests/test_integration_pretool.py
  .claude/hooks/pretool_hook.py` — clean. (Hook byte-compiled as a control; it
  was not modified by the fixes.)
- **`python3 tests/run_all_tests.py --module integration_pretool`:**
  before fixes 404 OK (review's own run) → after fixes **404 OK** (84.5 s).
  Same count, no new failures.
- **`python3 tests/run_all_tests.py --module unit_permissions_mcp`:**
  **42 OK** (unchanged; `MANUAL_CONFIRM_LOG` import path untouched).
- **Full suite** (`python3 tests/run_all_tests.py`, run because a test file was
  edited and to confirm the two skips by name): before fixes 1966 / OK
  (skipped=2) exit 0 → after fixes **1966 tests, OK (skipped=2), exit 0**
  (312 s). Skips identical to baseline by name:
  `test_all_hook_modules_import` (test_unit_python_compat) and
  `test_headless_spawn` (test_unit_amux_spawn). No other tests consumed the
  `/ws/project` constant of this class (verified by grep; the unrelated
  `TestWorkspaceRelativeBinary.WS` is untouched and its suite is green).
- **Scope:** `git status --short` = the same three files as before the fix-up
  (pretool_hook.py, state.md, test_integration_pretool.py); no git mutation
  commands used at any point; `./install.sh` not run.

## Residual concerns

- The global settings merge remains a real merge: if the operator's global file
  ever contained a pattern that denies or asks on `frobnicate`/`dd`/`echo`
  differently than the seed, a payload test could still shift behavior. The
  seed makes the tests self-sufficient but not assertion-blind to the merge
  order; that is inherent to `SettingsLoader`'s global→workspace design and out
  of scope here (fixing it would mean stubbing the global path in tests, a
  change to SettingsLoader testability the review did not ask for).
- The review's Informational note (pathological unhashable `permission_mode`
  skips the confirm-log write in main()) was explicitly no-action-needed and
  was left as is.
