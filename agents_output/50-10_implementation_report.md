# Implementation Report - 50-10 Docs and installation

## Summary

Added the `epic-driver` installer feature (off by default, requires `amux statusline keep-awake questions`). It places the
stamped CLI, `epic_driver_lib.py`, the three lib files, the two role prompts and the three unit files, writes
`stamps.json` last in the run, and enables nothing. Wrote `docs/epic-driver.md` (§24 author guidance, operator on/off,
attaching, status, alarms link, `startup_dialogs`, the silent-stall gap), and updated `docs/installer.md` and the
top-level `architecture.md` module list. Nine new tests in `tests/test_unit_installer.py`; full suite green.

## Files Created

- `docs/epic-driver.md` — the epic-author and operator guide.

## Files Modified

- `install.sh`
  - `FEATURES` gets `epic-driver` (last); `REQUIRED_HOOKS` and `MODULE_OWNERS` get `epic_driver_lib.py` (owner `epic-driver`).
  - New feature block after `daily-review`: `feature_epic_driver_{title,writes,default,requires,modules,suboptions,probe,install,uninstall}`, and the helpers `_epic_driver_place_files`, `_epic_driver_write_units`, `_epic_driver_write_stamps`, `_epic_driver_finalize_stamps`, `_epic_driver_fail`, plus the `EPIC_DRIVER_STAMPED` table.
  - `_epic_driver_finalize_stamps` is called once in the main flow, after the feature loop and before `manifest_write`.
  - `crontab`/`systemctl`/`loginctl`/`tmux set`/`ln -s` are only called through `ext_systemd_daemon_reload`, `ext_systemd_disable` and `ext_symlink_add`/`ext_symlink_remove`; the source-level invariant test still passes.
- `tests/test_unit_installer.py`
  - New class `TestEpicDriverFeature` (9 tests).
  - `TestFeatureModuleClosure` now stages a feature's modules together with those of the features it (transitively) `requires`, via the new `_parse_feature_requires()`. `epic_driver_lib.py` imports `amux_spawn_lib` and `claude_event_reducer`, which another feature ships, so the old one-feature-alone staging could not hold for it.
  - The registry-patch anchor in `test_registry_assertion_fires_on_missing_module_owner` moved to the new last `REQUIRED_HOOKS` entry.
- `docs/installer.md` — feature count, table row, default-skip sentence, "what each feature writes" for `epic-driver` including the `stamps.json` format and when it is rewritten, uninstall's "never removes" list.
- `architecture.md` — module list: `epic_driver_lib.py`, `bin/epic-driver`, `docs/epic-driver.md`, the two role prompts.

## Installed file list and units

Under a home directory `$H`:

| File | Source |
|---|---|
| `$H/.claude/bin/epic-driver` (mode +x); symlink `$H/.local/bin/epic-driver` through `ext_symlink_add` | `.claude/bin/epic-driver` |
| `$H/.claude/hooks/epic_driver_lib.py` (module closure, owner `epic-driver`) | `.claude/hooks/` |
| `$H/.claude/epic-driver/lib/context_mcp_lib.py`, `models.json` | `context-mcp/` |
| `$H/.claude/epic-driver/lib/questions_mcp_lib.py` | `questions-mcp/` |
| `$H/.claude/epic-driver/prompts/epic-driver-driver.md`, `epic-driver-worker.md` | `docs/prompts/` (the path 50-06 seeds: `ROLE_PROMPTS_DIR = ~/.claude/epic-driver/prompts`, verified in the lib) |
| `$H/.claude/epic-driver/stamps.json` | written by the installer |
| `$H/.config/systemd/user/claude-epic-driver.service`, `.timer`, `claude-epic-driver-inhibit.service` | the exact text of architecture §22; `daemon-reload` through `ext_systemd_daemon_reload`; none enabled |

Uninstall stops/disables (`ext_systemd_disable`, which uses `disable --now`) and removes the three units and every file
above, including the CLI's `__pycache__` in `epic-driver/lib/` (found in the sandbox: running the installed CLI creates
it, and it blocked the `rmdir`). It leaves `~/.local/state/epic-driver/`. `epic_driver_lib.py` goes through the
existing refcounted module removal.

## Manifest contents/format

`$H/.claude/epic-driver/stamps.json`:

```
{"revision": "<git describe --always --dirty of the repo>", "installed_at": "<UTC>",
 "modules": {"<name>": {"path": "<installed path>", "sha256": "<hex>"}, ...}}
```

16 modules, exactly the CLI's `STAMPED_FILES` (keys and paths): `epic-driver`, `epic_driver_lib.py`, `amux_spawn_lib.py`,
`claude_event_reducer.py`, `keep_awake_hook.py`, `questions_store.py`, `questions_listen_lib.py`, `questions_git.py`,
`questions_ops.py`, `roles_config.py`, `statusline.py`, `context_mcp_lib.py`, `models.json`, `questions_mcp_lib.py`,
`epic-driver-driver.md`, `epic-driver-worker.md`. Written atomically (temp file, `os.replace`, mode 0644). It fails the
feature (not silently) if any listed file is missing.

When it is written: inside `feature_epic_driver_install` after the copies, and again at the end of every run in which
the feature's action is `install`, `update` or `keep` (`_epic_driver_finalize_stamps`, after the feature loop and before
`manifest_write`, so it is after every other feature's modules). On a `keep` run the feature's own files are re-copied
first, so the manifest never vouches for a stale CLI or prompt (Decision 2). A run where the feature is not in the plan
leaves the manifest untouched, so a shared module reinstalled by such a run makes the driver stop with
`stamp_mismatch` until `./install.sh --yes` is run; that is §17's stated behaviour, and `docs/installer.md` says so.

## Sandbox (feature ON, temp HOME)

What I ran (`SB=/tmp/ed_home.EsQr`, a fresh temp dir with a fake `uv` in `$SB/fake-bin`, as the installer tests do):

```
env -u XDG_RUNTIME_DIR HOME=$SB XDG_CONFIG_HOME=$SB/.config XDG_DATA_HOME=$SB/.local/share \
    XDG_STATE_HOME=$SB/.local/state XDG_CACHE_HOME=$SB/.cache PATH=$SB/fake-bin:$PATH \
    CLAUDE_INSTALL_NO_EXTERNAL=1 CLAUDE_INSTALL_ASSUME_TTY=0 bash ./install.sh --only epic-driver --yes
```

`CLAUDE_INSTALL_NO_EXTERNAL=1`, so no real systemctl, loginctl, tmux, crontab or ln -s ran. Exit 0. Relevant log lines:

```
[INFO]   Promoted prerequisite: amux / statusline / keep-awake / questions (required by epic-driver), profiles, telegram, permission-hooks
[INFO]   Installed module: epic_driver_lib.py
[INFO] Installed: epic-driver → /tmp/ed_home.EsQr/.claude/bin/epic-driver
[INFO] Installed: epic-driver lib (3 files) → .../.claude/epic-driver/lib, prompts (2 files) → .../.claude/epic-driver/prompts
[INFO] [NO_EXTERNAL] ext_symlink_add: would create symlink /tmp/ed_home.EsQr/.local/bin/epic-driver -> /tmp/ed_home.EsQr/.claude/bin/epic-driver
[INFO] Installed systemd units (not enabled): claude-epic-driver.service, claude-epic-driver.timer, claude-epic-driver-inhibit.service
[INFO] epic-driver installed; it is OFF. Turn it on with: epic-driver enable <ledger>
[INFO] epic-driver: stamps.json written (after all features)
  - epic-driver: installed — Epic driver (unattended step-ledger runner)
```

`ext_symlink_add` is skipped under `NO_EXTERNAL`, so I created that one link by hand, exactly what the seam does:
`ln -s $SB/.claude/bin/epic-driver $SB/.local/bin/epic-driver`. No `.wants` symlinks exist under `$SB/.config/systemd`.

Toy ledger: `/tmp/ed_sandbox/ws/plan/ledger.yaml` (one driver step, plus a `startup_dialogs` entry copied from the guide).
All runs with `HOME=$SB` and `EPIC_DRIVER_ALLOW_UNSTAMPED` unset:

```
$ $SB/.local/bin/epic-driver validate /tmp/ed_sandbox/ws/plan/ledger.yaml
warning: [context_window_default_fallback] roles.driver: no known context window for model 'sonnet' ...
/tmp/ed_sandbox/ws/plan/ledger.yaml: valid (run epic-toy, 1 steps, 1 warning(s))      rc=0

$ echo tamper >> $SB/.claude/epic-driver/prompts/epic-driver-worker.md
$ $SB/.local/bin/epic-driver validate ...
epic-driver validate: refused: stamp_mismatch: epic-driver-worker.md: .../prompts/epic-driver-worker.md changed since it was installed (sha256 9a56adfd4835, stamped 931799f35d6e)
  fix: re-run ./install.sh --yes with the epic-driver feature on                       rc=3

$ EPIC_DRIVER_ALLOW_UNSTAMPED=1 $SB/.local/bin/epic-driver validate ...                 -> same refusal, rc=3 (override ignored on an installed copy)

$ install.sh --yes   (same env: manifest replay)  -> "epic-driver: update", "stamps.json written (after all features)", rc=0
$ $SB/.local/bin/epic-driver validate ...        -> valid, rc=0   (the replay restored the file and the stamps agree again)

$ install.sh --uninstall epic-driver --yes       -> three "[NO_EXTERNAL] ext_systemd_disable: would disable claude-epic-driver…",
   "ext_symlink_remove", "Removed module: epic_driver_lib.py (no remaining owners)"; $SB/.local/state/epic-driver/x kept.
```

## Real `./install.sh --yes` (once, at the end, after the full suite)

Before it I ran `./install.sh --yes --dry-run </dev/null`: the plan listed only the ten previously selected features
(all `update`), no `epic-driver`. The real run (`</dev/null`, so a prompt would have failed rather than hung):
exit 0, no prompt, no sudo, "Hook modules are working correctly!". The replay printed
`[WARN] Newly-offered features (not in manifest — not installed by this run): epic-driver (default: skip)`, and the
feature actions were the same ten `update`s as before.

Read-only checks afterwards:

- `systemctl --user list-unit-files | grep -i epic`: no output (exit 1). No epic-driver unit exists.
- `~/.claude/bin/epic-driver`, `~/.local/bin/epic-driver`, `~/.claude/epic-driver/`, `~/.claude/hooks/epic_driver_lib.py`: none exist (the feature is off).
- `cmp` of the repo and `~/.claude/hooks/` copies: `amux_spawn_lib.py` identical (sha256 `03f8957e579a22a1...`), `claude_event_reducer.py` identical (`331054db2344ddbf...`).
- Side effect: `~/.claude/install-manifest.json` now records `features["epic-driver"] = {"state": "skipped", ...}` (the replay writes every registry feature; a recorded skip is a decision). Turn it on later with `./install.sh --yes --with epic-driver`.

## §17-vs-code differences

- §17's list omits `questions_git.py` and `questions_ops.py`, and does not name the CLI itself or the two prompts as manifest entries by name. The CLI's `STAMPED_FILES` (and `NOTIFY_IMPORT_CLOSURE`, from the 50-08 second fix) include `questions_git.py` and `questions_ops.py`: `questions_mcp_lib` imports them transitively. I followed the code: the manifest has all 16 entries the CLI requires. A test (`test_stamps_installer_list_equals_the_cli_list`) compares the installer's list to the CLI's, key and path, so they cannot drift.
- `check_stamps` verifies every manifest entry, not only the required names, so the manifest must contain no entry for a file the installer does not place; it contains none.
- `revision` is `git describe --always --dirty` as the task says (the CLI only reads it for display); the manifest's `installed_at` is informational.
- `status` shows `stamps ok (<revision>)`; no code change needed.

## Verification

- Compile/import: PASS. `bash -n install.sh`; `python3 -m py_compile tests/test_unit_installer.py`; the changed lib files were not edited.
- Tests: `python3 tests/run_all_tests.py` (foreground, all code edits done): `Ran 3055 tests in 463.865s` / `OK (skipped=2)`, exit 0. Baseline 3046 OK, 2 skipped; +9 are the new tests. `--module unit_installer`: Ran 222, OK (was 214).
- New tests (`TestEpicDriverFeature`): off by default and a replay does not turn it on; `requires`/registry/owners; on: every file byte-equal to its source, the three units line-for-line equal to §22, daemon-reload through the seam, no `ext_systemd_enable`, no unit symlink, no state dir created; stamps equal the file hashes and cover the CLI's `STAMPED_FILES`; the installer's list equals the CLI's list; re-install with another feature's repo module changed (keep_awake_hook.py in a temp repo copy) updates the manifest hash; a replay rewrites a clobbered `stamps.json`; uninstall removes files and units, keeps the state directory (including after a `__pycache__` appears in `lib/`); docs contain the required topics. Existing registry tests pass with the new module (`test_real_registry_passes_assertions`, the owner/hooks match test, the source-level seam invariant).
- Installed (install.sh --yes re-run): yes (once; the real machine's selection is unchanged and epic-driver stays off).

## Decisions

1. Feature id `epic-driver`, appended last in `FEATURES` (it requires four earlier features; a last position also kept the selector numbering the tests assume). Probe = `~/.claude/bin/epic-driver` exists.
2. On a `keep` run the feature's own files are re-copied before the manifest is written, so the manifest cannot bless a stale CLI. This goes slightly beyond "no wiring change", and is limited to the feature's own files.
3. The unit files are written unconditionally (no systemd-availability guard as in the questions feature): they are plain files, the seam handles `daemon-reload`, and the tests need them under `NO_EXTERNAL`.
4. A stamps failure marks the feature `failed` (non-zero exit) instead of warning: a missing file in the set would leave the driver refusing every verb.
5. `TestFeatureModuleClosure` was changed to include required features' modules; `epic_driver_lib` cannot import alone, and `requires` is the installer's guarantee that its dependencies are present.
6. The guide describes `startup_dialogs` with a worked entry (validated by `epic-driver validate`), and links the alarm catalogue instead of copying it.
7. I did not touch `tasks/50_epic_driver/state.md` (modified by the lead before I started).

## Blockers

None. (Sandbox note: under `NO_EXTERNAL` the `~/.local/bin` symlink is not made by the installer, so for the "run from `~/.local/bin`" demonstration I created that one link by hand; the real seam does `ln -sf`.)

## Questions for User

None.
