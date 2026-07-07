# Epic 13 — Model profiles

**Status:** planning · **Owner:** Anton · **Created:** 2026-07-07 · **Rev:** 1

## 1. Problem & thesis

Model configurations live in an external, unversioned `claude.bashrc` as bash
functions (`claude_glm5_env`, `claude_ds_env`, ...). Two parallel sets of wrapper
functions exist: the bashrc ones (direct `claude` launch, no tracking) and
`shell/amux-spawn.bash` ones (routed through `amux-spawn`, but depend on external
env-setters existing). This is fragile — forgetting to source the external file,
or moving to a new machine, silently degrades to wrong models or missing auth.

**Thesis:** store model profiles as structured data (TOML) in `~/.claude/profiles.toml`,
managed and documented within the claude-hooks workspace. A single file replaces
the external bashrc's model functions, the two parallel wrapper sets, and the
manual wiring between them. Shell aliases are auto-generated, amux wrapping is
automatic when available, and adding a new model is editing one file.

## 2. Configuration format

Single TOML file at `~/.claude/profiles.toml`. Three sections:

- **`[vars]`** — interpolation-only values. Referenced via `${name}` in
  `[all-profiles]` and `[profile.*]` values. Never exported as env vars.
  Good for secrets, model name aliases, shared URLs.

- **`[all-profiles]`** — env vars exported for every profile. Overridden by
  per-profile keys with the same name. Supports `${var}` interpolation.
  Good for tokens, timeouts, flags that apply everywhere.

- **`[profile.<name>]`** — per-profile env vars. Merged on top of
  `[all-profiles]` (profile wins on collision). The profile name becomes the
  shell alias verbatim. Names are unrestricted — `claude`, `claude-glm5`, `ds`,
  `my-local-llm`, anything.

Merge order: `[all-profiles]` → `[profile.X]` (profile wins). Both layers get
`${var}` interpolation from `[vars]`.

## 3. User experience

- Profile name IS the shell command: `claude-glm5`, `claude-ds`, `claude`, etc.
- If amux is installed, the wrapper routes through `amux-spawn spawn --profile <name>`
  (full tracking, Telegram, session handle).
- If amux is not installed, the wrapper exports env vars in a subshell and exec's
  `claude` directly (same model, no tracking).
- `amux-spawn profiles [--json]` lists available profiles.
- Adding a profile = add a `[profile.X]` section, open new shell. No code changes.

## 4. Components

| # | Task | Changes |
|---|------|---------|
| 13-01 | Profile loader | `amux_spawn_lib.py`: TOML parse, var interpolation, merge |
| 13-02 | CLI + shell integration | `amux-spawn --profile`, `profiles` subcommand, `amux-spawn.bash` rewrite, completion, `profiles.example.toml` |
| 13-03 | Installer + migration docs | `install-claude-config.sh` step, example file, migration guide |

## 5. Out of scope

- Project-level profiles (layer on later if needed)
- Profile inheritance / `extends` (vars + `[all-profiles]` handle reuse)
- Secret encryption / keyring (`chmod 600`, same as ssh keys)
- Auto-migrating claude.bashrc (too personal to automate)
- Non-Claude tools (codex, TaskMaster stay external)
