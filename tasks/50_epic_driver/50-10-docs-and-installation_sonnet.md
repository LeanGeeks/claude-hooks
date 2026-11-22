# 50-10 — Docs and installation

**Status:** todo · **Depends on:** 50-08, 50-09
**Read first:** [brd.md](./brd.md) §6, §9 · [architecture.md](./architecture.md) §17, §22, §24 ·
[state.md](./state.md) P0-12, OQ-3 · `install.sh` (§1 feature registry, `REQUIRED_HOOKS`, `MODULE_OWNERS`,
`assert_registry_integrity`, the questions feature and `_install_questions_listen_unit`, the `ext_systemd_*` and
`ext_symlink_*` seams, `NO_EXTERNAL` test mode) · `tests/test_unit_installer.py` · `docs/installer.md`

## Goal

Ship the driver the way every other feature ships: an installer feature that places stamped copies and the units,
off by default, and the guide an epic author needs.

## Scope

1. **Installer feature `epic-driver`** (§22): title, writes, `default` = off, `requires` = `amux statusline keep-awake
   questions`, probe, install, uninstall. Install copies the CLI (`~/.claude/bin/epic-driver` + symlink in
   `~/.local/bin`), `epic_driver_lib.py` into `~/.claude/hooks/` (new `REQUIRED_HOOKS` and `MODULE_OWNERS` entries),
   `context_mcp_lib.py`, `models.json`, `questions_mcp_lib.py` into `~/.claude/epic-driver/lib/`, both prompts into
   `~/.claude/epic-driver/prompts/`; writes the three unit files and runs `daemon-reload` through the seams; enables
   **none** of them. Uninstall stops/disables/removes the units and files, leaves `~/.local/state/epic-driver/`.
2. **Stamps** (§17, OQ-3): after every copy of the feature's own files *and* on any install run where the feature is
   on, write `~/.claude/epic-driver/stamps.json` covering every module and prompt in §17's list (revision from `git
   describe --always --dirty` of the repo, sha256 per installed file). Because shared modules belong to other
   features, the manifest is written last in the run, after all features installed.
3. **`docs/epic-driver.md`**: §24's author guidance, the operator's on/off procedure, attaching to a session, the
   status screen, the alarm catalogue (link to the architecture), and the silent-stall gap (brd §6) stated plainly.
4. **`docs/installer.md`**: the new feature and what it writes. Top-level `architecture.md`: the module list.

## Tests

`tests/test_unit_installer.py` under `NO_EXTERNAL`: the feature is off by default; on, it places every file of §17
and the three units with the exact `[Service]`/`[Timer]` lines of §22; no unit is enabled; `stamps.json` matches the
installed files' hashes; uninstall removes them and keeps the state directory; `assert_registry_integrity` passes
with the new module; re-install with another feature's module changed updates the manifest.

## Done when

- `./install.sh --yes` with the feature on yields an `epic-driver validate` that runs from `~/.local/bin` with the
  stamps agreeing.
- The docs tell an epic author everything §24 lists without reading the architecture.
