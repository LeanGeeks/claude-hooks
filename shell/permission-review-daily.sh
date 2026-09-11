#!/usr/bin/env bash
#
# permission-review-daily.sh — cron launcher for the daily permission reviewer
# (epic 22, task 22-05 §3).
#
# Starts one amux-spawn session in the claude-hooks checkout, seeded with
# docs/prompts/permission-review-daily.md. amux-spawn gives the run a tmux home,
# a Telegram notification when it goes idle (which is how the summary reaches a
# human), and `amux peek` for post-hoc inspection.
#
# Install (HUMAN STEP — the repo does not touch your crontab):
#
#     crontab -e
#     15 6 * * * /data/sync/work/leangeeks-ai/claude-hooks/shell/permission-review-daily.sh
#
# See docs/permission-review-daily.md for the full operator notes.
#
# Three things this script exists to guarantee:
#
#   1. **Model and effort are pinned.** state.md invariant 9 / brd H8: an
#      automated spawn that pins neither reads the harness default, which floats
#      with whatever the operator last typed into /model. amux-spawn warns about
#      exactly this on the non-TTY path (epic 21) — a warning this launcher must
#      never trigger. The flags below are always passed, never conditionally.
#   2. **Cron's environment is nearly empty.** No profile is sourced, PATH is
#      typically `/usr/bin:/bin`, and HOME may be unset. Everything the run needs
#      is set here explicitly.
#   3. **A silent 06:15 failure is diagnosable.** Both streams go to a dated file
#      under the repo's temp/ (gitignored), including the amux-spawn warnings the
#      pin above is meant to suppress.

set -uo pipefail

# ── Environment (cron gives us almost none of this) ───────────────────────────
# HOME first: everything below derives from it, and cron may not export it.
if [[ -z "${HOME:-}" ]]; then
    HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
    export HOME
fi

# amux-spawn and uv live in ~/.local/bin; amux is installed to /usr/local/bin;
# claude is a native binary in ~/.local/bin; tmux, git, jq, python3 are in
# /usr/bin. ~/.bin carries this machine's personal tools.
export PATH="$HOME/.local/bin:$HOME/.bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin"

# ── Locate the repo ───────────────────────────────────────────────────────────
# The script is repo-bound (it reads a prompt out of docs/prompts/), so it
# resolves the checkout from its own location. CLAUDE_HOOKS_REPO overrides for a
# non-standard layout; the installer does not copy this file to PATH, which is
# why the crontab line names the repo path directly (task 22-05 §3).
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="${CLAUDE_HOOKS_REPO:-$(dirname "$(dirname "$SCRIPT_PATH")")}"

PROMPT_FILE="$REPO_DIR/docs/prompts/permission-review-daily.md"
LOG_DIR="$REPO_DIR/temp/permission-review"
LOG_FILE="$LOG_DIR/$(date -u +%Y-%m-%d).log"

# Pinned knobs. Overridable for a manual run, but never absent: the defaults are
# literals, so the spawn is pinned even with an empty environment.
#
# opus, not sonnet: this job judges allowlist proposals, edits settings.json,
# commits, and re-runs the installer unattended. The blast radius of a bad
# judgement here is a widened permission gate on every session on the machine.
REVIEW_MODEL="${PERMISSION_REVIEW_MODEL:-opus}"
REVIEW_EFFORT="${PERMISSION_REVIEW_EFFORT:-high}"

mkdir -p "$LOG_DIR" || {
    echo "permission-review-daily: cannot create $LOG_DIR" >&2
    exit 1
}

# Everything from here on is captured. Appended, not truncated: a manual run on
# the same day must not erase the scheduled one.
exec >>"$LOG_FILE" 2>&1

echo "=== permission-review-daily $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "repo:   $REPO_DIR"
echo "model:  $REVIEW_MODEL"
echo "effort: $REVIEW_EFFORT"
echo "PATH:   $PATH"

if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "FATAL: prompt not found at $PROMPT_FILE"
    exit 1
fi

if ! command -v amux-spawn >/dev/null 2>&1; then
    echo "FATAL: amux-spawn not on PATH ($PATH)"
    echo "       run ./install-claude-config.sh to place it in ~/.local/bin"
    exit 1
fi

# Dirty-tree check: if install.sh has uncommitted changes, the installer must
# not be run unattended — it would execute whatever half-finished edit is in the
# working tree against the real $HOME. Withhold propagation but proceed with the
# review (draining the queue and reviewing traffic are still worth doing).
_dirty="$(git -C "$REPO_DIR" status --porcelain -- install.sh 2>/dev/null || true)"
if [[ -n "$_dirty" ]]; then
    export PERMISSION_REVIEW_NO_PROPAGATE=1
    echo "WARN: install.sh has uncommitted changes — propagation withheld"
    echo "      The review will run, but step 3 (installer merge) will be skipped."
    echo "      Commit or stash the install.sh changes to re-enable propagation."
fi
unset _dirty

PROMPT="$(cat "$PROMPT_FILE")"

# --dir is mandatory in practice: cron's cwd is $HOME, and amux-spawn keys its
# workspace (and therefore the queue this reviewer drains) off the directory.
# --detach is explicit rather than implied by the non-TTY path, so the behaviour
# does not change if this is ever run by hand from a terminal.
# The prompt is one argv element after `--`.
amux-spawn spawn permission-review \
    --dir "$REPO_DIR" \
    --detach \
    --model="$REVIEW_MODEL" \
    --effort="$REVIEW_EFFORT" \
    -- "$PROMPT"
status=$?

echo "=== amux-spawn exit $status at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
exit "$status"
