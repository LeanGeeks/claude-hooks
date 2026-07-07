#!/usr/bin/env bash
# amux-spawn shell integration (task 13-02 / epic 13)
#
# PURPOSE
#   Auto-generates one wrapper function per profile in ~/.claude/profiles.toml
#   at source-time. Each function routes through `amux-spawn spawn --profile
#   <name>` when amux-spawn is on PATH (full tracking, Telegram, session handle),
#   or falls back to a subshell with profile env vars exported and exec's claude
#   directly (same model, no tracking).
#
#   Adding a profile = edit ~/.claude/profiles.toml, open a new shell (or
#   re-source this file). No install script rerun needed.
#
# OPT-IN (REVERSIBLE)
#   Add ONE line to your personal bashrc (e.g. ~/.bashrc):
#
#     source /path/to/this/file
#
#   To UNDO: remove that `source` line and restart the shell.
#
# SAFETY
#   This file is NOT sourced automatically by install-claude-config.sh. The
#   install script only copies it to ~/.claude/shell/ and prints the opt-in
#   source line; you choose whether to add it.
#   Nothing here touches ~/.claude/settings.json, amux config, or any system
#   file. It ONLY defines shell functions in the current shell session.

# Guard: don't re-source if already loaded.
if [[ "${_AMUX_SPAWN_SHELL_LOADED:-}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi
_AMUX_SPAWN_SHELL_LOADED=1

# Locate amux_spawn_lib.py.
# Priority: installed copy (~/.claude/hooks/) → repo copy (sibling of shell/).
_amux_spawn_hooks_dir=""
if [[ -f "${HOME}/.claude/hooks/amux_spawn_lib.py" ]]; then
    _amux_spawn_hooks_dir="${HOME}/.claude/hooks"
else
    _amux_spawn_this_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    if [[ -n "$_amux_spawn_this_dir" && \
          -f "${_amux_spawn_this_dir}/../.claude/hooks/amux_spawn_lib.py" ]]; then
        _amux_spawn_hooks_dir="${_amux_spawn_this_dir}/../.claude/hooks"
    fi
    unset _amux_spawn_this_dir
fi

# Eval the auto-generated shell functions.
# If python3 fails or profiles.toml doesn't exist, eval receives empty output —
# no functions defined, no error.
if [[ -n "$_amux_spawn_hooks_dir" ]]; then
    eval "$(AMUX_SPAWN_HOOKS_DIR="$_amux_spawn_hooks_dir" \
        python3 -c "
import sys, os
sys.path.insert(0, os.environ['AMUX_SPAWN_HOOKS_DIR'])
import amux_spawn_lib
print(amux_spawn_lib.emit_shell_functions())
" 2>/dev/null)" 2>/dev/null || true
fi

unset _amux_spawn_hooks_dir
