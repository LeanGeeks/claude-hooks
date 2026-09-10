# 29-04 — Ownership-scoped `settings.json` and `~/.claude.json` writes

**Status:** todo · **Depends on:** 29-02 (registry, manifest)
**Read first:** [brd.md](./brd.md) D7, §8, constraint 2.7 ·
[architecture.md](./architecture.md) §7 · [state.md](./state.md) invariants 8, 9 ·
`install-claude-config.sh:646-794` (the current merge) · `:796-881` (the three
MCP registrations) · `.claude/settings.json` (the source of the allowlist)

> **Which file you edit:** `install.sh`. 29-02 §0 froze
> `install-claude-config.sh` as the pre-epic reference copy — every
> `install-claude-config.sh:NNN` citation below points into that frozen file and
> describes code as it was before the epic. Do not edit it. Run `install.sh` only
> against a temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`
> ([state.md](./state.md), bootstrapping hazard).

## Goal

Stop the installer clobbering configuration it does not own, and make every
settings write attributable to a feature so 29-05 can reverse it.

## Scope

### 1. The clobbering, stated

Two writes destroy user configuration today, silently:

- `:665-669` — `del(.allowedTools, .disallowedTools) | . + {permissions: {...}}`
  replaces the user's **entire** global `permissions` block with the project's
  (277 allow / 9 deny / 9 ask as of writing).
- `:770` — `. + {hooks: $hooks}` replaces the **entire** `hooks` object, so any
  hook the user configured globally is gone.

Neither is announced. Both are recoverable only from the timestamped backup, and
only if the user notices in time.

### 2. Hook entries — identified by command, not by a marker

Per architecture §7.1: **an entry is ours if its `command` references
`$GLOBAL_HOOKS_DIR`.** Nothing foreign points there, so this needs no marker key
and adds no pollution to `settings.json`.

- Install/update a feature: remove our entries for the events that feature owns,
  then append the current ones. Foreign entries in the same event array survive,
  in their original relative order.
- Uninstall: remove our entries for those events; delete the event key if its
  array becomes empty.

Event ownership is the table in architecture §7.1. Implement it as data the
registry exposes, not as inline `jq` scattered through the script.

**`Notification` is shared** between `telegram` (matcher `idle_prompt`) and
`amux` (matcher `permission_prompt`). Merging must key on `matcher`. Replacing
the `Notification` array wholesale — which is what the script does today — would
make installing one feature silently uninstall the other's notification wiring.
This is the single subtlest case in the task; the current code's two-element
array literal at `:716-732` is what you are replacing.

### 3. Permissions

Implement both modes from brd D7 / architecture §7.2. Default is **union**.

- `union`: set-union with the existing arrays, original order preserved,
  duplicates dropped. Record the added subset in the manifest as `added_allow` /
  `added_deny` (architecture §3) so uninstall can subtract exactly it.
- `overwrite`: today's replace semantics, but report the count of entries about
  to be discarded **before** writing.

Keep folding in and deleting the legacy `allowedTools` / `disallowedTools` keys
(`:654-655`, `:665-669`) — that migration is still wanted.

The `ask` array is read from `.permissions.ask` and has no legacy equivalent
(`:656`); the `// []` default stays.

### 4. `~/.claude.json` (MCP servers)

The three registration blocks (`:796-881`) are near-identical copies. Collapse
them into one helper taking `(name, script, env)` and call it from the owning
feature (`context-mcp`, `telegram`, `questions` — brd D10). Add the matching
de-registration for 29-05's use: remove the named key from `.mcpServers`, leaving
every other server alone.

Keep the existing `uv` gate and the validate-then-replace discipline already
there at `:809-816`.

**`~/.claude.json` has never been backed up — add that.** `settings.json` gets a
timestamped backup at `:642` and `~/.tmux.conf` at `:564`; the `~/.claude.json`
write at `:800-820` does tmp-write → `jq empty` → `mv` with no backup at all. §5
below (and this task's "Done when") says failures restore from the timestamped
backup *for both JSON files* — for `~/.claude.json` there is currently nothing to
restore from, so this task has to create it, on the same
`$BACKUP_DIR/<name>.<timestamp>.bak` pattern.

This is the least reproducible file the installer touches: it carries the user's
per-project history, their MCP servers and their account state, and it is ~70 KB
on a working machine. Claude Code writes its own `.claude.json.backup.*` files
into `$BACKUP_DIR`, which is a safety net but not one the installer may lean on —
it is not ours, its timing is not ours, and it is not guaranteed to predate our
write.

### 5. Atomicity

Unchanged and worth stating (architecture §7.3): build in memory, `jq empty`,
write, re-validate, restore from the timestamped backup on failure
(`:1004-1021`). Per-run all-or-nothing for both JSON files, even though feature
execution is per-feature isolated (brd D17.1).

## Done when

- A hand-added `hooks.PreToolUse` entry pointing at the user's own script
  survives install, update and uninstall of `permission-hooks`.
- A hand-added `permissions.allow` pattern survives a `union` install and is
  **not** removed by a later uninstall.
- Installing `telegram` then `amux` leaves both `Notification` matchers wired;
  uninstalling one leaves the other's matcher intact.
- `overwrite` mode reports the discarded count before writing.
- Registering the questions MCP leaves an unrelated `mcpServers` entry untouched;
  de-registering removes only its own key.
- An intentionally corrupted merge result restores the backup and exits non-zero,
  for `settings.json` **and** for `~/.claude.json`.
- A run that touches `~/.claude.json` leaves a timestamped backup of its
  pre-edit content in `$BACKUP_DIR`.

## Tests

Extend `tests/test_unit_installer.py`.

- Foreign-entry survival for `hooks` (all six owned events) and for
  `permissions.allow` / `.deny`.
- The `Notification` matcher case, explicitly, in both orders: telegram→amux and
  amux→telegram, install and uninstall.
- Union: added set recorded in the manifest; uninstall subtracts exactly it;
  a pattern present in both project and user config is not double-added and not
  removed on uninstall.
- Overwrite: discarded count correct.
- MCP: register/de-register with a pre-existing foreign server present.
- Corrupted-merge restore path, asserted on the file contents and exit status —
  once per JSON file.
- `~/.claude.json` backup: seed a `tmp_home` copy with a recognisable foreign
  `mcpServers` entry, run an install, assert the backup exists and matches the
  pre-run bytes.
