# claude-hooks installer reference

`install.sh` is the interactive installer for claude-hooks. It selects which
features to install, merges permissions and hook wiring into the global
`~/.claude/settings.json`, and records every choice in a manifest so future runs
can replay it.

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [Feature inventory](#feature-inventory)
3. [Sub-toggles](#sub-toggles)
4. [The tri-state model](#the-tri-state-model)
5. [The manifest](#the-manifest)
6. [CLI reference](#cli-reference)
7. [enable / disable subcommands](#enable--disable-subcommands)
8. [Uninstall](#uninstall)
9. [CLAUDE_INSTALL_NO_EXTERNAL seam](#claude_install_no_external-seam)

---

## Prerequisites

`_check_dependencies` enforces most of these before any feature runs, and exits
1 with an actionable message if one is missing. `--probe` and `--list` run
before the check, so they still work on an unsupported interpreter.

| Requirement | Why | Enforced |
| --- | --- | --- |
| `jq` | settings.json merging | every run |
| `python3` >= 3.9 | the floor the hook modules are written to (`MIN_PYTHON_MINOR` in install.sh) | every run |
| a TOML parser | stdlib `tomllib` on 3.11+, otherwise the `tomli` backport | runs that install modules |
| `uv` (optional) | only the context-usage and permissions MCP servers | warning only |

The TOML parser is the odd one out: it is needed to *run* the hooks, not to
manage them, so `_check_dependencies` only warns about it and the hard failure
lives in `_compute_and_install_module_closure` — raised once the closure is
known to be non-empty, and still before the first `cp`. Uninstalling is
therefore always possible. That matters because the interpreter is resolved
through `PATH` at hook-fire time (below): it can move *after* a successful
install, and a user whose hooks have started tracebacking must be able to take
them out without first installing a package. Note that merely reaching the
closure is not the test — an uninstall-only run gets there too and finds it
empty.

### Which python3?

Hooks are wired into `settings.json` as bare `python3 <path>`, so Claude Code
resolves the interpreter through `PATH` at hook-fire time. That is the
interpreter the installer checks — **a pyenv, conda, or venv shim on `PATH` is
what counts**, not `/usr/bin/python3`. A stock Ubuntu 22.04 `python3` is 3.10
and needs the backport:

```bash
sudo apt install python3-tomli      # or: python3 -m pip install --user tomli
```

`tomllib` only entered the stdlib in 3.11. Every hook that reads a `.toml`
config (profiles, roles, questions, relay) imports it at module scope, and an
import-time failure takes down the *entire* hook — including the parts that
never touch TOML — surfacing as a raw traceback in the user's session. The
modules fall back to `tomli` on 3.9/3.10, and the gate above makes a missing
parser an install-time error instead of a runtime one.

### The closing import check


After installing, the closing "Testing hook installation..." step imports every
module *this run* copied into `~/.claude/hooks/` — both the ones a feature
install shipped and the ones the module closure refreshed — and reports any that
fail. It deliberately does not glob the directory, which would also pick up
stale or user-authored files the installer does not own. A failure there is
fatal and exits 1: those hooks are already wired into `settings.json` and will
traceback on every fire.

One consequence worth knowing, since the usual workflow is `git pull` followed
by an install run: the closure refreshes *every* module belonging to a feature
that is being installed, updated, or kept, so a narrow command like
`./install.sh --only statusline` can exit 1 over a module owned by a different
feature. That is not a misfire — the closure really did just overwrite that file
from the repo, so the user's hooks really are broken on disk.

---

## Feature inventory

Twelve features, in registry (execution) order. The registry order matters:
`profiles` precedes `amux` because `amux-spawn.bash` sources
`claude-profiles.bash`.

| id | Feature | Default | Writes outside `~/.claude` | Requires |
|----|---------|---------|----------------------------|----------|
| `statusline` | Statusline (+ per-subagent rows) | install | — | — |
| `permission-hooks` | Permission hooks (PreToolUse, PermissionRequest, PostToolUse) | install | — | — |
| `telegram` | Telegram integration (approvals, notifications, reply-injection) | install | user-site `.pth`, `~/.local/bin/claude-roles` | `permission-hooks` |
| `profiles` | Model profiles (`~/.claude/profiles.toml`) | install | — | — |
| `amux` | amux integration (launcher, tmux options, bash completion) | install | `/usr/local/bin/amux`, `~/.tmux.conf` + live tmux server, `~/.local/bin/amux-spawn`, bash-completion dir | `profiles` |
| `keep-awake` | Keep the machine awake while agents work (logind inhibitor per working session) | install | — (takes a logind lock at runtime) | `systemd-inhibit` on PATH (else a no-op) |
| `permissions-allowlist` | Global permissions allowlist (union or overwrite mode) | install | — | — |
| `context-mcp` | MCP: context-usage (registers `uv --script` MCP server) | install | `~/.claude.json` | `uv` on PATH |
| `claude-history` | `claude-history` diagnostic tool | install | `~/.local/bin/claude-history` | — |
| `questions` | Async questions (queue + listener + MCP server) | **skip** | `~/.local/bin/{claude-questions,questions-listen}`, systemd unit file | `telegram` |
| `daily-review` | Daily permission review (cron launcher) | **skip** | — | `amux` |
| `epic-driver` | Epic driver (unattended step-ledger runner) | **skip** | `~/.local/bin/epic-driver`, three systemd unit files (none enabled) | `amux`, `statusline`, `keep-awake`, `questions` |

`questions`, `daily-review` and `epic-driver` default to **skip** on a fresh machine (brd D14)
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

**`keep-awake`**
- `~/.claude/hooks/keep_awake_hook.py`
- `~/.claude/settings.json` — `UserPromptSubmit(*)`, `Stop(*)`, `SessionEnd(*)`,
  `Notification(permission_prompt)` hook entries
- At runtime: `~/.claude/keep-awake/<session>.json` state and
  `~/.claude/keep_awake.log`; see [keep-awake.md](./keep-awake.md)
- Uninstall drops every live lock (`keep_awake_hook.py --release-all`)

**`permissions-allowlist`**
- `~/.claude/settings.json` — `permissions.allow` and `permissions.deny` keys
  (union-mode: set-union with any existing entries, deduplicated;
   or overwrite-mode: full replacement — chosen at install time)
- The rules come from this repository's `.claude/settings.json`.  Among them,
  the ones an unattended session needs to use async questions without stopping
  at a prompt (task 48):

  ```
  mcp__questions__ask        mcp__questions__notify
  mcp__questions__claim      mcp__questions__withdraw
  Bash(claude-questions:*)
  ```

  The shell rule covers the `claude-questions` command and nothing wider.
  They belong to this feature, not to `questions`: every host that installs
  the allowlist gets them, re-installing adds nothing twice, and uninstalling
  the allowlist removes exactly the rules it added (they are recorded in the
  manifest).  Note what they permit: in a workspace with
  `[questions] sync = "push"`, `claim`, `withdraw` and `claude-questions
  --sync` commit and push queue files without a prompt
  (`docs/async-questions.md`, "Reliable ids").

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

**`epic-driver`** (epic 50; the operator guide is [epic-driver.md](./epic-driver.md))
- `~/.claude/bin/epic-driver` — the CLI; `~/.local/bin/epic-driver` symlinks to it
  (the units' `ExecStart` is the symlink)
- `~/.claude/hooks/epic_driver_lib.py` — the lib (a module with one owner, so
  uninstall removes it)
- `~/.claude/epic-driver/lib/` — `context_mcp_lib.py` and `models.json` (from
  `context-mcp/`), `questions_mcp_lib.py` (from `questions-mcp/`); the CLI puts
  this directory on `sys.path`
- `~/.claude/epic-driver/prompts/` — `epic-driver-driver.md` and
  `epic-driver-worker.md`, the role prompts (from `docs/prompts/`)
- `~/.claude/epic-driver/stamps.json` — see below
- `~/.config/systemd/user/claude-epic-driver.service`, `claude-epic-driver.timer`
  and `claude-epic-driver-inhibit.service`, followed by a `daemon-reload` through
  the `ext_systemd_*` seam. **The installer enables none of them**: that is
  `epic-driver enable <ledger>`, which runs its gates first.

`stamps.json` is `{"revision": "<git describe --always --dirty of the repo>",
"installed_at": "…", "modules": {"<name>": {"path": "<installed path>",
"sha256": "…"}}}`. It lists every file the installed CLI imports or reads: the
CLI itself, `epic_driver_lib.py`, the shared modules other features own
(`amux_spawn_lib.py`, `claude_event_reducer.py`, `keep_awake_hook.py`, the
`questions_*` modules and `roles_config.py`, `statusline.py`), the three lib files
and the two prompts. The CLI hashes each file before it imports anything but the
standard library; a missing manifest, a missing file or a changed hash is
`stamp_mismatch` and the driver does nothing (architecture, epic 50, §17).

When `stamps.json` is (re)written:

- **Every run in which the feature is installed or already recorded as installed.**
  The feature's own install step writes it, and `_epic_driver_finalize_stamps` writes it
  again at the very end of the run, after every other feature has installed. This
  holds for a fresh install, an update, and a `keep` run: a run that selects only other
  features (for example `./install.sh --only keep-awake`, or a `--yes` replay) still
  refreshes the shared modules and, because `epic-driver` is detected as installed,
  rewrites the manifest. A `keep` run also re-copies the feature's own files (CLI, lib
  files, prompts) first, so the manifest never vouches for a stale one.
- A `keep` happens only when the run installs or updates at least one feature. A run
  that plans nothing but uninstalls leaves the manifest as it is.
- **Feature off: no manifest is written or kept.** If `epic-driver` is not installed
  (never turned on, or uninstalled) and not in the plan, the run writes no
  `stamps.json`; the feature's files and manifest do not exist, so nothing is stale.
- **Uninstall** (`--uninstall epic-driver`) stops and disables the units, then removes
  `stamps.json` with the other files listed above (and `~/.claude/epic-driver/` once it
  is empty). It leaves `~/.local/state/epic-driver/` (run state) alone.
- If an installed file the manifest covers is missing when it is written, the run fails
  the feature (`epic-driver` is reported failed) instead of writing a manifest that skips it.

---

## Sub-toggles

Four nested opt-ins. Each is off by default and must be explicitly enabled after
the parent feature is installed.

| id | Parent | Default | What it writes |
|----|--------|---------|----------------|
| `profiles-autosource` | `profiles` | off | `~/.bashrc` — one marked sourcing line |
| `amux-autowrap` | `amux` | off | `~/.bashrc` — one marked sourcing line |
| `questions-listen` | `questions` | off | `systemctl --user enable` + `restart claude-questions-listen`, `loginctl enable-linger`; `disable` also stops it |
| `daily-review-cron` | `daily-review` | off | crontab — one marked `15 6 * * *` line |

`profiles-autosource` and `amux-autowrap` are mutually exclusive: enabling one
automatically disables the other if it was active.

`questions-listen` is the **only** switch for the answer listener — the
listener itself has no "enabled" setting (`[questions_listen] enabled` in
`config.toml` is inert). While it is on, every `./install.sh --yes` restarts the
unit, since the run may have replaced the library the resident process loaded.
The installer reaches the user manager from a shell that has no
`XDG_RUNTIME_DIR` (an agent's tool shell, cron) by pointing at
`/run/user/<uid>` itself.

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
- `~/.local/state/epic-driver/` — the epic driver's run state
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
