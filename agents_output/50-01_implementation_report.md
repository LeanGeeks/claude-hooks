# Implementation Report - 50-01 amux-spawn: flag persistence, pinned env, Claude resume

## Summary

`amux-spawn spawn` now persists a Claude session's model and effort with amux. It rewrites `--model=X`/`--effort=X` to the space form, and it accepts `--pin-env KEY=VALUE` for the two compaction keys and forwards each pair as amux `--env`. It also records `profile`, `model`, `effort` and `pinned_env` in the handle; these four fields are documented in `HANDLE_FIELDS`. `amux-spawn resume` has a new Claude branch (architecture §13.3 steps 1–6). It restarts a terminated driven session in place on its own transcript, with the same model, effort, profile and pinned env. The Codex branch is unchanged. The fake amux now models amux 0.3.0's Claude `exec`/`start` path, and the agent docs were updated.

## Files Created

- None. `agents_output/50-01_implementation_report.md` is this report.

## Files Modified

- `.claude/hooks/amux_spawn_lib.py`:
  - `HANDLE_FIELDS` gains `profile`, `model`, `effort`, `pinned_env`, with an epic-50 comment block in the epic-20/37 style. There is also a new `HANDLE_FIELDS_ADDED_50_01`.
  - New `PINNABLE_ENV_KEYS = ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS")`.
  - `new_handle` takes the four new keyword arguments (default `None`; `pinned_env` is copied).
  - `upgrade_handle` reads legacy handles as null for these fields and rewrites nothing.
  - New helpers: `expected_model_id(model, profile_env)` and `model_matches(expected, alias, observed)` (§13.2), `tmux_pane_dead`, `tmux_kill_session` (kills only the tmux session, unlike `amux rm`), and `read_claude_session_meta` (`claude_session_id`, `started` from amux's `meta.json`).
- `.claude/bin/amux-spawn`:
  - New `normalise_value_flags` and `parse_pin_env`.
  - `cmd_spawn`: normalisation for Claude only. `--pin-env` is validated before anything is exported, locked or created. A Codex spawn with `--pin-env` is refused. The handle is written with the new fields.
  - `_amux_create_detached`: optional `pin_env` produces `--env K=V`. It is passed only when non-empty, so the epic-10 keyword set and argv are unchanged without it.
  - `cmd_resume`: an unlocked read of the handle picks the branch, and a Claude handle goes to the new `_cmd_resume_claude`.
  - New pieces of the Claude branch: `_resume_claude_refusal`, `_amux_start_claude`, the growth check (`RESUME_GROWTH_TIMEOUT_S = 30.0`, overridable through `AMUX_SPAWN_RESUME_GROWTH_TIMEOUT_S`), and the handle write.
  - The old "resume is a Codex-thread operation … Claude sessions continue interactively" refusal was rewritten. It is now reached only for an unknown provider, or if the handle is rewritten between the two reads.
  - Parser: a `--pin-env` option on `spawn`. The `spawn` epilog explains the normalisation. The `resume` help and epilog now cover the Claude branch.
- `tests/fake_amux_env.py`:
  - The fake amux now has amux 0.3.0's Claude path: `parse_amux_opts`, `parse_claude_flags` (the same value-flag and boolean-flag lists; `--model=x` goes to the extra args, never to `CC_FLAGS`), and `exec` → register → `start`. It applies `CC_NO_DEFAULT_MODEL`, appends `--session-id` or `--resume` from `meta.json` `started`, keeps inline args as given (including a `--` token), and exports `--env` in the pane setup.
  - The fake amux also logs its own argv (`AMUX_ARGV_LOG`).
  - New fake `claude`. It records argv, cwd and the `ANTHROPIC_*`/`CLAUDE_*` env, appends to `$HOME/.claude/projects/<enc>/<sid>.jsonl`, and has three modes: `grow`, `nogrow`, `exit`.
  - The state-file tmux now models a session that is still there with a dead pane (`"dead"`), plus `list-panes`.
  - New helpers: `claude_calls`, `amux_calls`, `session_env`, `set_session_env`, `kill_session`, `kill_pane_keep_session`, `tmux_entry`.
  - The Codex paths of the fake are unchanged. The Claude dispatch in `cmd_start` runs before the old checks, and those checks keep their order.
- `tests/test_unit_amux_spawn.py`: new classes `TestClaudeFlagNormalisation`, `TestPinEnv`, `TestExpectedModel`, `TestHandleFields50`, `TestSpawnPersistsFlagsInFakeAmux` (22 tests).
- `tests/test_unit_amux_resume.py`:
  - Two tests that depended on the old Claude refusal were replaced; see Decisions.
  - New `TestClaudeResume` (12) and `TestClaudeResumeRefusals` (8), run against the fake amux.
- `docs/amux-spawn-agent-reference.md`: new section "Resuming a dead session", `--pin-env` in the flag table, a normalisation note, and a paragraph on handle fields that points to `HANDLE_FIELDS`.
- `docs/amux-spawn-model-selection.md`: the space-form table now shows what reaches amux after normalisation. The note on profiles now says a Claude resume re-exports the profile recorded in the handle.

## Verification

- Compile/import: PASS. `python3 -m py_compile` was run on `amux_spawn_lib.py`, `amux-spawn` and `fake_amux_env.py`, and the CLI was loaded via `SourceFileLoader`.
- Tests:
  - `python3 tests/run_all_tests.py --module unit_amux_spawn`: 101 run, OK (1 skipped, the live test).
  - `--module unit_amux_resume`: 62 run, OK.
  - Full suite `python3 tests/run_all_tests.py`: **2481 run, OK (2 skipped), 0 failed, 0 errors** (456 s). This task adds 42 tests: 22 in spawn, 20 in resume, with 2 replaced. The remaining growth over the brief's figure of about 2199 comes from other work in the shared checkout, not from this task. The 6 known failures appear only under pytest, and pytest was not run.
- Mutation check (backup with `cp`, restore with `cp`, confirmed with `diff -q`):
  - Keeping `CLAUDE_CODE_CHILD_SESSION`: fails `test_dead_session_resumes_with_profile_env_and_persistence`.
  - Always adding the handle's `--model`: fails `test_model_effort_from_cc_flags_are_not_repassed` and `test_only_the_missing_one_is_added`.
  - Dropping the normalisation: fails both spawn persistence tests, plus the resume argv tests.
- Installed (`install.sh --yes` re-run): no. The brief said no live verification.

## Decisions

1. **The verified `amux start` argv. It deviates from the spec's `-- <prompt>` form.** I ran the real `/usr/local/bin/amux` read-only with an isolated `HOME`/`CC_HOME`: `amux register`, then `amux __build-command`. `__build-command` calls the same `build_provider_command` as `cmd_start` and touches no tmux. Results:
   - `register --model=opus --effort=high --yolo` persists `CC_FLAGS=--dangerously-skip-permissions`. The space form persists `'--model opus --effort high --dangerously-skip-permissions'`. This confirms B1.
   - A restart with `started=true` and `--no-attach --env K=V -- "RESUME E-1: read x"` builds `claude --model opus --effort high --dangerously-skip-permissions -- 'RESUME E-1: read x' --resume <sid>`. On a Claude (interactive) session amux drops the leading `--` only in exec mode, and it appends `--resume` after the inline args. So `--resume` would land after the end-of-options marker, and claude would treat it as a positional: a fresh conversation, not a resume.
   - The same restart with the prompt as a trailing positional builds `claude <CC_FLAGS> 'RESUME E-1: read x' --resume <sid>`, which is correct.
   - So the branch sends `amux start <name> --no-attach [--env K=V …] [--model M] [--effort E] [<prompt>]`, with **no `--`**. This is the same way `spawn` already passes its prompt. `--env` goes into the pane's shell setup (`export K=<shlex-quoted value>`), never onto the claude argv.
   - A prompt that starts with `-` would be parsed as a flag, so it is refused.
   - The fake models this exactly. The tests assert the full `amux start` argv and the full claude argv, including `[..., prompt, "--resume", sid]` with no `--` in it.
   - This keeps every locked part of the decision (amux start, `--no-attach`, `--env`, model/effort only when missing, amux adds `--resume`). Only the separator token changes, because the spec asked for the argv to be verified against `cmd_start`. Please confirm.
2. **Growth check only when a prompt is given.** Claude appends to the transcript when a turn starts. A resume without a prompt opens an idle TUI that need not write anything, so it is confirmed by the launch alone. The tick always passes a prompt (§11.4). Please confirm.
3. **The lock is released during the growth wait.** It is held across the step-2 checks and the launch, then released for the wait of up to 30 s, so other spawns are not blocked. It is taken again for the handle write, which re-reads the handle first so writes made in the meantime by `UserPromptSubmit`/`Stop` are kept. A second resume during the wait is refused, because the session is live.
4. **Refusals in addition to the ones listed:**
   - amux's `meta.json` must hold `claude_session_id` equal to the handle's `session_id`, with `started=true`. Otherwise amux would not append `--resume`, or would pass `--session-id` and collide.
   - A malformed `pinned_env` in the handle is refused.
   - A prompt that starts with `-` is refused.
   - A missing effort is refused the same way as a missing model ("neither in CC_FLAGS nor the handle ⇒ refuse"). As a result, a legacy session with no effort pinned cannot be resumed.
5. **Terminated means tmux gone, or every pane dead** (`tmux list-panes -s -t =amux-<n> -F '#{pane_dead}'`, checked against a private tmux socket). A live pane is refused whatever state is stored. A dead tmux session is removed with `tmux kill-session`, not `amux rm`, because `rm` would delete `.env`/`meta.json`. It is removed only after every validation has passed, just before the launch.
6. **The handle's `model`/`effort` record what was actually forwarded to amux**: the caller's value, else the model inherited from the parent. They are recorded for Claude only; Codex handles hold null for all four fields. `pinned_env` is null when no `--pin-env` is given. The `--json` `model`/`effort` are the values the session was restarted on: `CC_FLAGS` first, else the handle.
7. **Constants live in the lib.** `PINNABLE_ENV_KEYS` is in `amux_spawn_lib`, so 50-04 ledger validation can import it.
8. **`expected_model_id` / `model_matches`.** Aliases are matched case-insensitively and may carry a `[...]` suffix, e.g. `opus[1m]`. `last_assistant_entry` was not added, because 50-01 does not need it; it stays with 50-03.
9. **`AMUX_SPAWN_RESUME_GROWTH_TIMEOUT_S`** overrides the 30 s window, in the same style as `AMUX_SPAWN_MAX_SESSIONS`. It exists so the subprocess test of the "not growing" case runs in 1 s.
10. **Replaced tests.** Two existing tests asserted the old Claude refusal. `test_claude_handle_resume_refusal_mentions_attach_not_profiles` became `test_claude_handle_takes_the_claude_branch`. `test_wrong_provider_resume_never_launches` now uses an unknown provider (`gemini`) and the rewritten message. All other existing amux-spawn tests pass unchanged.
11. **The fake records no producer hook.** No hooks run in the fake pane, so `--wait` is tested through the shared timeout contract (exit 3 plus the marker, with the JSON on stderr).

## Blockers

None.

## Questions for User

None. Decisions 1 and 2 are flagged for the reviewer, because they read the spec text against the verified amux behaviour.
