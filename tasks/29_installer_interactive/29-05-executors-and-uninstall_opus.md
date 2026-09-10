# 29-05 — Executors, module closure, refcounted uninstall

**Status:** todo · **Depends on:** 29-02 (registry, manifest, probes), 29-03
(external seam), 29-04 (settings ownership)
**Read first:** [brd.md](./brd.md) D3, D4, D17.1, §6.4, §6.5 ·
[architecture.md](./architecture.md) §6, §2.1 · [state.md](./state.md)
invariants 3, 4, 10 · `install-claude-config.sh:852-859` (the partial-install
prohibition, quoted below) · the outputs of 29-02/29-03/29-04

> **Which file you edit:** `install.sh`. 29-02 §0 froze
> `install-claude-config.sh` as the pre-epic reference copy — every
> `install-claude-config.sh:NNN` citation below points into that frozen file and
> describes code as it was before the epic. Do not edit it. Run `install.sh` only
> against a temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`
> ([state.md](./state.md), bootstrapping hazard).

## Goal

The engine: turn a plan into a machine state. Install, update, keep and
uninstall, with the module closure and the refcounting that makes uninstall safe.

**This is the task that can destroy a working machine.** Uninstall deletes files
and settings keys; a refcount error takes out a module a still-installed feature
needs. Read `MODULE_OWNERS` (architecture §2.1) until you can recite which four
features share `permission_state_store.py`.

## Scope

### 1. The executor loop

Per architecture §6.1, in registry order:

```
install | update  → feature_<id>_install,  record artifacts in the manifest
keep              → no-op, except shared modules (§3 below)
uninstall         → feature_<id>_uninstall, clear the manifest entry
```

`install` and `update` are the same code path. `_install` must be idempotent —
that is what makes them the same — and the distinction exists for the user's
mental model and the summary, not for the executor.

### 2. Failure isolation

Per brd D17.1: a failing feature is caught, recorded `failed` in the manifest,
and the loop continues. The summary names every failure and the run exits
non-zero.

This requires `set -e` to be **scoped rather than global**. The script currently
opens with `set -euo pipefail` (`:12`) and runs 1100 lines straight through, so
any failure aborts mid-way and leaves partial state with only a `settings.json`
backup. Run each `feature_<id>_install` so that a non-zero return is data, not an
abort — and be careful that this does not silently swallow errors *inside* a
feature: the feature's own body should still stop at its first failure.

The JSON writes stay all-or-nothing per run (29-04 §5).

### 3. The module closure and `Keep`

Per architecture §6.3 and brd D3.

The closure is computed from every feature whose action is `install`, `update`
**or** `keep`. Everything in it is copied fresh, every run.

`Keep` means *"do not change my wiring or my external surfaces"*. It does **not**
mean "freeze these `.py` files at the version I have". The installer's own
comment at `:852-859` says why, about the questions MCP and its hook library:

> *"A state where one is new and the other is old silently drops index fields
> (see state.md 23-05 log), so partial installs must never occur."*

The tangled import graph (brd constraint 2.1) means a kept feature shares modules
with an updated one in almost every combination. Refreshing all of them is the
only state that is not a partial install. The selector (29-06) tells the user this
at the point of choosing; this task enforces it.

### 4. Refcounted removal

On uninstall, per brd D4:

- Remove the artifacts the manifest records for that feature (architecture §3 —
  `_install` recorded them; do not re-derive them).
- Remove its settings keys and MCP registrations via 29-04's helpers.
- Revert its external surfaces via 29-03's `ext_*_remove`.
- Then, for each module the feature owned: delete it **only if no feature still
  `installed` in the manifest owns it** (`MODULE_OWNERS`).

The relay `.pth` in the Python user-site follows the same rule, owned by
`telegram` alone.

**Never remove:** `~/.claude/profiles.toml` (holds API tokens — brd §8),
`~/.claude/history.jsonl`, anything under `~/.claude/projects/`,
`~/.claude/backups/`, or any state store under `~/.claude/`. Uninstalling a
feature removes the machinery, never the user's data or history. When in doubt,
leave it and say so in the summary.

`/usr/local/bin/amux` is likewise never removed (brd §9) — it was installed by a
different script under elevation.

### 5. Dependency refusal

Setting a feature to `uninstall` while an installed feature requires it is
refused, naming the dependent. The selector (29-06) prevents this
interactively; the executor must also refuse it for the `--only` /
`--without` paths, because those bypass the selector entirely.

### 6. Reporting

The summary reports, per feature: action taken, artifacts touched, settings keys
changed, external surfaces changed, and any failure. Keep the existing summary's
level of detail (`:1023-1175`) — it is genuinely good — and extend it with the
uninstall side and the failure list.

## Done when

- Install `telegram` + `questions`, then uninstall `telegram`: `roles_config.py`
  survives, `questions` still probes installed and still works.
- Install `amux` + `profiles`, then uninstall `amux`: `amux_spawn_lib.py`
  survives (owned by `profiles` too) and the profile functions still generate.
- Uninstall the last feature owning a module: the module is removed.
- `Keep` on one feature and `Update` on another refreshes every shared module and
  changes no wiring for the kept one.
- A feature forced to fail leaves every other feature installed, is recorded
  `failed` in the manifest, and the run exits non-zero.
- Uninstalling every feature leaves `profiles.toml`, `history.jsonl` and the
  state stores in place.
- `uninstall permission-hooks` with `telegram` installed is refused, naming
  `telegram`.

## Tests

Extend `tests/test_unit_installer.py`.

- The refcount matrix: for each of the seven shared modules, install two owners,
  uninstall one, assert survival; uninstall both, assert removal.
- `Keep` semantics: shared module mtime/content refreshes, wiring unchanged.
- Failure isolation: inject a failing `_install` (a registry override in the
  harness), assert the others complete, the manifest records `failed`, and the
  exit status is non-zero.
- Data preservation: populate `profiles.toml`, `history.jsonl` and a state store,
  uninstall everything, assert all three are byte-identical.
- Dependency refusal on both the selector-free paths (`--only`, `--without`).
- Full round trip: `--all --yes`, then uninstall all, then `--all --yes` again —
  the third state equals the first, byte-for-byte, in `settings.json`,
  `~/.claude.json` and the hooks directory listing.
