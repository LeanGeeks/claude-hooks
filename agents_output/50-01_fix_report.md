# 50-01 fix report

## Issue 1 (growth check only with a prompt)
Accepted as is. No code change. `docs/amux-spawn-agent-reference.md` already says "With a prompt, success means the transcript grew within 30 s", so it does not contradict the behaviour.

## Issue 2 (growth failure left the handle `terminated`)
- `.claude/bin/amux-spawn`, `_cmd_resume_claude`: the growth result is now computed first (`grew = prompt is None or _transcript_grew(...)`). The step-6 handle write then runs under `SpawnLock` with the handle re-read: `attempt` + 1, `state = "spawning"`, `updated_at`. After the write, `if not grew` prints the "resumed but the transcript is not growing" message and exits 1, leaving the session running. A comment explains why. The success path is unchanged.
- Docs: `docs/amux-spawn-agent-reference.md` (resume section) now says the handle is still updated (`attempt` + 1, `spawning`) when the transcript does not grow.
- Test: `tests/test_unit_amux_resume.py::test_transcript_not_growing_exits_1_and_leaves_the_session` now asserts `attempt == old + 1` and `state == "spawning"` (previously asserted the attempt unchanged).

## Issue 3 (missing effort refused the resume)
- `.claude/bin/amux-spawn`, `_cmd_resume_claude`: only a missing model is refused (model in neither `CC_FLAGS` nor the handle; message "harness default model"). `--effort E` is passed when `CC_FLAGS` lacks it and the handle has it. With no effort source, none is passed and the resume proceeds. The comment and the step-2 header comment were updated.
- Docs: `docs/amux-spawn-agent-reference.md` states a missing effort is not refused. The `--help` text of `resume` never mentioned the effort refusal, so it was left unchanged.
- Tests: `test_refused_when_neither_source_has_an_effort` replaced by `test_resume_without_any_effort_source_proceeds_unpinned` (CC_FLAGS `--model opus`, handle effort None: exit 0, amux start argv is `start <name> --no-attach "go on"`, and the claude argv has no `--effort`). `test_refused_when_neither_source_has_a_model` and `test_only_the_missing_one_is_added` kept.

## Compile
`python3 -m py_compile .claude/bin/amux-spawn tests/test_unit_amux_resume.py` ran clean. `.claude/hooks/amux_spawn_lib.py` was not touched in this fix.

## Suite
`python3 tests/run_all_tests.py` (foreground, full): Ran 2481 tests in 476.6s, OK (skipped=2).

No git operations were run and nothing was committed.

## Questions for User
None
