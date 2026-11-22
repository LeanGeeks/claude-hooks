# Fix Report 2 - 50-01

## Test added
`tests/test_unit_amux_resume.py`, `TestClaudeResume.test_no_prompt_resume_skips_the_growth_check`. It resumes a dead Claude session with no prompt, with `FAKE_CLAUDE_MODE="nogrow"` and `AMUX_SPAWN_RESUME_GROWTH_TIMEOUT_S="1"`. It asserts exit 0, no "not growing" in stderr or stdout, `attempt == old + 1` and `state == "spawning"`. No production code changed.

## Mutation check
Backed up `.claude/bin/amux-spawn` with `cp` (/tmp/amux-spawn.bak50). Changed line 1388 to `grew = _transcript_grew(transcript, baseline, growth_s)` (unconditional). The new test FAILED (exit 1, "resumed but the transcript is not growing"). Restored from the cp backup; `diff` showed the files identical, and line 1388 is again `grew = prompt is None or _transcript_grew(...)`.

## Counts
- `python3 tests/run_all_tests.py --module unit_amux_resume`: Ran 63 tests, OK (62 before plus the new one).
- Full suite `python3 tests/run_all_tests.py` (foreground-equivalent, output redirected to a file because it passed 180s): `Ran 2482 tests in 583.532s` / `OK (skipped=2)`. The runner printed no separate summary line after that; exit code 0 (the same pass-through exit was recorded by the wrapper).

## git status --short
```
 M .claude/bin/amux-spawn
 M .claude/hooks/amux_spawn_lib.py
 M docs/amux-spawn-agent-reference.md
 M docs/amux-spawn-model-selection.md
 M tasks/50_epic_driver/50-01-amux-spawn-flags-and-claude-resume_opus.md
 M tasks/50_epic_driver/architecture.md
 M tests/fake_amux_env.py
 M tests/test_unit_amux_resume.py
 M tests/test_unit_amux_spawn.py
```
(`agents_output/` is gitignored, so this report does not appear.)
