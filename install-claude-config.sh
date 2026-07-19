#!/bin/bash
# Installs Claude Code configuration globally
# - Copies hooks from .claude/hooks/ to ~/.claude/hooks/
# - Copies statusline from .claude/statusline/ to ~/.claude/statusline/
# - Merges hooks configuration (PreToolUse, PermissionRequest, PostToolUse, and idle Notification) from project to global settings
# - Merges statusLine configuration from project to global settings
# - Merges allowedTools/disallowedTools from project to global settings
# - Ensures ~/.tmux.conf tmux options for amux-spawned Claude sessions
#   (focus-events on; forward Claude's OSC title to the terminal tab)
# - Preserves all other settings in the global config

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_HOOKS_DIR=".claude/hooks"
PROJECT_BIN_DIR=".claude/bin"
PROJECT_STATUSLINE_DIR=".claude/statusline"
PROJECT_COMMANDS_DIR=".claude/commands"
PROJECT_CONFIG=".claude/settings.json"
PROJECT_RELAY_DIR="$SCRIPT_DIR/relay-server"
GLOBAL_HOOKS_DIR="$HOME/.claude/hooks"
GLOBAL_STATUSLINE_DIR="$HOME/.claude/statusline"
GLOBAL_COMMANDS_DIR="$HOME/.claude/commands"
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

# Check uv is available (needed for context-usage MCP server)
if command -v uv &> /dev/null; then
    UV_AVAILABLE=true
else
    UV_AVAILABLE=false
    log_warn "uv not found — context-usage MCP server will not be installed."
    log_warn "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
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
    REQUIRED_HOOKS=("pretool_hook.py" "bash_command_parser.py" "settings_loader.py" "notification_hook.py" "permission_request_hook.py" "permission_state_store.py" "session_yolo_store.py" "telegram_permission_router.py" "posttool_hook.py" "reply_injector.py" "amux_spawn_lib.py" "spawn_producer_hook.py")
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

# Install global slash commands (e.g. /yolo, /yolo-off). These are user-global
# markdown commands; they call the installed session_yolo_store.py CLI with the
# current ${CLAUDE_SESSION_ID} to toggle per-session YOLO mode.
COMMANDS_INSTALLED=false
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
# STEP 3a: Install model profiles config (epic 13)
# =============================================================================
#
# profiles.toml stores model backends as structured data (TOML). On a fresh
# install, copy the shipped example so the user has a ready-to-edit template.
# Never overwrite an existing file — it may contain user secrets (API tokens).

log_step "Step 3a/5: Installing model profiles config"

PROFILES_SRC="$SCRIPT_DIR/shell/profiles.example.toml"
PROFILES_DEST="$HOME/.claude/profiles.toml"
PROFILES_INSTALLED=false

if [[ -f "$PROFILES_DEST" ]]; then
    log_info "profiles.toml already present — skipping (never overwrite user config)"
    log_info "  $PROFILES_DEST"
elif [[ -f "$PROFILES_SRC" ]]; then
    mkdir -p "$(dirname "$PROFILES_DEST")"
    cp "$PROFILES_SRC" "$PROFILES_DEST"
    chmod 600 "$PROFILES_DEST"
    log_info "Installed: profiles.example.toml → $PROFILES_DEST"
    PROFILES_INSTALLED=true
else
    log_warn "profiles.example.toml not found at $PROFILES_SRC — skipping"
fi

# Always remind the user to fill in their tokens, whether the file was just
# created or was already present. Skip only if the file doesn't exist at all
# (source not found and dest not present).
if [[ -f "$PROFILES_DEST" ]]; then
    log_info "Edit ~/.claude/profiles.toml with your model tokens."
fi

# =============================================================================
# STEP 3b: Install bash completion + shell integration snippet (epic 10 / 10-05)
# =============================================================================
#
# Completion: installed to a user-level completions dir; automatically sourced by
# bash-completion >= 2.x without any bashrc edit.
# Shell snippet: copied to ~/.claude/shell/; NOT sourced automatically.
#   The user adds one `source` line to opt in (see the printed message below).

log_step "Step 3b/5: Installing amux-spawn completion + shell integration snippet"

COMPLETION_SRC="$SCRIPT_DIR/shell/amux-spawn-completion.bash"
PROFILES_SNIPPET_SRC="$SCRIPT_DIR/shell/claude-profiles.bash"
AMUX_SNIPPET_SRC="$SCRIPT_DIR/shell/amux-spawn.bash"
COMPLETION_INSTALLED=false
SNIPPET_INSTALLED=false

# ── Bash completion ────────────────────────────────────────────────────────────
USER_COMPLETIONS_DIR="$HOME/.local/share/bash-completion/completions"
if [[ -f "$COMPLETION_SRC" ]]; then
    mkdir -p "$USER_COMPLETIONS_DIR"
    cp "$COMPLETION_SRC" "$USER_COMPLETIONS_DIR/amux-spawn"
    chmod 644 "$USER_COMPLETIONS_DIR/amux-spawn"
    log_info "Installed completion: amux-spawn → $USER_COMPLETIONS_DIR/amux-spawn"
    COMPLETION_INSTALLED=true
else
    log_warn "Completion script not found at $COMPLETION_SRC — skipping"
fi

# ── Shell integration snippets ─────────────────────────────────────────────────
CLAUDE_SHELL_DIR="$HOME/.claude/shell"
mkdir -p "$CLAUDE_SHELL_DIR"

if [[ -f "$PROFILES_SNIPPET_SRC" ]]; then
    cp "$PROFILES_SNIPPET_SRC" "$CLAUDE_SHELL_DIR/claude-profiles.bash"
    chmod 644 "$CLAUDE_SHELL_DIR/claude-profiles.bash"
    log_info "Installed shell snippet: claude-profiles.bash → $CLAUDE_SHELL_DIR/claude-profiles.bash"
    SNIPPET_INSTALLED=true
else
    log_warn "Shell snippet not found at $PROFILES_SNIPPET_SRC — skipping"
fi

if [[ -f "$AMUX_SNIPPET_SRC" ]]; then
    cp "$AMUX_SNIPPET_SRC" "$CLAUDE_SHELL_DIR/amux-spawn.bash"
    chmod 644 "$CLAUDE_SHELL_DIR/amux-spawn.bash"
    log_info "Installed shell snippet: amux-spawn.bash → $CLAUDE_SHELL_DIR/amux-spawn.bash"
    SNIPPET_INSTALLED=true
else
    log_warn "Shell snippet not found at $AMUX_SNIPPET_SRC — skipping"
fi

# Print the opt-in instructions once (NOT applied automatically).
if [[ "$SNIPPET_INSTALLED" == true ]]; then
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────────────┐"
    echo "  │  Shell integration opt-in — choose ONE:                            │"
    echo "  │                                                                     │"
    echo "  │  Profiles only (model aliases, no amux/Telegram):                  │"
    echo "  │    source $CLAUDE_SHELL_DIR/claude-profiles.bash"
    echo "  │                                                                     │"
    echo "  │  Profiles + amux-spawn (session tracking, Telegram notifications): │"
    echo "  │    source $CLAUDE_SHELL_DIR/amux-spawn.bash"
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

# =============================================================================
# STEP 3c: Ensure tmux options for amux-spawned Claude sessions (epic 10)
# =============================================================================
#
# amux-spawn creates sessions detached (amux exec --no-attach -> tmux
# new-session -d) and attaches afterward, so Claude's TUI initializes with no
# client attached and comes up in inline (non-fullscreen) mode. We tune two
# tmux behaviours that improve that experience:
#
#   - focus-events on            Silences Claude's "tmux focus-events off …"
#                                startup nag and lets Claude track when you
#                                switch away from a pane.
#   - set-titles on +            Forward the OSC title Claude sets (captured by
#     set-titles-string '#{pane_title}'
#                                tmux into the pane title) up to the outer
#                                terminal's tab/window title. Default would be
#                                tmux's verbose "#S:#I:#W - …"; we surface just
#                                Claude's title.
#
# Applied two ways: (1) persistent lines in ~/.tmux.conf (for future tmux
# servers); (2) a live `tmux set -g` on any already-running server, since a
# running server reads ~/.tmux.conf only at start — without this the file edit
# would not reach sessions on the current server until it restarts. Idempotent
# (exact-line / current-value checks) and reversible (lines are marked; the
# file is backed up before any edit).

log_step "Step 3c/5: Ensuring tmux options (focus-events, tab title)"

TMUX_CONF="$HOME/.tmux.conf"
TMUX_MARKER="# Added by claude-hooks install-claude-config.sh (tmux options for amux-spawned Claude sessions)"
# Persistent ~/.tmux.conf lines we manage (exact strings → idempotent match).
TMUX_LINES=(
    "set -g focus-events on"
    "set -g set-titles on"
    "set -g set-titles-string '#{pane_title}'"
)
TMUX_FILE_STATUS="unchanged"            # ~/.tmux.conf outcome
TMUX_LIVE_STATUS="no running server"    # live running-server outcome

# 1) Persist any missing lines. Exact whole-line match (grep -Fxq) keeps this
#    idempotent and avoids re-adding lines a previous run (or the user) wrote.
TMUX_MISSING=()
for line in "${TMUX_LINES[@]}"; do
    if [[ -f "$TMUX_CONF" ]] && grep -Fxq "$line" "$TMUX_CONF"; then
        continue
    fi
    TMUX_MISSING+=("$line")
done

if [[ ${#TMUX_MISSING[@]} -eq 0 ]]; then
    log_info "tmux options already present in $TMUX_CONF — leaving as is"
    TMUX_FILE_STATUS="already present"
else
    # Back up an existing config before appending (consistent with the settings
    # backup in Step 4). A brand-new file needs no backup.
    if [[ -f "$TMUX_CONF" ]]; then
        mkdir -p "$BACKUP_DIR"
        TMUX_BACKUP="$BACKUP_DIR/tmux.conf.$(date +%Y%m%d_%H%M%S).bak"
        cp "$TMUX_CONF" "$TMUX_BACKUP"
        log_info "Backup created: $TMUX_BACKUP"
    fi
    {
        # Separate from prior content only when appending to a non-empty file.
        [[ -s "$TMUX_CONF" ]] && echo ""
        echo "$TMUX_MARKER"
        for line in "${TMUX_MISSING[@]}"; do echo "$line"; done
    } >> "$TMUX_CONF"
    log_info "Added ${#TMUX_MISSING[@]} tmux option line(s) to $TMUX_CONF"
    TMUX_FILE_STATUS="updated (${#TMUX_MISSING[@]} line(s) added)"
fi

# 2) Apply to any already-running tmux server too, so newly-spawned sessions
#    pick the options up without a restart (existing Claude sessions re-check
#    only on relaunch). No-op when tmux isn't installed or no server is running.
#    Mirrors amux's own "server running?" probe (list-sessions).
if command -v tmux >/dev/null 2>&1 && tmux list-sessions >/dev/null 2>&1; then
    declare -A TMUX_WANT=(
        [focus-events]="on"
        [set-titles]="on"
        [set-titles-string]="#{pane_title}"
    )
    tmux_set=0 tmux_already=0 tmux_failed=0
    for opt in focus-events set-titles set-titles-string; do
        cur="$(tmux show -gv "$opt" 2>/dev/null || true)"
        if [[ "$cur" == "${TMUX_WANT[$opt]}" ]]; then
            tmux_already=$((tmux_already + 1))
        elif tmux set -g "$opt" "${TMUX_WANT[$opt]}" 2>/dev/null; then
            tmux_set=$((tmux_set + 1))
        else
            log_warn "Could not set tmux option '$opt' on the running server"
            tmux_failed=$((tmux_failed + 1))
        fi
    done
    log_info "Running tmux server: set $tmux_set, already-correct $tmux_already, failed $tmux_failed"
    TMUX_LIVE_STATUS="set $tmux_set, already $tmux_already, failed $tmux_failed"
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
                    # Both Bash and Monitor execute a shell command via
                    # tool_input.command; the pretool hook validates either.
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

# Register context-usage MCP server in ~/.claude.json (not settings.json)
# Claude Code reads MCP servers from ~/.claude.json (user-scoped) or .mcp.json (project-scoped).
CONTEXT_MCP_INSTALLED=false
CONTEXT_MCP_SCRIPT="$SCRIPT_DIR/context-mcp/server.py"
CLAUDE_JSON="$HOME/.claude.json"
if [[ "$UV_AVAILABLE" == true && -f "$CONTEXT_MCP_SCRIPT" ]]; then
    if [[ ! -f "$CLAUDE_JSON" ]]; then
        echo '{}' > "$CLAUDE_JSON"
    fi
    if jq empty "$CLAUDE_JSON" 2>/dev/null; then
        jq --arg script "$CONTEXT_MCP_SCRIPT" \
            '.mcpServers = (.mcpServers // {}) + {"context-usage": {type: "stdio", command: "uv", args: ["run", "--script", $script], env: {}}}' \
            "$CLAUDE_JSON" > "$CLAUDE_JSON.tmp"
        if jq empty "$CLAUDE_JSON.tmp" 2>/dev/null; then
            mv "$CLAUDE_JSON.tmp" "$CLAUDE_JSON"
            log_info "MCP server registered in ~/.claude.json: context-usage (uv run --script $CONTEXT_MCP_SCRIPT)"
            CONTEXT_MCP_INSTALLED=true
        else
            log_warn "Failed to produce valid JSON for ~/.claude.json — MCP server not registered"
            rm -f "$CLAUDE_JSON.tmp"
        fi
    else
        log_warn "~/.claude.json is not valid JSON — skipping MCP server registration"
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

# Show slash-command status
if [[ "$COMMANDS_INSTALLED" == true ]]; then
    echo "  - slash commands: installed ($GLOBAL_COMMANDS_DIR/) — /yolo, /yolo-off"
fi

# Show amux-spawn status
if [[ "$AMUX_SPAWN_INSTALLED" == true ]]; then
    echo "  - amux-spawn: installed ($USER_BIN_DIR/amux-spawn)"
else
    echo "  - amux-spawn: not installed"
fi

# Show completion + snippet status
if [[ "$COMPLETION_INSTALLED" == true ]]; then
    echo "  - amux-spawn completion: installed ($USER_COMPLETIONS_DIR/amux-spawn)"
else
    echo "  - amux-spawn completion: not installed"
fi
if [[ "$SNIPPET_INSTALLED" == true ]]; then
    echo "  - shell snippets: installed ($CLAUDE_SHELL_DIR/)"
    echo "    OPT-IN (choose one):"
    echo "      source $CLAUDE_SHELL_DIR/claude-profiles.bash  # profiles only"
    echo "      source $CLAUDE_SHELL_DIR/amux-spawn.bash       # profiles + amux"
else
    echo "  - shell snippets: not installed"
fi

# Show model profiles status
if [[ "$PROFILES_INSTALLED" == true ]]; then
    echo "  - profiles.toml: installed ($PROFILES_DEST)"
    echo "    Edit with your model tokens, then re-source your chosen shell snippet"
elif [[ -f "$PROFILES_DEST" ]]; then
    echo "  - profiles.toml: already present ($PROFILES_DEST)"
else
    echo "  - profiles.toml: not installed (profiles.example.toml not found)"
fi

# Show context-usage MCP status
if [[ "$CONTEXT_MCP_INSTALLED" == true ]]; then
    echo "  - MCP server (context-usage): installed"
else
    echo "  - MCP server (context-usage): not installed (uv missing or server.py not found)"
fi

# Show tmux options status (file + running server)
echo "  - tmux options (focus-events, tab title): $TMUX_FILE_STATUS ($TMUX_CONF); running server: $TMUX_LIVE_STATUS"

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
