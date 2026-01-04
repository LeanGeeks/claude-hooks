# Bash completion for notify-interactive

_notify_interactive_completion() {
    local cur prev words cword
    _init_completion || return

    case "$prev" in
        -u|--urgency)
            COMPREPLY=($(compgen -W "low normal high critical" -- "$cur"))
            return
            ;;
        -l|--layout)
            COMPREPLY=($(compgen -W "horizontal vertical" -- "$cur"))
            return
            ;;
        -t|--timeout|-e|--expire|-n|--max-lines)
            # Numeric values
            return
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "
            -h --help
            -u --urgency
            -e --expire
            -a --action
            -l --layout
            -c --code
            -m --markdown
            -n --max-lines
            -w --wait
            -t --timeout
            -j --json
            --show-progress
        " -- "$cur"))
    fi
}

complete -F _notify_interactive_completion notify-interactive
