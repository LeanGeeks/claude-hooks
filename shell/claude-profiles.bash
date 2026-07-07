#!/usr/bin/env bash
# Claude model profiles — shell integration
#
# Auto-generates one wrapper function per profile in ~/.claude/profiles.toml
# at source-time. Each function exports the profile's env vars in a subshell
# and exec's claude directly.
#
# For amux-spawn integration (session tracking, Telegram notifications),
# source amux-spawn.bash instead — it sources this file and overrides the
# functions with amux-spawn routing.
#
# OPT-IN (REVERSIBLE)
#   Add ONE line to your bashrc:
#
#     source ~/.claude/shell/claude-profiles.bash
#
#   To UNDO: remove that line and restart the shell.

# Guard: don't re-source if already loaded.
if [[ "${_CLAUDE_PROFILES_LOADED:-}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi
_CLAUDE_PROFILES_LOADED=1

# Locate amux_spawn_lib.py.
# Priority: installed copy (~/.claude/hooks/) → repo copy (sibling of shell/).
_claude_profiles_hooks_dir=""
if [[ -f "${HOME}/.claude/hooks/amux_spawn_lib.py" ]]; then
    _claude_profiles_hooks_dir="${HOME}/.claude/hooks"
else
    _claude_profiles_this_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    if [[ -n "$_claude_profiles_this_dir" && \
          -f "${_claude_profiles_this_dir}/../.claude/hooks/amux_spawn_lib.py" ]]; then
        _claude_profiles_hooks_dir="${_claude_profiles_this_dir}/../.claude/hooks"
    fi
    unset _claude_profiles_this_dir
fi

# Eval the auto-generated shell functions (profiles only, no amux-spawn).
if [[ -n "$_claude_profiles_hooks_dir" ]]; then
    eval "$(AMUX_SPAWN_HOOKS_DIR="$_claude_profiles_hooks_dir" \
        python3 -c "
import sys, os
sys.path.insert(0, os.environ['AMUX_SPAWN_HOOKS_DIR'])
import amux_spawn_lib
print(amux_spawn_lib.emit_profile_functions())
" 2>/dev/null)" 2>/dev/null || true
fi

unset _claude_profiles_hooks_dir
