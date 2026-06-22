#!/usr/bin/env bash
# Bash completion for `amux-spawn` (task 10-05 / epic 10)
#
# Installed by install-claude-config.sh to ~/.local/share/bash-completion/completions/
# (or sourced from ~/.bashrc / /etc/bash_completion.d/).
#
# Completes:
#   amux-spawn a|attach <TAB>   → session SUFFIXES from live `amux ls`, preferring
#                                   the current workspace's sessions first.
#   amux-spawn <TAB>            → subcommands.
#   amux-spawn status|last <TAB>→ tracked session names (from ~/.amux/spawn/*.json).
#   amux-spawn spawn <TAB>      → (no suffix completion; too context-sensitive)
#   amux-spawn ls <TAB>         → flags.
#
# FAST: a single `amux ls` call (fast, no jq, no Python) per TAB press for the
# attach path; the tracking registry glob is local and instant.
# No heavy work; no network calls.

_amux_spawn_completions() {
    local cur prev words cword
    _init_completion 2>/dev/null || {
        # Fallback if bash-completion library is not installed.
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    }

    local subcommand="${words[1]:-}"

    # ── Top-level subcommand completion ──────────────────────────────────────
    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "spawn a attach status last ls" -- "$cur"))
        return
    fi

    # ── a / attach: complete the <suffix> from live amux sessions ─────────────
    if [[ "$subcommand" == "a" || "$subcommand" == "attach" ]] && [[ $cword -eq 2 ]]; then
        _amux_spawn_complete_suffix "$cur"
        return
    fi

    # ── status / last: complete the <handle> from the spawn registry ──────────
    if [[ "$subcommand" == "status" || "$subcommand" == "last" ]] && [[ $cword -eq 2 ]]; then
        local spawn_dir="${HOME}/.amux/spawn"
        if [[ -d "$spawn_dir" ]]; then
            local names=()
            local f
            for f in "$spawn_dir"/*.json; do
                [[ -f "$f" ]] || continue
                local base="${f##*/}"
                names+=("${base%.json}")
            done
            COMPREPLY=($(compgen -W "${names[*]}" -- "$cur"))
        fi
        return
    fi

    # ── ls: complete flags ────────────────────────────────────────────────────
    if [[ "$subcommand" == "ls" ]]; then
        COMPREPLY=($(compgen -W "--json --all --dir --run-id" -- "$cur"))
        return
    fi
}

# Complete the suffix part for `amux-spawn a|attach <suffix>`.
#
# Strategy:
#   1. Run `amux ls` once (fast; amux is a small shell script / binary).
#   2. Determine cwd-prefix = basename(cwd).
#   3. Build two lists:
#      a. Workspace sessions: names starting with <prefix>- or equal to <prefix>.
#         Strip the <prefix>- part and offer the suffix.
#      b. All other sessions: offer the full name (for cross-workspace switches).
#      Workspace suffixes come first in COMPREPLY for a cleaner Tab experience.
_amux_spawn_complete_suffix() {
    local cur="$1"
    local prefix
    prefix="$(basename "$(pwd)")"

    # Parse `amux ls` output: "<index>[*] <name> <dir>"
    local ws_completions=()
    local other_completions=()

    if command -v amux &>/dev/null; then
        local line name idx_col
        while IFS= read -r line; do
            # Columns: <N>[*]  <name>  <dir>
            read -r idx_col name _ <<<"$line"
            # idx_col is like "1" or "3*"
            idx_col="${idx_col%\*}"
            [[ "$idx_col" =~ ^[0-9]+$ ]] || continue
            [[ -n "$name" ]] || continue

            # Prefer workspace session suffixes.
            if [[ "$name" == "${prefix}-"* ]]; then
                local suffix="${name#${prefix}-}"
                ws_completions+=("$suffix")
            elif [[ "$name" == "$prefix" ]]; then
                # Bare prefix matches `a` with no suffix; offer "" or the full name.
                # Offer the full name so the user can Tab it in.
                ws_completions+=("$name")
            else
                other_completions+=("$name")
            fi
        done < <(amux ls 2>/dev/null)
    fi

    # Combine: workspace first, then all others.
    local all_completions=("${ws_completions[@]}" "${other_completions[@]}")
    COMPREPLY=($(compgen -W "${all_completions[*]}" -- "$cur"))
}

complete -F _amux_spawn_completions amux-spawn
