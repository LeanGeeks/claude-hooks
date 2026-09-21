# Review Report 2 (confirm pass) - 40-01 fix-up of the three LOW issues

Reviewer: independent confirm pass, 2026-09-21. Working tree re-diffed with
`git status --short --untracked-files=all` and `git diff` (never
`git diff HEAD~1`; no repo-wide git command, no install, no commit). Fixer
claims checked against `agents_output/40-01_fix_report.md`.

## Verdict: PASS

All three LOW issues are resolved, and I reproduced each fix rather than
accepting the description. No new issues. The canonical runner is still green
at the same counts, and the two skips are unchanged by name.

- `integration_pretool`: **404 OK** (80 s) — same count as before the fix-up.
- Full suite: **1966 tests, OK (skipped=2), exit 0** (272 s).
- The two skips, from my own run's log, identical to baseline:
  `test_all_hook_modules_import`
  (`test_unit_python_compat.TestImportUnderOldInterpreter`, "no pre-3.11
  interpreter with tomli available") and `test_headless_spawn`
  (`test_unit_amux_spawn.TestLiveSpawn`, "live spawn test (needs tmux + model
  auth); set AMUX_SPAWN_LIVE_TEST=1").
- Scope unchanged: `git status --short` shows the same three files
  (`pretool_hook.py`, `state.md` — the manager's, `test_integration_pretool.py`).
  `pretool_hook.py` is **byte-identical** to the revision I reviewed in report 1
  (md5 `1072c99e9a1e99c4564cd3a2cfca83eb` against my pre-mutation backup) — the
  fixer correctly did not touch the implementation. `git diff --numstat` for the
  test file is `361 0` (purely additive; the only diff hunks for this file are
  inside the new class).

## Issue 1 (was LOW) — payload tests depended on the operator's global settings — RESOLVED

- `TestPermissionModeParity.setUpClass` (`tests/test_integration_pretool.py:1577-1596`)
  now `mkdtemp`s a workspace and seeds `.claude/settings.json` with
  `{"allow": ["Bash(echo:*)"], "deny": ["Bash(dd:*)"]}`; `cls.WS` points at it,
  and `tearDownClass` (`:1598-1600`) removes it. The old `WS = "/ws/project"`
  is gone from this class.
- **Isolation independently probed, not taken on faith.** I ran the real hook
  as a subprocess against a workspace seeded exactly as `setUpClass` builds it,
  with `HOME` pointed at a temp dir containing **no** `~/.claude/settings.json`
  at all (verified `exists()` is False). Results: `echo hi`/auto → `allow`,
  `dd …`/auto → `deny`, `frobnicate`/no-mode → `ask`, `frobnicate`/auto →
  `defer`. All four matched. The seed alone therefore carries every pattern the
  payload cases assert; the tests no longer need anything from the global file.
  (The loader still merges global underneath — the fixer's own "Residual
  concerns" is candid about this — but that is now a merge of an
  *optional* layer, not a dependency: with the layer absent the assertions
  still hold.)
- **`TestWorkspaceRelativeBinary` was left alone.** Its `WS = "/ws/project"`
  still stands at `:1457`, and `git diff` on the test file contains no hunk
  mentioning `TestWorkspaceRelativeBinary` — it is used with `_FakeLoader`, so
  it never loaded settings and was correctly out of scope. Its tests are green
  in the 404.
- No leaked temp dirs: `ls /tmp/mode-parity-ws-*` is empty after the runs, so
  `tearDownClass` works.

## Issue 2 (was LOW) — `test_case17` raised `FileNotFoundError` on a log-less machine — RESOLVED

- `test_case17_confirm_log_respects_env_redirect`
  (`tests/test_integration_pretool.py:1837-1860`) now does
  `stat_before = real_log.stat() if real_log.exists() else None`.
- **Absent-log branch asserts what the coordinator asked and more.** When
  `stat_before is None` the test asserts, after the run that logs,
  `assertFalse(real_log.exists(), "the run created the real
  ~/.claude/bash_manual_confirm.log — CLAUDE_MANUAL_CONFIRM_LOG is not
  honoured")` (`:1846-1851`) — i.e. it asserts the hook did **not** create the
  log, then returns. That is strictly stronger than the `skipTest` I had
  suggested, and it cannot silently pass: the assertion is on the real path.
- **Present-log branch unchanged and still meaningful** (`:1852-1860`): size and
  `mtime_ns` must be identical across a run that logs. That is the branch this
  machine exercises (the real log exists, 7.09 MB), so the H7 guarantee is
  still actively tested here, not routed around.

## Issue 3 (was LOW) — inaccurate sentence in the implementation report — RESOLVED

- `agents_output/40-01_implementation_report.md:146-153`: the `_run_payload`
  bullet now states that `TestPermissionModeParity.setUpClass` seeds the temp
  workspace that the `CLAUDE_WORKSPACE_DIR` override points at (allow echo /
  deny dd), that the payload tests exercise that workspace's settings, that the
  global file is still merged underneath but no longer depended on, and it
  parenthesises the pre-fix state. This is factually accurate and matches the
  code and my probe. No code change, as instructed.

## New issues

None. Two informational notes, neither a defect:

1. The residual the fixer itself recorded is real but inherent and was already
   present before this task: `SettingsLoader` merges global → workspace → local,
   so a hypothetical global pattern that altered `echo`/`dd`/`frobnicate`
   handling could still perturb a case. It is not a regression and not fixable
   within this task's scope (it would mean stubbing the global path inside
   `SettingsLoader`). Worth remembering only if the suite is ever run on a
   machine with an unusual global settings file.
2. Cosmetic wording slip in `agents_output/40-01_fix_report.md`: the scope line
   describes the hook diff as "204+/61-", but `git diff --numstat` gives
   `145 59` for `pretool_hook.py` (204 is the combined stat-bar total). The
   substantive claim — the hook was not modified — is true and I verified it by
   md5, so this is a reporting arithmetic nit only.

## Questions for User

None.
