# Fix report 2 - 50-08 (second pass)

All five issues fixed. Lib restored once from /tmp/mut_bk (md5 a811e083, the pre-fix lib) after one of my edits clobbered a wrong range; all edits redone and verified.

## 1. MEDIUM: deferred infrastructure checks
- Code already kept things unchanged: a skipped check gives `None`, no condition, and `run_alarms(clear=not (partial or infra.deferred))`. No code change.
- Tests (TestTickTiming): `test_a_deferred_unit_check_is_unknown_not_down` (ensure_unit/check_infrastructure with fits refusing: None, no start, no listener_down/inhibitor_down, `deferred`),
  `test_a_pass_that_defers_the_infrastructure_raises_no_listener_or_inhibitor_alarm` (tick, INFRA_END_S=0),
  `test_a_pass_that_defers_the_infrastructure_leaves_an_active_alarm_as_it_was` (active alarms byte-equal across two deferred passes, even past the 4 h repeat; no notify; clear once looked at again).

## 2. MEDIUM: unread sessions in reconcile
- No code change. Tests: `test_a_session_the_deadline_left_unread_is_not_dead_resumed_or_alarmed_and_keeps_its_state` (readings budget 12 s so sessions are refused but amux fits, and 0 s: no resume/spawn/send, sessions and step records unchanged, seeded session_stuck/verdict_missing not cleared or repeated, no new alarm, no notify; next pass resumes both),
  `test_take_readings_flags_each_kind_of_session_the_deadline_left_unread` (refuses exactly one read at a time: record, parked, foreign; plus amux-only refusal).
- Note: the first version of these tests let the amux flag mask the per-session flags (site 1-3 mutants survived); fixed by refusing sessions only.

## 3. MEDIUM: stamp_mismatch import closure
- `.claude/bin/epic-driver`: `NOTIFY_IMPORT_CLOSURE` next to STAMPED_FILES = questions_mcp_lib.py, questions_store.py, questions_git.py, questions_listen_lib.py, questions_ops.py, roles_config.py. `unsafe` is true if mismatched is None or any closure name is mismatched.
- Tests (TestStamps): ast-derived transitive import closure of questions_mcp_lib (all import statements, nested too; hooks, questions-mcp and statusline dirs) must be a subset of the list, and the list a subset of STAMPED_FILES; each closure member mismatched blocks the import (subTest each); only non-closure modules mismatched still notifies.

## 4. LOW: CLAUDE_HOOKS_REPO and the notify thread
- `questions_mcp_lib` reads it at import only, so variant "around the import alone": new `RealWorld.prepare_notify()` imports once in the calling thread with the variable set, restores it right after (before any thread). `Notifier.__init__` calls it (main thread). `notify` itself never touches os.environ. `tick_stamp_mismatch` (no threads) unchanged.
- Tests: import happens once with the repo set in the main thread and the env is restored; the notify thread sees no variable and os.environ is unchanged; no child env contains it after a notify. The old set-around-call test was replaced.

## 5. LOW: enable rollback and a pre-existing pointer
- `cmd_enable` keeps the prior bytes and mode before gate 9; on Refused restores them via new `atomic_write_bytes`, or unlinks if absent. Also fixed a latent crash found by the test: a non-UTF-8 pointer made gate 9 raise UnicodeDecodeError (now `(OSError, ValueError)`).
- Tests: restored byte for byte plus mode for both gate-10 refusals (non-UTF-8, no trailing newline, mode 0640), no tmp files left; absent pointer is removed; the old "already there" test now asserts exact bytes.

## Mutations (cp backup to /tmp/fix2_lib_bk, /tmp/fix2_bin_bk; restored by cp, diff clean, md5 equal at the end)
| mutation | result |
|---|---|
| `if not l_ok:` | caught (3 tests) |
| `if not i_ok:` | caught (3 tests) |
| extra: clear alarms on deferred infra | caught (1) |
| all 4 `incomplete = True` in take_readings removed | caught (4) |
| each of the 4 sites individually (record, parked, foreign, amux) | each caught (3, 1, 1, 2 tests) |
| unsafe uses only questions_mcp_lib.py | caught (5 subtests) |
| roles_config.py dropped from the list | caught (ast closure test) |

## Counts
- py_compile OK: .claude/bin/epic-driver, epic_driver_lib.py, questions_mcp_lib.py, test_unit_epic_driver.py, test_unit_questions_mcp.py.
- unit_epic_driver: Ran 485 OK (473 before, +12). unit_questions_mcp: Ran 56 OK.
- Full suite, foreground, last: `Ran 3030 tests in 436.344s` / `OK (skipped=2)`, exit code 0 (baseline 3018 + 12).

## git status --short
```
 M .claude/hooks/epic_driver_lib.py
 M questions-mcp/questions_mcp_lib.py
 M tasks/50_epic_driver/architecture.md
 M tasks/50_epic_driver/state.md
 M tests/test_unit_epic_driver.py
 M tests/test_unit_questions_mcp.py
?? .claude/bin/epic-driver
```

## Questions for User

None.
