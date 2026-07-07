#!/usr/bin/env bash
# amux-spawn shell integration
#
# Sources claude-profiles.bash for base profile functions, then overrides
# them with amux-spawn-aware versions that route through amux-spawn when it
# is on PATH (full tracking, Telegram, session handle), falling back to the
# plain profile behaviour otherwise.
#
# If you only want profile aliases WITHOUT amux-spawn routing, source
# claude-profiles.bash directly instead.
#
# OPT-IN (REVERSIBLE)
#   Add ONE line to your bashrc:
#
#     source ~/.claude/shell/amux-spawn.bash
#
#   To UNDO: remove that line and restart the shell.

# Guard: don't re-source if already loaded.
if [[ "${_AMUX_SPAWN_SHELL_LOADED:-}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi
_AMUX_SPAWN_SHELL_LOADED=1

# Source profiles first (sets _CLAUDE_PROFILES_LOADED so it won't re-source).
_amux_spawn_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [[ -f "${_amux_spawn_dir}/claude-profiles.bash" ]]; then
    source "${_amux_spawn_dir}/claude-profiles.bash"
elif [[ -f "${HOME}/.claude/shell/claude-profiles.bash" ]]; then
    source "${HOME}/.claude/shell/claude-profiles.bash"
fi

# Locate amux_spawn_lib.py.
# Priority: installed copy (~/.claude/hooks/) → repo copy (sibling of shell/).
_amux_spawn_hooks_dir=""
if [[ -f "${HOME}/.claude/hooks/amux_spawn_lib.py" ]]; then
    _amux_spawn_hooks_dir="${HOME}/.claude/hooks"
else
    if [[ -n "$_amux_spawn_dir" && \
          -f "${_amux_spawn_dir}/../.claude/hooks/amux_spawn_lib.py" ]]; then
        _amux_spawn_hooks_dir="${_amux_spawn_dir}/../.claude/hooks"
    fi
fi
unset _amux_spawn_dir

# Override with amux-spawn-aware functions (replaces the profile-only versions).
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
