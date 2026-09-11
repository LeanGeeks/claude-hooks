# claude-hooks installer reference

`install.sh` is the interactive installer for claude-hooks. It selects which
features to install, merges permissions and hook wiring into the global
`~/.claude/settings.json`, and records every choice in a manifest so future runs
can replay it.

---

## Table of contents

1. [Feature inventory](#feature-inventory)
2. [Sub-toggles](#sub-toggles)
3. [The tri-state model](#the-tri-state-model)
4. [The manifest](#the-manifest)
5. [CLI reference](#cli-reference)
6. [enable / disable subcommands](#enable--disable-subcommands)
7. [Uninstall](#uninstall)
8. [CLAUDE_INSTALL_NO_EXTERNAL seam](#claude_install_no_external-seam)

---

## Feature inventory

Ten features, in registry (execution) order. The registry order matters:
`profiles` precedes `amux` because `amux-spawn.bash` sources
`claude-profiles.bash`.

| id | Feature | Default | Writes outside `~/.claude` | Requires |
|----|---------|---------|----------------------------|----------|
| `statusline` | Statusline (+ per-subagent rows) | install | — | — |
| `permission-hooks` | Permission hooks (PreToolUse, PermissionRequest, PostToolUse) | install | — | — |
| `telegram` | Telegram integration (approvals, notifications, reply-injection) | install | user-site `.pth`, `~/.local/bin/claude-roles` | `permission-hooks` |
| `profiles` | Model profiles (`~/.claude/profiles.toml`) | install | — | — |
| `amux` | amux integration (launcher, tmux options, bash completion) | install | `/usr/local/bin/amux`, `~/.tmux.conf` + live tmux server, `~/.local/bin/amux-spawn`, bash-completion dir | `profiles` |
| `permissions-allowlist` | Global permissions allowlist (union or overwrite mode) | install | — | — |
| `context-mcp` | MCP: context-usage (registers `uv --script` MCP server) | install | `~/.claude.json` | `uv` on PATH |
| `claude-history` | `claude-history` diagnostic tool | install | `~/.local/bin/claude-history` | — |
| `questions` | Async questions (queue + listener + MCP server) | **skip** | `~/.local/bin/{claude-questions,questions-listen}`, systemd unit file | `telegram` |
| `daily-review` | Daily permission review (cron launcher) | **skip** | — | `amux` |

`questions` and `daily-review` default to **skip** on a fresh machine (brd D14)
because they schedule ongoing autonomous activity. Every other feature defaults
to install.

### What each feature writes

**`statusline`**
- `~/.claude/statusline/statusline.py` — status-line script
- `~/.claude/settings.json` — `statusLine` and `subagentStatusLine` keys

**`permission-hooks`**
- Hook modules copied to `~/.claude/hooks/`
- `~/.claude/settings.json` — `PreToolUse(Bash)`, `PermissionRequest(*)`,
  `PostToolUse(*)`, `UserPromptSubmit(*)`, `Stop(*)`, `SubagentStop(*)`
  hook entries

**`telegram`**
- Hook modules to `~/.claude/hooks/`
- `~/.claude/settings.json` — `Notification(idle_prompt)` hook entry
- `~/.claude/settings.json` — `Notification(permission_prompt)` hook entry
- Python user-site `.pth` file (relay-server package import path)
- `~/.local/bin/claude-roles` symlink

**`profiles`**
- `~/.claude/profiles.toml` — created from example if absent, never overwritten
- `~/.claude/shell/claude-profiles.bash` — shell snippet
- Sub-toggle `profiles-autosource`: writes one sourcing line to `~/.bashrc`

**`amux`**
- Runs `install-amux.sh` to install the pinned forked amux binary at
  `/usr/local/bin/amux` (elevation delegated to `install-amux.sh`)
- `~/.claude/shell/amux-spawn.bash`, `~/.claude/shell/amux-spawn-completion.bash`
- `~/.local/bin/amux-spawn` and bash-completion symlinks
- `~/.tmux.conf` — marked tmux options block (focus-events, set-titles)
- Live `tmux set -g` commands applied to any running tmux server
- Sub-toggle `amux-autowrap`: writes one sourcing line to `~/.bashrc`

**`permissions-allowlist`**
- `~/.claude/settings.json` — `permissions.allow` and `permissions.deny` keys
  (union-mode: set-union with any existing entries, deduplicated;
   or overwrite-mode: full replacement — chosen at install time)

**`context-mcp`**
- `~/.claude.json` — registers the `context-usage` MCP server
  (backed up before write, restored on failure)

**`claude-history`**
- `~/.claude/shell/claude-history` script
- `~/.local/bin/claude-history` symlink

**`questions`**
- Hook modules to `~/.claude/hooks/`
- `~/.claude.json` — registers the `questions` MCP server
- `~/.claude/shell/claude-questions` diagnostic tool
- `~/.local/bin/claude-questions` symlink
- `~/.claude/bin/questions-listen` binary
- `~/.local/bin/questions-listen` symlink
- `~/.config/systemd/user/claude-questions-listen.service` unit file
  (installed but not enabled — enabling is the `questions-listen` sub-toggle)

**`daily-review`**
- No files installed — the only artifact is the `daily-review-cron` sub-toggle

---

## Sub-toggles

Four nested opt-ins. Each is off by default and must be explicitly enabled after
the parent feature is installed.

| id | Parent | Default | What it writes |
|----|--------|---------|----------------|
| `profiles-autosource` | `profiles` | off | `~/.bashrc` — one marked sourcing line |
| `amux-autowrap` | `amux` | off | `~/.bashrc` — one marked sourcing line |
| `questions-listen` | `questions` | off | `systemctl --user enable claude-questions-listen`, `loginctl enable-linger` |
| `daily-review-cron` | `daily-review` | off | crontab — one marked `15 6 * * *` line |

`profiles-autosource` and `amux-autowrap` are mutually exclusive: enabling one
automatically disables the other if it was active.

Enable or disable a sub-toggle with the `enable`/`disable` subcommands (see
[below](#enable--disable-subcommands)).

---

## The tri-state model

For a feature that is **not yet installed** the installer offers:

- **Install** — install the feature on this machine
- **Skip** — do not install; recorded as a decision, not an oversight

For a feature that is **already installed** the installer offers:

- **Update** (default) — re-run the install function; refreshes all artifacts
  and hook modules, applies any new wiring
- **Keep** — do not change what this machine runs. Shared hook modules are
  still refreshed to the current repo version (constraint 2.4: a state where one
  module is new and another is old silently breaks the shared import graph).
  Wiring, symlinks, and external surfaces are left as-is.
- **Uninstall** — remove the feature (see [Uninstall](#uninstall))

`Keep` is not "freeze the `.py` files". It means "do not change the wiring or
side effects". The modules are always current. What `Keep` prevents is:
a new `settings.json` entry, a new symlink on PATH, or a new tmux block being
added by this run.

---

## The manifest

`~/.claude/install-manifest.json` records intent after every successful run. It
is **not** the source of truth for what is installed — probes read the disk.

The manifest carries:
- Schema version, repo path, git revision, timestamp
- Per feature: `state` (`installed` | `skipped` | `failed`), timestamp, sub-toggle states
- For `permissions-allowlist`: the exact patterns added (so uninstall can subtract them)

`--yes` replays the manifest: a feature recorded as `installed` is updated; one
recorded as `skipped` stays skipped. A feature id not yet in the manifest is
reported as newly-offered and skipped for this run. Use `--with <id>` to add it.

---

## CLI reference

```
install.sh [OPTIONS]
install.sh enable  <toggle-id>
install.sh disable <toggle-id>
```

### Options

| Flag | Description |
|------|-------------|
| `--all` | Install every feature; sub-toggles at their defaults (all off) |
| `--only a,b,c` | Install exactly these features (comma-separated ids) |
| `--with x` | Add feature `x` to the selection (repeatable) |
| `--without y` | Remove feature `y` from the selection |
| `--yes` | Non-interactive; replay the manifest or apply explicit selection |
| `--list` | Print feature table with detected state, then exit 0 |
| `--dry-run` | Render the plan and exit 0 without writing anything |
| `--help` | Show help and exit 0 |

### --yes semantics

`--yes` means "replay the manifest exactly" when a manifest exists and no
explicit selection is given. It does **not** mean "install everything silently".

- With a manifest and no explicit selection: replay. Skipped features stay
  skipped. New features not yet in the manifest are reported but not installed.
- With `--all` or `--only`: apply that selection non-interactively. No manifest
  required.
- With neither a manifest nor an explicit selection: **error**. This is
  intentional — falling back to install-everything is the footgun the flag exists
  to prevent.

Use `--with` / `--without` to adjust a manifest replay without a full interactive
run:

```bash
./install.sh --yes --with questions      # replay + add questions
./install.sh --yes --without telegram    # replay, skip telegram this time
```

### Examples

```bash
./install.sh                              # interactive checklist
./install.sh --all --yes                  # install everything non-interactively
./install.sh --yes                        # replay manifest (requires existing manifest)
./install.sh --only statusline --yes      # install only statusline, non-interactively
./install.sh --yes --with telegram        # replay manifest, also install telegram
./install.sh --list                       # show current detected state
./install.sh --dry-run                    # print plan, touch nothing
./install.sh enable questions-listen      # enable the listener daemon
./install.sh disable amux-autowrap        # disable amux auto-wrap in ~/.bashrc
```

### Refreshing hooks non-interactively (for agents)

After a repo hook edit, re-run the installer to make the change live:

```bash
./install.sh --yes
```

`--yes` is the only safe form for an unattended or agent-driven run. It replays
what this machine already chose — no feature is added or removed. Never use
`--all` from an agent or script: `--all` installs every feature regardless of
what the developer previously chose to skip.

---

## enable / disable subcommands

```bash
./install.sh enable  <toggle-id>
./install.sh disable <toggle-id>
```

Flip one sub-toggle with a narrow blast radius — no full re-install needed. The
parent feature must already be installed.

Valid toggle ids: `amux-autowrap`, `profiles-autosource`, `questions-listen`,
`daily-review-cron`.

```bash
# Enable the questions-listen systemd unit (parent 'questions' must be installed)
./install.sh enable questions-listen

# Disable the daily crontab line (parent 'daily-review' must be installed)
./install.sh disable daily-review-cron
```

The enable/disable subcommands update the manifest so `--yes` replays the new
state on the next run.

---

## Uninstall

There is no `--uninstall-all` command. Uninstall is per-feature, offered as the
third option in the interactive checklist for any already-installed feature, or
via the `--uninstall` flag:

```bash
./install.sh --uninstall telegram
```

**What uninstall does:**
- Removes the feature's owned artifacts (hook wiring entries, symlinks, shell
  scripts, systemd unit file, crontab line, tmux block, settings keys)
- Shared hook modules are refcounted: a module is removed only when no remaining
  installed feature claims it (brd D4). Seven modules have more than one owner.
- Removes the feature's entry from the manifest

**What uninstall deliberately never removes:**
- `~/.claude/profiles.toml` — holds API tokens and the user's profile definitions
- `~/.claude/projects/` — Claude Code's own per-project session storage
- `~/.claude/history.jsonl` — Claude Code's command history
- State stores (`~/.claude/permission_state.json`, etc.) — live session state
- `/usr/local/bin/amux` — the amux binary was installed by `install-amux.sh`
  under elevation; the `amux` feature's uninstall removes the launcher and wiring
  but leaves the binary itself

**Live tmux options are not reverted.** The `amux` feature writes to `~/.tmux.conf`
and applies options to any running tmux server with `tmux set -g`. Uninstalling
removes the marked block from `~/.tmux.conf` so it does not re-apply on next
tmux start, but the currently running server keeps the options active until it
exits or they are manually reverted.

---

## CLAUDE_INSTALL_NO_EXTERNAL seam

`CLAUDE_INSTALL_NO_EXTERNAL=1` is a **testing aid**, not a user-facing dry-run.

When set, every function in the `ext_*` family (the external-surface seam,
architecture §8) logs what it would do and returns success without doing it.
This means:

- No `crontab` edits
- No `systemctl` or `loginctl` calls
- No `tmux set -g` on the running server
- No `ln -s` symlinks outside `~/.claude`

File writes inside `$HOME` still happen. The guard is designed for automated
tests that point `HOME` at a temporary directory.

**Do not use this as a substitute for `--dry-run`** in normal operation.
`--dry-run` renders the full plan and exits before any write — including writes
inside `~/.claude`. `CLAUDE_INSTALL_NO_EXTERNAL=1` runs the install but skips
only the system-level side effects.

The seam is enforced by a source-level grep test in
`tests/test_unit_installer.py` (`TestSourceLevelInvariant`): every invocation of
`crontab`, `systemctl`, `loginctl`, `tmux set`, and `ln -s` in `install.sh` must
be inside an `ext_*` function body. This test will fail if a future contributor
adds a direct call outside the seam.
