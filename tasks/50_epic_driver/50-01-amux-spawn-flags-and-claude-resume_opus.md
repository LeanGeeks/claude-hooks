# 50-01 — amux-spawn: flag persistence, pinned env, Claude resume

**Status:** todo · **Depends on:** Phase 0
**Read first:** [brd.md](./brd.md) §4 (OD-3, OD-7) · [architecture.md](./architecture.md) §13.1–§13.3, §11.4, §11.6,
§3 · [state.md](./state.md) P0-3, P0-9, invariant 7 · review B1 (`agents_output/50_spec_review_report.md`) ·
`.claude/bin/amux-spawn` (`cmd_spawn`, `_amux_create_detached`, `cmd_resume`, `_amux_start_resume`,
`_launch_confirmed`, `_derive_claude_status`) · `.claude/hooks/amux_spawn_lib.py` (`HANDLE_FIELDS`, `new_handle`,
`parent_cc_flags`, `resolve_profile`) · `/usr/local/bin/amux` (`parse_claude_flags`, `cmd_start`, `--env`) ·
`tests/fake_amux_env.py`, `tests/test_unit_amux_resume.py`, `tests/test_unit_amux_spawn.py`

## Goal

A driven Claude session can be spawned with its model, effort, profile and compaction environment recorded, and
resumed in place after a crash or reboot **on the same model and effort**, with its context. Today a resume would run
on the harness default: amux persists only `--model X`, never `--model=X`, and nothing records the profile.

## Scope

1. **Normalisation** (Claude spawns only): in `cmd_spawn`, rewrite `--model=X` → `--model X` and `--effort=X` →
   `--effort X` in the forwarded flags before `_amux_create_detached`. Codex spawns are untouched. Keep the help
   text's advice consistent with the change.
2. **`--pin-env KEY=VALUE`** on `spawn` (repeatable, Claude only). Allowed keys: a new constant `PINNABLE_ENV_KEYS =
   ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS")`; any other key, a malformed pair, or
   `--provider codex` is refused before anything is created. Forwarded to amux as `--env KEY=VALUE` (amux applies it
   last in the pane, over the profile's value).
3. **Handle fields** `profile`, `model`, `effort`, `pinned_env` (architecture §13.1 table): added to `HANDLE_FIELDS`
   with a comment block in the style of the epic-20/37 additions, accepted by `new_handle`, written by `cmd_spawn`.
   Legacy handles read as null; nothing rewrites old handles.
4. **Helpers in `amux_spawn_lib`**: `expected_model_id(model, profile_env)` and `model_matches(expected, alias,
   observed)` (architecture §13.2). If `last_assistant_entry` is needed for scope item 6, add it here with the
   §13.2 signature (50-03 then only adds its tests and the `since` form, if not already done).
5. **Claude resume branch** in `cmd_resume`, per architecture §13.3 steps 1–6: refusals first; under `SpawnLock` the
   terminated check (removing a dead pane's tmux session), the missing-transcript refusal; profile re-export,
   `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, no `CLAUDE_CODE_CHILD_SESSION`; `amux start <name> --no-attach
   [--env …] [--model M] [--effort E] [<prompt>]` (no `--`: architecture §13.3 step 4) with model/effort added only when the persisted `CC_FLAGS` lack
   them, and a refusal when neither `CC_FLAGS` nor the handle has a model (a missing effort is passed when known, not refused); launch confirmation, then transcript growth
   within 30 s; the handle write (`attempt` + 1, `state = "spawning"`). Verify the `amux start` argv for a Claude
   session against amux 0.3.0's `cmd_start` before writing it, and extend `tests/fake_amux_env.py` to model it
   (inline args after `--`, `--env`, `--resume <id>` added by amux when `meta.json` has `started=true`).
   The Codex branch keeps its existing validation and behaviour byte for byte; the current "resume is a
   Codex-thread operation" refusal for Claude handles is replaced by the new branch.
6. **`--json`** output for the Claude branch: `{name, provider, state, attempt, session_id, transcript_path, model,
   effort, profile}`. `--wait` reuses `_wait_and_report`.
7. Update `docs/amux-spawn-agent-reference.md` (resume for Claude, `--pin-env`, the normalisation) and the
   `HANDLE_FIELDS` documentation it cites.

Out of scope: the model check after a restart (the tick's, 50-05), the lifecycle latch (50-02).

## Tests

- Spawn: `--model=opus --effort=high` reaches the fake amux as `--model opus --effort high` and lands in the
  persisted `CC_FLAGS`; the space form is unchanged; a Codex spawn keeps the `=` form; the handle holds `profile`,
  `model`, `effort`, `pinned_env`.
- `--pin-env`: allowed keys forwarded as `--env`; an unknown key, `KEY` without `=`, and Codex are refused with
  nothing created.
- `expected_model_id`: alias through `ANTHROPIC_DEFAULT_FABLE_MODEL` (the `profile.claude` case), alias with no
  mapping, full id; `model_matches` both ways.
- Claude resume against the fake amux: a dead session resumes with the profile env exported, persistence forced,
  `CLAUDE_CODE_CHILD_SESSION` absent; `--model`/`--effort` passed only when `CC_FLAGS` lacks them (both cases);
  refusal when neither source has a model; `--env` re-passed from `pinned_env`; the prompt reaches the pane as one
  argv element; attempt bumped and state `spawning`; `--json` shape.
- Refusals: a live session; a missing transcript; `--yolo`, `--profile`, `--dir`, a pass-through flag; a Codex handle
  still goes down the Codex path with its old tests green.
- A transcript that does not grow within the wait ⇒ exit 1 with the documented message, session left running.

## Done when

- A resumed driven session's command line carries the spawn's model and effort in every case the tests cover.
- `HANDLE_FIELDS` documents every new field and nothing else writes one (invariant 7).
- The whole existing amux-spawn suite passes unchanged apart from the replaced Claude-resume refusal test.
