#!/bin/bash
# install.sh — Epic 29 working script.
# This is the WORKING copy being restructured. install-claude-config.sh is FROZEN.
# See tasks/29_installer_interactive/state.md for the bootstrapping hazard.
# Task 29-09 collapses this back into install-claude-config.sh.

set -euo pipefail

# CLAUDE_INSTALL_SCRIPT_DIR lets the test harness point patched-script copies
# back at the real repo root so PROJECT_CONFIG and other repo-relative paths
# resolve correctly even when the script file lives in a temp directory.
SCRIPT_DIR="${CLAUDE_INSTALL_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PROJECT_HOOKS_DIR="$SCRIPT_DIR/.claude/hooks"
PROJECT_BIN_DIR="$SCRIPT_DIR/.claude/bin"
PROJECT_STATUSLINE_DIR="$SCRIPT_DIR/.claude/statusline"
PROJECT_COMMANDS_DIR="$SCRIPT_DIR/.claude/commands"
PROJECT_CONFIG="$SCRIPT_DIR/.claude/settings.json"
PROJECT_RELAY_DIR="$SCRIPT_DIR/relay-server"
GLOBAL_HOOKS_DIR="$HOME/.claude/hooks"
GLOBAL_STATUSLINE_DIR="$HOME/.claude/statusline"
GLOBAL_COMMANDS_DIR="$HOME/.claude/commands"
GLOBAL_CONFIG="$HOME/.claude/settings.json"
BACKUP_DIR="$HOME/.claude/backups"
MANIFEST_FILE="$HOME/.claude/install-manifest.json"
MANIFEST_SCHEMA=1

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

# =============================================================================
# §0.1 REAL-$HOME GUARD — TEMPORARY — remove in 29-09.
# Guards the developer's machine while install.sh is under construction.
# See tasks/29_installer_interactive/state.md.
# =============================================================================
_guard_real_home() {
    if [[ -n "${CLAUDE_INSTALL_EPIC29_LIVE:-}" ]]; then
        return 0
    fi
    local real_home
    real_home="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6 || true)"
    if [[ -n "$real_home" && "$HOME" == "$real_home" ]]; then
        log_error "install.sh is under construction (epic 29) and refuses to run"
        log_error "against the real \$HOME without an explicit override."
        log_error ""
        log_error "Two alternatives:"
        log_error "  (1) Run against a temporary HOME with CLAUDE_INSTALL_NO_EXTERNAL=1:"
        log_error "      HOME=/tmp/test-home CLAUDE_INSTALL_NO_EXTERNAL=1 ./install.sh"
        log_error "  (2) To make a hook edit live, use the frozen copy instead:"
        log_error "      ./install-claude-config.sh"
        log_error ""
        log_error "To override (29-10 live verification only):"
        log_error "  CLAUDE_INSTALL_EPIC29_LIVE=1 ./install.sh"
        exit 1
    fi
}
_guard_real_home

# =============================================================================
# Housekeeping functions (always run, never a toggle)
# =============================================================================

# Rotate hook debug/error logs in ~/.claude/. Each install ships a new version,
# which is a natural rotation point. For every known log that exists and exceeds
# a small size threshold, gzip it into "<name>.<UTC-timestamp>.gz" (so the live
# log starts fresh next run), then prune all but the most recent N archives per
# base name. Tiny logs are left untouched so they aren't churned every run.
rotate_hook_logs() {
    local claude_dir="$HOME/.claude"
    local keep=5                 # archives to retain per base log name
    local min_bytes=$((1024 * 1024))  # only rotate logs larger than 1 MB
    local stamp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"

    # Base log names this project writes. reply_injector*.log is expanded via glob.
    local logs=(
        bash_hook_debug.log
        permission_request_debug.log
        permission_state_debug.log
        posttool_debug.log
        notification_hook_debug.log
        permission_telegram_errors.log
        telegram_daemon.log
    )
    # Include any reply_injector*.log files present.
    local rij
    for rij in "$claude_dir"/reply_injector*.log; do
        [[ -e "$rij" ]] && logs+=("$(basename "$rij")")
    done

    local name path size archives count old
    for name in "${logs[@]}"; do
        path="$claude_dir/$name"
        [[ -f "$path" ]] || continue
        size=$(wc -c < "$path" 2>/dev/null || echo 0)
        if [[ "$size" -le "$min_bytes" ]]; then
            continue
        fi

        if gzip -c "$path" > "$path.$stamp.gz" 2>/dev/null; then
            : > "$path"
            log_info "Rotated log: $name → $name.$stamp.gz ($(( size / 1024 )) KB)"
        else
            log_warn "Could not rotate log: $name"
            rm -f "$path.$stamp.gz"
            continue
        fi

        archives=()
        while IFS= read -r old; do
            [[ -n "$old" ]] && archives+=("$old")
        done < <(ls -1t "$path".*.gz 2>/dev/null)
        count=${#archives[@]}
        if [[ "$count" -gt "$keep" ]]; then
            for old in "${archives[@]:$keep}"; do
                rm -f "$old"
                log_info "  Pruned old archive: $(basename "$old")"
            done
        fi
    done
}

# =============================================================================
# §1. FEATURE REGISTRY
# =============================================================================
# Presentation order. Also the execution order (dependencies come first).
FEATURES=(
    statusline
    permission-hooks
    telegram
    profiles
    amux
    permissions-allowlist
    context-mcp
    claude-history
    questions
    daily-review
)

# Full list of hook .py modules this project installs (brd constraint 2.1).
# Must match MODULE_OWNERS keys exactly — asserted at startup (§2).
REQUIRED_HOOKS=(
    "pretool_hook.py"
    "bash_command_parser.py"
    "settings_loader.py"
    "notification_hook.py"
    "permission_request_hook.py"
    "permission_state_store.py"
    "session_yolo_store.py"
    "settings_writer.py"
    "project_key.py"
    "telegram_permission_router.py"
    "posttool_hook.py"
    "reply_injector.py"
    "amux_spawn_lib.py"
    "spawn_producer_hook.py"
    "codex_event_reducer.py"
    "claude_event_reducer.py"
    "lifecycle_events.py"
    "roles_config.py"
    "questions_store.py"
    "questions_listen_lib.py"
)

# ---------------------------------------------------------------------------
# Feature: statusline
# ---------------------------------------------------------------------------
feature_statusline_title()      { echo "Statusline (+ per-subagent rows)"; }
feature_statusline_writes()     { echo ""; }
feature_statusline_default()    { echo "install"; }
feature_statusline_requires()   { echo ""; }
feature_statusline_modules()    { echo ""; }
feature_statusline_suboptions() { echo ""; }

feature_statusline_probe() {
    # Artifact AND wiring: statusline.py on disk AND settings.json references it
    [[ -f "$GLOBAL_STATUSLINE_DIR/statusline.py" ]] || return 1
    [[ -f "$GLOBAL_CONFIG" ]] || return 1
    jq -e --arg dir "$GLOBAL_STATUSLINE_DIR" \
        '.statusLine.command? // "" | contains($dir)' \
        "$GLOBAL_CONFIG" >/dev/null 2>&1
}

feature_statusline_install() {
    # Lifted from STEP 2 of install-claude-config.sh
    log_step "Installing: $(feature_statusline_title)"

    if [[ ! -d "$PROJECT_STATUSLINE_DIR" ]]; then
        log_warn "Project statusline directory not found: $PROJECT_STATUSLINE_DIR"
        log_warn "Skipping statusline installation..."
        return 0
    fi

    local statusline_script="$PROJECT_STATUSLINE_DIR/statusline.py"
    if [[ ! -f "$statusline_script" ]]; then
        log_warn "statusline.py not found in $PROJECT_STATUSLINE_DIR"
        log_warn "Skipping statusline installation..."
        return 0
    fi

    mkdir -p "$GLOBAL_STATUSLINE_DIR"
    cp "$statusline_script" "$GLOBAL_STATUSLINE_DIR/statusline.py"
    chmod +x "$GLOBAL_STATUSLINE_DIR/statusline.py"
    log_info "Installed: statusline.py → $GLOBAL_STATUSLINE_DIR/statusline.py"
    STATUSLINE_INSTALLED=true

    # Per-subagent status line
    local subagent_script="$PROJECT_STATUSLINE_DIR/subagent.py"
    if [[ -f "$subagent_script" ]]; then
        cp "$subagent_script" "$GLOBAL_STATUSLINE_DIR/subagent.py"
        chmod +x "$GLOBAL_STATUSLINE_DIR/subagent.py"
        SUBAGENT_STATUSLINE_INSTALLED=true
        log_info "Installed: subagent.py → $GLOBAL_STATUSLINE_DIR/subagent.py"
    else
        log_warn "subagent.py not found — agent panel rows stay at their defaults"
    fi

    # Pricing config
    local pricing_default="$PROJECT_STATUSLINE_DIR/pricing.default.json"
    if [[ -f "$pricing_default" ]]; then
        cp "$pricing_default" "$GLOBAL_STATUSLINE_DIR/pricing.default.json"
        log_info "Installed: pricing.default.json → $GLOBAL_STATUSLINE_DIR/pricing.default.json"
    else
        log_warn "pricing.default.json not found — API cost will render as 'cost ?'"
    fi

    # Slash commands (/yolo, /yolo-off) — logically belong with permission-hooks
    # but currently live in this step (behaviour-neutral for 29-02; will move in a
    # later task if needed).
    if [[ -d "$PROJECT_COMMANDS_DIR" ]]; then
        mkdir -p "$GLOBAL_COMMANDS_DIR"
        shopt -s nullglob
        for cmd in "$PROJECT_COMMANDS_DIR"/*.md; do
            cp "$cmd" "$GLOBAL_COMMANDS_DIR/"
            log_info "Installed: $(basename "$cmd") → $GLOBAL_COMMANDS_DIR/$(basename "$cmd")"
            COMMANDS_INSTALLED=true
        done
        shopt -u nullglob
    fi

    # Contribute to settings
    if [[ "$STATUSLINE_INSTALLED" == true ]]; then
        HOOKS_JSON_SETTINGS_STATUSLINE=true
    fi
}

feature_statusline_uninstall() {
    log_error "feature 'statusline' uninstall not implemented until 29-05"
    return 1
}

# ---------------------------------------------------------------------------
# Feature: permission-hooks
# ---------------------------------------------------------------------------
feature_permission_hooks_title()      { echo "Permission hooks (bash guard, approvals, /yolo)"; }
feature_permission_hooks_writes()     { echo ""; }
feature_permission_hooks_default()    { echo "install"; }
feature_permission_hooks_requires()   { echo ""; }
feature_permission_hooks_modules()    {
    echo "pretool_hook.py bash_command_parser.py settings_loader.py permission_request_hook.py session_yolo_store.py permission_state_store.py project_key.py"
}
feature_permission_hooks_suboptions() { echo ""; }

feature_permission_hooks_probe() {
    # Artifact AND wiring
    [[ -f "$GLOBAL_HOOKS_DIR/pretool_hook.py" ]] || return 1
    [[ -f "$GLOBAL_CONFIG" ]] || return 1
    jq -e --arg dir "$GLOBAL_HOOKS_DIR" \
        '.hooks.PreToolUse // [] | any(.[]; .hooks // [] | any(.[]; .command // "" | contains($dir)))' \
        "$GLOBAL_CONFIG" >/dev/null 2>&1
}

feature_permission_hooks_install() {
    log_step "Installing: $(feature_permission_hooks_title)"
    _install_modules "$(feature_permission_hooks_modules)" || return 0

    # Build PreToolUse and PermissionRequest hook config
    HOOKS_JSON=$(echo "$HOOKS_JSON" | jq \
        --arg pretool_path "$GLOBAL_HOOKS_DIR/pretool_hook.py" \
        --arg permission_path "$GLOBAL_HOOKS_DIR/permission_request_hook.py" \
        '. + {
            PreToolUse: [{
                matcher: "Bash|Monitor",
                hooks: [{
                    type: "command",
                    command: ("python3 " + $pretool_path)
                }]
            }],
            PermissionRequest: [{
                matcher: "*",
                hooks: [{
                    type: "command",
                    command: ("CLAUDE_HOOK_DEBUG=1 python3 " + $permission_path),
                    timeout: 43200
                }]
            }]
        }')

    HOOKS_INSTALLED=true
    log_info "Permission hooks wired (PreToolUse, PermissionRequest)"
}

feature_permission_hooks_uninstall() {
    log_error "feature 'permission-hooks' uninstall not implemented until 29-05"
    return 1
}

# ---------------------------------------------------------------------------
# Feature: telegram
# ---------------------------------------------------------------------------
feature_telegram_title()      { echo "Telegram integration"; }
feature_telegram_writes()     { echo "python user-site .pth, ~/.local/bin/claude-roles"; }
feature_telegram_default()    { echo "install"; }
feature_telegram_requires()   { echo "permission-hooks"; }
feature_telegram_modules()    {
    echo "bash_command_parser.py settings_loader.py permission_state_store.py project_key.py telegram_permission_router.py posttool_hook.py notification_hook.py reply_injector.py settings_writer.py roles_config.py"
}
feature_telegram_suboptions() { echo ""; }

feature_telegram_probe() {
    # Artifact AND wiring: telegram_permission_router.py AND hooks.PostToolUse
    [[ -f "$GLOBAL_HOOKS_DIR/telegram_permission_router.py" ]] || return 1
    [[ -f "$GLOBAL_CONFIG" ]] || return 1
    jq -e --arg dir "$GLOBAL_HOOKS_DIR" \
        '.hooks.PostToolUse // [] | any(.[]; .hooks // [] | any(.[]; .command // "" | contains($dir)))' \
        "$GLOBAL_CONFIG" >/dev/null 2>&1
}

feature_telegram_install() {
    log_step "Installing: $(feature_telegram_title)"
    _install_modules "$(feature_telegram_modules)" || return 0

    # .pth file so relay_server is importable by the hooks
    if [[ -f "$PROJECT_RELAY_DIR/relay_server/__init__.py" ]]; then
        local user_site
        user_site="$(python3 -m site --user-site 2>/dev/null)"
        if [[ -n "$user_site" ]]; then
            mkdir -p "$user_site"
            echo "$PROJECT_RELAY_DIR" > "$user_site/claude-relay-server.pth"
            log_info "  Linked relay_server via $user_site/claude-relay-server.pth"
        else
            log_warn "  Could not determine user site-packages; Telegram relay hooks may not import relay_server"
        fi
        # Warn early if httpx is absent
        if ! python3 -c "import httpx" 2>/dev/null; then
            log_warn "  Python 'httpx' not available for system python3 — Telegram relay will stay disabled."
            log_warn "  Install it with: sudo apt install python3-httpx"
        fi
    fi

    # claude-roles diagnostic tool
    local claude_shell_dir="$HOME/.claude/shell"
    local user_bin_dir="$HOME/.local/bin"
    local claude_roles_src="$SCRIPT_DIR/shell/claude-roles"
    if [[ -f "$claude_roles_src" ]]; then
        mkdir -p "$claude_shell_dir"
        cp "$claude_roles_src" "$claude_shell_dir/claude-roles"
        chmod +x "$claude_shell_dir/claude-roles"
        log_info "Installed: claude-roles → $claude_shell_dir/claude-roles"
        if [[ -d "$user_bin_dir" ]]; then
            case ":$PATH:" in
                *":$user_bin_dir:"*)
                    ln -sf "$claude_shell_dir/claude-roles" "$user_bin_dir/claude-roles"
                    log_info "  Symlinked: $user_bin_dir/claude-roles → $claude_shell_dir/claude-roles"
                    ;;
            esac
        fi
        CLAUDE_ROLES_INSTALLED=true
    else
        log_warn "claude-roles not found at $claude_roles_src — skipping"
    fi

    # permissions MCP server in ~/.claude.json
    local permissions_mcp_script="$SCRIPT_DIR/permissions-mcp/server.py"
    local claude_json="$HOME/.claude.json"
    if [[ "$UV_AVAILABLE" == true && -f "$permissions_mcp_script" ]]; then
        if [[ ! -f "$claude_json" ]]; then
            echo '{}' > "$claude_json"
        fi
        if jq empty "$claude_json" 2>/dev/null; then
            jq --arg script "$permissions_mcp_script" --arg repo "$SCRIPT_DIR" \
                '.mcpServers = (.mcpServers // {}) + {"permissions": {type: "stdio", command: "uv", args: ["run", "--script", $script], env: {"CLAUDE_HOOKS_REPO": $repo}}}' \
                "$claude_json" > "$claude_json.tmp"
            if jq empty "$claude_json.tmp" 2>/dev/null; then
                mv "$claude_json.tmp" "$claude_json"
                log_info "MCP server registered in ~/.claude.json: permissions"
                PERMISSIONS_MCP_INSTALLED=true
            else
                log_warn "Failed to produce valid JSON for ~/.claude.json — permissions MCP server not registered"
                rm -f "$claude_json.tmp"
            fi
        else
            log_warn "~/.claude.json is not valid JSON — skipping permissions MCP server registration"
        fi
    fi

    # Build PostToolUse and Notification hook config
    HOOKS_JSON=$(echo "$HOOKS_JSON" | jq \
        --arg posttool_path "$GLOBAL_HOOKS_DIR/posttool_hook.py" \
        --arg notification_path "$GLOBAL_HOOKS_DIR/notification_hook.py" \
        --arg producer_path "$GLOBAL_HOOKS_DIR/spawn_producer_hook.py" \
        '. + {
            PostToolUse: [{
                matcher: "*",
                hooks: [{
                    type: "command",
                    command: ("python3 " + $posttool_path)
                }]
            }],
            Notification: [
                {
                    matcher: "idle_prompt",
                    hooks: [{
                        type: "command",
                        command: ("CLAUDE_HOOK_DEBUG=1 python3 " + $notification_path)
                    }]
                },
                {
                    matcher: "permission_prompt",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $producer_path + " --event Notification")
                    }]
                }
            ]
        }')

    log_info "Telegram hooks wired (PostToolUse, Notification)"
}

feature_telegram_uninstall() {
    log_error "feature 'telegram' uninstall not implemented until 29-05"
    return 1
}

# ---------------------------------------------------------------------------
# Feature: profiles
# ---------------------------------------------------------------------------
feature_profiles_title()      { echo "Model profiles"; }
feature_profiles_writes()     { echo ""; }
feature_profiles_default()    { echo "install"; }
feature_profiles_requires()   { echo ""; }
feature_profiles_modules()    { echo "amux_spawn_lib.py"; }
feature_profiles_suboptions() { echo "profiles-autosource"; }

feature_profiles_probe() {
    # Artifact: claude-profiles.bash installed (no settings wiring for profiles)
    [[ -f "$HOME/.claude/shell/claude-profiles.bash" ]]
}

feature_profiles_install() {
    # Lifted from STEP 3a and part of STEP 3b
    log_step "Installing: $(feature_profiles_title)"
    _install_modules "$(feature_profiles_modules)" || true  # modules are shared, non-fatal if missing

    # profiles.toml — never overwrite, mode 600
    local profiles_src="$SCRIPT_DIR/shell/profiles.example.toml"
    local profiles_dest="$HOME/.claude/profiles.toml"
    if [[ -f "$profiles_dest" ]]; then
        log_info "profiles.toml already present — skipping (never overwrite user config)"
        log_info "  $profiles_dest"
    elif [[ -f "$profiles_src" ]]; then
        mkdir -p "$(dirname "$profiles_dest")"
        cp "$profiles_src" "$profiles_dest"
        chmod 600 "$profiles_dest"
        log_info "Installed: profiles.example.toml → $profiles_dest"
        PROFILES_INSTALLED=true
    else
        log_warn "profiles.example.toml not found at $profiles_src — skipping"
    fi

    if [[ -f "$profiles_dest" ]]; then
        log_info "Edit ~/.claude/profiles.toml with your model tokens."
    fi

    # Shell snippet: claude-profiles.bash
    local profiles_snippet_src="$SCRIPT_DIR/shell/claude-profiles.bash"
    local claude_shell_dir="$HOME/.claude/shell"
    mkdir -p "$claude_shell_dir"
    if [[ -f "$profiles_snippet_src" ]]; then
        cp "$profiles_snippet_src" "$claude_shell_dir/claude-profiles.bash"
        chmod 644 "$claude_shell_dir/claude-profiles.bash"
        log_info "Installed shell snippet: claude-profiles.bash → $claude_shell_dir/claude-profiles.bash"
        SNIPPET_INSTALLED=true
    else
        log_warn "Shell snippet not found at $profiles_snippet_src — skipping"
    fi
}

feature_profiles_uninstall() {
    log_error "feature 'profiles' uninstall not implemented until 29-05"
    return 1
}

# Sub-toggle probe: profiles-autosource
feature_profiles_autosource_probe() {
    local marker="# claude-hooks:profiles-autosource"
    [[ -f "$HOME/.bashrc" ]] && grep -Fxq "$marker" "$HOME/.bashrc" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Feature: amux
# ---------------------------------------------------------------------------
feature_amux_title()      { echo "amux integration"; }
feature_amux_writes()     { echo "/usr/local/bin/amux (sudo), ~/.tmux.conf, ~/.local/bin/amux-spawn, bash-completion dir"; }
feature_amux_default()    { echo "install"; }
feature_amux_requires()   { echo "profiles"; }
feature_amux_modules()    {
    echo "permission_state_store.py amux_spawn_lib.py lifecycle_events.py claude_event_reducer.py codex_event_reducer.py spawn_producer_hook.py"
}
feature_amux_suboptions() { echo "amux-autowrap"; }

feature_amux_probe() {
    # Artifact AND wiring: amux-spawn on PATH AND hooks.Stop references GLOBAL_HOOKS_DIR
    [[ -f "$HOME/.local/bin/amux-spawn" ]] || return 1
    [[ -f "$GLOBAL_CONFIG" ]] || return 1
    jq -e --arg dir "$GLOBAL_HOOKS_DIR" \
        '.hooks.Stop // [] | any(.[]; .hooks // [] | any(.[]; .command // "" | contains($dir)))' \
        "$GLOBAL_CONFIG" >/dev/null 2>&1
}

feature_amux_install() {
    # Lifted from STEP 3, STEP 3b, STEP 3c
    log_step "Installing: $(feature_amux_title)"
    _install_modules "$(feature_amux_modules)" || true

    local user_bin_dir="$HOME/.local/bin"

    # amux-spawn launcher
    if [[ ! -f "$PROJECT_BIN_DIR/amux-spawn" ]]; then
        log_warn "amux-spawn not found at $PROJECT_BIN_DIR/amux-spawn — skipping launcher install"
    else
        mkdir -p "$user_bin_dir"
        cp "$PROJECT_BIN_DIR/amux-spawn" "$user_bin_dir/amux-spawn"
        chmod +x "$user_bin_dir/amux-spawn"
        log_info "Installed: amux-spawn → $user_bin_dir/amux-spawn"
        AMUX_SPAWN_INSTALLED=true

        # Sanity import check
        if [[ "$HOOKS_INSTALLED" == true ]]; then
            if python3 -c "import sys; sys.path.insert(0, '$GLOBAL_HOOKS_DIR'); import amux_spawn_lib, codex_event_reducer" 2>/dev/null; then
                log_info "  amux_spawn_lib + codex_event_reducer importable from $GLOBAL_HOOKS_DIR"
            else
                log_warn "  amux_spawn_lib / codex_event_reducer not importable — amux-spawn may fail"
            fi
        fi

        # Codex provider probe
        local amux_resolved
        amux_resolved="$(command -v amux 2>/dev/null || true)"
        AMUX_CODEX_STATUS="unknown (amux not on PATH)"
        if [[ -n "$amux_resolved" ]]; then
            if grep -q -- "__codex-run" "$amux_resolved" 2>/dev/null; then
                log_info "  amux at $amux_resolved carries the Codex provider surface (epic 20)"
                AMUX_CODEX_STATUS="capable ($amux_resolved)"
            else
                log_warn "  amux at $amux_resolved predates the Codex provider (epic 20)."
                log_warn "    Codex workers will fail until amux is updated: ./install-amux.sh"
                AMUX_CODEX_STATUS="STALE ($amux_resolved lacks the Codex provider)"
            fi
        fi

        case ":$PATH:" in
            *":$user_bin_dir:"*) ;;
            *) log_warn "  $user_bin_dir is not on your PATH. Add it, e.g.: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
        esac
    fi

    # Bash completion
    local completion_src="$SCRIPT_DIR/shell/amux-spawn-completion.bash"
    local user_completions_dir="$HOME/.local/share/bash-completion/completions"
    if [[ -f "$completion_src" ]]; then
        mkdir -p "$user_completions_dir"
        cp "$completion_src" "$user_completions_dir/amux-spawn"
        chmod 644 "$user_completions_dir/amux-spawn"
        log_info "Installed completion: amux-spawn → $user_completions_dir/amux-spawn"
        COMPLETION_INSTALLED=true
    else
        log_warn "Completion script not found at $completion_src — skipping"
    fi

    # amux-spawn.bash shell snippet
    local amux_snippet_src="$SCRIPT_DIR/shell/amux-spawn.bash"
    local claude_shell_dir="$HOME/.claude/shell"
    mkdir -p "$claude_shell_dir"
    if [[ -f "$amux_snippet_src" ]]; then
        cp "$amux_snippet_src" "$claude_shell_dir/amux-spawn.bash"
        chmod 644 "$claude_shell_dir/amux-spawn.bash"
        log_info "Installed shell snippet: amux-spawn.bash → $claude_shell_dir/amux-spawn.bash"
        SNIPPET_INSTALLED=true
    else
        log_warn "Shell snippet not found at $amux_snippet_src — skipping"
    fi

    # Print opt-in instructions (not applied automatically)
    if [[ "$SNIPPET_INSTALLED" == true ]]; then
        echo ""
        echo "  ┌─────────────────────────────────────────────────────────────────────┐"
        echo "  │  Shell integration opt-in — choose ONE:                            │"
        echo "  │                                                                     │"
        echo "  │  Profiles only (model aliases, no amux/Telegram):                  │"
        echo "  │    source $claude_shell_dir/claude-profiles.bash"
        echo "  │                                                                     │"
        echo "  │  Profiles + amux-spawn (session tracking, Telegram notifications): │"
        echo "  │    source $claude_shell_dir/amux-spawn.bash"
        echo "  │                                                                     │"
        echo "  │  Do NOT source both — amux-spawn.bash sources profiles internally. │"
        echo "  └─────────────────────────────────────────────────────────────────────┘"
        echo ""
        echo "  ┌─────────────────────────────────────────────────────────────────────┐"
        echo "  │  Migrating from claude.bashrc env functions to profiles.toml:       │"
        echo "  │                                                                     │"
        echo "  │  1. Copy tokens from claude_*_env() into [vars] in profiles.toml.  │"
        echo "  │  2. Translate each claude_*_env() into a [profile.*] section.       │"
        echo "  │  3. Move shared env vars (timeouts, PATs) into [all-profiles].      │"
        echo "  │  4. Remove the old wrapper functions from claude.bashrc.             │"
        echo "  │  5. Keep non-Claude env vars (TaskMaster, Milvus, etc.) in          │"
        echo "  │     claude.bashrc.                                                   │"
        echo "  └─────────────────────────────────────────────────────────────────────┘"
        echo ""
    fi

    # tmux options (focus-events, tab title)
    _install_tmux_options
    AMUX_CODEX_STATUS="${AMUX_CODEX_STATUS:-not probed}"

    # Build amux hooks: UserPromptSubmit, Stop, SubagentStop, SessionEnd
    if [[ "$AMUX_SPAWN_INSTALLED" == true ]]; then
        HOOKS_JSON=$(echo "$HOOKS_JSON" | jq \
            --arg producer_path "$GLOBAL_HOOKS_DIR/spawn_producer_hook.py" \
            '. + {
                UserPromptSubmit: [{
                    matcher: "*",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $producer_path + " --event UserPromptSubmit")
                    }]
                }],
                Stop: [{
                    matcher: "*",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $producer_path + " --event Stop")
                    }]
                }],
                SubagentStop: [{
                    matcher: "*",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $producer_path + " --event SubagentStop")
                    }]
                }],
                SessionEnd: [{
                    matcher: "*",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $producer_path + " --event SessionEnd")
                    }]
                }]
            }')
        log_info "amux hooks wired (UserPromptSubmit, Stop, SubagentStop, SessionEnd)"
    fi
}

feature_amux_uninstall() {
    log_error "feature 'amux' uninstall not implemented until 29-05"
    return 1
}

# Sub-toggle probe: amux-autowrap
feature_amux_autowrap_probe() {
    local marker="# claude-hooks:amux-autowrap"
    [[ -f "$HOME/.bashrc" ]] && grep -Fxq "$marker" "$HOME/.bashrc" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Feature: permissions-allowlist
# ---------------------------------------------------------------------------
feature_permissions_allowlist_title()      { echo "Global permissions allowlist"; }
feature_permissions_allowlist_writes()     { echo ""; }
feature_permissions_allowlist_default()    { echo "install"; }
feature_permissions_allowlist_requires()   { echo ""; }
feature_permissions_allowlist_modules()    { echo ""; }
feature_permissions_allowlist_suboptions() { echo ""; }

feature_permissions_allowlist_probe() {
    [[ -f "$GLOBAL_CONFIG" ]] || return 1
    jq -e '.permissions.allow // [] | length > 0' "$GLOBAL_CONFIG" >/dev/null 2>&1
}

feature_permissions_allowlist_install() {
    log_step "Installing: $(feature_permissions_allowlist_title)"
    # Settings contribution flag — actual merge happens in build_and_write_settings
    PERMISSIONS_ALLOWLIST_SELECTED=true
    log_info "Permissions allowlist will be merged from project config"
}

feature_permissions_allowlist_uninstall() {
    log_error "feature 'permissions-allowlist' uninstall not implemented until 29-05"
    return 1
}

# ---------------------------------------------------------------------------
# Feature: context-mcp
# ---------------------------------------------------------------------------
feature_context_mcp_title()      { echo "MCP: context-usage"; }
feature_context_mcp_writes()     { echo "~/.claude.json mcpServers.context-usage"; }
feature_context_mcp_default()    { echo "install"; }
feature_context_mcp_requires()   { echo ""; }  # requires uv (checked at runtime)
feature_context_mcp_modules()    { echo ""; }
feature_context_mcp_suboptions() { echo ""; }

feature_context_mcp_probe() {
    local claude_json="$HOME/.claude.json"
    [[ -f "$claude_json" ]] || return 1
    jq -e '.mcpServers["context-usage"]' "$claude_json" >/dev/null 2>&1
}

feature_context_mcp_install() {
    log_step "Installing: $(feature_context_mcp_title)"
    local context_mcp_script="$SCRIPT_DIR/context-mcp/server.py"
    local claude_json="$HOME/.claude.json"

    if [[ "$UV_AVAILABLE" != true ]]; then
        log_warn "uv not available — skipping context-usage MCP server"
        return 0
    fi
    if [[ ! -f "$context_mcp_script" ]]; then
        log_warn "context-mcp server.py not found at $context_mcp_script — skipping"
        return 0
    fi

    if [[ ! -f "$claude_json" ]]; then
        echo '{}' > "$claude_json"
    fi
    if jq empty "$claude_json" 2>/dev/null; then
        jq --arg script "$context_mcp_script" \
            '.mcpServers = (.mcpServers // {}) + {"context-usage": {type: "stdio", command: "uv", args: ["run", "--script", $script], env: {}}}' \
            "$claude_json" > "$claude_json.tmp"
        if jq empty "$claude_json.tmp" 2>/dev/null; then
            mv "$claude_json.tmp" "$claude_json"
            log_info "MCP server registered in ~/.claude.json: context-usage"
            CONTEXT_MCP_INSTALLED=true
        else
            log_warn "Failed to produce valid JSON for ~/.claude.json — context-usage MCP not registered"
            rm -f "$claude_json.tmp"
        fi
    else
        log_warn "~/.claude.json is not valid JSON — skipping context-usage MCP registration"
    fi
}

feature_context_mcp_uninstall() {
    log_error "feature 'context-mcp' uninstall not implemented until 29-05"
    return 1
}

# ---------------------------------------------------------------------------
# Feature: claude-history
# ---------------------------------------------------------------------------
feature_claude_history_title()      { echo "claude-history"; }
feature_claude_history_writes()     { echo "~/.local/bin/claude-history"; }
feature_claude_history_default()    { echo "install"; }
feature_claude_history_requires()   { echo ""; }
feature_claude_history_modules()    { echo ""; }
feature_claude_history_suboptions() { echo ""; }

feature_claude_history_probe() {
    [[ -f "$HOME/.local/bin/claude-history" ]]
}

feature_claude_history_install() {
    # 29-08 implements this feature. For 29-02 it is a no-op — the feature is
    # registered in the registry so the registry assertions can verify it, but
    # the actual install code is deferred to task 29-08.
    log_step "Installing: $(feature_claude_history_title)"
    log_info "claude-history install deferred to task 29-08 — skipping"
    return 0
}

feature_claude_history_uninstall() {
    log_error "feature 'claude-history' uninstall not implemented until 29-05"
    return 1
}

# ---------------------------------------------------------------------------
# Feature: questions
# ---------------------------------------------------------------------------
feature_questions_title()      { echo "Async questions"; }
feature_questions_writes()     { echo "~/.local/bin/claude-questions, ~/.local/bin/questions-listen, systemd unit file"; }
feature_questions_default()    { echo "skip"; }  # D14: defaults OFF
feature_questions_requires()   { echo "telegram"; }
feature_questions_modules()    { echo "roles_config.py questions_store.py questions_listen_lib.py"; }
feature_questions_suboptions() { echo "questions-listen"; }

feature_questions_probe() {
    # Artifact: questions-listen installed to ~/.claude/bin/
    [[ -f "$HOME/.claude/bin/questions-listen" ]]
}

feature_questions_install() {
    # Lifted from the questions section of STEP 5 in install-claude-config.sh
    log_step "Installing: $(feature_questions_title)"
    _install_modules "$(feature_questions_modules)" || return 0

    local user_bin_dir="$HOME/.local/bin"
    local claude_shell_dir="$HOME/.claude/shell"
    local claude_bin_dir="$HOME/.claude/bin"
    mkdir -p "$claude_bin_dir"

    # questions MCP server in ~/.claude.json
    local questions_mcp_script="$SCRIPT_DIR/questions-mcp/server.py"
    local claude_json="$HOME/.claude.json"
    if [[ "$UV_AVAILABLE" == true && -f "$questions_mcp_script" ]]; then
        if [[ ! -f "$claude_json" ]]; then
            echo '{}' > "$claude_json"
        fi
        if jq empty "$claude_json" 2>/dev/null; then
            jq --arg script "$questions_mcp_script" --arg repo "$SCRIPT_DIR" \
                '.mcpServers = (.mcpServers // {}) + {"questions": {type: "stdio", command: "uv", args: ["run", "--script", $script], env: {"CLAUDE_HOOKS_REPO": $repo}}}' \
                "$claude_json" > "$claude_json.tmp"
            if jq empty "$claude_json.tmp" 2>/dev/null; then
                mv "$claude_json.tmp" "$claude_json"
                log_info "MCP server registered in ~/.claude.json: questions"
                QUESTIONS_MCP_INSTALLED=true
            else
                log_warn "Failed to produce valid JSON for ~/.claude.json — questions MCP not registered"
                rm -f "$claude_json.tmp"
            fi
        else
            log_warn "~/.claude.json is not valid JSON — skipping questions MCP registration"
        fi
    fi

    # claude-questions diagnostic tool
    local claude_questions_src="$SCRIPT_DIR/shell/claude-questions"
    if [[ -f "$claude_questions_src" ]]; then
        mkdir -p "$claude_shell_dir"
        cp "$claude_questions_src" "$claude_shell_dir/claude-questions"
        chmod +x "$claude_shell_dir/claude-questions"
        log_info "Installed: claude-questions → $claude_shell_dir/claude-questions"
        if [[ -d "$user_bin_dir" ]]; then
            case ":$PATH:" in
                *":$user_bin_dir:"*)
                    ln -sf "$claude_shell_dir/claude-questions" "$user_bin_dir/claude-questions"
                    log_info "  Symlinked: $user_bin_dir/claude-questions → $claude_shell_dir/claude-questions"
                    ;;
            esac
        fi
        CLAUDE_QUESTIONS_INSTALLED=true
    else
        log_warn "claude-questions not found at $claude_questions_src — skipping"
    fi

    # questions-listen binary (copy into ~/.claude/bin/, symlink into ~/.local/bin/)
    local questions_listen_src="$SCRIPT_DIR/.claude/bin/questions-listen"
    if [[ -f "$questions_listen_src" ]]; then
        cp "$questions_listen_src" "$claude_bin_dir/questions-listen"
        chmod +x "$claude_bin_dir/questions-listen"
        log_info "Installed: questions-listen → $claude_bin_dir/questions-listen"
        if [[ -d "$user_bin_dir" ]]; then
            ln -sf "$claude_bin_dir/questions-listen" "$user_bin_dir/questions-listen"
            log_info "  Symlinked: $user_bin_dir/questions-listen → $claude_bin_dir/questions-listen"
            QUESTIONS_LISTEN_INSTALLED=true
        else
            log_warn "  $user_bin_dir does not exist — questions-listen not on PATH"
        fi
    else
        log_warn "questions-listen not found at $questions_listen_src — skipping"
    fi

    # systemd unit (install; enable only via questions-listen sub-toggle)
    _install_questions_listen_unit
}

feature_questions_uninstall() {
    log_error "feature 'questions' uninstall not implemented until 29-05"
    return 1
}

# Sub-toggle probe: questions-listen (systemd unit enabled)
feature_questions_listen_probe() {
    systemctl --user is-enabled "claude-questions-listen.service" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Feature: daily-review
# ---------------------------------------------------------------------------
feature_daily_review_title()      { echo "Daily permission review"; }
feature_daily_review_writes()     { echo ""; }
feature_daily_review_default()    { echo "skip"; }  # D14: defaults OFF
feature_daily_review_requires()   { echo "amux"; }
feature_daily_review_modules()    { echo ""; }
feature_daily_review_suboptions() { echo "daily-review-cron"; }

feature_daily_review_probe() {
    # No files installed — check for the cron marker as a proxy
    crontab -l 2>/dev/null | grep -q "claude-hooks:daily-review" 2>/dev/null
}

feature_daily_review_install() {
    # 29-08 implements this feature. For 29-02 it is a no-op.
    log_step "Installing: $(feature_daily_review_title)"
    log_info "daily-review install deferred to task 29-08 — skipping"
    return 0
}

feature_daily_review_uninstall() {
    log_error "feature 'daily-review' uninstall not implemented until 29-05"
    return 1
}

# Sub-toggle probe: daily-review-cron
feature_daily_review_cron_probe() {
    crontab -l 2>/dev/null | grep -q "# claude-hooks:daily-review-cron" 2>/dev/null
}

# =============================================================================
# §2. MODULE OWNERS AND STARTUP ASSERTIONS
# =============================================================================

# Map from module basename to the space-separated list of feature ids that own it.
# Used for dependency-closure install and refcounted uninstall (brd D4).
# Must cover exactly REQUIRED_HOOKS — asserted at startup.
declare -A MODULE_OWNERS=(
    [bash_command_parser.py]="permission-hooks telegram"
    [settings_loader.py]="permission-hooks telegram"
    [pretool_hook.py]="permission-hooks"
    [permission_request_hook.py]="permission-hooks"
    [session_yolo_store.py]="permission-hooks"
    [permission_state_store.py]="permission-hooks telegram amux"
    [project_key.py]="permission-hooks telegram"
    [telegram_permission_router.py]="telegram"
    [posttool_hook.py]="telegram"
    [notification_hook.py]="telegram"
    [reply_injector.py]="telegram"
    [settings_writer.py]="telegram"
    [roles_config.py]="telegram questions"
    [amux_spawn_lib.py]="amux profiles"
    [lifecycle_events.py]="amux"
    [claude_event_reducer.py]="amux"
    [codex_event_reducer.py]="amux"
    [spawn_producer_hook.py]="amux"
    [questions_store.py]="questions"
    [questions_listen_lib.py]="questions"
)

assert_registry_integrity() {
    local errors=0

    # Assertion 1: every key of MODULE_OWNERS is in REQUIRED_HOOKS and vice versa
    local mod hook found
    for mod in "${!MODULE_OWNERS[@]}"; do
        found=false
        for hook in "${REQUIRED_HOOKS[@]}"; do
            [[ "$hook" == "$mod" ]] && { found=true; break; }
        done
        if [[ "$found" == false ]]; then
            log_error "Registry: MODULE_OWNERS key '$mod' is not in REQUIRED_HOOKS"
            errors=$((errors + 1))
        fi
    done
    for hook in "${REQUIRED_HOOKS[@]}"; do
        if [[ -z "${MODULE_OWNERS[$hook]+x}" ]]; then
            log_error "Registry: REQUIRED_HOOKS entry '$hook' has no entry in MODULE_OWNERS"
            errors=$((errors + 1))
        fi
    done

    # Assertion 2: every id in every _requires() exists in FEATURES
    local id req req_fn reqs fid
    for id in "${FEATURES[@]}"; do
        req_fn="feature_${id//-/_}_requires"
        reqs="$($req_fn)"
        for req in $reqs; do
            found=false
            for fid in "${FEATURES[@]}"; do
                [[ "$fid" == "$req" ]] && { found=true; break; }
            done
            if [[ "$found" == false ]]; then
                log_error "Registry: feature '$id' requires '$req' which is not in FEATURES"
                errors=$((errors + 1))
            fi
        done
    done

    # Assertion 3: every feature appears in FEATURES after all of its _requires()
    local i j req_pos
    for i in "${!FEATURES[@]}"; do
        id="${FEATURES[$i]}"
        req_fn="feature_${id//-/_}_requires"
        reqs="$($req_fn)"
        for req in $reqs; do
            req_pos=-1
            for j in "${!FEATURES[@]}"; do
                [[ "${FEATURES[$j]}" == "$req" ]] && { req_pos=$j; break; }
            done
            if [[ $req_pos -ge $i ]]; then
                log_error "Registry: feature '$id' (pos $i) requires '$req' (pos $req_pos) but must come after it"
                errors=$((errors + 1))
            fi
        done
    done

    if [[ $errors -gt 0 ]]; then
        log_error "Registry integrity: $errors error(s). Fix the registry before running."
        exit 1
    fi
}

# =============================================================================
# §3. MANIFEST
# =============================================================================

# State tracking for manifest (populated by each feature's _install)
declare -A FEATURE_STATE    # id → installed | skipped | failed

manifest_read() {
    if [[ ! -f "$MANIFEST_FILE" ]]; then
        return 0  # First run — not an error
    fi
    if ! jq empty "$MANIFEST_FILE" 2>/dev/null; then
        log_warn "install-manifest.json is not valid JSON — ignoring"
        return 0
    fi
    local schema
    schema="$(jq -r '.schema // "unknown"' "$MANIFEST_FILE" 2>/dev/null)"
    if [[ "$schema" != "1" ]]; then
        log_warn "install-manifest.json has unknown schema '$schema' — ignoring (will overwrite at end)"
        return 0
    fi
    local repo
    repo="$(jq -r '.repo // ""' "$MANIFEST_FILE" 2>/dev/null)"
    if [[ -n "$repo" && "$repo" != "$SCRIPT_DIR" ]]; then
        log_warn "Manifest repo '$repo' differs from current SCRIPT_DIR '$SCRIPT_DIR'"
        log_warn "  daily-review and MCP registrations embed absolute paths that may be stale"
    fi
}

manifest_write() {
    # Written once at end of successful run, in full.
    # Never incrementally mutated mid-run.
    local timestamp revision
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    revision="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")"

    # Build features JSON object from FEATURE_STATE
    local features_json='{}'
    local id state
    for id in "${FEATURES[@]}"; do
        state="${FEATURE_STATE[$id]:-skipped}"
        features_json=$(echo "$features_json" | jq \
            --arg id "$id" --arg state "$state" --arg at "$timestamp" \
            '. + {($id): {state: $state, at: $at, artifacts: [], options: {}}}')
    done

    local manifest_tmp
    manifest_tmp="$(dirname "$MANIFEST_FILE")/install-manifest.json.tmp"

    mkdir -p "$(dirname "$MANIFEST_FILE")"
    jq -n \
        --argjson schema "$MANIFEST_SCHEMA" \
        --arg repo "$SCRIPT_DIR" \
        --arg revision "$revision" \
        --arg updated_at "$timestamp" \
        --argjson features "$features_json" \
        '{schema: $schema, repo: $repo, revision: $revision, updated_at: $updated_at, features: $features}' \
        > "$manifest_tmp"

    # validate-then-replace
    if ! jq empty "$manifest_tmp" 2>/dev/null; then
        log_warn "Manifest build produced invalid JSON — not writing manifest"
        rm -f "$manifest_tmp"
        return 1
    fi

    mv "$manifest_tmp" "$MANIFEST_FILE"

    # re-validate after write
    if ! jq empty "$MANIFEST_FILE" 2>/dev/null; then
        log_warn "Manifest re-validation failed after write"
        return 1
    fi

    log_info "Manifest written: $MANIFEST_FILE"
}

# =============================================================================
# Helper: install modules for a feature
# =============================================================================

# _install_modules <space-separated module list>
# Copies listed modules from PROJECT_HOOKS_DIR to GLOBAL_HOOKS_DIR.
# Idempotent. Returns 1 (with warnings) if any module is missing from source.
_install_modules() {
    local modules=($1)
    [[ ${#modules[@]} -eq 0 ]] && return 0

    local missing=() mod
    for mod in "${modules[@]}"; do
        [[ -f "$PROJECT_HOOKS_DIR/$mod" ]] || missing+=("$mod")
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_warn "Missing hook modules in $PROJECT_HOOKS_DIR:"
        for mod in "${missing[@]}"; do
            echo "  - $mod"
        done
        log_warn "Skipping module installation for this feature..."
        return 1
    fi

    mkdir -p "$GLOBAL_HOOKS_DIR"
    for mod in "${modules[@]}"; do
        cp "$PROJECT_HOOKS_DIR/$mod" "$GLOBAL_HOOKS_DIR/"
        chmod +x "$GLOBAL_HOOKS_DIR/$mod"
        log_info "  Installed module: $mod"
    done
    return 0
}

# =============================================================================
# Helper: tmux options (used by amux feature)
# =============================================================================

_install_tmux_options() {
    local tmux_conf="$HOME/.tmux.conf"
    local tmux_marker="# Added by claude-hooks install-claude-config.sh (tmux options for amux-spawned Claude sessions)"
    local tmux_lines=(
        "set -g focus-events on"
        "set -g set-titles on"
        "set -g set-titles-string '#{pane_title}'"
    )

    local tmux_missing=() line
    for line in "${tmux_lines[@]}"; do
        if [[ -f "$tmux_conf" ]] && grep -Fxq "$line" "$tmux_conf"; then
            continue
        fi
        tmux_missing+=("$line")
    done

    if [[ ${#tmux_missing[@]} -eq 0 ]]; then
        log_info "tmux options already present in $tmux_conf — leaving as is"
        TMUX_FILE_STATUS="already present"
    else
        if [[ -f "$tmux_conf" ]]; then
            mkdir -p "$BACKUP_DIR"
            local tmux_backup="$BACKUP_DIR/tmux.conf.$(date +%Y%m%d_%H%M%S).bak"
            cp "$tmux_conf" "$tmux_backup"
            log_info "Backup created: $tmux_backup"
        fi
        {
            [[ -s "$tmux_conf" ]] && echo ""
            echo "$tmux_marker"
            for line in "${tmux_missing[@]}"; do echo "$line"; done
        } >> "$tmux_conf"
        log_info "Added ${#tmux_missing[@]} tmux option line(s) to $tmux_conf"
        TMUX_FILE_STATUS="updated (${#tmux_missing[@]} line(s) added)"
    fi

    # Apply to running tmux server if present
    if command -v tmux >/dev/null 2>&1 && tmux list-sessions >/dev/null 2>&1; then
        declare -A tmux_want=(
            [focus-events]="on"
            [set-titles]="on"
            [set-titles-string]="#{pane_title}"
        )
        local tmux_set=0 tmux_already=0 tmux_failed=0 opt cur
        for opt in focus-events set-titles set-titles-string; do
            cur="$(tmux show -gv "$opt" 2>/dev/null || true)"
            if [[ "$cur" == "${tmux_want[$opt]}" ]]; then
                tmux_already=$((tmux_already + 1))
            elif tmux set -g "$opt" "${tmux_want[$opt]}" 2>/dev/null; then
                tmux_set=$((tmux_set + 1))
            else
                log_warn "Could not set tmux option '$opt' on the running server"
                tmux_failed=$((tmux_failed + 1))
            fi
        done
        log_info "Running tmux server: set $tmux_set, already-correct $tmux_already, failed $tmux_failed"
        TMUX_LIVE_STATUS="set $tmux_set, already $tmux_already, failed $tmux_failed"
    fi
}

# =============================================================================
# Helper: questions-listen systemd unit (used by questions feature)
# =============================================================================

_install_questions_listen_unit() {
    local questions_listen_service_name="claude-questions-listen.service"
    local systemd_user_dir="$HOME/.config/systemd/user"

    if ! command -v systemctl >/dev/null 2>&1 || [[ -z "${XDG_RUNTIME_DIR:-}" && ! -d "/run/user/$(id -u)" ]]; then
        log_info "systemd not available — skipping questions-listen service install"
        return 0
    fi

    mkdir -p "$systemd_user_dir"
    cat > "$systemd_user_dir/$questions_listen_service_name" << 'EOF'
[Unit]
Description=Claude async-question answer listener
After=network-online.target

[Service]
ExecStart=%h/.local/bin/questions-listen
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    log_info "Installed systemd unit: $systemd_user_dir/$questions_listen_service_name"
    systemctl --user daemon-reload 2>/dev/null || true

    # Opt-in check: enable only when config.toml carries [questions_listen] enabled = true
    local relay_config_toml="$HOME/.config/claude-tg-relay/config.toml"
    local questions_listen_opted_in=false
    if [[ -f "$relay_config_toml" ]]; then
        if python3 - "$relay_config_toml" << 'PYEOF' 2>/dev/null
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    raw = tomllib.load(fh)
section = raw.get("questions_listen", {})
sys.exit(0 if section.get("enabled") else 1)
PYEOF
        then
            questions_listen_opted_in=true
        fi
    fi

    if [[ "$questions_listen_opted_in" == true ]]; then
        if systemctl --user enable "$questions_listen_service_name" 2>/dev/null; then
            log_info "Enabled: $questions_listen_service_name"
            QUESTIONS_LISTEN_SERVICE_ENABLED=true
        fi
        if command -v loginctl >/dev/null 2>&1; then
            loginctl enable-linger "$(id -un)" 2>/dev/null || true
            log_info "loginctl enable-linger: applied"
        fi
    else
        log_info "questions-listen not enabled (add [questions_listen] enabled = true to config.toml to opt in)"
    fi
}

# =============================================================================
# Settings build and write (validate-then-replace discipline)
# =============================================================================

build_and_write_settings() {
    # Extract permissions from project config (for permissions-allowlist feature)
    local allowed_tools disallowed_tools ask_tools
    allowed_tools=$(jq '.allowedTools // .permissions.allow // []' "$PROJECT_CONFIG")
    disallowed_tools=$(jq '.disallowedTools // .permissions.deny // []' "$PROJECT_CONFIG")
    ask_tools=$(jq '.permissions.ask // []' "$PROJECT_CONFIG")

    local allowed_count disallowed_count ask_count
    allowed_count=$(echo "$allowed_tools" | jq 'length')
    disallowed_count=$(echo "$disallowed_tools" | jq 'length')
    ask_count=$(echo "$ask_tools" | jq 'length')
    log_info "Found $allowed_count allowed tools, $disallowed_count disallowed tools, and $ask_count ask tools in project config"

    # Start with current global config
    local merged
    merged=$(jq '.' "$GLOBAL_CONFIG")

    # permissions-allowlist feature contribution
    if [[ "${PERMISSIONS_ALLOWLIST_SELECTED:-false}" == true ]]; then
        merged=$(echo "$merged" | jq \
            --argjson allowed "$allowed_tools" \
            --argjson disallowed "$disallowed_tools" \
            --argjson ask "$ask_tools" \
            'del(.allowedTools, .disallowedTools) | . + {permissions: {allow: $allowed, deny: $disallowed, ask: $ask}}')
    fi

    # hooks contribution (built up by permission-hooks, telegram, amux features)
    if [[ -n "$HOOKS_JSON" && "$HOOKS_JSON" != '{}' ]]; then
        merged=$(echo "$merged" | jq --argjson hooks "$HOOKS_JSON" '. + {hooks: $hooks}')
        log_info "Hooks configuration merged"
        if echo "$HOOKS_JSON" | jq -e '.PreToolUse' >/dev/null 2>&1; then
            log_info "  - PreToolUse: python3 $GLOBAL_HOOKS_DIR/pretool_hook.py"
        fi
        if echo "$HOOKS_JSON" | jq -e '.PermissionRequest' >/dev/null 2>&1; then
            log_info "  - PermissionRequest: python3 $GLOBAL_HOOKS_DIR/permission_request_hook.py"
        fi
        if echo "$HOOKS_JSON" | jq -e '.PostToolUse' >/dev/null 2>&1; then
            log_info "  - PostToolUse: python3 $GLOBAL_HOOKS_DIR/posttool_hook.py"
        fi
        if echo "$HOOKS_JSON" | jq -e '.Notification' >/dev/null 2>&1; then
            log_info "  - Notification (idle_prompt + permission_prompt)"
        fi
        if echo "$HOOKS_JSON" | jq -e '.Stop' >/dev/null 2>&1; then
            log_info "  - UserPromptSubmit/Stop/SubagentStop/SessionEnd → spawn_producer_hook.py"
        fi
    fi

    # statusline contribution
    if [[ "${STATUSLINE_INSTALLED:-false}" == true ]]; then
        merged=$(echo "$merged" | jq \
            --arg cmd "python3 $GLOBAL_STATUSLINE_DIR/statusline.py" \
            '. + {statusLine: {type: "command", command: $cmd, refreshInterval: 30}}')
        log_info "StatusLine configuration merged:"
        log_info "  - command: python3 $GLOBAL_STATUSLINE_DIR/statusline.py"
        log_info "  - refreshInterval: 30"
    fi

    # subagent statusline contribution
    if [[ "${SUBAGENT_STATUSLINE_INSTALLED:-false}" == true ]]; then
        merged=$(echo "$merged" | jq \
            --arg cmd "python3 $GLOBAL_STATUSLINE_DIR/subagent.py" \
            '. + {subagentStatusLine: {type: "command", command: $cmd}}')
        log_info "SubagentStatusLine configuration merged:"
        log_info "  - command: python3 $GLOBAL_STATUSLINE_DIR/subagent.py"
    fi

    # Validate merged JSON
    if ! echo "$merged" | jq empty 2>/dev/null; then
        log_error "Merged config is not valid JSON!"
        log_error "Restoring from backup..."
        cp "$BACKUP_FILE" "$GLOBAL_CONFIG"
        exit 1
    fi

    # Write merged config
    echo "$merged" | jq '.' > "$GLOBAL_CONFIG"
    log_info "Global config updated: $GLOBAL_CONFIG"

    # Final validation
    if ! jq empty "$GLOBAL_CONFIG" 2>/dev/null; then
        log_error "Final validation failed!"
        log_error "Restoring from backup..."
        cp "$BACKUP_FILE" "$GLOBAL_CONFIG"
        exit 1
    fi
}

# =============================================================================
# Dependency checks
# =============================================================================

_check_dependencies() {
    if ! command -v jq &> /dev/null; then
        log_error "jq is required but not installed. Install with: sudo apt install jq"
        exit 1
    fi
    if ! command -v python3 &> /dev/null; then
        log_error "python3 is required but not installed."
        exit 1
    fi
    if command -v uv &> /dev/null; then
        UV_AVAILABLE=true
    else
        UV_AVAILABLE=false
        log_warn "uv not found — the context-usage and permissions MCP servers will not be installed."
        log_warn "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
}

# =============================================================================
# Global state variables (set by feature installs)
# =============================================================================
HOOKS_INSTALLED=false
STATUSLINE_INSTALLED=false
SUBAGENT_STATUSLINE_INSTALLED=false
COMMANDS_INSTALLED=false
AMUX_SPAWN_INSTALLED=false
AMUX_CODEX_STATUS="not probed"
COMPLETION_INSTALLED=false
SNIPPET_INSTALLED=false
CLAUDE_ROLES_INSTALLED=false
PROFILES_INSTALLED=false
CONTEXT_MCP_INSTALLED=false
PERMISSIONS_MCP_INSTALLED=false
QUESTIONS_MCP_INSTALLED=false
QUESTIONS_LISTEN_INSTALLED=false
QUESTIONS_LISTEN_SERVICE_ENABLED=false
CLAUDE_QUESTIONS_INSTALLED=false
TMUX_FILE_STATUS="unchanged"
TMUX_LIVE_STATUS="no running server"
HOOKS_JSON='{}'
PERMISSIONS_ALLOWLIST_SELECTED=false

# =============================================================================
# Argument parsing
# =============================================================================
SELECTED_FEATURES=()   # empty = all features
PROBE_FEATURE=""       # non-empty = run probe and exit

_parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --only)
                shift
                IFS=',' read -ra SELECTED_FEATURES <<< "${1:-}"
                shift
                ;;
            --probe)
                shift
                PROBE_FEATURE="${1:-}"
                shift
                ;;
            --)
                shift; break
                ;;
            -*)
                # Unknown options are silently ignored for now.
                # Later tasks add --all, --with, --without, --yes, --list, --dry-run.
                shift
                [[ $# -gt 0 && "${1:-}" != -* ]] && shift || true
                ;;
            *)
                break
                ;;
        esac
    done
}

_parse_args "$@"

# =============================================================================
# --probe dispatch: run a feature probe and exit
# =============================================================================
if [[ -n "$PROBE_FEATURE" ]]; then
    # Minimal re-init for probes: just need the feature probe functions
    fn="feature_${PROBE_FEATURE//-/_}_probe"
    if declare -f "$fn" >/dev/null 2>&1; then
        if $fn; then
            echo "installed"
            exit 0
        else
            echo "not-installed"
            exit 1
        fi
    else
        echo "unknown-feature: $PROBE_FEATURE" >&2
        exit 2
    fi
fi

# =============================================================================
# Startup: dependency checks and registry assertions
# =============================================================================
_check_dependencies
assert_registry_integrity

# Default feature selection: all features
if [[ ${#SELECTED_FEATURES[@]} -eq 0 ]]; then
    SELECTED_FEATURES=("${FEATURES[@]}")
fi

# =============================================================================
# Main execution
# =============================================================================

log_step "Starting claude-hooks install (epic 29 registry)"

# Read manifest (first run is fine — not an error)
manifest_read

# HOUSEKEEPING (always, not a toggle)
rotate_hook_logs

# Legacy daemon cleanup — gated on pidfile under $HOME so safe with temp HOME
LEGACY_PIDFILE="$HOME/.claude/telegram_daemon.pid"
if [[ -f "$LEGACY_PIDFILE" ]]; then
    LEGACY_PID="$(cat "$LEGACY_PIDFILE" 2>/dev/null || true)"
    if [[ -n "$LEGACY_PID" ]] && kill -0 "$LEGACY_PID" 2>/dev/null; then
        log_info "Stopping legacy telegram daemon (pid $LEGACY_PID)"
        kill "$LEGACY_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$LEGACY_PID" 2>/dev/null || true
    fi
    rm -f "$LEGACY_PIDFILE"
fi
rm -f "$GLOBAL_HOOKS_DIR/telegram_daemon.py"

# Validate project config
log_step "Validating and backing up configs"

if [[ ! -f "$PROJECT_CONFIG" ]]; then
    log_error "Project config not found: $PROJECT_CONFIG"
    log_error "Run this script from a project directory with .claude/settings.json"
    exit 1
fi
if ! jq empty "$PROJECT_CONFIG" 2>/dev/null; then
    log_error "Project config is not valid JSON: $PROJECT_CONFIG"
    exit 1
fi
log_info "Project config validated: $PROJECT_CONFIG"

mkdir -p "$BACKUP_DIR"

if [[ ! -f "$GLOBAL_CONFIG" ]]; then
    log_warn "Global config doesn't exist, creating empty one"
    mkdir -p "$(dirname "$GLOBAL_CONFIG")"
    echo '{}' > "$GLOBAL_CONFIG"
fi
if ! jq empty "$GLOBAL_CONFIG" 2>/dev/null; then
    log_error "Global config is not valid JSON: $GLOBAL_CONFIG"
    exit 1
fi
log_info "Global config validated: $GLOBAL_CONFIG"

BACKUP_FILE="$BACKUP_DIR/settings.json.$(date +%Y%m%d_%H%M%S).bak"
cp "$GLOBAL_CONFIG" "$BACKUP_FILE"
log_info "Backup created: $BACKUP_FILE"

# Execute selected features
log_step "Executing feature installs"
for id in "${SELECTED_FEATURES[@]}"; do
    fn="feature_${id//-/_}_install"
    if declare -f "$fn" >/dev/null 2>&1; then
        $fn
        FEATURE_STATE["$id"]="installed"
    else
        log_warn "Unknown feature id: $id"
        FEATURE_STATE["$id"]="skipped"
    fi
done

# Build and write settings (validate-then-replace)
log_step "Merging and writing settings"
build_and_write_settings

# Write manifest once at end of successful run
manifest_write

# =============================================================================
# Summary report
# =============================================================================

local_user_bin_dir="$HOME/.local/bin"
local_user_completions_dir="$HOME/.local/share/bash-completion/completions"
local_claude_shell_dir="$HOME/.claude/shell"

echo ""
log_info "Success! Global config now contains:"
echo "  - permissions.allow: $(jq '.permissions.allow | length' "$GLOBAL_CONFIG") entries"
echo "  - permissions.deny: $(jq '.permissions.deny | length' "$GLOBAL_CONFIG") entries"

if [[ "$HOOKS_INSTALLED" == true ]]; then
    echo "  - hooks: installed and configured"
    echo "    - PreToolUse: Bash command interception"
    echo "    - PermissionRequest: Telegram-gated permission approval"
    echo "    - PostToolUse: Telegram message cleanup on terminal response"
    echo "    - Notification: idle_prompt → Telegram (operator-started sessions only)"
    echo "    - Stop/SubagentStop/Notification(permission_prompt)/SessionEnd → amux-spawn producer"
else
    echo "  - hooks: not installed (missing files or feature not selected)"
fi

if [[ "$STATUSLINE_INSTALLED" == true ]]; then
    echo "  - statusLine: installed and configured ($GLOBAL_STATUSLINE_DIR/statusline.py)"
else
    echo "  - statusLine: not installed (missing statusline.py or feature not selected)"
fi
if [[ "$SUBAGENT_STATUSLINE_INSTALLED" == true ]]; then
    echo "  - subagentStatusLine: installed and configured ($GLOBAL_STATUSLINE_DIR/subagent.py)"
else
    echo "  - subagentStatusLine: not installed (missing subagent.py)"
fi

if [[ "$COMMANDS_INSTALLED" == true ]]; then
    echo "  - slash commands: installed ($GLOBAL_COMMANDS_DIR/) — /yolo, /yolo-off"
fi

if [[ "$AMUX_SPAWN_INSTALLED" == true ]]; then
    echo "  - amux-spawn: installed ($local_user_bin_dir/amux-spawn)"
    echo "    - amux Codex provider: $AMUX_CODEX_STATUS"
else
    echo "  - amux-spawn: not installed"
fi

if [[ "$COMPLETION_INSTALLED" == true ]]; then
    echo "  - amux-spawn completion: installed ($local_user_completions_dir/amux-spawn)"
else
    echo "  - amux-spawn completion: not installed"
fi
if [[ "$SNIPPET_INSTALLED" == true ]]; then
    echo "  - shell snippets: installed ($local_claude_shell_dir/)"
    echo "    OPT-IN (choose one):"
    echo "      source $local_claude_shell_dir/claude-profiles.bash  # profiles only"
    echo "      source $local_claude_shell_dir/amux-spawn.bash       # profiles + amux"
else
    echo "  - shell snippets: not installed"
fi

if [[ "$CLAUDE_ROLES_INSTALLED" == true ]]; then
    echo "  - claude-roles: installed ($local_claude_shell_dir/claude-roles)"
    roles_toml="$SCRIPT_DIR/.claude/roles.toml"
    if [[ -f "$roles_toml" ]]; then
        echo "  - roles.toml: found ($roles_toml)"
        echo "    Inspect routing with: claude-roles  (run from the workspace directory)"
    else
        echo "  - roles.toml: not configured in this workspace (default routing only)"
        echo "    Template: $SCRIPT_DIR/docs/roles.example.toml"
    fi
else
    echo "  - claude-roles: not installed"
fi

if [[ "$PROFILES_INSTALLED" == true ]]; then
    echo "  - profiles.toml: installed ($HOME/.claude/profiles.toml)"
    echo "    Edit with your model tokens, then re-source your chosen shell snippet"
elif [[ -f "$HOME/.claude/profiles.toml" ]]; then
    echo "  - profiles.toml: already present ($HOME/.claude/profiles.toml)"
else
    echo "  - profiles.toml: not installed (profiles.example.toml not found)"
fi

if [[ "$CONTEXT_MCP_INSTALLED" == true ]]; then
    echo "  - MCP server (context-usage): installed"
else
    echo "  - MCP server (context-usage): not installed (uv missing or server.py not found)"
fi

if [[ "$PERMISSIONS_MCP_INSTALLED" == true ]]; then
    echo "  - MCP server (permissions): installed"
else
    echo "  - MCP server (permissions): not installed (uv missing or server.py not found)"
fi

if [[ "$QUESTIONS_MCP_INSTALLED" == true ]]; then
    echo "  - MCP server (questions): installed"
else
    echo "  - MCP server (questions): not installed (uv missing or server.py not found)"
fi

if [[ "$CLAUDE_QUESTIONS_INSTALLED" == true ]]; then
    echo "  - claude-questions: installed ($local_claude_shell_dir/claude-questions)"
else
    echo "  - claude-questions: not installed"
fi

if [[ "$QUESTIONS_LISTEN_INSTALLED" == true ]]; then
    echo "  - questions-listen: installed ($local_user_bin_dir/questions-listen)"
    if [[ "$QUESTIONS_LISTEN_SERVICE_ENABLED" == true ]]; then
        echo "    systemd unit enabled — start with: systemctl --user start claude-questions-listen"
    else
        echo "    systemd unit installed but NOT enabled"
        echo "    To opt in: add [questions_listen] enabled = true to ~/.config/claude-tg-relay/config.toml"
        echo "    Then re-run install.sh to enable the unit"
    fi
else
    echo "  - questions-listen: not installed"
fi

echo "  - tmux options (focus-events, tab title): $TMUX_FILE_STATUS ($HOME/.tmux.conf); running server: $TMUX_LIVE_STATUS"

echo ""
log_info "Other settings preserved:"
jq 'del(.permissions, .hooks, .statusLine, .subagentStatusLine, .description, .notes) | keys[]' \
    "$GLOBAL_CONFIG" 2>/dev/null | while read -r key; do
    echo "  - $key"
done || echo "  (none)"

echo ""
log_info "Backup location: $BACKUP_FILE"
log_info "To restore: cp \"$BACKUP_FILE\" \"$GLOBAL_CONFIG\""

if [[ "$HOOKS_INSTALLED" == true ]]; then
    echo ""
    log_info "Testing hook installation..."
    if python3 -c "import sys; sys.path.insert(0, '$GLOBAL_HOOKS_DIR'); from bash_command_parser import BashCommandParser; import amux_spawn_lib, codex_event_reducer; print('Hook modules loaded successfully')" 2>/dev/null; then
        log_info "Hook modules are working correctly!"
    else
        log_warn "Hook modules test failed (this may be okay if dependencies are missing)"
    fi
fi
