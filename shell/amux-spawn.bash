#!/usr/bin/env bash
# amux-spawn shell integration — opt-in wrapper functions (task 10-05 / epic 10)
#
# PURPOSE
#   Redefine the everyday Claude launchers so they route through `amux-spawn spawn`,
#   which creates an amux session and (inside tmux) switch-clients to it — giving
#   every launch Telegram follow-up, a trackable handle, and proper nested-tmux
#   behaviour.  The model environment is preserved: env vars ride tmux
#   update-environment (task 12 E1 / D-Env), so `claude_glm5_env && amux-spawn spawn`
#   delivers the GLM model/auth vars into the child pane without a `ps` leak.
#
# OPT-IN (REVERSIBLE)
#   Add ONE line to your personal bashrc (e.g. ~/.bashrc or claude.bashrc):
#
#     source /path/to/this/file          # absolute path to this file in the repo
#                                        # or the installed copy (~/.claude/shell/amux-spawn.bash)
#
#   To UNDO: remove that `source` line.  The functions vanish and the original
#   `claude` / `claude-glm5` aliases or functions (if any) are NOT restored —
#   either ensure the original definitions appear BEFORE this source line so they
#   survive, or simply restart the shell without this file sourced.
#
# SAFETY
#   - This file is NOT sourced or modified automatically by install-claude-config.sh.
#     The install script only copies it to ~/.claude/shell/ and prints the opt-in
#     source line; you choose whether to add it.
#   - Nothing in this file touches ~/.claude/settings.json, amux config, or any
#     system file.  It ONLY defines shell functions in the current shell session.
#   - The bash completion (installed separately) works regardless of whether this
#     snippet is sourced.
#
# FUNCTIONS DEFINED
#   claude          — amux-spawn spawn "$@"  (plain Claude session with amux)
#   claude-glm5     — claude_glm5_env + amux-spawn spawn "$@" (GLM env via subshell)
#   claude-glm5-f   — alias for claude-glm5 (the "-f" family shares the same env)
#
# CUSTOMISATION
#   If your repo defines additional env-setter functions (e.g. `claude_opus_env`),
#   add wrappers following the same pattern below.

# Guard: don't re-source if already loaded.
if [[ "${_AMUX_SPAWN_SHELL_LOADED:-}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi
_AMUX_SPAWN_SHELL_LOADED=1

# ---------------------------------------------------------------------------
# claude — drop-in replacement that creates an amux session and attaches to it.
# All flags and arguments are forwarded to `amux-spawn spawn` unchanged.
# ---------------------------------------------------------------------------
claude() {
    if declare -f claude_env &>/dev/null; then
        ( claude_env && amux-spawn spawn "$@" )
    else
        amux-spawn spawn "$@"
    fi
}

# ---------------------------------------------------------------------------
# claude-glm5 / claude-glm5-f — GLM model wrappers.
#
# Runs `claude_glm5_env` in a subshell first to export the GLM model/auth vars
# into the spawning shell's environment, then calls `amux-spawn spawn`.  The
# vars reach the amux session's pane via tmux update-environment (task 12 E1),
# which copies them from the spawner's LIVE env when the session is created —
# so the GLM credentials are present in the child without being inlined into the
# launch command (no `ps` leak).
#
# `claude_glm5_env` must be defined in your claude.bashrc (or wherever your
# personal model functions live).  If it is absent this function falls back to a
# plain `amux-spawn spawn` with a warning so you don't silently get the wrong
# model.
# ---------------------------------------------------------------------------
claude-glm5() {
    if declare -f claude_glm5_env &>/dev/null; then
        # Subshell: set env vars, then exec amux-spawn in the same subshell so
        # those vars are in its LIVE environment when amux E1 snapshots them.
        ( claude_glm5_env && amux-spawn spawn "$@" )
    else
        echo "amux-spawn: warning: claude_glm5_env is not defined; falling back to plain spawn" >&2
        amux-spawn spawn "$@"
    fi
}

# `-f` family (same GLM model, if your rc defines a separate env function for it)
claude-glm5-f() {
    if declare -f claude_glm5_f_env &>/dev/null; then
        ( claude_glm5_f_env && amux-spawn spawn "$@" )
    elif declare -f claude_glm5_env &>/dev/null; then
        ( claude_glm5_env && amux-spawn spawn "$@" )
    else
        echo "amux-spawn: warning: claude_glm5_f_env / claude_glm5_env not defined; plain spawn" >&2
        amux-spawn spawn "$@"
    fi
}
