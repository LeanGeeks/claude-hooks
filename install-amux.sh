#!/bin/bash
# Installs the pinned amux fork CLI that this repo depends on.
# - Clones github.com/aDorofeev/amux into the parent directory (sibling of this repo)
# - Checks out the extension branch and reports drift from the pinned commit
# - "Builds" the CLI (amux is a bash script: syntax-gate + runtime dep check)
# - Installs to /usr/local/bin via sudo; if sudo is unavailable it prints the
#   root command and waits for you to run it elsewhere, then continues
# - Verifies the install (byte-identity + fork feature probe + PATH resolution)
#
# CLI ONLY, by design: amux-server.py and amux-remote are NOT installed. They
# carry fork drift unrelated to the spawn chain and nothing in claude-hooks uses
# them (see tasks/12_amux_extensions.md "Deployment"). Only `amux serve` needs
# amux-server.py; if you want it, run ../amux/install.sh instead.
#
# Run this BEFORE ./install-claude-config.sh. The two are only coupled at
# runtime — amux-spawn resolves `amux` from PATH — but installing amux first
# means the claude-hooks run finishes in a working, verified state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

# Defaults (overridable by flag or environment)
AMUX_DIR="${AMUX_DIR:-$PARENT_DIR/amux}"
AMUX_BRANCH="${AMUX_BRANCH:-feat/epic-10-amux-extensions}"
AMUX_PIN="${AMUX_PIN:-9b05d10}"          # commit amux-spawn is validated against
INSTALL_DIR="${AMUX_INSTALL_DIR:-/usr/local/bin}"
AMUX_REPO_SSH="git@github.com:aDorofeev/amux.git"
AMUX_REPO_HTTPS="https://github.com/aDorofeev/amux.git"

FORCE=false
PIN_CHECK=true
WAIT_TIMEOUT=900                         # seconds to wait for a manual root install

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

usage() {
    cat <<EOF
Usage: ./install-amux.sh [options]

Clones/updates the amux fork, then installs its CLI to $INSTALL_DIR.

Options:
  --dir PATH          Clone location (default: $AMUX_DIR)
  --branch NAME       Branch to check out (default: $AMUX_BRANCH)
  --pin SHA           Commit amux-spawn is validated against (default: $AMUX_PIN)
  --no-pin-check      Don't compare HEAD against the pin
  --install-dir PATH  Install target directory (default: $INSTALL_DIR)
  --force             Reinstall even when the installed binary is already current
  -h, --help          Show this help

Environment: AMUX_DIR, AMUX_BRANCH, AMUX_PIN, AMUX_INSTALL_DIR
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)          AMUX_DIR="$2"; shift 2 ;;
        --branch)       AMUX_BRANCH="$2"; shift 2 ;;
        --pin)          AMUX_PIN="$2"; shift 2 ;;
        --no-pin-check) PIN_CHECK=false; shift ;;
        --install-dir)  INSTALL_DIR="$2"; shift 2 ;;
        --force)        FORCE=true; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              log_error "Unknown option: $1"; echo; usage; exit 1 ;;
    esac
done

SRC="$AMUX_DIR/amux"                     # the fork's CLI script (committed, not built)
TARGET="$INSTALL_DIR/amux"
STAGE="$INSTALL_DIR/.amux.new"           # staged copy, renamed into place atomically
BACKUP="$INSTALL_DIR/amux.pre-epic10.bak"

# Fork markers: the fork does NOT bump CC_VERSION (still 0.3.0), so `amux
# --version` cannot tell fork from upstream. These strings are the E1-E5
# extensions; all are absent from upstream 0.3.0.
FORK_MARKERS=(
    "--no-default-model"   # E2 — suppress the injected `--model sonnet`
    "--no-attach"          # E5 — detached create
    "switch-client"        # E3 — nested-tmux attach
    "claude_session_id"    # E4 — session id in <name>.meta.json, not CC_FLAGS
    "update-environment"   # E1 — env propagation allowlist
)

if ! command -v git &> /dev/null; then
    log_error "git is required but not installed. Install with: sudo apt install git"
    exit 1
fi

echo ""
log_step "amux fork installer — $AMUX_BRANCH → $TARGET"
echo ""

# =============================================================================
# STEP 1: Clone or update the fork in the parent directory
# =============================================================================

log_step "Step 1/5: Fetching the fork clone at $AMUX_DIR"

CLONE_ACTION="updated"
if [[ -d "$AMUX_DIR/.git" ]]; then
    REMOTE_URL="$(git -C "$AMUX_DIR" remote get-url origin 2>/dev/null || echo "")"
    if [[ "$REMOTE_URL" != *"aDorofeev/amux"* ]]; then
        log_warn "origin is '$REMOTE_URL', not the aDorofeev/amux fork"
        log_warn "  Branch '$AMUX_BRANCH' may not exist there — continuing anyway"
    fi
    log_info "Existing clone found — fetching origin"
    if ! git -C "$AMUX_DIR" fetch origin --quiet; then
        log_warn "git fetch failed (offline?) — continuing with the local clone"
    fi
elif [[ -e "$AMUX_DIR" ]]; then
    log_error "$AMUX_DIR exists but is not a git clone. Move it aside or pass --dir."
    exit 1
else
    log_info "Cloning $AMUX_REPO_SSH → $AMUX_DIR"
    if git clone --quiet "$AMUX_REPO_SSH" "$AMUX_DIR" 2>/dev/null; then
        CLONE_ACTION="cloned (ssh)"
    else
        log_warn "SSH clone failed (no key for GitHub?) — retrying over HTTPS"
        if git clone --quiet "$AMUX_REPO_HTTPS" "$AMUX_DIR"; then
            CLONE_ACTION="cloned (https)"
        else
            log_error "Could not clone the fork from either $AMUX_REPO_SSH or $AMUX_REPO_HTTPS"
            exit 1
        fi
    fi
fi
log_info "Clone: $CLONE_ACTION ($AMUX_DIR)"

# =============================================================================
# STEP 2: Switch to the extension branch, check the pin
# =============================================================================

echo ""
log_step "Step 2/5: Checking out $AMUX_BRANCH"

# A dirty tree means the `amux` we are about to install may not be the pinned
# code. Refuse rather than silently deploying local edits.
DIRTY="$(git -C "$AMUX_DIR" status --porcelain 2>/dev/null || true)"
if [[ -n "$DIRTY" ]]; then
    if [[ "$FORCE" == true ]]; then
        log_warn "Clone has uncommitted changes — installing them anyway (--force)"
    else
        log_error "Clone has uncommitted changes:"
        while IFS= read -r line; do echo "         $line"; done <<< "$DIRTY"
        log_error "Commit/stash them, or pass --force to install the working tree as is."
        exit 1
    fi
fi

CURRENT_BRANCH="$(git -C "$AMUX_DIR" branch --show-current 2>/dev/null || echo "")"
if [[ "$CURRENT_BRANCH" == "$AMUX_BRANCH" ]]; then
    log_info "Already on $AMUX_BRANCH"
elif git -C "$AMUX_DIR" show-ref --verify --quiet "refs/heads/$AMUX_BRANCH"; then
    git -C "$AMUX_DIR" checkout --quiet "$AMUX_BRANCH"
    log_info "Switched to local branch $AMUX_BRANCH"
elif git -C "$AMUX_DIR" show-ref --verify --quiet "refs/remotes/origin/$AMUX_BRANCH"; then
    git -C "$AMUX_DIR" checkout --quiet -b "$AMUX_BRANCH" --track "origin/$AMUX_BRANCH"
    log_info "Created tracking branch $AMUX_BRANCH from origin/$AMUX_BRANCH"
else
    log_error "Branch '$AMUX_BRANCH' not found locally or on origin."
    log_error "  The E1-E5 extensions live on an unmerged branch; the fork's main does NOT have them."
    log_error "  If the branch exists only on another machine, push it from there first:"
    log_error "      git -C <that-clone> push -u origin $AMUX_BRANCH"
    log_error "  Then re-run this script."
    exit 1
fi

# Fast-forward only: never rewrite local work, and a diverged branch is a
# condition the pin check below should report rather than paper over.
if git -C "$AMUX_DIR" show-ref --verify --quiet "refs/remotes/origin/$AMUX_BRANCH"; then
    if ! git -C "$AMUX_DIR" merge --ff-only --quiet "origin/$AMUX_BRANCH" 2>/dev/null; then
        log_warn "Could not fast-forward to origin/$AMUX_BRANCH (diverged or no upstream) — using local HEAD"
    fi
else
    # This clone has the branch but the fork does not: the extension commits were
    # never pushed, so no other machine can install them from GitHub.
    log_warn "origin/$AMUX_BRANCH does not exist — this branch is local-only, never pushed"
    log_warn "  Other machines cannot install the fork until you publish it:"
    log_warn "      git -C $AMUX_DIR push -u origin $AMUX_BRANCH"
fi

HEAD_SHORT="$(git -C "$AMUX_DIR" rev-parse --short HEAD)"
HEAD_SUBJECT="$(git -C "$AMUX_DIR" log -1 --pretty=%s)"
log_info "HEAD: $HEAD_SHORT — $HEAD_SUBJECT"

PIN_STATUS="not checked"
if [[ "$PIN_CHECK" == true ]]; then
    if git -C "$AMUX_DIR" rev-parse --verify --quiet "${AMUX_PIN}^{commit}" >/dev/null 2>&1; then
        PIN_FULL="$(git -C "$AMUX_DIR" rev-parse "${AMUX_PIN}^{commit}")"
        HEAD_FULL="$(git -C "$AMUX_DIR" rev-parse HEAD)"
        if [[ "$PIN_FULL" == "$HEAD_FULL" ]]; then
            PIN_STATUS="at pin $AMUX_PIN"
            log_info "At the pinned commit $AMUX_PIN"
        elif git -C "$AMUX_DIR" merge-base --is-ancestor "$PIN_FULL" HEAD; then
            AHEAD="$(git -C "$AMUX_DIR" rev-list --count "$PIN_FULL"..HEAD)"
            PIN_STATUS="$AHEAD commit(s) ahead of pin $AMUX_PIN"
            log_warn "HEAD is $AHEAD commit(s) ahead of the pin $AMUX_PIN"
            log_warn "  amux-spawn was validated against the pin; re-run the amux tests if spawns misbehave."
        else
            PIN_STATUS="does NOT contain pin $AMUX_PIN"
            log_warn "HEAD does not contain the pinned commit $AMUX_PIN — the branch was rewritten?"
        fi
    else
        PIN_STATUS="pin $AMUX_PIN not found"
        log_warn "Pinned commit $AMUX_PIN not found in this clone"
    fi
fi

# =============================================================================
# STEP 3: Build
# =============================================================================

echo ""
log_step "Step 3/5: Building"

# amux is a single committed bash script — there is no compile step for the CLI.
# (build-desktop.sh builds the macOS Swift app; not part of the spawn chain.)
# The equivalent gate is a syntax check plus amux's own runtime dependencies.
if [[ ! -f "$SRC" ]]; then
    log_error "$SRC not found — is $AMUX_DIR really the amux fork?"
    exit 1
fi

if bash -n "$SRC" 2>/dev/null; then
    log_info "No compile step (amux is a bash script); syntax check passed"
else
    log_error "Syntax check failed for $SRC — refusing to install:"
    bash -n "$SRC" || true
    exit 1
fi

MISSING_DEPS=()
for dep in tmux python3; do
    command -v "$dep" &> /dev/null || MISSING_DEPS+=("$dep")
done
if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    log_error "amux needs: ${MISSING_DEPS[*]} — install with: sudo apt install ${MISSING_DEPS[*]}"
    exit 1
fi
log_info "Runtime deps present: tmux $(tmux -V | awk '{print $2}'), $(python3 --version)"

# =============================================================================
# STEP 4: Install (sudo, or hand the command to the user and wait)
# =============================================================================

echo ""
log_step "Step 4/5: Installing to $TARGET"

installed_is_current() { cmp -s "$SRC" "$TARGET" 2>/dev/null; }

# Back up whatever is there now, but only once and only if it is NOT already
# the fork build — otherwise a second run would overwrite the genuine
# pre-epic10 original with a copy of the fork.
NEED_BACKUP=false
if [[ ! -e "$BACKUP" && -f "$TARGET" ]] && ! installed_is_current; then
    NEED_BACKUP=true
fi

# The privileged work is described once as a list of ops, and both the runner
# and the printed "run this as root" block derive their form from it — so the
# command we execute and the command we tell you to run can never drift apart.
op_argv() {
    case "$1" in
        mkdir)   OP_ARGV=(mkdir -p "$INSTALL_DIR") ;;
        backup)  OP_ARGV=(cp -n "$TARGET" "$BACKUP") ;;
        # Stage + rename: bash reads scripts incrementally, so overwriting the
        # target in place can corrupt an amux invocation that is running right
        # now. The rename swaps the inode; in-flight processes keep the old one.
        install) OP_ARGV=(install -m0755 "$SRC" "$STAGE") ;;
        mv)      OP_ARGV=(mv -f "$STAGE" "$TARGET") ;;
        *)       log_error "internal: unknown op '$1'"; exit 1 ;;
    esac
}

INSTALL_STATUS="unchanged"
if installed_is_current && [[ "$FORCE" != true ]]; then
    log_info "$TARGET is already byte-identical to the fork build — nothing to install"
    log_info "  (pass --force to reinstall anyway)"
    INSTALL_STATUS="already current"
else
    OPS=()
    if [[ ! -d "$INSTALL_DIR" ]]; then OPS+=("mkdir"); fi
    if [[ "$NEED_BACKUP" == true ]]; then OPS+=("backup"); fi
    OPS+=("install" "mv")

    # Writability is decided by the nearest existing ancestor, so a not-yet-created
    # user prefix (e.g. ~/.local/bin) is made by us rather than root-owned by sudo.
    WRITE_PROBE="$INSTALL_DIR"
    while [[ ! -e "$WRITE_PROBE" && "$WRITE_PROBE" != "/" && "$WRITE_PROBE" != "." ]]; do
        WRITE_PROBE="$(dirname "$WRITE_PROBE")"
    done

    SUDO_PREFIX=()
    ELEVATION=""
    if [[ "$(id -u)" -eq 0 ]]; then
        ELEVATION="root"
        log_info "Running as root — installing directly"
    elif [[ -w "$WRITE_PROBE" ]]; then
        ELEVATION="writable"
        log_info "$WRITE_PROBE is writable — installing without sudo"
    elif ! command -v sudo &> /dev/null; then
        log_warn "sudo not found on PATH"
    elif sudo -n true 2>/dev/null; then
        ELEVATION="sudo"
        SUDO_PREFIX=(sudo)
        log_info "Passwordless sudo available — installing with sudo"
    elif [[ -t 0 ]]; then
        log_step "sudo needs your password (Ctrl-C to fall back to a manual root install)"
        if sudo -v; then
            ELEVATION="sudo"
            SUDO_PREFIX=(sudo)
            log_info "sudo authenticated — installing with sudo"
        else
            log_warn "sudo authentication failed"
        fi
    else
        log_warn "No TTY to prompt for a sudo password"
    fi

    if [[ -n "$ELEVATION" ]]; then
        for op in "${OPS[@]}"; do
            op_argv "$op"
            # ${arr[@]+...} guard: expanding an empty array under `set -u`
            # errors on bash 3.2 (macOS), where SUDO_PREFIX is empty for root.
            if ! ${SUDO_PREFIX[@]+"${SUDO_PREFIX[@]}"} "${OP_ARGV[@]}"; then
                log_error "Install step failed: ${OP_ARGV[*]}"
                exit 1
            fi
        done
        INSTALL_STATUS="installed via ${ELEVATION}"
        log_info "Installed: $SRC → $TARGET"
        if [[ "$NEED_BACKUP" == true ]]; then
            log_info "Backed up the previous binary → $BACKUP"
        fi
    else
        # ---- Manual path: print the command, then wait for it to happen -----
        echo ""
        log_warn "Could not elevate automatically. Run this as root, in another terminal:"
        echo ""
        ONE_LINER=""
        echo "  ┌────────────────────────────────────────────────────────────────────"
        for op in "${OPS[@]}"; do
            op_argv "$op"
            # %q quotes for the shell, so paths with spaces survive copy-paste.
            QUOTED="$(printf '%q ' "${OP_ARGV[@]}")"
            QUOTED="${QUOTED% }"
            echo "  │  $QUOTED"
            if [[ -n "$ONE_LINER" ]]; then
                ONE_LINER="$ONE_LINER && $QUOTED"
            else
                ONE_LINER="$QUOTED"
            fi
        done
        echo "  └────────────────────────────────────────────────────────────────────"
        echo ""
        echo "  As one line:"
        # Wrap in single quotes (escaping any embedded ones) so the whole
        # sequence reaches bash -c as a single, readable argument.
        printf "    sudo bash -c '%s'\n" "${ONE_LINER//\'/\'\\\'\'}"
        echo ""

        if [[ ! -t 0 ]]; then
            log_error "No TTY to wait on — re-run ./install-amux.sh after running the command above."
            exit 1
        fi

        log_step "Waiting for $TARGET to appear (polls every 2s, or press Enter to re-check)..."
        WAITED=0
        while ! installed_is_current; do
            if [[ "$WAITED" -ge "$WAIT_TIMEOUT" ]]; then
                echo ""
                log_error "Timed out after ${WAIT_TIMEOUT}s waiting for the manual install."
                log_error "Run the command above, then re-run ./install-amux.sh."
                exit 1
            fi
            # -t 2 doubles as the poll interval and an Enter-to-recheck prompt.
            read -r -t 2 _ || true
            WAITED=$((WAITED + 2))
        done
        echo ""
        INSTALL_STATUS="installed manually as root"
        log_info "Detected the new binary at $TARGET"
    fi
fi

# =============================================================================
# STEP 5: Verify
# =============================================================================

echo ""
log_step "Step 5/5: Verifying"

VERIFY_FAILED=false

if [[ ! -x "$TARGET" ]]; then
    log_error "$TARGET is missing or not executable"
    VERIFY_FAILED=true
else
    if installed_is_current; then
        log_info "Byte-identical to the fork build at $HEAD_SHORT"
    else
        log_error "$TARGET differs from $SRC — the install did not take"
        VERIFY_FAILED=true
    fi

    VERSION_OUT="$("$TARGET" --version 2>&1 | head -1 || true)"
    if [[ -n "$VERSION_OUT" ]]; then
        log_info "Runs: $VERSION_OUT (the fork does not bump this — see the feature probe)"
    else
        log_warn "$TARGET --version produced no output"
    fi

    # The real fork/upstream discriminator.
    MISSING_MARKERS=()
    for marker in "${FORK_MARKERS[@]}"; do
        grep -q -- "$marker" "$TARGET" || MISSING_MARKERS+=("$marker")
    done
    if [[ ${#MISSING_MARKERS[@]} -eq 0 ]]; then
        log_info "Fork feature probe: all ${#FORK_MARKERS[@]} extension markers present (E1-E5)"
    else
        log_error "Fork feature probe FAILED — missing: ${MISSING_MARKERS[*]}"
        log_error "  The installed binary looks like upstream amux, not the fork."
        VERIFY_FAILED=true
    fi
fi

# PATH resolution: an amux earlier on PATH would shadow what we just installed.
RESOLVED="$(command -v amux 2>/dev/null || echo "")"
if [[ -z "$RESOLVED" ]]; then
    log_warn "amux is not on PATH — add $INSTALL_DIR to PATH (amux-spawn resolves 'amux' from PATH)"
    VERIFY_FAILED=true
elif [[ "$RESOLVED" != "$TARGET" ]]; then
    # A different path only matters if that copy is not the fork build — that is
    # the one amux-spawn would actually run.
    if cmp -s "$SRC" "$RESOLVED"; then
        log_info "PATH resolves amux → $RESOLVED (different path, same fork build — OK)"
    else
        log_error "PATH resolves amux to $RESOLVED, not $TARGET"
        log_error "  That copy shadows this install and is NOT the fork build — amux-spawn would run it."
        VERIFY_FAILED=true
    fi
else
    log_info "PATH resolves amux → $TARGET"
fi

if [[ ! -f "$INSTALL_DIR/amux-server.py" ]]; then
    log_info "amux-server.py not installed (by design) — only 'amux serve' needs it"
fi

# =============================================================================
# Summary
# =============================================================================

echo ""
if [[ "$VERIFY_FAILED" == true ]]; then
    log_error "amux install NOT verified:"
else
    log_info "amux fork installed and verified:"
fi
echo "  - clone:    $AMUX_DIR ($CLONE_ACTION)"
echo "  - branch:   $AMUX_BRANCH @ $HEAD_SHORT"
echo "  - pin:      $PIN_STATUS"
echo "  - binary:   $TARGET ($INSTALL_STATUS)"
if [[ -e "$BACKUP" ]]; then
    echo "  - rollback: sudo install -m0755 $BACKUP $TARGET"
fi
echo ""

if [[ "$VERIFY_FAILED" == true ]]; then
    exit 1
fi

echo "Next step:"
echo "  ./install-claude-config.sh    # hooks, amux-spawn launcher, tmux options"
echo ""
