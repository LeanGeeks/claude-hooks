# 13-03 — Installer and migration docs

**Status:** todo · **Depends on:** 13-02 (example file exists)
**Read first:** [brd.md](./brd.md) §3-4

## Goal

Wire profiles into the install flow and provide a clear migration path from
the external `claude.bashrc`.

## Scope

### install-claude-config.sh

Add one step (between amux-spawn install and shell snippet install):

- If `~/.claude/profiles.toml` does not exist, copy
  `shell/profiles.example.toml` to `~/.claude/profiles.toml`.
- If it already exists, skip (never overwrite user config).
- Print a note: "Edit ~/.claude/profiles.toml with your model tokens."

### Migration guide

Update the shell snippet install output to explain:

1. Copy your tokens from `claude.bashrc` env functions into `[vars]`.
2. Translate each `claude_*_env()` into a `[profile.*]` section.
3. Move shared env vars (timeouts, PATs) into `[all-profiles]`.
4. Remove the old wrapper functions from `claude.bashrc`.
5. Keep non-Claude env vars (TaskMaster, Milvus, etc.) in `claude.bashrc`.

## Done criteria

- [ ] Fresh install: `profiles.example.toml` is copied to `~/.claude/profiles.toml`.
- [ ] Existing install: file is not overwritten.
- [ ] Install output includes migration guidance.
