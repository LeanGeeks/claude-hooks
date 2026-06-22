#!/bin/bash
# Installs Claude Code configuration globally
# - Copies hooks from .claude/hooks/ to ~/.claude/hooks/
# - Copies statusline from .claude/statusline/ to ~/.claude/statusline/
# - Merges hooks configuration (PreToolUse, PermissionRequest, PostToolUse, and idle Notification) from project to global settings
# - Merges statusLine configuration from project to global settings
# - Merges allowedTools/disallowedTools from project to global settings
# - Preserves all other settings in the global config

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_HOOKS_DIR=".claude/hooks"
PROJECT_BIN_DIR=".claude/bin"
PROJECT_STATUSLINE_DIR=".claude/statusline"
PROJECT_CONFIG=".claude/settings.json"
PROJECT_RELAY_DIR="$SCRIPT_DIR/relay-server"
GLOBAL_HOOKS_DIR="$HOME/.claude/hooks"
GLOBAL_STATUSLINE_DIR="$HOME/.claude/statusline"
GLOBAL_CONFIG="$HOME/.claude/settings.json"
BACKUP_DIR="$HOME/.claude/backups"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

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
        # Skip empty or sub-threshold logs.
        size=$(wc -c < "$path" 2>/dev/null || echo 0)
        if [[ "$size" -le "$min_bytes" ]]; then
            continue
        fi

        # gzip into a timestamped archive; -c keeps stdout so the live log can be
        # truncated afterward, leaving a fresh empty file for the next run.
        if gzip -c "$path" > "$path.$stamp.gz" 2>/dev/null; then
            : > "$path"
            log_info "Rotated log: $name → $name.$stamp.gz ($(( size / 1024 )) KB)"
        else
            log_warn "Could not rotate log: $name"
            rm -f "$path.$stamp.gz"
            continue
        fi

        # Prune older archives, keeping the newest $keep per base name.
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

# Check dependencies
if ! command -v jq &> /dev/null; then
    log_error "jq is required but not installed. Install with: sudo apt install jq"
    exit 1
fi

# Check python3 is available
if ! command -v python3 &> /dev/null; then
    log_error "python3 is required but not installed."
    exit 1
fi

# =============================================================================
# STEP 1: Install Hooks
# =============================================================================

log_step "Step 1/5: Installing hooks"

# Rotate (gzip) hook debug/error logs before wiring anything. A fresh install is
# a natural rotation point and keeps these logs from growing unbounded.
rotate_hook_logs

# ---------------------------------------------------------------------------
# Legacy daemon cleanup. Pre-relay installs ran a long-lived telegram_daemon.py
# that polled getUpdates directly. The relay server now owns the bot token via
# a webhook, so a surviving daemon (a) gets HTTP 409 on every getUpdates and
# (b) tries to revoke Telegram messages with relay message ids, failing with
# HTTP 400. Stop any such process and remove its artifacts so it can't linger
# across the migration. Idempotent: a no-op on clean installs.
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
# Drop the orphaned daemon script if a previous install left it behind; nothing
# in the relay architecture imports it.
rm -f "$GLOBAL_HOOKS_DIR/telegram_daemon.py"

# Check project hooks directory exists
if [[ ! -d "$PROJECT_HOOKS_DIR" ]]; then
    log_warn "Project hooks directory not found: $PROJECT_HOOKS_DIR"
    log_warn "Skipping hook installation..."
    HOOKS_INSTALLED=false
else
    # Check for required hook files
    REQUIRED_HOOKS=("pretool_hook.py" "bash_command_parser.py" "settings_loader.py" "notification_hook.py" "permission_request_hook.py" "permission_state_store.py" "telegram_permission_router.py" "posttool_hook.py" "reply_injector.py" "amux_spawn_lib.py" "spawn_producer_hook.py")
    # Optional utility scripts (none at present — get_telegram_chat_id.py was
    # removed in Phase 6: the relay server owns the bot token, so direct
    # getUpdates polling from a device is no longer needed).
    UTILITY_SCRIPTS=()
    MISSING_HOOKS=()

    for hook in "${REQUIRED_HOOKS[@]}"; do
        if [[ ! -f "$PROJECT_HOOKS_DIR/$hook" ]]; then
            MISSING_HOOKS+=("$hook")
        fi
    done

    if [[ ${#MISSING_HOOKS[@]} -gt 0 ]]; then
        log_warn "Missing required hook files:"
        for hook in "${MISSING_HOOKS[@]}"; do
            echo "  - $hook"
        done
        log_warn "Skipping hook installation..."
        HOOKS_INSTALLED=false
    else
        # Create global hooks directory
        mkdir -p "$GLOBAL_HOOKS_DIR"

        # Copy hook files
        log_info "Copying hook files to $GLOBAL_HOOKS_DIR"
        for hook in "${REQUIRED_HOOKS[@]}"; do
            cp "$PROJECT_HOOKS_DIR/$hook" "$GLOBAL_HOOKS_DIR/"
            chmod +x "$GLOBAL_HOOKS_DIR/$hook"
            log_info "  Installed: $hook"
        done

        # Copy utility scripts (optional, warn if missing)
        for util in "${UTILITY_SCRIPTS[@]}"; do
            if [[ -f "$PROJECT_HOOKS_DIR/$util" ]]; then
                cp "$PROJECT_HOOKS_DIR/$util" "$GLOBAL_HOOKS_DIR/"
                chmod +x "$GLOBAL_HOOKS_DIR/$util"
                log_info "  Installed utility: $util"
            else
                log_warn "  Optional utility not found: $util"
            fi
        done

        # Make the relay_server package importable by the copied hooks. The
        # hooks are flat copies under ~/.claude/hooks/, so they cannot locate the
        # repo on their own (telegram_permission_router.py only finds the package
        # via an env var, a checkout it can walk up to, or an importable install).
        # Drop a user-site .pth pointing at the repo's relay-server dir so
        # `import relay_server` works for the system python3 that runs the hooks
        # — no pip and no --break-system-packages needed.
        if [[ -f "$PROJECT_RELAY_DIR/relay_server/__init__.py" ]]; then
            USER_SITE="$(python3 -m site --user-site 2>/dev/null)"
            if [[ -n "$USER_SITE" ]]; then
                mkdir -p "$USER_SITE"
                echo "$PROJECT_RELAY_DIR" > "$USER_SITE/claude-relay-server.pth"
                log_info "  Linked relay_server via $USER_SITE/claude-relay-server.pth"
            else
                log_warn "  Could not determine user site-packages; Telegram relay hooks may not import relay_server"
            fi
            # The relay client imports httpx at runtime; warn early if it's absent
            # for the system python3 (the relay stays disabled without it).
            if ! python3 -c "import httpx" 2>/dev/null; then
                log_warn "  Python 'httpx' not available for system python3 — Telegram relay will stay disabled."
                log_warn "  Install it with: sudo apt install python3-httpx   (or: pip install --user httpx)"
            fi
        fi

        HOOKS_INSTALLED=true
    fi
fi

# =============================================================================
# STEP 2: Install Statusline
# =============================================================================

log_step "Step 2/5: Installing statusline"

STATUSLINE_INSTALLED=false
if [[ ! -d "$PROJECT_STATUSLINE_DIR" ]]; then
    log_warn "Project statusline directory not found: $PROJECT_STATUSLINE_DIR"
    log_warn "Skipping statusline installation..."
else
    STATUSLINE_SCRIPT="$PROJECT_STATUSLINE_DIR/statusline.py"
    if [[ ! -f "$STATUSLINE_SCRIPT" ]]; then
        log_warn "statusline.py not found in $PROJECT_STATUSLINE_DIR"
        log_warn "Skipping statusline installation..."
    else
        mkdir -p "$GLOBAL_STATUSLINE_DIR"
        cp "$STATUSLINE_SCRIPT" "$GLOBAL_STATUSLINE_DIR/statusline.py"
        chmod +x "$GLOBAL_STATUSLINE_DIR/statusline.py"
        log_info "Installed: statusline.py → $GLOBAL_STATUSLINE_DIR/statusline.py"

        # Copy pricing config alongside the script. statusline.py loads
        # this file relative to its own location; without it every API
        # model renders as "cost ?".
        PRICING_DEFAULT="$PROJECT_STATUSLINE_DIR/pricing.default.json"
        if [[ -f "$PRICING_DEFAULT" ]]; then
            cp "$PRICING_DEFAULT" "$GLOBAL_STATUSLINE_DIR/pricing.default.json"
            log_info "Installed: pricing.default.json → $GLOBAL_STATUSLINE_DIR/pricing.default.json"
        else
            log_warn "pricing.default.json not found in $PROJECT_STATUSLINE_DIR — API cost will render as 'cost ?'"
        fi

        STATUSLINE_INSTALLED=true
    fi
fi

# =============================================================================
# STEP 3: Install amux-spawn launcher on PATH (epic 10)
# =============================================================================
#
# amux-spawn (and its producer/read hooks in later tasks) must reach PATH
# system-wide and must NOT depend on repo-local files at runtime. We install the
# executable into a user-global bin dir and rely on its shared library being
# copied alongside the hooks (amux_spawn_lib.py is in REQUIRED_HOOKS above, so it
# lands in ~/.claude/hooks/, which the launcher adds to sys.path).
#
# Install target precedence: ~/.local/bin (on PATH for most distros via the XDG
# user dirs) — created if missing. We warn if it is not on PATH so the operator
# can fix their shell rc.

log_step "Step 3/5: Installing amux-spawn launcher"

AMUX_SPAWN_INSTALLED=false
USER_BIN_DIR="$HOME/.local/bin"
if [[ ! -f "$PROJECT_BIN_DIR/amux-spawn" ]]; then
    log_warn "amux-spawn not found at $PROJECT_BIN_DIR/amux-spawn — skipping launcher install"
else
    mkdir -p "$USER_BIN_DIR"
    cp "$PROJECT_BIN_DIR/amux-spawn" "$USER_BIN_DIR/amux-spawn"
    chmod +x "$USER_BIN_DIR/amux-spawn"
    log_info "Installed: amux-spawn → $USER_BIN_DIR/amux-spawn"
    AMUX_SPAWN_INSTALLED=true

    # Sanity: it must import its shared lib (copied to ~/.claude/hooks/ in Step 1).
    if [[ "$HOOKS_INSTALLED" == true ]]; then
        if python3 -c "import sys; sys.path.insert(0, '$GLOBAL_HOOKS_DIR'); import amux_spawn_lib" 2>/dev/null; then
            log_info "  amux_spawn_lib importable from $GLOBAL_HOOKS_DIR"
        else
            log_warn "  amux_spawn_lib not importable — amux-spawn will fail until Step 1 succeeds"
        fi
    else
        log_warn "  Hooks not installed, so amux_spawn_lib.py is not in $GLOBAL_HOOKS_DIR — amux-spawn will not run"
    fi

    case ":$PATH:" in
        *":$USER_BIN_DIR:"*) ;;
        *) log_warn "  $USER_BIN_DIR is not on your PATH. Add it, e.g.: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
fi

# =============================================================================
# STEP 4: Validate and Backup Configs
# =============================================================================

log_step "Step 4/5: Validating and backing up configs"

# Check project config exists
if [[ ! -f "$PROJECT_CONFIG" ]]; then
    log_error "Project config not found: $PROJECT_CONFIG"
    log_error "Run this script from a project directory with .claude/settings.json"
    exit 1
fi

# Validate project config JSON
if ! jq empty "$PROJECT_CONFIG" 2>/dev/null; then
    log_error "Project config is not valid JSON: $PROJECT_CONFIG"
    exit 1
fi
log_info "Project config validated: $PROJECT_CONFIG"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Create global config if it doesn't exist
if [[ ! -f "$GLOBAL_CONFIG" ]]; then
    log_warn "Global config doesn't exist, creating empty one"
    mkdir -p "$(dirname "$GLOBAL_CONFIG")"
    echo '{}' > "$GLOBAL_CONFIG"
fi

# Validate global config JSON
if ! jq empty "$GLOBAL_CONFIG" 2>/dev/null; then
    log_error "Global config is not valid JSON: $GLOBAL_CONFIG"
    exit 1
fi
log_info "Global config validated: $GLOBAL_CONFIG"

# Create timestamped backup
BACKUP_FILE="$BACKUP_DIR/settings.json.$(date +%Y%m%d_%H%M%S).bak"
cp "$GLOBAL_CONFIG" "$BACKUP_FILE"
log_info "Backup created: $BACKUP_FILE"

# =============================================================================
# STEP 5: Merge Permissions, Hooks, and Statusline Configuration
# =============================================================================

log_step "Step 5/5: Merging permissions, hooks, and statusline configuration"

# Extract permissions from project config (supports both old and new format)
ALLOWED_TOOLS=$(jq '.allowedTools // .permissions.allow // []' "$PROJECT_CONFIG")
DISALLOWED_TOOLS=$(jq '.disallowedTools // .permissions.deny // []' "$PROJECT_CONFIG")

ALLOWED_COUNT=$(echo "$ALLOWED_TOOLS" | jq 'length')
DISALLOWED_COUNT=$(echo "$DISALLOWED_TOOLS" | jq 'length')

log_info "Found $ALLOWED_COUNT allowed tools and $DISALLOWED_COUNT disallowed tools in project config"

# Start with permissions merge
MERGED=$(jq --argjson allowed "$ALLOWED_TOOLS" \
            --argjson disallowed "$DISALLOWED_TOOLS" \
            'del(.allowedTools, .disallowedTools) | . + {permissions: {allow: $allowed, deny: $disallowed}}' \
            "$GLOBAL_CONFIG")

# Merge hooks configuration if hooks were installed
# Note: hooks are NOT defined in project settings.json to avoid duplication
# (Claude Code merges project + global settings, so defining hooks in both fires them twice).
# The install script always writes hooks to global settings unconditionally.
if [[ "$HOOKS_INSTALLED" == true ]]; then
        # Build the hooks config with absolute paths
        HOOKS_CONFIG=$(jq -n --arg pretool_path "$GLOBAL_HOOKS_DIR/pretool_hook.py" \
                       --arg notification_path "$GLOBAL_HOOKS_DIR/notification_hook.py" \
                       --arg permission_path "$GLOBAL_HOOKS_DIR/permission_request_hook.py" \
                       --arg posttool_path "$GLOBAL_HOOKS_DIR/posttool_hook.py" \
                       --arg producer_path "$GLOBAL_HOOKS_DIR/spawn_producer_hook.py" \
            '{
                PreToolUse: [{
                    matcher: "Bash",
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
                        # Block up to 12h waiting for a Telegram approval. Must be
                        # >= REQUEST_TTL in permission_request_hook.py, else Claude
                        # Code kills the hook before the request can be answered.
                        timeout: 43200
                    }]
                }],
                PostToolUse: [{
                    matcher: "*",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $posttool_path)
                    }]
                }],
                # Notification has TWO matchers, kept separate and additive:
                #  - idle_prompt   -> the existing Telegram idle notification (task 09)
                #  - permission_prompt -> the epic-10 producer sets permission_pending
                #                         on the tracked sessions handle (no-op for
                #                         plain/human sessions). These never clobber
                #                         each other; the producer ignores idle_prompt.
                Notification: [{
                    matcher: "idle_prompt",
                    hooks: [{
                        type: "command",
                        # CLAUDE_HOOK_DEBUG=1 mirrors PermissionRequest: it logs the
                        # idle-notification path AND propagates (via inherited env)
                        # to the detached reply_injector.py it spawns, so the
                        # reply-from-Telegram chain is observable end-to-end.
                        command: ("CLAUDE_HOOK_DEBUG=1 python3 " + $notification_path)
                    }]
                }, {
                    matcher: "permission_prompt",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $producer_path + " --event Notification")
                    }]
                }],
                # Epic-10 producer (Stop-based state machine for tracked amux
                # sessions). Each handle-gates: a no-op for plain/human sessions and
                # other repos. Stop is authoritative (sets idle); SubagentStop is
                # freshness-only; SessionEnd marks terminated.
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

        # Merge hooks into global config
        MERGED=$(echo "$MERGED" | jq --argjson hooks "$HOOKS_CONFIG" '. + {hooks: $hooks}')
        log_info "Hooks configuration merged:"
        log_info "  - PreToolUse: python3 $GLOBAL_HOOKS_DIR/pretool_hook.py"
        log_info "  - PermissionRequest: python3 $GLOBAL_HOOKS_DIR/permission_request_hook.py"
        log_info "  - PostToolUse: python3 $GLOBAL_HOOKS_DIR/posttool_hook.py"
        log_info "  - Notification (idle_prompt): python3 $GLOBAL_HOOKS_DIR/notification_hook.py"
        log_info "  - Notification (permission_prompt) + Stop/SubagentStop/SessionEnd: python3 $GLOBAL_HOOKS_DIR/spawn_producer_hook.py"
fi

# Merge statusLine configuration if statusline was installed
if [[ "$STATUSLINE_INSTALLED" == true ]]; then
    MERGED=$(echo "$MERGED" | jq --arg cmd "python3 $GLOBAL_STATUSLINE_DIR/statusline.py" \
        '. + {statusLine: {type: "command", command: $cmd, refreshInterval: 30}}')
    log_info "StatusLine configuration merged:"
    log_info "  - command: python3 $GLOBAL_STATUSLINE_DIR/statusline.py"
    log_info "  - refreshInterval: 30"
fi

# Validate merged JSON
if ! echo "$MERGED" | jq empty 2>/dev/null; then
    log_error "Merged config is not valid JSON!"
    log_error "Restoring from backup..."
    cp "$BACKUP_FILE" "$GLOBAL_CONFIG"
    exit 1
fi

# Write merged config
echo "$MERGED" | jq '.' > "$GLOBAL_CONFIG"
log_info "Global config updated: $GLOBAL_CONFIG"

# Final validation
if ! jq empty "$GLOBAL_CONFIG" 2>/dev/null; then
    log_error "Final validation failed!"
    log_error "Restoring from backup..."
    cp "$BACKUP_FILE" "$GLOBAL_CONFIG"
    exit 1
fi

# =============================================================================
# Summary
# =============================================================================

echo ""
log_info "Success! Global config now contains:"
echo "  - permissions.allow: $(jq '.permissions.allow | length' "$GLOBAL_CONFIG") entries"
echo "  - permissions.deny: $(jq '.permissions.deny | length' "$GLOBAL_CONFIG") entries"

# Show hooks status
if [[ "$HOOKS_INSTALLED" == true ]]; then
    echo "  - hooks: installed and configured"
    echo "    - PreToolUse: Bash command interception"
    echo "    - PermissionRequest: Telegram-gated permission approval"
    echo "    - PostToolUse: Telegram message cleanup on terminal response"
    echo "    - Notification: idle_prompt → Telegram (forwards agent's last message)"
    echo "    - Stop/SubagentStop/Notification(permission_prompt)/SessionEnd → amux-spawn producer (tracked-session state)"
else
    echo "  - hooks: not installed (missing files)"
fi

# Show statusline status
if [[ "$STATUSLINE_INSTALLED" == true ]]; then
    echo "  - statusLine: installed and configured ($GLOBAL_STATUSLINE_DIR/statusline.py)"
else
    echo "  - statusLine: not installed (missing statusline.py)"
fi

# Show amux-spawn status
if [[ "$AMUX_SPAWN_INSTALLED" == true ]]; then
    echo "  - amux-spawn: installed ($USER_BIN_DIR/amux-spawn)"
else
    echo "  - amux-spawn: not installed"
fi

echo ""
log_info "Other settings preserved:"
jq 'del(.permissions, .hooks, .statusLine, .description, .notes) | keys[]' "$GLOBAL_CONFIG" 2>/dev/null | while read -r key; do
    echo "  - $key"
done || echo "  (none)"

echo ""
log_info "Backup location: $BACKUP_FILE"
log_info "To restore: cp \"$BACKUP_FILE\" \"$GLOBAL_CONFIG\""

# Test the hook if it was installed
if [[ "$HOOKS_INSTALLED" == true ]]; then
    echo ""
    log_info "Testing hook installation..."
    if python3 -c "import sys; sys.path.insert(0, '$GLOBAL_HOOKS_DIR'); from bash_command_parser import BashCommandParser; print('Hook modules loaded successfully')" 2>/dev/null; then
        log_info "Hook modules are working correctly!"
    else
        log_warn "Hook modules test failed (this may be okay if dependencies are missing)"
    fi
fi
