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
                    ext_symlink_add "$claude_shell_dir/claude-roles" "$user_bin_dir/claude-roles"
                    log_info "  Symlinked: $user_bin_dir/claude-roles -> $claude_shell_dir/claude-roles"
                    ;;
            esac
        fi
        CLAUDE_ROLES_INSTALLED=true
    else
        log_warn "claude-roles not found at $claude_roles_src — skipping"
    fi

    # permissions MCP server in ~/.claude.json — collapsed into the shared helper
    local permissions_mcp_script="$SCRIPT_DIR/permissions-mcp/server.py"
    if [[ -f "$permissions_mcp_script" ]]; then
        if _register_mcp_server "permissions" "$permissions_mcp_script" \
                "{\"CLAUDE_HOOKS_REPO\": \"$SCRIPT_DIR\"}"; then
            PERMISSIONS_MCP_INSTALLED=true
        fi
    fi

    # Build PostToolUse and Notification[idle_prompt] hook config.
    # telegram owns PostToolUse and Notification[idle_prompt] only.
    # Notification[permission_prompt] is owned by amux (architecture §7.1).
    HOOKS_JSON=$(echo "$HOOKS_JSON" | jq \
        --arg posttool_path "$GLOBAL_HOOKS_DIR/posttool_hook.py" \
        --arg notification_path "$GLOBAL_HOOKS_DIR/notification_hook.py" \
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
                        # CLAUDE_HOOK_DEBUG=1 mirrors PermissionRequest: it logs the
                        # idle-notification path AND propagates (via inherited env)
                        # to the detached reply_injector.py it spawns, so the
                        # reply-from-Telegram chain is observable end-to-end.
                        command: ("CLAUDE_HOOK_DEBUG=1 python3 " + $notification_path)
                    }]
                }
            ]
        }')

    log_info "Telegram hooks wired (PostToolUse, Notification[idle_prompt])"
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

    # Build amux hooks: Notification[permission_prompt], UserPromptSubmit, Stop,
    # SubagentStop, SessionEnd (architecture §7.1 — amux owns all of these).
    # Notification[permission_prompt] is amux's share of the shared Notification array;
    # telegram owns Notification[idle_prompt].
    if [[ "$AMUX_SPAWN_INSTALLED" == true ]]; then
        HOOKS_JSON=$(echo "$HOOKS_JSON" | jq \
            --arg producer_path "$GLOBAL_HOOKS_DIR/spawn_producer_hook.py" \
            '. + {
                Notification: ((.Notification // []) + [{
                    matcher: "permission_prompt",
                    hooks: [{
                        type: "command",
                        # Epic-10 producer: sets permission_pending on tracked session handles.
                        # No-op for plain/human sessions and other repos.
                        command: ("python3 " + $producer_path + " --event Notification")
                    }]
                }]),
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
        log_info "amux hooks wired (Notification[permission_prompt], UserPromptSubmit, Stop, SubagentStop, SessionEnd)"
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

    if [[ "$UV_AVAILABLE" != true ]]; then
        log_warn "uv not available — skipping context-usage MCP server"
        return 0
    fi
    if [[ ! -f "$context_mcp_script" ]]; then
        log_warn "context-mcp server.py not found at $context_mcp_script — skipping"
        return 0
    fi

    if _register_mcp_server "context-usage" "$context_mcp_script" "{}"; then
        CONTEXT_MCP_INSTALLED=true
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

    # questions MCP server in ~/.claude.json — collapsed into the shared helper.
    # IMPORTANT: questions_listen_lib.py and questions-mcp must stay in sync (see
    # install-claude-config.sh:852). Both are installed in this same function.
    local questions_mcp_script="$SCRIPT_DIR/questions-mcp/server.py"
    if [[ -f "$questions_mcp_script" ]]; then
        if _register_mcp_server "questions" "$questions_mcp_script" \
                "{\"CLAUDE_HOOKS_REPO\": \"$SCRIPT_DIR\"}"; then
            QUESTIONS_MCP_INSTALLED=true
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
                    ext_symlink_add "$claude_shell_dir/claude-questions" "$user_bin_dir/claude-questions"
                    log_info "  Symlinked: $user_bin_dir/claude-questions -> $claude_shell_dir/claude-questions"
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
            ext_symlink_add "$claude_bin_dir/questions-listen" "$user_bin_dir/questions-listen"
            log_info "  Symlinked: $user_bin_dir/questions-listen -> $claude_bin_dir/questions-listen"
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
    ext_systemd_is_enabled "claude-questions-listen.service"
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
    # No files installed — the only artifact is the daily-review-cron sub-toggle
    # line. Use that marker; checking "daily-review" would false-positive against
    # "daily-review-cron" because ext_cron_has_marker uses substring grep -Fq.
    ext_cron_has_marker "daily-review-cron"
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
    ext_cron_has_marker "daily-review-cron"
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
        if [[ "$id" == "permissions-allowlist" && "$state" == "installed" ]]; then
            # Record union-mode additions so uninstall (29-05) can subtract exactly them.
            features_json=$(echo "$features_json" | jq \
                --arg  id           "$id" \
                --arg  state        "$state" \
                --arg  at           "$timestamp" \
                --arg  mode         "${PERMISSIONS_ALLOWLIST_MODE:-union}" \
                --argjson added_allow "${PERMISSIONS_ADDED_ALLOW:-[]}" \
                --argjson added_deny  "${PERMISSIONS_ADDED_DENY:-[]}" \
                '. + {($id): {state: $state, at: $at, artifacts: [], options: {mode: $mode}, added_allow: $added_allow, added_deny: $added_deny}}')
        else
            features_json=$(echo "$features_json" | jq \
                --arg id "$id" --arg state "$state" --arg at "$timestamp" \
                '. + {($id): {state: $state, at: $at, artifacts: [], options: {}}}')
        fi
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
    # Delegates to ext_tmux_apply (§8 seam). All tmux-related writes and live
    # server mutations go through the seam function.
    ext_tmux_apply
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
    # daemon-reload so the new unit is visible; systemctl escapes HOME → via seam.
    ext_systemd_daemon_reload

    # Opt-in check: enable only when config.toml carries [questions_listen] enabled = true
    # (architecture §8.4: enable and loginctl enable-linger are the sub-toggle
    # actions. 29-09 will migrate the config.toml check to the manifest sub-toggle flag.)
    local relay_config_toml="$HOME/.config/claude-tg-relay/config.toml"
    local questions_listen_opted_in=false
    if [[ -f "$relay_config_toml" ]]; then
        if python3 - "$relay_config_toml" <<'PYEOF' 2>/dev/null
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
        # enable + loginctl enable-linger both escape HOME → via seam.
        ext_systemd_enable "$questions_listen_service_name"
    else
        log_info "questions-listen not enabled (add [questions_listen] enabled = true to config.toml to opt in)"
    fi
}

# =============================================================================
# §8. EXTERNAL-SURFACE SEAM
# =============================================================================
# Every function in this section honours CLAUDE_INSTALL_NO_EXTERNAL=1 by
# logging what it would do and returning success without doing it. This is the
# safety boundary of the epic (cross-task invariant 5): no code outside these
# functions may invoke crontab, systemctl, loginctl, "tmux set", ln -s, or
# another installer script. A source-level grep test in
# tests/test_unit_installer.py enforces this invariant.
#
# Marker format (architecture §8.1):
#   # claude-hooks:<id>  (install.sh — remove with: disable <id>)
#   <managed line>
# =============================================================================

# ---------------------------------------------------------------------------
# §8.2 ~/.bashrc
# ---------------------------------------------------------------------------

# _bashrc_exclusive_of <id>
# Returns the marker id that is mutually exclusive with <id>, or empty string.
_bashrc_exclusive_of() {
    case "$1" in
        amux-autowrap)       echo "profiles-autosource" ;;
        profiles-autosource) echo "amux-autowrap" ;;
        *)                   echo "" ;;
    esac
}

# ext_bashrc_add <marker-id> <line>
# Appends a marked line to ~/.bashrc. Idempotent (exact-match marker).
# Backs up before any edit. Enforces mutual exclusion between amux-autowrap
# and profiles-autosource (architecture §2, §8.2). Absent ~/.bashrc → warn
# and skip; never creates it (the snippets are bash-only; absent bashrc
# usually means a different shell).
ext_bashrc_add() {
    local id="$1" line="$2"
    local marker="# claude-hooks:${id}  (install.sh — remove with: disable ${id})"
    local bashrc="$HOME/.bashrc"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_bashrc_add: would add '${id}' to ~/.bashrc: ${line}"
        return 0
    fi

    if [[ ! -f "$bashrc" ]]; then
        log_warn "ext_bashrc_add: ~/.bashrc absent — skipping '${id}' (different shell?)"
        return 0
    fi

    # Idempotency: marker already present
    if grep -Fxq "$marker" "$bashrc" 2>/dev/null; then
        log_info "ext_bashrc_add: '${id}' already in ~/.bashrc — no-op"
        return 0
    fi

    # Mutual exclusion: remove conflicting marker before adding ours
    local excl
    excl="$(_bashrc_exclusive_of "$id")"
    if [[ -n "$excl" ]]; then
        local excl_marker="# claude-hooks:${excl}  (install.sh — remove with: disable ${excl})"
        if grep -Fxq "$excl_marker" "$bashrc" 2>/dev/null; then
            log_info "ext_bashrc_add: removing conflicting '${excl}' before adding '${id}' (mutual exclusion)"
            ext_bashrc_remove "$excl"
        fi
    fi

    # Backup before edit
    mkdir -p "$BACKUP_DIR"
    local bak="$BACKUP_DIR/bashrc.$(date +%Y%m%d_%H%M%S).bak"
    cp "$bashrc" "$bak"
    log_info "Backup created: $bak"

    # Append blank separator (if file non-empty), marker, managed line
    {
        [[ -s "$bashrc" ]] && echo ""
        echo "$marker"
        echo "$line"
    } >> "$bashrc"
    log_info "ext_bashrc_add: added '${id}' to ~/.bashrc"
}

# ext_bashrc_remove <marker-id>
# Removes the marker and the single managed line following it from ~/.bashrc.
# No-op (with message) if marker not found. Backs up before edit.
ext_bashrc_remove() {
    local id="$1"
    local marker="# claude-hooks:${id}  (install.sh — remove with: disable ${id})"
    local bashrc="$HOME/.bashrc"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_bashrc_remove: would remove '${id}' from ~/.bashrc"
        return 0
    fi

    if [[ ! -f "$bashrc" ]]; then
        log_info "ext_bashrc_remove: ~/.bashrc absent — nothing to remove for '${id}'"
        return 0
    fi

    if ! grep -Fxq "$marker" "$bashrc" 2>/dev/null; then
        log_info "ext_bashrc_remove: '${id}' marker not found in ~/.bashrc — no-op"
        return 0
    fi

    # Backup before edit
    mkdir -p "$BACKUP_DIR"
    cp "$bashrc" "$BACKUP_DIR/bashrc.$(date +%Y%m%d_%H%M%S).bak"

    # Remove the marker line and the managed line following it.
    # Also strips a preceding blank line if we added one.
    python3 - "$bashrc" "$marker" <<'PYEOF'
import sys
path, marker = sys.argv[1], sys.argv[2]
with open(path) as fh:
    lines = fh.readlines()
out = []
skip_next = False
for line in lines:
    if skip_next:
        skip_next = False
        continue
    if line.rstrip('\n') == marker:
        skip_next = True
        # Strip preceding blank line added by ext_bashrc_add
        if out and out[-1] == '\n':
            out.pop()
        continue
    out.append(line)
with open(path, 'w') as fh:
    fh.writelines(out)
PYEOF
    log_info "ext_bashrc_remove: removed '${id}' from ~/.bashrc"
}

# ---------------------------------------------------------------------------
# §8.3 crontab
# ---------------------------------------------------------------------------

# ext_cron_has_marker <marker-id>
# Read-only probe: exits 0 if the marker is present in the crontab, 1 otherwise.
# With CLAUDE_INSTALL_NO_EXTERNAL=1: logs and returns 1 (never ran = not present).
ext_cron_has_marker() {
    local id="$1"
    local marker="# claude-hooks:${id}"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_cron_has_marker: would check crontab for '${id}'"
        return 1
    fi

    crontab -l 2>/dev/null | grep -Fq "$marker"
}

# ext_cron_add <marker-id> <line>
# Adds a marked cron line. Idempotent. Saves crontab to $BACKUP_DIR first.
# Handles: no crontab (normal), unrelated lines (preserved), an existing
# unmarked line whose command matches ours (adopted — marker added above it).
# Never calls crontab -r under any circumstance.
ext_cron_add() {
    local id="$1" line="$2"
    local marker="# claude-hooks:${id}  (install.sh — remove with: disable ${id})"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_cron_add: would add cron entry '${id}': ${line}"
        return 0
    fi

    local current_crontab
    current_crontab="$(crontab -l 2>/dev/null || true)"

    # Idempotency: marker already present
    if printf '%s\n' "$current_crontab" | grep -Fxq "$marker"; then
        log_info "ext_cron_add: '${id}' already in crontab — no-op"
        return 0
    fi

    # Backup before any edit
    mkdir -p "$BACKUP_DIR"
    local cron_backup="$BACKUP_DIR/crontab.$(date +%Y%m%d_%H%M%S).bak"
    printf '%s\n' "$current_crontab" > "$cron_backup"
    log_info "Crontab backup created: $cron_backup"

    # Extract the command part (field 6+) of our desired line for adopt detection.
    local cmd_part
    cmd_part="$(printf '%s\n' "$line" | awk '{out=""; for(i=6;i<=NF;i++) out=out (i>6?" ":"") $i; print out}')"

    # Check for an existing unmarked line with matching command (adopt it).
    if [[ -n "$cmd_part" ]] && printf '%s\n' "$current_crontab" | grep -qF "$cmd_part"; then
        log_info "ext_cron_add: adopting existing unmarked cron line for '${id}'"
        # Insert marker before the matching non-comment line.
        printf '%s\n' "$current_crontab" | python3 -c "
import sys
marker, cmd_part = sys.argv[1], sys.argv[2]
for raw in sys.stdin:
    s = raw.rstrip('\n')
    if cmd_part and cmd_part in s and not s.lstrip().startswith('#'):
        print(marker)
    print(s)
" "$marker" "$cmd_part" | crontab -
    else
        # Append: existing lines + blank separator + marker + new cron line
        {
            printf '%s\n' "$current_crontab"
            [[ -n "$current_crontab" ]] && echo ""
            echo "$marker"
            echo "$line"
        } | crontab -
    fi
    log_info "ext_cron_add: added cron entry '${id}'"
}

# ext_cron_remove <marker-id>
# Removes the marker comment line and the cron line following it.
# No-op if marker not found. Never calls crontab -r.
ext_cron_remove() {
    local id="$1"
    local marker="# claude-hooks:${id}  (install.sh — remove with: disable ${id})"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_cron_remove: would remove cron entry '${id}'"
        return 0
    fi

    local current_crontab
    current_crontab="$(crontab -l 2>/dev/null || true)"

    if ! printf '%s\n' "$current_crontab" | grep -Fxq "$marker"; then
        log_info "ext_cron_remove: '${id}' marker not found in crontab — no-op"
        return 0
    fi

    # Backup before edit
    mkdir -p "$BACKUP_DIR"
    printf '%s\n' "$current_crontab" > "$BACKUP_DIR/crontab.$(date +%Y%m%d_%H%M%S).bak"

    # Remove marker line and the cron line following it.
    # We write the crontab to a temp file (python3 - reads its script from stdin,
    # so we can't pipe both the script and the data through stdin simultaneously).
    # Write the filtered result back via crontab - (never crontab -r, even if empty).
    local _cron_tmp
    _cron_tmp="$(mktemp)"
    printf '%s\n' "$current_crontab" > "$_cron_tmp"

    local _filtered
    _filtered="$(python3 - "$_cron_tmp" "$marker" <<'PYEOF'
import sys
path, marker = sys.argv[1], sys.argv[2]
with open(path) as fh:
    lines = fh.readlines()
out = []
skip_next = False
for line in lines:
    if skip_next:
        skip_next = False
        continue
    if line.rstrip('\n') == marker:
        skip_next = True
        if out and out[-1] == '\n':
            out.pop()
        continue
    out.append(line)
sys.stdout.write(''.join(out))
PYEOF
)"
    rm -f "$_cron_tmp"

    printf '%s' "$_filtered" | crontab -
    log_info "ext_cron_remove: removed cron entry '${id}'"
}

# ---------------------------------------------------------------------------
# §8 tmux
# ---------------------------------------------------------------------------

# ext_tmux_apply
# Writes the amux tmux options to ~/.tmux.conf (persistent) and applies them
# to any running server (live). Both halves are idempotent.
# ~/.tmux.conf is under HOME so the file write is safe with a HOME override;
# "tmux set -g" mutates the running server and is gated by NO_EXTERNAL.
# NOTE on the legacy marker: machines installed before 29-03 have
#   "# Added by claude-hooks install-claude-config.sh (...)"
# as their marker. ext_tmux_remove targets the new marker only; 29-09
# handles migration of the old one.
ext_tmux_apply() {
    local tmux_conf="$HOME/.tmux.conf"
    local marker="# claude-hooks:amux-tmux-options  (install.sh — remove with: uninstall amux)"
    local tmux_lines=(
        "set -g focus-events on"
        "set -g set-titles on"
        "set -g set-titles-string '#{pane_title}'"
    )

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_tmux_apply: would write ~/.tmux.conf and apply live tmux options"
        TMUX_FILE_STATUS="skipped (no-external)"
        TMUX_LIVE_STATUS="skipped (no-external)"
        return 0
    fi

    # --- Persistent ~/.tmux.conf ---
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
            echo "$marker"
            for line in "${tmux_missing[@]}"; do echo "$line"; done
        } >> "$tmux_conf"
        log_info "Added ${#tmux_missing[@]} tmux option line(s) to $tmux_conf"
        TMUX_FILE_STATUS="updated (${#tmux_missing[@]} line(s) added)"
    fi

    # --- Live tmux server (escapes HOME — gated above) ---
    if command -v tmux >/dev/null 2>&1 && tmux list-sessions >/dev/null 2>&1; then
        local -A _tmux_want=(
            [focus-events]="on"
            [set-titles]="on"
            [set-titles-string]="#{pane_title}"
        )
        local tmux_set=0 tmux_already=0 tmux_failed=0 opt cur
        for opt in focus-events set-titles set-titles-string; do
            cur="$(tmux show -gv "$opt" 2>/dev/null || true)"
            if [[ "$cur" == "${_tmux_want[$opt]}" ]]; then
                tmux_already=$((tmux_already + 1))
            elif tmux set -g "$opt" "${_tmux_want[$opt]}" 2>/dev/null; then
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

# ext_tmux_remove
# Removes the claude-hooks:amux-tmux-options block from ~/.tmux.conf.
# NOTE: The live 'tmux set -g' options are NOT reverted — a running server's
# options belong to that server. The uninstall summary should inform the user.
ext_tmux_remove() {
    local tmux_conf="$HOME/.tmux.conf"
    local marker="# claude-hooks:amux-tmux-options  (install.sh — remove with: uninstall amux)"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_tmux_remove: would remove amux-tmux-options block from ~/.tmux.conf"
        return 0
    fi

    if [[ ! -f "$tmux_conf" ]]; then
        log_info "ext_tmux_remove: ~/.tmux.conf absent — nothing to remove"
        return 0
    fi

    if ! grep -Fxq "$marker" "$tmux_conf"; then
        log_info "ext_tmux_remove: amux-tmux-options marker not found in ~/.tmux.conf — no-op"
        return 0
    fi

    # Backup before edit
    mkdir -p "$BACKUP_DIR"
    cp "$tmux_conf" "$BACKUP_DIR/tmux.conf.$(date +%Y%m%d_%H%M%S).bak"

    # Remove the marker line and all "set -g ..." lines following it.
    # Also strips a preceding blank separator line added by ext_tmux_apply.
    python3 - "$tmux_conf" "$marker" <<'PYEOF'
import sys
path, marker = sys.argv[1], sys.argv[2]
with open(path) as fh:
    lines = fh.readlines()
out = []
i = 0
while i < len(lines):
    if lines[i].rstrip('\n') == marker:
        # Skip marker and all following "set -g" lines
        i += 1
        while i < len(lines) and lines[i].startswith('set -g'):
            i += 1
        # Strip preceding blank separator
        if out and out[-1] == '\n':
            out.pop()
    else:
        out.append(lines[i])
        i += 1
with open(path, 'w') as fh:
    fh.writelines(out)
PYEOF
    log_info "ext_tmux_remove: removed amux-tmux-options block from ~/.tmux.conf"
    log_info "NOTE: live tmux options (focus-events, set-titles) were not reverted on the running server"
}

# ---------------------------------------------------------------------------
# §8.4 systemd
# ---------------------------------------------------------------------------

# ext_systemd_daemon_reload
# Runs 'systemctl --user daemon-reload'. Called after writing a unit file.
# Gated by CLAUDE_INSTALL_NO_EXTERNAL (systemctl escapes HOME).
ext_systemd_daemon_reload() {
    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_systemd_daemon_reload: would run systemctl --user daemon-reload"
        return 0
    fi
    systemctl --user daemon-reload 2>/dev/null || true
    log_info "systemctl --user daemon-reload: done"
}

# ext_systemd_enable <unit>
# Enables a systemd user unit (without --now) and runs loginctl enable-linger.
# Called only when the questions-listen sub-toggle is on (architecture §8.4).
# Gated by CLAUDE_INSTALL_NO_EXTERNAL.
ext_systemd_enable() {
    local unit="$1"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_systemd_enable: would enable ${unit} and loginctl enable-linger"
        return 0
    fi

    if systemctl --user enable "$unit" 2>/dev/null; then
        log_info "Enabled: $unit"
        QUESTIONS_LISTEN_SERVICE_ENABLED=true
    else
        log_warn "systemctl --user enable $unit failed"
    fi

    if command -v loginctl >/dev/null 2>&1; then
        loginctl enable-linger "$(id -un)" 2>/dev/null || true
        log_info "loginctl enable-linger: applied"
    fi
}

# ext_systemd_disable <unit>
# Disables a systemd user unit.
# Gated by CLAUDE_INSTALL_NO_EXTERNAL.
ext_systemd_disable() {
    local unit="$1"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_systemd_disable: would disable ${unit}"
        return 0
    fi

    systemctl --user disable "$unit" 2>/dev/null || true
    log_info "Disabled: $unit"
}

# ext_systemd_is_enabled <unit>
# Read-only probe: exits 0 if the unit is currently enabled, 1 otherwise.
# With CLAUDE_INSTALL_NO_EXTERNAL=1: logs and returns 1 (never enabled = false).
ext_systemd_is_enabled() {
    local unit="$1"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_systemd_is_enabled: would check systemctl --user is-enabled ${unit}"
        return 1
    fi

    systemctl --user is-enabled "$unit" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# §8 symlinks
# ---------------------------------------------------------------------------

# ext_symlink_add <target> <link>
# Creates a symlink at <link> pointing to <target> (ln -sf).
# Gated by CLAUDE_INSTALL_NO_EXTERNAL.
ext_symlink_add() {
    local target="$1" link="$2"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_symlink_add: would create symlink ${link} -> ${target}"
        return 0
    fi

    ln -sf "$target" "$link"
    log_info "Symlinked: $link -> $target"
}

# ext_symlink_remove <link>
# Removes a symlink. No-op if <link> is not a symlink.
# Gated by CLAUDE_INSTALL_NO_EXTERNAL.
ext_symlink_remove() {
    local link="$1"

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_symlink_remove: would remove symlink ${link}"
        return 0
    fi

    if [[ -L "$link" ]]; then
        rm -f "$link"
        log_info "Removed symlink: $link"
    else
        log_info "ext_symlink_remove: ${link} is not a symlink — no-op"
    fi
}

# ---------------------------------------------------------------------------
# §8 sub-installer
# ---------------------------------------------------------------------------

# ext_run_installer <script> [args...]
# Wraps install-amux.sh. Delegates elevation entirely (install-amux.sh already
# implements the full privilege ladder including a 900 s interactive wait —
# brd constraint 2.6). No re-prompt, no output wrapping, no timeout added here.
# Gated by CLAUDE_INSTALL_NO_EXTERNAL.
ext_run_installer() {
    local script="$1"
    shift

    if [[ "${CLAUDE_INSTALL_NO_EXTERNAL:-}" == "1" ]]; then
        log_info "[NO_EXTERNAL] ext_run_installer: would run ${script} $*"
        return 0
    fi

    if [[ ! -x "$script" ]]; then
        log_error "ext_run_installer: ${script} is not executable"
        return 1
    fi

    "$script" "$@"
    local rc=$?
    [[ $rc -ne 0 ]] && log_warn "ext_run_installer: ${script} exited with status $rc"
    return $rc
}

# =============================================================================
# §7. Settings helpers — ownership-scoped merge and MCP management
# =============================================================================

# ---------------------------------------------------------------------------
# §7 / §4 ~/.claude.json backup helpers
# ---------------------------------------------------------------------------

# _backup_claude_json
# Takes a timestamped backup of ~/.claude.json into $BACKUP_DIR before the
# first write of this run. Idempotent — subsequent calls within the same run
# are no-ops, so the backup always reflects the pre-run state.
_backup_claude_json() {
    local claude_json="$HOME/.claude.json"
    if [[ -n "$CLAUDE_JSON_BACKUP_FILE" ]]; then
        return 0  # Already backed up this run — use the pre-run state
    fi
    if [[ ! -f "$claude_json" ]]; then
        return 0  # Nothing to back up
    fi
    mkdir -p "$BACKUP_DIR"
    CLAUDE_JSON_BACKUP_FILE="$BACKUP_DIR/claude.json.$(date +%Y%m%d_%H%M%S).bak"
    cp "$claude_json" "$CLAUDE_JSON_BACKUP_FILE"
    log_info "Backup created: $CLAUDE_JSON_BACKUP_FILE"
}

# _restore_claude_json_from_backup
# Restores ~/.claude.json from the backup taken by _backup_claude_json.
# No-op if no backup was taken (e.g. no write happened yet this run).
_restore_claude_json_from_backup() {
    if [[ -n "$CLAUDE_JSON_BACKUP_FILE" && -f "$CLAUDE_JSON_BACKUP_FILE" ]]; then
        cp "$CLAUDE_JSON_BACKUP_FILE" "$HOME/.claude.json"
        log_info "Restored ~/.claude.json from backup: $CLAUDE_JSON_BACKUP_FILE"
    fi
}

# ---------------------------------------------------------------------------
# §4 MCP server registration / de-registration
# ---------------------------------------------------------------------------

# _register_mcp_server <name> <script> <env_json>
# Registers (or updates) one MCP server in ~/.claude.json.
# - uv gate: returns 1 early if uv is not available
# - backup before first write of the run
# - atomic tmp→mv write
# - validate-then-replace; restore from backup on re-validation failure
# - foreign mcpServers entries are preserved
_register_mcp_server() {
    local name="$1"
    local script="$2"
    # Avoid ${3:-{}} — when $3 = "{}", the expansion appends a stray "}" producing
    # invalid JSON "{}}". Use an explicit test instead.
    local env_json
    if [[ -n "${3:-}" ]]; then env_json="$3"; else env_json="{}"; fi
    local claude_json="$HOME/.claude.json"

    if [[ "$UV_AVAILABLE" != true ]]; then
        log_warn "_register_mcp_server: uv not available — skipping MCP server registration: $name"
        return 1
    fi

    # Create if absent
    if [[ ! -f "$claude_json" ]]; then
        echo '{}' > "$claude_json"
    fi

    # Validate existing file
    if ! jq empty "$claude_json" 2>/dev/null; then
        log_warn "_register_mcp_server: ~/.claude.json is not valid JSON — skipping: $name"
        return 1
    fi

    # Backup before any write (§4 requirement — once per run)
    _backup_claude_json

    # Build merged content
    local tmp="$claude_json.tmp.$$"
    if ! jq --arg name "$name" \
            --arg script "$script" \
            --argjson env_json "$env_json" \
            '.mcpServers = (.mcpServers // {}) + {($name): {type: "stdio", command: "uv", args: ["run", "--script", $script], env: $env_json}}' \
            "$claude_json" > "$tmp" 2>/dev/null; then
        log_warn "_register_mcp_server: jq failed producing merged content for: $name"
        rm -f "$tmp"
        return 1
    fi

    # Validate output
    if ! jq empty "$tmp" 2>/dev/null; then
        log_warn "_register_mcp_server: merged content is not valid JSON — $name not registered"
        rm -f "$tmp"
        return 1
    fi

    mv "$tmp" "$claude_json"

    # Re-validate after write (§7.3 atomicity — same discipline as settings.json)
    if ! jq empty "$claude_json" 2>/dev/null; then
        log_error "_register_mcp_server: re-validation failed after writing $name. Restoring backup and aborting..."
        _restore_claude_json_from_backup
        exit 1
    fi

    log_info "MCP server registered in ~/.claude.json: $name (uv run --script $script)"
    return 0
}

# _deregister_mcp_server <name>
# Removes one MCP server from ~/.claude.json by key.
# Foreign mcpServers entries are left untouched.
# No-op if the server is not registered or ~/.claude.json does not exist.
_deregister_mcp_server() {
    local name="$1"
    local claude_json="$HOME/.claude.json"

    if [[ ! -f "$claude_json" ]]; then
        log_info "_deregister_mcp_server: ~/.claude.json absent — nothing to remove for: $name"
        return 0
    fi

    if ! jq empty "$claude_json" 2>/dev/null; then
        log_warn "_deregister_mcp_server: ~/.claude.json is not valid JSON — skipping: $name"
        return 1
    fi

    # No-op if not registered
    if ! jq -e --arg name "$name" '.mcpServers[$name]' "$claude_json" >/dev/null 2>&1; then
        log_info "_deregister_mcp_server: $name not found in ~/.claude.json — no-op"
        return 0
    fi

    _backup_claude_json

    local tmp="$claude_json.tmp.$$"
    if ! jq --arg name "$name" 'del(.mcpServers[$name])' "$claude_json" > "$tmp" 2>/dev/null; then
        log_warn "_deregister_mcp_server: jq failed removing $name from ~/.claude.json"
        rm -f "$tmp"
        return 1
    fi

    if ! jq empty "$tmp" 2>/dev/null; then
        log_warn "_deregister_mcp_server: merged content is not valid JSON — $name not removed"
        rm -f "$tmp"
        return 1
    fi

    mv "$tmp" "$claude_json"

    if ! jq empty "$claude_json" 2>/dev/null; then
        log_error "_deregister_mcp_server: re-validation failed after removing $name. Restoring backup and aborting..."
        _restore_claude_json_from_backup
        exit 1
    fi

    log_info "MCP server de-registered from ~/.claude.json: $name"
    return 0
}

# ---------------------------------------------------------------------------
# §7.1 Hooks merge — ownership-scoped (identifies entries by command)
# ---------------------------------------------------------------------------

# _merge_hooks_owned <current_settings_json> <hooks_json> <hooks_dir>
# Merges the per-feature HOOKS_JSON into the existing settings.json hooks,
# ownership-scoped: an entry is ours if any of its hooks[].command references
# $hooks_dir.
#
# Rules (architecture §7.1):
# - Install/update: remove ours for the events being installed, append new ones.
#   Foreign entries in the same array survive in order.
# - Notification is shared between telegram (idle_prompt) and amux (permission_prompt).
#   Merge keys on .matcher — never replaces the whole array.
# - Returns the full settings JSON with merged hooks on stdout.
_merge_hooks_owned() {
    local current="$1"   # full settings.json string
    local new_hooks="$2" # HOOKS_JSON built by feature installs
    local hooks_dir="$3" # GLOBAL_HOOKS_DIR

    echo "$current" | jq \
        --argjson nh "$new_hooks" \
        --arg hdir "$hooks_dir" \
        '
        # True if any hooks[].command in this entry references $hdir (= owned by us).
        def is_ours:
          [.hooks // [] | .[] | .command // ""] | any(.[]; contains($hdir));

        # Merge a simple (non-Notification) event array:
        # remove our old entries, append the new ones.
        def merge_event(event_key):
          if ($nh[event_key] // [] | length) == 0 then .
          else
            .hooks[event_key] = (
              (.hooks[event_key] // [] | map(select(is_ours | not))) +
              $nh[event_key]
            )
          end;

        # Merge Notification, keyed by .matcher.
        # Keeps: foreign entries (not ours) and our entries for matchers NOT being replaced.
        # Replaces: our entries for matchers present in $nh.Notification.
        def merge_notification:
          if ($nh.Notification // [] | length) == 0 then .
          else
            ($nh.Notification | [.[].matcher]) as $our_matchers |
            .hooks.Notification = (
              (.hooks.Notification // [] | map(select(
                (is_ours | not)
                or
                ((.matcher // "") as $m | ($our_matchers | any(.[]; . == $m)) | not)
              ))) +
              $nh.Notification
            )
          end;

        merge_event("PreToolUse") |
        merge_event("PermissionRequest") |
        merge_event("PostToolUse") |
        merge_notification |
        merge_event("UserPromptSubmit") |
        merge_event("Stop") |
        merge_event("SubagentStop") |
        merge_event("SessionEnd")
        '
}

# ---------------------------------------------------------------------------
# §7.2 Permissions merge — union and overwrite modes
# ---------------------------------------------------------------------------

# _merge_permissions_union <current_settings_json> <new_allow_json> <new_deny_json> <new_ask_json>
# Set-union merge: existing entries are preserved; new entries are appended only
# if not already present. Sets globals PERMISSIONS_ADDED_ALLOW and
# PERMISSIONS_ADDED_DENY with the actually-added subsets (for the manifest).
# Also folds in legacy allowedTools / disallowedTools keys.
# Returns the merged settings JSON on stdout.
_merge_permissions_union() {
    local current="$1"
    local new_allow="$2"
    local new_deny="$3"
    local new_ask="$4"

    # Compute added subsets (items in new_* that are not already in existing)
    PERMISSIONS_ADDED_ALLOW=$(echo "$current" | jq \
        --argjson na "$new_allow" \
        '(.allowedTools // .permissions.allow // []) as $existing |
         $na | map(select(. as $x | $existing | any(.[]; . == $x) | not))')

    PERMISSIONS_ADDED_DENY=$(echo "$current" | jq \
        --argjson nd "$new_deny" \
        '(.disallowedTools // .permissions.deny // []) as $existing |
         $nd | map(select(. as $x | $existing | any(.[]; . == $x) | not))')

    # Produce merged settings with union applied
    echo "$current" | jq \
        --argjson added_allow "$PERMISSIONS_ADDED_ALLOW" \
        --argjson added_deny  "$PERMISSIONS_ADDED_DENY" \
        --argjson new_ask     "$new_ask" \
        '(.allowedTools // .permissions.allow // []) as $ex_allow |
         (.disallowedTools // .permissions.deny // []) as $ex_deny |
         (.permissions.ask // []) as $ex_ask |
         ($new_ask | map(select(. as $x | $ex_ask | any(.[]; . == $x) | not))) as $added_ask |
         del(.allowedTools, .disallowedTools) |
         . + {permissions: {
             allow: ($ex_allow + $added_allow),
             deny:  ($ex_deny  + $added_deny),
             ask:   ($ex_ask   + $added_ask)
         }}'
}

# _merge_permissions_overwrite <current_settings_json> <new_allow_json> <new_deny_json> <new_ask_json>
# Overwrite mode: replaces the permissions block wholesale (today's semantics).
# Reports the count of allow entries being discarded before writing.
# Sets PERMISSIONS_ADDED_ALLOW / PERMISSIONS_ADDED_DENY to empty arrays (nothing
# to track for uninstall in overwrite mode).
# Also folds in legacy allowedTools / disallowedTools keys.
# Returns the merged settings JSON on stdout.
_merge_permissions_overwrite() {
    local current="$1"
    local new_allow="$2"
    local new_deny="$3"
    local new_ask="$4"

    # Count entries being discarded (in existing but NOT in new_allow)
    local discarded_count
    discarded_count=$(echo "$current" | jq \
        --argjson na "$new_allow" \
        '(.allowedTools // .permissions.allow // []) |
         map(select(. as $x | $na | any(.[]; . == $x) | not)) | length')

    if [[ "$discarded_count" -gt 0 ]]; then
        log_warn "permissions-allowlist overwrite: discarding $discarded_count existing allow entries not in project config" >&2
    fi

    PERMISSIONS_ADDED_ALLOW='[]'
    PERMISSIONS_ADDED_DENY='[]'

    echo "$current" | jq \
        --argjson allowed    "$new_allow" \
        --argjson disallowed "$new_deny" \
        --argjson ask        "$new_ask" \
        'del(.allowedTools, .disallowedTools) |
         . + {permissions: {allow: $allowed, deny: $disallowed, ask: $ask}}'
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

    # permissions-allowlist feature contribution (§7.2 union / overwrite)
    if [[ "${PERMISSIONS_ALLOWLIST_SELECTED:-false}" == true ]]; then
        if [[ "${PERMISSIONS_ALLOWLIST_MODE:-union}" == "overwrite" ]]; then
            merged=$(_merge_permissions_overwrite "$merged" "$allowed_tools" "$disallowed_tools" "$ask_tools")
            # Reset globals: _merge_permissions_overwrite sets them inside a subshell
            # so they are lost; explicitly set them in the parent scope.
            PERMISSIONS_ADDED_ALLOW='[]'
            PERMISSIONS_ADDED_DENY='[]'
            log_info "Permissions merged (overwrite): $allowed_count allow, $disallowed_count deny, $ask_count ask entries"
        else
            # Compute added subsets in parent scope BEFORE calling the merge function
            # in a subshell. Bash subshells cannot propagate variable assignments back
            # to the parent, so PERMISSIONS_ADDED_ALLOW / PERMISSIONS_ADDED_DENY must
            # be set here, not inside _merge_permissions_union.
            PERMISSIONS_ADDED_ALLOW=$(echo "$merged" | jq \
                --argjson na "$allowed_tools" \
                '(.allowedTools // .permissions.allow // []) as $existing |
                 $na | map(select(. as $x | $existing | any(.[]; . == $x) | not))')
            PERMISSIONS_ADDED_DENY=$(echo "$merged" | jq \
                --argjson nd "$disallowed_tools" \
                '(.disallowedTools // .permissions.deny // []) as $existing |
                 $nd | map(select(. as $x | $existing | any(.[]; . == $x) | not))')
            merged=$(_merge_permissions_union "$merged" "$allowed_tools" "$disallowed_tools" "$ask_tools")
            local added_allow_count added_deny_count
            added_allow_count=$(echo "$PERMISSIONS_ADDED_ALLOW" | jq 'length')
            added_deny_count=$(echo "$PERMISSIONS_ADDED_DENY" | jq 'length')
            log_info "Permissions merged (union): +${added_allow_count} allow, +${added_deny_count} deny added"
        fi
    fi

    # hooks contribution — ownership-scoped merge (§7.1).
    # This replaces the old '. + {hooks: $hooks}' wholesale write.
    # Foreign hook entries (command does not reference GLOBAL_HOOKS_DIR) survive.
    if [[ -n "$HOOKS_JSON" && "$HOOKS_JSON" != '{}' ]]; then
        merged=$(_merge_hooks_owned "$merged" "$HOOKS_JSON" "$GLOBAL_HOOKS_DIR")
        log_info "Hooks configuration merged (ownership-scoped)"
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
            log_info "  - Notification (matchers: $(echo "$HOOKS_JSON" | jq -r '[.Notification // [] | .[].matcher] | join(", ")'))"
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

    # Validate merged JSON (§7.3 atomicity — build, validate, write, re-validate)
    if ! echo "$merged" | jq empty 2>/dev/null; then
        log_error "Merged settings.json is not valid JSON! Restoring from backup..."
        cp "$BACKUP_FILE" "$GLOBAL_CONFIG"
        exit 1
    fi

    # Write merged config
    echo "$merged" | jq '.' > "$GLOBAL_CONFIG"
    log_info "Global config updated: $GLOBAL_CONFIG"

    # Re-validate after write
    if ! jq empty "$GLOBAL_CONFIG" 2>/dev/null; then
        log_error "settings.json re-validation failed after write! Restoring from backup..."
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
# "union" (default) or "overwrite" (today's replace semantics).
# Set via CLAUDE_INSTALL_PERMISSIONS_MODE env var; wired to the interactive
# selector in 29-06.
PERMISSIONS_ALLOWLIST_MODE="${CLAUDE_INSTALL_PERMISSIONS_MODE:-union}"
# Tracks what a union install added so uninstall (29-05) can subtract exactly it.
PERMISSIONS_ADDED_ALLOW='[]'
PERMISSIONS_ADDED_DENY='[]'
# Path to the timestamped backup of ~/.claude.json taken before any write this run.
# Empty until the first _register_mcp_server call.
CLAUDE_JSON_BACKUP_FILE=""

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
