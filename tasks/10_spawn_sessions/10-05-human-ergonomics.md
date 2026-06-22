# 10-05 — Human ergonomics: `a|attach`, completion, shell integration

**Status:** todo · **Depends on:** 10-01 (naming/prefix), [task 12 E3](../12_amux_extensions.md)
(nested-tmux switch-client)
**Read first:** [brd.md](./brd.md) C8–C10 + [architecture.md](./architecture.md)
§3 and decisions D-Entry, D-Switch, D-Name, D-Env.

## Goal

Make `amux-spawn` pleasant enough to fully replace bare `claude` and tmux tabs:
one-key-ish session switching with completion, and shell aliases that route the
existing model functions through `amux-spawn`.

## Scope

- `amux-spawn a|attach <suffix>`: resolve `prefix = basename(cwd)`, attach
  `<prefix>-<suffix>` (fuzzy, like amux). **If the cwd-prefix yields no match (e.g.
  run from a subdir), fall back to a fuzzy match across the whole session list**
  (D-Switch). Inside tmux ⇒ `switch-client` (task 12 E3), else `attach-session`.
  `a` with no suffix attaches the workspace's bare-`<prefix>` (or lists if ambiguous).
- **Bash completion**: complete `<suffix>` from live sessions (prefer the current
  workspace's, then all), parsing `amux ls`. Install the completion script via
  `install-claude-config.sh`.
- **Shell integration** (`/data/sync/Config/bash/claude.bashrc`): provide
  drop-in functions so the everyday launchers go through `amux-spawn spawn`,
  preserving model env — e.g. redefine `claude` and the `claude-glm5` family to run
  `( <model>_env && amux-spawn spawn "$@" )`. This works because the env mechanism is
  tmux `update-environment` (Decision 1 / D-Env / task 12 E1), which copies the curated
  vars from the spawning subshell's **live env** — so the `_env` function's vars reach
  the child. (It would NOT work under plain tmux inheritance on a running server.)
  Offer this as an opt-in addition rather than silently breaking the existing aliases.

## Implementation hints / watch-outs

- The prefix algorithm here must match 10-01's naming exactly, or `a` won't find
  sessions; share the helper.
- Nested-tmux is the **common** case (you switch sessions from inside tmux) —
  `attach-session` from within tmux misbehaves; rely on E3 / detect `$TMUX`.
- Completion should be fast (avoid heavy work per TAB); cache/parse `amux ls` cheaply.
- Subdir fuzzy fallback must avoid surprising cross-workspace matches — rank
  current-workspace candidates first; if still ambiguous, list rather than guess.
- Shell integration touches a file full of secrets and personal aliases — make
  minimal, clearly-marked, reversible edits (or ship a sourced snippet the user
  opts into from their bashrc).

## Done criteria

- [ ] `amux-spawn a <suffix>` switches to the right session from the repo root and
      from a subdir; from inside tmux it switches without the nested-attach warning.
- [ ] TAB completion lists plausible suffixes for the current workspace.
- [ ] A redefined `claude-glm5` (via amux-spawn) launches a GLM amux session in one
      step with Telegram follow-up working.
- [ ] Existing workflows still available (nothing silently broken).

## Testing

- From repo root and a subdir, `a <suffix>` to several sessions; verify correct
  target and clean switch inside/outside tmux.
- TAB-complete in a workspace with multiple sessions.
- Launch via the integrated `claude`/`claude-glm5` functions; confirm amux session
  + model + Telegram reply path.
