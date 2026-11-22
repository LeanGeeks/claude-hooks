# Review Report 2 - 50-01 amux-spawn flags and Claude resume

## Verdict: PASS

0 BLOCKER, 0 HIGH, 0 MEDIUM, 1 LOW. Compile is clean (`py_compile` of the CLI, lib, fake env, resume tests). `python3 tests/run_all_tests.py` ran in the foreground and exited 0. Its closing output did not print a summary line (the tail was a test's own output), so I re-ran `-m unit_amux_resume` (62 tests, OK) and `-m unit_amux_spawn` (101 tests, OK, 1 skipped). The implementer's fix report gives 2481, OK, 2 skipped for the whole suite. After my mutations, `git status` shows the same 9 modified files as at the start, and the CLI matches its `cp` backup byte for byte.

## Completeness Check (the three first-pass LOWs)

1. Growth check only with a prompt: **resolved as decided**. `grew = prompt is None or _transcript_grew(...)` (`.claude/bin/amux-spawn`, `_cmd_resume_claude`). The amended §13.3 step 5 says "when a prompt is given". The task, the reference doc and the help epilog ("With a prompt, exit 1 if ...") agree.
2. Handle write on no growth: **resolved**. The step-6 write now runs under a fresh `SpawnLock` with a re-read handle (`attempt` + 1, `state = "spawning"`, `updated_at`) before `if not grew:` prints the message and returns 1. Mutating the write to skip the bump when not grown fails `test_transcript_not_growing_exits_1_and_leaves_the_session`, which asserts `attempt == old + 1` and `state == "spawning"`. The test is meaningful.
3. Missing effort: **resolved**. Only a missing model refuses. `add_effort` is the handle's effort only when `CC_FLAGS` lack one, and is passed only when known. Mutating the code to refuse a missing effort again fails `test_resume_without_any_effort_source_proceeds_unpinned`. That test asserts the exact `amux start` argv (no `--effort`, no `--`) and the claude argv. `test_only_the_missing_one_is_added` and `test_refused_when_neither_source_has_a_model` are kept.
4. No `--`: the code, the `_amux_start_claude` docstring, the test argv (`["start", name, "--no-attach", "go on"]`) and `test_prompt_reaches_the_pane_as_one_argv_element` (asserts `"--"` is not in the claude argv) match the amended §13.3 step 4.

## Regression check

- **Lock handling**: the first `SpawnLock` block covers checks, the amux start and the launch confirmation, and every early return is inside it. It is released before the growth wait. The second block re-reads the handle, so hook writes made during the wait are not lost. Both failure returns inside it (the handle vanished, the write raised) leave the lock through the `with`. `new_attempt` is defined before any later use. The growth-failure path cannot reach the success output, because the `if not grew` return comes first.
- **Handle disappears or the write fails during the growth-failure path**: exit 1, and the message says the session is running. Acceptable, and consistent with the success path.
- **Codex branch**: unchanged. The dispatch to `_cmd_resume_claude` is gated by `is_claude_handle`, the missing-handle fallthrough is intact, and the Codex resume tests and `unit_amux_spawn` pass. The help text keeps the Codex paragraph.
- **Docs**: `docs/amux-spawn-agent-reference.md` states a missing model is refused, a missing effort is passed only when known, the prompt is not dash-prefixed, and the handle is still updated on no growth. The `resume --help` epilog agrees. `docs/amux-spawn-model-selection.md` is consistent. The amended `architecture.md` and the task text are consistent with the code.

## Issues Found

### Issue 1: [severity: LOW]
- **File:** `tests/test_unit_amux_resume.py` (`test_prompt_is_optional`)
- **Problem:** Nothing pins "no growth check without a prompt". The fake claude grows the transcript in its default mode, so a prompt-less resume passes either way. I mutated `grew = prompt is None or ...` to `grew = _transcript_grew(...)` and all 62 tests still passed.
- **Fix:** Add a test that resumes without a prompt using `FAKE_CLAUDE_MODE="nogrow"` and `AMUX_SPAWN_RESUME_GROWTH_TIMEOUT_S="1"`, and expects exit 0 with the attempt bumped.

## Code Quality Notes

- The reworded step-6 comment explains why the write is not conditional on growth. The code reads clearly.
- The repo edits are not live until `./install.sh --yes` re-runs. I confirmed no install was run.

## Questions for User

None.
