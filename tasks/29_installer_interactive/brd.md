# Epic 29 — Interactive installer

**Status:** done · **Owner:** Anton · **Created:** 2026-08-27 · **Rev:** 1

> Rev 0 was a deliberate one-fact stub. Rev 1 fills in everything it listed as
> *"not yet written"*: the interaction model, how choices are persisted and
> replayed, what happens on upgrade, and the full toggle inventory. Broken down
> into tasks — see [state.md](./state.md) for ordering and cross-task
> invariants.

## 1. Problem & thesis

`install-claude-config.sh` is 1186 lines. It is labelled "Step N/5" but performs
**~16 independent install actions**, every one of them unconditional, writing to
**14 destinations** — most of them outside `~/.claude`. The only things that ever
vary today are accidental: a missing source file, `uv` absent, `systemctl`
absent. There is exactly one deliberate opt-in in the whole script
(`[questions_listen] enabled`, `install-claude-config.sh:970-998`).

Running it is therefore a single decision with a very wide blast radius. A user
who wants the status line gets a Telegram permission gate, a resident-daemon unit
file, an edit to `~/.tmux.conf`, a live mutation of their running tmux server, a
`.pth` into their Python user-site, four symlinks on `PATH`, and 277 permission
patterns copied over their global settings.

**Thesis (unchanged from rev 0): the installer mixes two different things.**

- *"Copy these artifacts into place"* — idempotent, safe, wanted by whoever asked
  for the feature.
- *"Decide what this machine runs"* — a policy choice, occasional, per-machine.

The interactive version separates them. The second kind becomes a **toggle**,
persisted and replayable, and every nested policy decision additionally gets a
**per-toggle command** so that changing one's mind never requires a full
re-install. That last point is rev 0's Fact 1 and it survives verbatim.

## 2. Constraints discovered

Recorded because each one closed off an option.

**2.1 — The hook modules are one tangled blob; file-level selection is not
viable.** The import graph does not respect the feature boundaries a user cares
about. `telegram_permission_router.py:406-408` lazily imports `pretool_hook`,
`settings_loader` and `bash_command_parser`, so the Telegram gate drags in the
whole bash-parser trio. `.claude/bin/amux-spawn:1253` lazily imports
`permission_state_store`, so the fleet launcher drags in the permission stack.
`notification_hook.py:62` imports `amux_spawn_lib` on top of the Telegram router.
The dependency closure of almost any selection is *all 20 modules* in
`REQUIRED_HOOKS` (`install-claude-config.sh:163`).

The consequence is the single most important design decision in this epic:
**selection governs wiring and side effects, not which `.py` files land in
`~/.claude/hooks/`.** A copied-but-unwired hook is inert — it costs a few KB and
does nothing. A half-copied import graph is a crash. Gating the `settings.json`
entries, the `PATH` symlinks, the systemd unit, the crontab line and the
`~/.tmux.conf` edit is both tractable and sufficient.

**2.2 — `/yolo` only functions through the Telegram gate.**
`session_yolo_store` is read in exactly one place —
`permission_request_hook.py:1603` — and that hook exists solely to route approvals
through the relay (its own docstring: *"sends a Telegram message via the central
relay server and long-polls"*). `pretool_hook.py` never consults the store.
`posttool_hook.py` is pure Telegram coordination — it revokes relay messages when
the terminal answered first. And `permission_request_hook.py:45` imports the
Telegram router *unconditionally*, so the hook cannot even load without the
Telegram feature installed.

So the natural grouping — `/yolo` belongs with the permission hooks, Telegram is
a delivery channel layered on top — is not achievable by regrouping alone. It
requires a code change (D9, task 29-01). Shipping `/yolo` without it would write
to a store nobody reads, while the command's own description promises auto-allow
"Telegram **or terminal**".

**2.3 — Every existing machine has artifacts and no manifest.** The old script
has been run on every machine this repo touches. If "is this feature installed?"
is answered from a manifest, the first run of the new installer shows
`Install / Skip` for things the user demonstrably already has, and `Skip` reads
as "leave it alone" while actually meaning "never wire it". Detection must probe
the disk (D2).

**2.4 — Partial installs are already forbidden, in writing.** The installer's own
comment at `install-claude-config.sh:852`, on the questions MCP and its hook
library: *"A state where one is new and the other is old silently drops index
fields (see state.md 23-05 log), so partial installs must never occur."* This
constrains the `Keep` state directly (D3): a user who keeps one feature and
updates another must not end up with a half-refreshed shared module.

**2.5 — The daily reviewer cannot be installed as a copy.**
`shell/permission-review-daily.sh:52` resolves the checkout from its own
location (`REPO_DIR="${CLAUDE_HOOKS_REPO:-$(dirname "$(dirname "$SCRIPT_PATH")")}"`),
reads `docs/prompts/permission-review-daily.md`, and logs into the repo's
`temp/`. Copied to `~/.claude/shell/` it would resolve `REPO_DIR` to `~/.claude`
and break. `docs/permission-review-daily.md:34-45` already argues for naming the
checkout path directly in the cron line. So this toggle installs *nothing*; it
only schedules (D13).

**2.6 — `install-amux.sh` already owns its own elevation.**
`install-amux.sh:397-418` implements the full ladder: running as root → target
already writable → passwordless sudo → interactive `sudo -v` → print the root
command and wait (`WAIT_TIMEOUT=900`). This epic delegates and reports; it adds
no sudo handling of its own (D12). It does mean a single feature can block a run
for up to 15 minutes, which is why all decisions are collected before any work
starts (D6).

**2.7 — The current merge clobbers.** `install-claude-config.sh:665-669` does
`del(.allowedTools, .disallowedTools) | . + {permissions: {...}}` — the user's
entire global `permissions` block is replaced by the project's, unprompted. Line
770 does `. + {hooks: $hooks}` — any hooks the user had in global settings are
gone. Neither is recoverable except from the timestamped backup, and neither is
announced as a replacement.

**2.8 — Two shipped artifacts are never installed.** `shell/claude-history` is
referenced only by a comment in the installer, and `shell/permission-review-daily.sh`
is installed entirely by hand. Both are dead weight in the repo until they become
toggles (D14, task 29-08).

## 3. Decisions

**D1 — Toggles are tri-state and state-aware.** A feature that is not installed
offers `Install / Skip`. A feature that is installed offers
`Update / Keep / Uninstall`, defaulting to `Update`. Deselection is never
implicit: removing something is a thing the user chooses, by name.

**D2 — Installed-state is detected by probing the disk, never by trusting the
manifest.** The manifest records *intent* and drives refcounting; it is not the
source of truth for what exists (constraint 2.3). Each feature declares a probe.

**D3 — `Keep` covers wiring and side effects; shared modules are always
updated.** `Keep` means "do not change what this machine runs" — it does not mean
"freeze these `.py` files", which constraint 2.4 forbids. The prompt says so at
the point of choosing.

**D4 — `Uninstall` refcounts shared modules.** A module is removed only when no
still-installed feature claims it. The manifest exists primarily to make this
answerable.

**D5 — Out-of-tree side effects ride their feature toggle.** No separate consent
prompts. Every toggle that writes outside `~/.claude` discloses what it writes,
where, in its prompt line. The set is closed and enumerated in §4.

**D6 — All decisions are collected before any work starts.** A run is: probe →
present → collect → confirm → execute → report. Nothing is asked mid-execution.
Constraint 2.6 makes this non-negotiable.

**D7 — The global permissions allowlist is its own toggle, with a union/overwrite
sub-choice.** The default is **union** (set-union, deduplicated), because the
current replace semantics silently discard hand-added entries (constraint 2.7).
Overwrite stays available for the "project config is the single source of truth"
workflow.

**D8 — Telegram is one toggle.** Approvals, idle notifications, reply-injection
and terminal coordination install together. Granular notification control belongs
on the **relay**, as per-chat or per-role settings, not as install-time surgery —
that is a later epic, not this one.

**D9 — `/yolo` belongs to the permission hooks, which requires a local mode.**
Task 29-01 makes `permission_request_hook.py` loadable and useful without
Telegram: lazy router import, honour yolo/bypass, otherwise exit without a
decision so Claude Code's native terminal prompt runs. This is a prerequisite,
not a nice-to-have — without it, `permission-hooks` cannot exist and constraint 2.2 stands.

**D10 — MCP registrations fold into their parent feature.** The permissions MCP
ships with Telegram integration (`permissions_mcp_lib` imports
`permission_state_store`, `bash_command_parser`, `settings_loader` — it reads the
rows that stack writes). The questions MCP ships with async questions.
context-usage is the only standalone MCP toggle, because it depends on nothing
but `uv`.

**D11 — Diagnostics ship with what they inspect; `claude-history` is
standalone.** `claude-roles` with Telegram, `claude-questions` with async
questions. `claude-history` reads `~/.claude/history.jsonl` and
`~/.claude/projects/` directly with no repo imports, so it is as standalone as
the status line and gets its own toggle.

**D12 — amux integration runs `install-amux.sh`.** Selecting the feature installs
the pinned `amux` as well as the launcher. Elevation is delegated entirely
(constraint 2.6). The existing `__codex-run` feature probe stays, as a
post-install verification rather than a warning-only path.

**D13 — The installer writes the crontab line.** This reverses
`docs/permission-review-daily.md:18` (*"The repo never writes your crontab"*).
The line is marked, idempotent, backed up before edit, and removed on uninstall —
the same discipline as the `~/.tmux.conf` block. The doc's reasoning about naming
the checkout path directly carries over unchanged and becomes the *reason* the
feature copies nothing (constraint 2.5). Writing it is a nested opt-in (10a), not
a side effect of installing the feature.

Enabling it also makes this repo the owner of an unattended job that runs the
installer: `docs/prompts/permission-review-daily.md` step 3 has the scheduled
agent re-run the installer so a permission pattern it applied reaches every other
workspace. That run must use `--yes` — the manifest replay of §6.3, which
preserves every choice this machine already made — and must never be a bare or
`--all` invocation. 29-08 §2.1 owns pinning the seed prompt to it; 29-07 §1.1
owns the guarantee it relies on.

**D14 — Two features default to OFF on a fresh machine.** Async questions and
daily permission review both start `Skip` on first run. Both schedule ongoing
autonomous activity — a resident daemon that polls a relay, and a cron job that
edits `settings.json` and commits — and rev 0's *"never enable it silently"*
applies to the whole feature, not only to the unit file. Every other feature
defaults to `Install`.

**D15 — Nested opt-ins get `enable` / `disable` subcommands.** This is rev 0's
Fact 1, discharged: `install-claude-config.sh enable questions-listen` flips one
boolean with a narrow blast radius and no full re-install. Applies to 4a, 7a and
10a.

**D16 — amux integration carries an auto-wrap sub-toggle (4a).** It manages the
`source ~/.claude/shell/amux-spawn.bash` line in the user's `.bashrc`, so that
profile-launched sessions route through amux-spawn automatically. Today the
installer refuses to touch `.bashrc` and prints instructions instead
(`install-claude-config.sh:482-492`); 4a replaces that prose with a managed,
marked, reversible line.

> **Scope note on 4a.** `amux_spawn_lib.emit_shell_functions()` (`:1382`)
> generates **one function per profile name** — it does not define a `claude()`
> wrapper. So 4a auto-wraps *profile-launched* sessions; a bare `claude`
> invocation is untouched. Wrapping bare `claude` would be a new function in
> `emit_shell_functions` and is **out of scope** here (§9).

**D17 — Recommendation-level, one-line to change if wrong.** Two choices were
made by recommendation rather than hard requirement, flagged here so a later
reader knows they are cheap to revisit:

1. **Per-feature failure isolation.** A feature whose install fails is recorded
   as not-installed, its error is collected, and the run continues to the next
   feature; the summary lists failures at the end with a non-zero exit. Today
   `set -euo pipefail` aborts the whole run mid-way and leaves partial state
   behind with only a `settings.json` backup. The alternative (abort on first
   failure) is a one-line change in the executor loop.
2. **A profiles auto-source sub-toggle (5a)**, symmetric with 4a. Not requested;
   added because the two `.bashrc` lines are mutually exclusive
   (`install-claude-config.sh:491`: *"Do NOT source both"*) and the exclusion
   logic has to exist regardless. Delete 5a and the machinery still works.

## 4. Surface — the toggle inventory

Ten features, four nested opt-ins. Every action in the current script maps to
exactly one entry; nothing is orphaned.

**Ids are canonical.** The table is in registry order (architecture §2), which is
also execution order — `profiles` precedes `amux` because `amux-spawn.bash`
sources `claude-profiles.bash`. The numbers shown in the checklist
(architecture §5.1) are **display positions only** and shift if the registry
order changes; never reference a feature by its number.

| id | Toggle | Default | Writes outside `~/.claude` | Requires |
|----|--------|---------|----------------------------|----------|
| `statusline` | **Statusline** | on | — | — |
| `permission-hooks` | **Permission hooks** | on | — | — |
| `telegram` | **Telegram integration** | on | user-site `.pth`, `~/.local/bin/claude-roles` | `permission-hooks` |
| `profiles` | **Model profiles** | on | — | — |
| `profiles-autosource` | ↳ *auto-source profiles* | off | `~/.bashrc` | `profiles`, **excl. `amux-autowrap`** |
| `amux` | **amux integration** | on | `/usr/local/bin/amux`, `~/.tmux.conf` + live tmux, `~/.local/bin/amux-spawn`, bash-completion dir | `profiles` |
| `amux-autowrap` | ↳ *auto-wrap sessions* | off | `~/.bashrc` | `amux`, **excl. `profiles-autosource`** |
| `permissions-allowlist` | **Global permissions allowlist** | on | — | — |
| `context-mcp` | **MCP: context-usage** | on | `~/.claude.json` | `uv` |
| `claude-history` | **`claude-history`** | on | `~/.local/bin/claude-history` | — |
| `questions` | **Async questions** | **off** | `~/.local/bin/{claude-questions,questions-listen}`, systemd unit file | `telegram` |
| `questions-listen` | ↳ *enable listener daemon* | off | `systemctl --user enable`, `loginctl enable-linger` | `questions` |
| `daily-review` | **Daily permission review** | **off** | — | `amux` |
| `daily-review-cron` | ↳ *write crontab line* | off | crontab | `daily-review` |

Per-feature contents, probes and removal steps are specified in
[architecture.md](./architecture.md) §2 — that table is the registry, and it is
data, not prose.

**Housekeeping, never a toggle:** log rotation, legacy `telegram_daemon.py`
cleanup, the `settings.json` backup. These run on every invocation.

**Shared modules, never a toggle:** `roles_config.py`, `permission_state_store.py`,
`amux_spawn_lib.py`, `settings_writer.py`, `project_key.py`,
`bash_command_parser.py`, `settings_loader.py`. Installed as the dependency
closure of whatever is selected, refcounted on uninstall (D4).

## 5. Configuration

**The manifest — `~/.claude/install-manifest.json`.** Records intent, not truth
(D2). Shape in architecture §3. It carries: the schema version, the repo path and
git revision of the install, and per feature the chosen state, the timestamp, the
sub-toggle states, and the list of artifacts that feature claimed. The artifact
lists are what make refcounted uninstall answerable.

**CLI surface.**

```
install-claude-config.sh                    # interactive; replays manifest as defaults
install-claude-config.sh --all              # every feature on, sub-toggles at their defaults
install-claude-config.sh --only a,b,c       # exactly these
install-claude-config.sh --with x --without y
install-claude-config.sh --yes              # non-interactive; manifest or --all/--only
install-claude-config.sh --list             # feature table with detected state, exit 0
install-claude-config.sh --dry-run          # full plan, touch nothing
install-claude-config.sh enable  <toggle>   # nested opt-ins (D15)
install-claude-config.sh disable <toggle>
```

No TTY and no `--yes` is an error, not an implicit `--all`. A non-interactive run
with neither a manifest nor an explicit selection is likewise an error: the one
thing the old script did — install everything without asking — must not be
reachable by accident.

## 6. Behaviour

**6.1 First run on a clean machine.** Probes find nothing. Every feature offers
`Install / Skip`, pre-set from D14 defaults. The user toggles, confirms the plan
(which lists every out-of-tree write), and the run executes.

**6.2 First run on a machine the old script installed.** Probes find artifacts;
the manifest is absent. Features detected as present offer
`Update / Keep / Uninstall` at `Update`, so the default action is "carry on as
you were, refreshed" — the old script's behaviour, arrived at by consent. Task
29-09 owns this path; it is the one every existing user hits exactly once.

**6.3 Re-run / upgrade.** Manifest supplies the defaults; the whole run is a
confirm. `--yes` makes it silent, which is what CI, `git pull && ./install` and
the scheduled daily reviewer (D13) want.

`--yes` is a **replay, not a re-decision**: every feature's action comes from its
manifest `state`, and a feature recorded as `skipped` stays skipped. The D14
defaults apply only to a feature id the manifest does not mention at all — one
added to the registry since it was written — and those are reported as
newly-offered rather than installed. No unattended run may install a feature the
user declined; that is what makes it safe to put the installer behind a cron job
at all.

**6.4 Uninstall.** Per feature: remove owned artifacts, delete owned settings
keys, revert owned external surfaces (bashrc line, cron line, tmux block, systemd
unit), then drop shared modules no remaining feature claims (D4). A feature that
was never installed is a no-op.

**6.5 Failure.** Per-feature isolation (D17.1): failures are collected, the run
continues, the summary names them, exit status is non-zero. `settings.json` and
`~/.claude.json` writes remain all-or-nothing per run — they are validated before
replacing, and restored from the backup on invalid output, exactly as today
(`install-claude-config.sh:1004-1021`).

## 7. Success criteria

1. A user who wants only the status line gets three files and two settings keys —
   no hooks wired, no `PATH` symlinks, no `.pth`, no tmux edit, no `.bashrc` edit.
2. Enabling the questions listener is **one command** (`enable questions-listen`),
   not a hand-edited TOML plus a full re-install. Rev 0's Fact 1 is discharged.
3. `--dry-run` on any selection prints every file, settings key and external
   surface that would change, and touches nothing.
4. Uninstalling Telegram integration on a machine that also has async questions
   leaves `roles_config.py` in place and async questions working.
5. `Keep` on one feature and `Update` on another never produces a mixed-version
   `~/.claude/hooks/` (constraint 2.4).
6. A machine installed by the old script re-runs into an identical end state, with
   the manifest now populated.
7. `/yolo` either works or is not installed — never installed-and-inert (D9).
8. No feature writes outside `~/.claude` without that write appearing in its
   prompt line and in the confirmed plan.

## 8. Security

The installer's authority is the user's own account, and this epic *widens* what
it touches: `.bashrc` (4a/5a) and the crontab (10a) are new. Both are therefore
nested opt-ins, both default off, both are written as **marked, single-purpose
lines** with a backup taken first, and both are removed on uninstall. The pattern
to copy is the existing `~/.tmux.conf` block (`install-claude-config.sh:536-576`):
an exact-match marker, `grep -Fxq` idempotency, and a timestamped backup before
any append.

Two existing behaviours are tightened rather than extended. The permissions merge
stops silently replacing the user's global allowlist (D7). The `hooks` key stops
being written wholesale; only entries this installer owns are touched, and
foreign entries survive (task 29-06).

`profiles.toml` keeps its current handling — created from the example if absent,
**never overwritten**, mode `600` — because it holds API tokens.

## 9. Out of scope

- **Wrapping bare `claude`** in the shell snippets. 4a covers profile-launched
  sessions only; a `claude()` wrapper is new behaviour in
  `emit_shell_functions` and belongs to the profiles epic (see D16's scope note).
- **Granular Telegram notification control.** Belongs on the relay (D8).
- **Uninstalling `amux` itself.** The `amux` feature's uninstall removes the launcher, the
  wiring and the tmux block; `/usr/local/bin/amux` was installed by a different
  script under elevation and is left alone.
- **A TUI.** The interaction is a numbered checklist in plain bash — no
  `dialog`, no `whiptail`, no new dependency. It must work over SSH on a machine
  with nothing installed but `bash`, `jq` and `python3`.
- **Per-project installs.** This installer is user-global, as today.
- **Migrating `claude.bashrc` env functions to `profiles.toml`.** The existing
  printed guidance (`install-claude-config.sh:494-503`) stays prose.
