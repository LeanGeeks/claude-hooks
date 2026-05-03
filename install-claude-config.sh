#!/bin/bash
# Installs Claude Code configuration globally
# - Copies hooks from .claude/hooks/ to ~/.claude/hooks/
# - Merges hooks configuration (PreToolUse and Notification) from project to global settings
# - Merges allowedTools/disallowedTools from project to global settings
# - Preserves all other settings in the global config

set -euo pipefail

PROJECT_HOOKS_DIR=".claude/hooks"
PROJECT_CONFIG=".claude/settings.json"
GLOBAL_HOOKS_DIR="$HOME/.claude/hooks"
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

log_step "Step 1/3: Installing hooks"

# Check project hooks directory exists
if [[ ! -d "$PROJECT_HOOKS_DIR" ]]; then
    log_warn "Project hooks directory not found: $PROJECT_HOOKS_DIR"
    log_warn "Skipping hook installation..."
    HOOKS_INSTALLED=false
else
    # Check for required hook files
    REQUIRED_HOOKS=("pretool_hook.py" "bash_command_parser.py" "settings_loader.py" "notification_hook.py" "permission_request_hook.py" "permission_state_store.py" "telegram_permission_router.py" "telegram_daemon.py" "posttool_hook.py")
    # Optional utility scripts
    UTILITY_SCRIPTS=("get_telegram_chat_id.py")
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

        HOOKS_INSTALLED=true
    fi
fi

# =============================================================================
# STEP 2: Validate and Backup Configs
# =============================================================================

log_step "Step 2/3: Validating and backing up configs"

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
# STEP 3: Merge Permissions and Hooks Configuration
# =============================================================================

log_step "Step 3/3: Merging permissions and hooks configuration"

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
if [[ "$HOOKS_INSTALLED" == true ]]; then
    # Extract hooks config from project
    HOOKS_CONFIG=$(jq '.hooks // {}' "$PROJECT_CONFIG")

    # Check if project has hooks configuration
    HOOKS_COUNT=$(echo "$HOOKS_CONFIG" | jq 'length')
    if [[ "$HOOKS_COUNT" -gt 0 ]]; then
        log_info "Found hooks configuration in project config"

        # Convert relative hook path to absolute path for global use
        # Build the hooks config with absolute path
        HOOKS_CONFIG=$(jq -n --arg pretool_path "$GLOBAL_HOOKS_DIR/pretool_hook.py" \
                       --arg notification_path "$GLOBAL_HOOKS_DIR/notification_hook.py" \
                       --arg permission_path "$GLOBAL_HOOKS_DIR/permission_request_hook.py" \
                       --arg posttool_path "$GLOBAL_HOOKS_DIR/posttool_hook.py" \
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
                        command: ("CLAUDE_HOOK_DEBUG=1 python3 " + $permission_path)
                    }]
                }],
                PostToolUse: [{
                    matcher: "*",
                    hooks: [{
                        type: "command",
                        command: ("python3 " + $posttool_path)
                    }]
                }],
                Notification: [
                    {
                        matcher: "permission_prompt",
                        hooks: [{
                            type: "command",
                            command: ("python3 " + $notification_path)
                        }]
                    },
                    {
                        matcher: "idle_prompt",
                        hooks: [{
                            type: "command",
                            command: ("python3 " + $notification_path)
                        }]
                    }
                ]
            }')

        # Merge hooks into global config
        MERGED=$(echo "$MERGED" | jq --argjson hooks "$HOOKS_CONFIG" '. + {hooks: $hooks}')
        log_info "Hooks configuration merged:"
        log_info "  - PreToolUse: python3 $GLOBAL_HOOKS_DIR/pretool_hook.py"
        log_info "  - PermissionRequest: python3 $GLOBAL_HOOKS_DIR/permission_request_hook.py"
        log_info "  - PostToolUse: python3 $GLOBAL_HOOKS_DIR/posttool_hook.py"
        log_info "  - Notification: python3 $GLOBAL_HOOKS_DIR/notification_hook.py"
    else
        log_warn "No hooks configuration found in project config"
        log_warn "Hooks were installed but not configured in settings.json"
    fi
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
    echo "    - Notification: permission_prompt and idle_prompt notifications"
else
    echo "  - hooks: not installed (missing files)"
fi

echo ""
log_info "Other settings preserved:"
jq 'del(.permissions, .hooks, .description, .notes) | keys[]' "$GLOBAL_CONFIG" 2>/dev/null | while read -r key; do
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
