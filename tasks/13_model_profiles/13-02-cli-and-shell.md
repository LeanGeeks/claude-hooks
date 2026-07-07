# 13-02 — CLI and shell integration

**Status:** todo · **Depends on:** 13-01 (profile loader)
**Read first:** [brd.md](./brd.md) §3 (UX), [13-01](./13-01-profile-loader.md)

## Goal

Wire the profile loader into `amux-spawn` (CLI) and `amux-spawn.bash` (shell
integration) so that profile names become one-command launchers.

## Scope

### amux-spawn CLI changes

1. **`spawn --profile <name>`** — new flag. Calls `resolve_profile(name)`,
   exports returned env vars into the process, then proceeds with existing
   create-detached-under-lock flow. Env vars reach child via tmux
   `update-environment` (no `ps` leak). When `--profile` is absent, behavior
   is unchanged (backward compatible).

2. **`profiles [--json]`** subcommand — lists available profiles. Default:
   human-readable table (name, model, base URL inferred from env vars).
   `--json`: array of `{name, env}` objects.

### Shell integration rewrite

Rewrite `shell/amux-spawn.bash` to auto-generate wrapper functions from
`profiles.toml` at source-time:

- At source-time, call `python3 -c "..."` with `emit_shell_functions()`.
  `eval` the output. Cost: ~40ms one-shot at shell startup.
- Each generated function: if `amux-spawn` on PATH → `amux-spawn spawn
  --profile <name> "$@"`. Else → subshell with profile env vars exported,
  then `exec claude "$@"`.
- If `profiles.toml` doesn't exist, define no functions (clean degradation).
- Keep the `_AMUX_SPAWN_SHELL_LOADED` re-source guard.

### Bash completion

Update `shell/amux-spawn-completion.bash`:
- Complete `--profile` values on the `spawn` subcommand (grep profile names
  from TOML, no Python at completion time).
- Complete `profiles` as a subcommand.

### Example config

New file `shell/profiles.example.toml`. This is the starting template users
will customize. Content should be:

```toml
# Claude Code model profiles
#
# This file configures model backends for Claude Code. Each [profile.X]
# section becomes a shell alias you can type to launch a session with
# that model.
#
# Three sections:
#   [vars]          — reusable values, referenced via ${name}. Never exported.
#   [all-profiles]  — env vars applied to every profile. Overridden per-profile.
#   [profile.X]     — per-profile env vars. Profile name = shell alias.
#
# After editing, open a new shell (or re-source amux-spawn.bash) to pick
# up changes. No install script rerun needed.

# ── Shared values ────────────────────────────────────────────────────
# Put tokens, model names, or any repeated values here.
# Reference them in [all-profiles] or profile sections as ${name}.

[vars]
# my_token = "sk-..."
# opus     = "claude-opus-4-6"

# ── Global env vars ──────────────────────────────────────────────────
# These are exported for every profile. Good for timeouts, PATs, and
# flags you always want. Per-profile keys with the same name override.

[all-profiles]
# API_TIMEOUT_MS = "600000"
# GITHUB_MCP_PAT = "${my_token}"

# ── Profiles ─────────────────────────────────────────────────────────
# Each profile name becomes a shell alias. Names are unrestricted.
# Keys are exported as env vars before launching Claude Code.

[profile.claude]
# Default Anthropic models — launches with your subscription.
# Uncomment and set to pin specific model versions:
# ANTHROPIC_DEFAULT_OPUS_MODEL   = "claude-opus-4-6"
# ANTHROPIC_DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"
# ANTHROPIC_DEFAULT_HAIKU_MODEL  = "claude-haiku-4.5"

# [profile.claude-latest]
# Empty profile — launches with Claude Code defaults + [all-profiles].

# ── Example: third-party provider ────────────────────────────────────
# Uncomment and fill in to add a provider:
#
# [profile.claude-glm5]
# ANTHROPIC_BASE_URL            = "https://api.z.ai/api/anthropic"
# ANTHROPIC_AUTH_TOKEN           = "${my_token}"
# ANTHROPIC_MODEL               = "glm-5.2[1m]"
# ANTHROPIC_DEFAULT_OPUS_MODEL   = "glm-5.2[1m]"
# ANTHROPIC_DEFAULT_SONNET_MODEL = "glm-5.1"
# ANTHROPIC_DEFAULT_HAIKU_MODEL  = "glm-4.5-air"
# ANTHROPIC_SMALL_FAST_MODEL    = "glm-4.5-air"
```

### Implementation notes

- **amux-spawn CLI**: The `spawn` subcommand already uses `argparse` with
  subparsers. Add `--profile` as an optional argument to the `spawn` subparser.
  In the spawn flow, after argument parsing but before the `SpawnLock` context,
  call `resolve_profile(args.profile)` and `os.environ.update()` with the
  result. This ensures the env vars are in the spawner's live environment
  when amux's `update-environment` snapshots them.

- **`profiles` subcommand**: Add a new subparser. For human-readable output,
  infer display info from each profile's env vars:
  - Model: `ANTHROPIC_MODEL` if set, else first of `ANTHROPIC_DEFAULT_OPUS_MODEL`
    / `ANTHROPIC_DEFAULT_SONNET_MODEL`
  - Provider: parse hostname from `ANTHROPIC_BASE_URL` if set, else "anthropic"

- **Shell integration**: The rewritten `amux-spawn.bash` should:
  1. Keep the `_AMUX_SPAWN_SHELL_LOADED` guard
  2. Locate `amux_spawn_lib.py` (try `~/.claude/hooks/amux_spawn_lib.py`,
     fall back to the repo's `.claude/hooks/amux_spawn_lib.py`)
  3. `eval "$(python3 -c "import sys; sys.path.insert(0, '<hooks_dir>'); import amux_spawn_lib; print(amux_spawn_lib.emit_shell_functions())")"` 
  4. If python3 fails or profiles.toml doesn't exist, the eval produces
     nothing — no functions defined, no error. The file should NOT define
     any hardcoded fallback functions.

- **Completion**: Profile names can be extracted without Python at completion
  time: `grep -oP '^\[profile\.\K[^\]]+' ~/.claude/profiles.toml 2>/dev/null`

## Done criteria

- [ ] `amux-spawn spawn --profile claude-glm5` launches with correct env vars.
- [ ] `amux-spawn spawn` without `--profile` works as before.
- [ ] `amux-spawn profiles` lists profiles from TOML.
- [ ] Sourcing `amux-spawn.bash` creates a shell function for each profile.
- [ ] Each function routes through amux-spawn when available, falls back to
      direct claude launch otherwise.
- [ ] TAB completion offers `--profile` values and `profiles` subcommand.
- [ ] `profiles.example.toml` is present and valid TOML.
