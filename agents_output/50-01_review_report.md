# Review Report - 50-01 amux-spawn: flag persistence, pinned env, Claude resume

## Verdict: PASS

0 BLOCKER, 0 HIGH, 0 MEDIUM, 3 LOW. Compile and import are clean (`py_compile` of the lib, the fake env and both test files; the CLI loads through `SourceFileLoader`). `python3 tests/run_all_tests.py` run in the foreground: 2481 run, OK, 2 skipped. I made no mutations, so the tree is unchanged. `git status` shows only the 7 files the implementer left modified, and there are no untracked files.

## Completeness Check

1. Normalisation (Claude only): **met**. `normalise_value_flags` and the `cmd_spawn` line `list(claude_flags) if codex else normalise_value_flags(...)`. Tests: `TestClaudeFlagNormalisation` (eq form → space form, space form unchanged, Codex keeps `=`), and `TestSpawnPersistsFlagsInFakeAmux`, which checks the persisted `CC_FLAGS`. The help epilog was updated and agrees with the code.
2. `--pin-env`: **met**. `PINNABLE_ENV_KEYS` is in the lib. A bad key, a pair without `=`, an empty value, a duplicate key and Codex are all refused before anything is exported, locked or created. Valid pairs go out as `--env K=V` in `_amux_create_detached`, only when non-empty, so the epic-10 argv is unchanged. Tests: `TestPinEnv`.
3. Handle fields: **met**. `profile`, `model`, `effort` and `pinned_env` are in `HANDLE_FIELDS` with a comment block, accepted by `new_handle` (the dict is copied) and written by `cmd_spawn`. `upgrade_handle` reads legacy handles as null and does not rewrite them. Tests: `TestHandleFields50`.
4. Helpers: **met**. `expected_model_id` and `model_matches` match the §13.2 signatures and are tested in both directions. `last_assistant_entry` was not added, which is allowed ("if needed").
5. Claude resume branch: **met**, apart from the LOWs below. `_cmd_resume_claude`: refusals come before any lock (yolo, profile, dir, pass-through flag, dash prompt). Under `SpawnLock` it checks the handle and provider, terminated (tmux gone, or every pane dead followed by `tmux kill-session`, which keeps `.env` and `meta.json`), the missing-transcript refusal and the meta `started=true`/sid check. It then re-exports the profile, sets `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` and pops `CLAUDE_CODE_CHILD_SESSION`. `--model`/`--effort` are added only when `CC_FLAGS` lack them, with a refusal when neither source has them. Launch is confirmed with `_launch_confirmed`, then the transcript growth wait runs (30 s default). The handle write re-reads the handle and sets `attempt + 1`, `state = "spawning"`, `updated_at`, under `SpawnLock`.
6. `--json`: **met**. The payload has the nine specified keys. `--wait` reuses `_wait_and_report`, and the JSON goes to stderr in wait mode, as for `spawn`.
7. Docs: **met**. `docs/amux-spawn-agent-reference.md` has the new resume section, the `--pin-env` row, the normalisation note and a pointer to `HANDLE_FIELDS`. `docs/amux-spawn-model-selection.md` is updated too.

Codex resume: **unchanged in behaviour**. The only edit on that path is the provider-mismatch message, which now says "cannot resume handle ... provider=..." instead of "resume is a Codex-thread operation". That change was asked for. `pre_handle` dispatches Claude handles away. A missing handle still falls into the Codex path, so the old "no tracked handle" message holds. The existing Codex resume tests pass. Two tests were replaced because they asserted the old Claude refusal: `test_claude_handle_resume_refusal_mentions_attach_not_profiles` became `test_claude_handle_takes_the_claude_branch`, and `test_wrong_provider_resume_never_launches` now uses an unknown provider.

### The `--` deviation: verified correct

I checked this against `/usr/local/bin/amux` 0.3.0, read-only, in an isolated `HOME`/`CC_HOME`.

- `build_claude_command` builds the argv as `claude <defaults> <CC_FLAGS> "$@" [--model sonnet] [--resume|--session-id <sid>]`. The inline args come before `--resume`.
- `cmd_start` drops a leading `--` only when `mode == exec`. On an interactive Claude session the `--` stays in the inline args.
- `amux register t1 --model=opus --effort high` persisted only `--effort high`, so B1 is confirmed. The space form persists `--model opus --effort high`.
- `amux __build-command t1 --no-attach --env A=B -- "do it"` with `started=true` gives `claude --model opus --effort high -- 'do it' --resume <sid>`. That puts `--resume` after the end-of-options marker, which is a fresh conversation, not a resume. The prompt as a trailing positional gives `claude ... 'do it -x' --resume <sid>`, which is correct and is one shell-quoted argv element.
- A prompt starting with `-` would be inline claude argv and read as a flag. `_resume_claude_refusal` refuses it up front, and `test_dash_prompt_refused` covers it. Prompts equal to an amux-level option (`--env`, `--no-attach`) start with `-`, so the same refusal covers them.
- `tests/fake_amux_env.py` is faithful. It carries the same value-flag and boolean-flag lists as amux, so `--model=x` goes to extra args and never to `CC_FLAGS`. `--env` and `--no-attach` are stripped. Inline args keep a `--` token. `--resume`/`--session-id` is appended last from `meta.json` `started`. `--no-default-model` is honoured. `exec` goes through register and then start with the transient options and extra args, as the real `cmd_exec` does. B1 is pinned by `test_fake_amux_models_b1_eq_form_is_not_persisted`. The fake does not merely accommodate the implementation: its argv was cross-checked against the real `__build-command` output above.

The decision keeps everything locked in Phase 0 (amux start, `--no-attach`, `--env`, model/effort only when missing, amux adds `--resume`). Only the separator token differs, because the spec itself asked for the verification. Accept it; `architecture.md` §13.3 step 4 and the task text should be amended to drop `--`.

### Invariant 7

Only `new_handle` (called from `cmd_spawn`) writes `profile`, `model`, `effort` and `pinned_env`. A grep of `.claude/` and `relay-server/` finds no other writer. The resume write touches only `attempt`, `state` and `updated_at`, which are already documented. The existing `set(h.keys()) == set(HANDLE_FIELDS)` tests in `test_unit_spawn_producer.py` and `test_unit_codex_reducer.py` pass.

### Test count

No test files were added. The three modified test files gain 44 `def test_` and lose 1 (and one more was rewritten in place). That is about +42 to +44, which matches the report. The suite figure of 2481 against the remembered 2199 (from 2026-11-08) is therefore mostly earlier commits, not this task. The working tree contains only the 7 files listed above.

## Issues Found

### Issue 1: [severity: LOW]
- **File:** `.claude/bin/amux-spawn` (`_cmd_resume_claude`, the growth-wait block)
- **Problem:** The growth check runs only when a prompt is given. Architecture §13.3 step 5 states it unconditionally. A prompt-less resume is confirmed by the launch alone. The implementer flagged this as Decision 2. The tick always passes a prompt, so the driver path is covered, and the docs say "With a prompt".
- **Fix:** Accept it, or amend §13.3 step 5 to read "when a prompt is given".

### Issue 2: [severity: LOW]
- **File:** `.claude/bin/amux-spawn` (`_cmd_resume_claude`, the lock release and the growth-failure return)
- **Problem:** Two deviations from the spec's single-lock wording. First, the lock is released during the up-to-30 s growth wait and retaken for the handle write. This is deliberate and re-reads the handle, so hook writes are not lost; a concurrent resume is refused as live. Second, when the transcript does not grow, the code exits 1 without writing the handle. The session runs while the handle says `terminated` with the old attempt, and the test asserts the attempt is unchanged. A retry is refused as live, which is the desired behaviour. The tick must not treat `terminated` plus a live pane as a fresh failure, which is 50-02/50-05 territory.
- **Fix:** Note it in the 50-02/50-05 briefs. Optionally write `state = "spawning"` and bump the attempt before returning 1.

### Issue 3: [severity: LOW]
- **File:** `.claude/bin/amux-spawn` (the `("model", "effort")` refusal loop)
- **Problem:** A missing effort refuses the resume just like a missing model. The spec's tests name only the model case: "refusal when neither source has a model". A session spawned without `--effort` (legal for non-driven spawns, which can `--model` only) can never be resumed. The literal §13.3 text "neither in CC_FLAGS nor the handle" is ambiguous. This is the stricter reading, and it is documented and tested (`test_refused_when_neither_source_has_an_effort`). No Phase-0 text prohibits it.
- **Fix:** Confirm that the driver always spawns with `--effort`. If not, relax effort to "pass only when known" and refuse only on a missing model.

## Code Quality Notes

- The tests exercise the real CLI as a subprocess against the fake amux, the fake claude and the state-file tmux, with an isolated HOME. Assertions check the full amux argv, the full claude argv, the pane env (`CLAUDE_CODE_CHILD_SESSION` absent), handle transitions and exit codes. Refusal tests assert that nothing launched. Mutation checks the implementer reported (keeping `CLAUDE_CODE_CHILD_SESSION`, always passing the handle's model, dropping the normalisation) are plausible given what the tests assert; I did not repeat them.
- `HANDLE_FIELDS_ADDED_50_01` is not used by `new_handle`, only by `upgrade_handle` and the tests. That is fine.
- The `AMUX_SPAWN_RESUME_GROWTH_TIMEOUT_S` env override matches `AMUX_SPAWN_MAX_SESSIONS` and keeps the not-growing test at 1 s.
- Installed hooks are not re-installed: the amux-spawn CLI and lib need `./install.sh --yes` before the change is live. The implementer's report says this was not run, as instructed.
- No `.claude/hooks` hook behaviour or fail-open posture changed. The diff touches only the amux-spawn CLI, its lib, the tests and the docs.

## Questions for User

None. Decision 1 (no `--`) is verified correct against amux 0.3.0. `architecture.md` §13.3 step 4 and the task text should be corrected for the next tasks (50-03/50-05 briefs).
