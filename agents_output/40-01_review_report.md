# Review Report - 40-01: The hook defers to the session's permission mode

Reviewer: independent review pass, 2026-09-21. Baseline commit `b57bc8f`; all
implementation changes reviewed as the uncommitted working tree (`git diff`).
Every load-bearing claim in `agents_output/40-01_implementation_report.md` was
re-verified against the code, the runner output, and the installed CLI bundle.

## Verdict: PASS

No BLOCKER or HIGH issues. All four edits are in, all 21 specified test cases
are covered (25 tests delivered), the canonical runner is green at the claimed
counts, the two skips are identical to baseline by name, the mutation proof was
independently reproduced, and file scope is exactly as required. Two LOW issues
(test portability), one LOW report-accuracy nit, and one informational note on
a pathological-path ordering nuance — none block.

## Completeness Check

### Edit 1 — additive `ask_kind` + `redirect_targets` on `validate_bash_command` — MET

- `ask_kind` assigned `'risk'` at the `permissions.ask` site
  (`.claude/hooks/pretool_hook.py:725`), `'risk'` at the write-redirect site
  (`:732`, with `redirect_targets = list(disallowed_targets)`), `'unknown'` at
  the not-in-allowlist site (`:753`), `None` on deny (`:721`), allow (`:737`)
  and every other path. Both keys present on every path via the return dict
  (`:763-764`) — stable shape as required.
- `decision` values unchanged: only `allow` / `deny` / `ask` are ever assigned
  (`:719`, `:723`, `:730`, `:735`, `:747`). `permissions_mcp_lib.py:310-352`
  (the epic-22 guard that raises on a fourth value) is untouched
  (`git diff --stat -- permissions-mcp/` is empty) and I re-read all three of
  its branches — deny (`:326`), ask (`:334`), allow (`:347`) — plus the raising
  fallback (`:353`). All reachable; the additive keys are invisible to it.
- The other external consumer, `telegram_permission_router.py:413`, reads only
  `validation_results` (`:427-439`) — also unaffected.
- `redirect_targets` structured as required (case 15b: `['/etc/passwd']` while
  the same targets stay in `reason` — asserted in
  `tests/test_integration_pretool.py:1636-1645`).
- The docstring now documents both keys (`pretool_hook.py:611-617`) and fixes a
  pre-existing error — the docstring at HEAD claimed `decision: 'allow' |
  'deny' | 'defer'` (verified with `git show HEAD:`), which was never the true
  vocabulary. Legitimate correction, not scope creep.

### Edit 2 — one emission helper used by both callers — MET

- `DEFERRING_MODES = frozenset({'auto', 'bypassPermissions', 'dontAsk'})` at
  `pretool_hook.py:443`; `build_pretool_output` at `:446-499`, pure, with the
  task's five rules in order: deny first and never mode-aware (`:469-475`),
  allow (`:476-482`), defer only on `ask_kind == 'unknown'` **and** mode in
  `DEFERRING_MODES` (`:485-492`, reason included on the defer payload as the
  task asks), all other asks → ask (`:493-499`), anything else → `None`.
- `main()` routes through it (`:2177`) and `replay_from_log` routes through it
  (`:571`). `grep -n permissionDecision pretool_hook.py` shows the payload is
  now constructed in exactly one place (inside the helper); the hand-built
  duplicates in `main()` and `replay_from_log` are gone.
- `permission_mode` read from the payload at `:2142`
  (`input_data.get('permission_mode', '')`), alongside the existing
  `tool_name` / `session_id` reads. Absent → `''` → not in the frozenset →
  rule 4 → `ask`. Explicit `null` → `None` → membership is `False` with no
  exception (probed directly). Unrecognised string → same.
- `replay_from_log --mode` added next to `--dry-run` (`:521-526`), choices =
  the six modes, `default=None`; resolution at `:570`: `--mode` wins, else the
  entry's recorded `permission_mode`, else `'default'` — exactly the task's
  fallback chain, so pre-40-01 log lines replay as `default`/ask.

### Edit 3 — confirm log records what was emitted; main() reordered — MET

- `log_manual_confirmation` signature extended with
  `permission_mode=None, output=None` (`:399-400`); entry gains
  `'permission_mode': permission_mode or None` (`:429`) and
  `'emitted'` (`:430`, derived from the built payload, `None` when nothing was
  printed). `decision` key unchanged (`:425`) —
  `permissions_mcp_lib.py:594`'s deny filter still works (re-read; the filter
  is on `entry.get("decision") == "deny"` and `decision` is untouched).
- Reordering done as the task specifies: payload computed first (`:2177`),
  then the log guarded by `result['decision'] != 'allow'` (`:2181-2183` — the
  condition is preserved), then print. All four debug lines present:
  `ALLOWING` (`:2193`), `DENYING` (`:2202`), `DEFERRING` (`:2206`, new),
  `ASKING` (`:2210`).

### Edit 4 — `MANUAL_CONFIRM_LOG` honours `CLAUDE_MANUAL_CONFIRM_LOG` — MET

- `pretool_hook.py:50-53`, read at module import, `or`-fallback to
  `expanduser('~/.claude/bash_manual_confirm.log')` — same pattern as
  `permission_state_store.py:52-53` and `permissions_mcp_lib.py:551-552`.
- Name preserved; `permissions_mcp_lib.py:83` still imports it by name
  (`from pretool_hook import MANUAL_CONFIRM_LOG, BashPermissionValidator`) and
  `--module unit_permissions_mcp` is green (42/42, run by me).
- Case 17 asserts against the real path by stat (size + mtime_ns) across a
  hook run that logs — I confirmed the real log exists (7.09 MB) and that the
  runner sets the redirect (`tests/run_all_tests.py:35`).

### Edit 5 (do-not-touch) — MET

`install.sh` untouched (`git diff --stat -- install.sh` empty); matcher still
`Bash|Monitor` (`install.sh:287`). `BashCommandParser`, allow/deny matching,
`_is_redirect_target_allowed`, `permission_request_hook.py`: no changes in the
diff. `tests/fixtures.py` untouched — its mode-less payloads remain the case-1
regression surface.

### Tests — MET (25 added; all 21 task cases covered)

New class `TestPermissionModeParity` (`tests/test_integration_pretool.py:1563-1879`).
Task case → test mapping, all verified present and asserting real values:
1→`:1670`, 2→`:1678`, 3→`:1683`, 4→`:1689`, 5→`:1695`, 6→`:1700` (subTest both
modes), 7→`:1707`, 8→`:1713`, 9→`:1719`, 10→`:1725`, 11→`:1730` (validator AND
payload level, with a temp settings dir), 12→`:1750`, 13→`:1758`, 14→`:1763`,
15→`:1613` (all five decision paths via subTest), 15b→`:1636`, 16→`:1780` (+16b
`:1799` for the default/ask recording), 17→`:1815` (real-path stat), 17b→`:1770`
(Monitor), 18→`:1833` (+18b `:1856` pre-40-01 entry replays as default/ask,
18c `:1873` recorded mode honoured). Assertions check actual
`permissionDecision` values, log-entry fields and exit codes — none vacuous.
Payload tests run the hook as a real subprocess, matching the suite's
established pattern. No skips added.

### Done criteria

1. `py_compile` clean — re-run by me on both files: OK.
2. Full runner green with counts reported — re-run by me (below): 1966 / OK
   (skipped=2) / exit 0. Before/after reported by the implementer against the
   state.md baseline, matching my own runs.
3. `validate_bash_command` consumers verified as above (criterion met).
4. Both callers route through `build_pretool_output`; no duplicated
   construction (verified by grep, criterion met).
5. Not-live caveat stated in the implementation report ("Nothing in this
   report is live"); no install run — correct per epic instructions.
6. Scope: working tree contains exactly `.claude/hooks/pretool_hook.py`,
   `tests/test_integration_pretool.py`, and
   `tasks/40_permission_mode_parity/state.md`. The state.md diff is the
   manager's (40-01 → in_progress; 40-03 operator decision) — not implementer
   edits, verified line by line. `agents_output/` is gitignored. No other
   changes exist (`git status --short --untracked-files=all` clean otherwise).

### Invariant checks (all hold)

1. **Deny never mode-aware** — deny branch is first in `build_pretool_output`
   and never reads `permission_mode` (`:469-475`); tests 8/9/14 pin it.
2. **Validator vocabulary frozen** — three values only; `permissions_mcp_lib`
   guard untouched and all branches reachable (re-read, above).
3. **Absent/empty/unrecognised mode ⇒ ask, exit 0** — `''`/`None`/unknown all
   fail membership and reach rule 4; the hook's outer `except Exception` →
   `sys.exit(0)` (fail-open, `:2218-2223`) backstops any pathological payload.
4. **Risk gates ask in every mode** — defer requires `ask_kind == 'unknown'`;
   both risk sites set `'risk'` (`:725`, `:732`); tests 11/12 assert ask in
   `auto`, including via the real settings path.
6. **Only the unknown case defers** — structural in the helper; tests 3/4/5/13
   vs 11/12/6/7 draw the line.

### Independent verification performed

- **Test runs (canonical runner only):** `--module integration_pretool` →
  **404 OK** (73.6 s; baseline 379, +25). `--module unit_permissions_mcp` →
  **42 OK**. Full suite → **1966 tests, OK (skipped=2), exit 0** (304 s) —
  matches the implementer's claim exactly (baseline 1941).
- **The two skips, by name** (from my own clean full-suite log):
  `test_all_hook_modules_import`
  (`test_unit_python_compat.TestImportUnderOldInterpreter`) — "no pre-3.11
  interpreter with tomli available"; `test_headless_spawn`
  (`test_unit_amux_spawn.TestLiveSpawn`) — "live spawn test (needs tmux +
  model auth); set AMUX_SPAWN_LIVE_TEST=1". Both are environment skips that
  predate this task; the skip count did not move and no test stopped running.
- **Mutation proof (state.md invariant 8) — independently reproduced.** I
  chose the implementer's mutation 2: removed `'auto'` from `DEFERRING_MODES`
  (cp-backup to `/tmp/pretool_hook.py.mutation_backup`, md5-verified before
  mutation). Result: **exactly 6 failures** — `test_case3_auto_mode_defers`,
  `test_case13_mixed_allow_unknown_in_auto_defers`,
  `test_case16_confirm_log_records_mode_and_emitted`,
  `test_case17b_monitor_unknown_in_auto_defers`,
  `test_case18_replay_with_mode_flag_defers`,
  `test_case18c_replay_honours_recorded_mode` (404 ran, failures=6). This
  matches the implementer's mutation-2 result test-for-test. Restored via
  `cp` from the backup, md5-verified identical, `git status --short` back to
  the expected three files, diff stat restored (145+/59-). Never used git
  restore/checkout. The first full-suite run I had started was invalidated by
  my own mutation and discarded; the reported 1966/OK run is from the clean
  tree afterwards.
- **CLI bundle claims (brd §1.5/§1.6, H3)** — re-verified against
  `~/.local/share/claude/versions/2.1.278`: `hookAskFloor` present (5 hits);
  schema `"allow","deny","ask","defer"` present; dispatch arm
  `case"defer":B.permissionBehavior="defer";` present; the parse-error text
  `Unknown hook permissionDecision type: … Valid types are: allow, deny, ask,
  defer` present. No evidence contradicting brd §1; H3's degradation path
  remains designed-for rather than reproduced, as brd itself labels it.
- **Import check:** module imports clean outside the runner;
  `MANUAL_CONFIRM_LOG` resolves to the real path without the env override and
  `DEFERRING_MODES` is exactly the three specified modes.

## Issues Found

### Issue 1: [severity: LOW]

- **File:** tests/test_integration_pretool.py:1577-1582 (`_run_payload` / `WS`)
- **Problem:** The payload-level tests run the hook with
  `CLAUDE_WORKSPACE_DIR=/ws/project` (a nonexistent directory), so
  `SettingsLoader` resolves only the operator's **global**
  `~/.claude/settings.json`. Cases 10 (`echo hi` → allow), 8/9 (`dd …` →
  deny) and 1 (`frobnicate` → ask) therefore depend on that machine-specific
  file containing `Bash(echo:*)` in allow and `Bash(dd:*)` in deny (both
  confirmed present today). Existing payload tests avoid this by using the
  repo workspace, whose `.claude/settings.json` is tracked. Note the
  failure mode is a loud false alarm on a foreign machine (a missing pattern
  turns case 10 into `ask` and the test fails), never a silent false pass.
- **Fix:** Point the payload tests' workspace at a temp dir seeded with a
  minimal `settings.json` (as case 11 already does), or reuse the repo
  workspace like `TestPreToolUseHookProcess`. Nice-to-have; the tests are
  honest on this machine and the repo is single-operator.

### Issue 2: [severity: LOW]

- **File:** tests/test_integration_pretool.py:1819 (`test_case17_…`)
- **Problem:** `real_log.stat()` raises `FileNotFoundError` (an ERROR, not a
  clean assertion) if `~/.claude/bash_manual_confirm.log` does not exist. It
  exists here (7.09 MB), so the test is fine today; it is only brittle on a
  machine without the real log.
- **Fix:** `if not real_log.exists(): self.skipTest(…)` or touch the file in
  the test before stat. Cosmetic robustness only.

### Issue 3: [severity: LOW] — report accuracy, not code

- **File:** agents_output/40-01_implementation_report.md ("Decisions" section)
- **Problem:** The note claims `_run_payload`'s `CLAUDE_WORKSPACE_DIR`
  override makes payload tests "hit the intended temp settings dir rather
  than the repo's own allowlist". That is true only for case 11, which seeds
  a temp settings dir; the rest of the payload tests hit `/ws/project` and
  therefore the operator's global settings (see Issue 1).
- **Fix:** Reword the note; no code change.

### Informational (not an issue)

In `main()` the confirm-log write now happens **after**
`build_pretool_output` (necessarily, since `emitted` must exist). If the
payload's `permission_mode` were an unhashable JSON value (e.g. an object),
frozenset membership raises `TypeError`, the outer handler swallows it and
exits 0 — fail-open, invariant 3 intact — but in that pathological path the
confirm-log line is also skipped (pre-change, the log write preceded any
emission logic and would have survived). Only reachable with a malformed
payload; the outer except guarantees the hook never blocks. No action needed.

## Code Quality Notes

- `build_pretool_output` is a clean, pure, deny-first mapping with an accurate
  docstring; the reason-on-defer detail is implemented as specified.
- `Optional[...]` typing (not PEP 604) matches the repo's Python-3.9 floor —
  consistent with `test_unit_python_compat`'s old-interpreter import gate.
- The pre-existing wrong docstring (`'allow' | 'deny' | 'defer'`) is now
  correct — good catch, correctly scoped.
- Test helper re-use is good: `_payload`/`_run_payload` are local, minimal,
  and subprocess-based like the rest of the suite; temp settings cleaned in
  `finally`; no network, no real bot, no real log writes.
- `permission_mode or None` in the log entry means an explicitly-empty
  payload value is stored as `None` rather than `''` — acceptable, and
  `emitted: None` correctly captures the silent-fallback case too.
- Coverage gap (acknowledged by design): there is no direct test that
  `default`-mode output is *byte-identical* to the pre-change payload; case 1
  plus the ~379 untouched pretool assertions serve that role, which is the
  task's own stated approach.

## Questions for User

None. The only judgement call embedded above (Issue 1's machine-dependent
global-settings reliance) is a test-robustness preference, not something that
needs an operator decision to accept this task.
