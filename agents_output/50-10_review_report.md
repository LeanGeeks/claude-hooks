# Review Report - 50-10 Docs and installation

## Verdict: PASS

No BLOCKER or HIGH issues. Two LOW issues.

Full suite, run first, in the foreground, before any mutation: `Ran 3055 tests in 510.273s` / `OK (skipped=2)`.
`bash -n install.sh` and `py_compile tests/test_unit_installer.py` are clean.

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| Feature title, writes, default off, `requires = amux statusline keep-awake questions`, probe | met | install.sh `feature_epic_driver_*` (about lines 1197-1208); default `skip`. `--only epic-driver` in the sandbox promotes amux, statusline, keep-awake, questions (and profiles, telegram, permission-hooks). |
| Files land where `.claude/bin/epic-driver` looks | met | `layout()` resolves `__file__`, so the `~/.local/bin` symlink resolves to `~/.claude/bin`. `installed` is true because `epic-driver/lib` exists and the script sits in `~/.claude/bin`. Prompts go to `~/.claude/epic-driver/prompts`, which is the lib's `ROLE_PROMPTS_DIR`. The lib files go to `epic-driver/lib`. `epic_driver_lib.py` goes to `hooks/`. |
| Three unit files match §22 | met | Compared line by line with §22: identical. The test `_EPIC_DRIVER_UNITS` asserts exact `splitlines()` equality. |
| daemon-reload, systemctl and `ln -s` only inside `ext_*` seams | met | `ext_systemd_daemon_reload`, `ext_systemd_disable`, `ext_symlink_add` and `ext_symlink_remove` are the only calls. No raw `systemctl` or `ln` in the feature code. |
| No unit enabled or started by install | met | No `ext_systemd_enable` call. The sandbox shows no `.wants` symlinks. Mutation M1 (an added enable call) is caught. |
| Uninstall stops, disables and removes the units and files; keeps `~/.local/state/epic-driver/` | met | Sandbox: uninstall removed `~/.claude/epic-driver`, `bin/epic-driver` and the units. The state dir `x` survived. The symlink removal and the three `disable` calls went through the seams. |
| `REQUIRED_HOOKS` / `MODULE_OWNERS` entries, `assert_registry_integrity` | met | Both added. No "Registry:" error in the install output. The existing registry tests pass. |
| Stamps cover exactly `STAMPED_FILES` | met | The 16 entries in `EPIC_DRIVER_STAMPED` equal the CLI's dict. `test_stamps_installer_list_equals_the_cli_list` guards drift. Hashes are of the installed bytes, read from the installed paths. |
| Stamps written last and on every run where the feature is on | met | `_epic_driver_finalize_stamps` runs after `build_and_write_settings`. It handles install, update and keep. The keep case refreshes the feature's own files first. |
| `docs/epic-driver.md` covers the §24 list | met | See the docs section below. |
| `docs/installer.md` and top-level `architecture.md` | met | Both updated. |

## Independent sandbox re-run (item 3)

Setup: a temp HOME, `--only epic-driver --yes`, `CLAUDE_INSTALL_NO_EXTERNAL=1`. Exit 0. `stamps.json` has 16 modules and revision `80ce028-dirty`. I created the symlink by hand, as the seam would.

1. `$SB/.local/bin/epic-driver validate <toy ledger>` with no override: valid, rc 0. Stamps agree.
2. Appended a line to the installed `keep_awake_hook.py`: `stamp_mismatch` refused, rc 3. The same refusal with `EPIC_DRIVER_ALLOW_UNSTAMPED=1` set. Restoring the file validates again.
3. Installing with a changed repo copy of `keep_awake_hook.py` via `--only keep-awake`. The feature goes to state `keep`, the feature's own files are refreshed, the stamps are rewritten, and the new hash matches the installed file.
4. Uninstall: as described above.

Everything in the implementer's report reproduced.

## Real machine, read-only (item 4)

- `systemctl --user list-unit-files | grep -i epic`: nothing.
- `~/.claude/bin/epic-driver`, `~/.local/bin/epic-driver` and `~/.claude/epic-driver/` do not exist. `epic_driver_lib.py` is not in `~/.claude/hooks`.
- `cmp` of the installed `amux_spawn_lib.py`, `claude_event_reducer.py` and `statusline.py` against the repo: identical.
- `context_mcp_lib.py` and `questions_mcp_lib.py` are not installed anywhere under `~/.claude`. The questions MCP server is registered with `uv run --script <repo>/questions-mcp/server.py` and `CLAUDE_HOOKS_REPO=<repo>`, so it runs from the repo. These two files are only copied to `~/.claude/epic-driver/lib` by this feature, which is off. This is expected.
- `~/.claude/install-manifest.json`: `epic-driver` skipped, `daily-review` skipped, and every other feature `installed`.
- Python imports of the installed `statusline`, `amux_spawn_lib`, `claude_event_reducer`, `questions_store`, `questions_listen_lib` and `roles_config`: fine.
- The reviewer's own sandbox HOME, `/tmp/rv_home.*`, was removed afterwards.

## Docs

- `docs/epic-driver.md` covers everything §24 lists:
  - D1, checks and exit codes, `$EPIC_DRIVER_SSH_OPTS`, and the windows-under-20-minutes rule.
  - Cards and approvals (map order, `mark --grant`).
  - The on/off procedure: `./install.sh --yes --with epic-driver`, then `epic-driver enable`, and disable, pause, uninstall.
  - Attaching with `amux-spawn a <name>`, and what an operator message does.
  - The status screen and `last_tick_at`.
  - The alarm catalogue link `architecture.md#162-catalogue`. The anchor resolves to "### 16.2 Catalogue".
  - `startup_dialogs`, with the id, match and key rules.
  - A plain "silent-stall gap" section.
- Every command and flag matches the CLI's argparse: validate, enable, disable, pause `--reason`, resume, status `--json`, tick `--dry-run`, mark `--grant`, `--question` and `--note`, and queue `--to` and `--file`. `--with` is a real installer flag.
- `docs/installer.md` has the inventory row, a "what it writes" section and a note on what uninstall never removes. Top-level `architecture.md` lists the lib, the CLI, the guide and the two prompts.

## Mutation checks (backup with `cp`, restored, `diff` clean)

Baseline: `TestEpicDriverFeature` 9 tests, OK. All mutations were on `install.sh`.

| Mutation | Result |
|---|---|
| M1: add `ext_systemd_enable` to install | caught by `test_on_places_every_file_and_the_three_units_and_enables_none` |
| M2: uninstall `rm -rf` the state dir | caught by `test_uninstall_removes_files_and_units_and_keeps_the_state_directory` |
| M3: remove the final `_epic_driver_finalize_stamps` call | caught by `test_a_replay_keeps_the_stamps_current` only (see LOW 1) |
| M4: change `KillMode=process` | caught by the unit-content assertions |
| M5: hash the wrong bytes | caught by two tests |

## Issues Found

### Issue 1: [severity: LOW]

- **File:** tests/test_unit_installer.py, `test_reinstall_after_another_features_module_changed_updates_the_manifest`
- **Problem:** The test does not prove "written last". `epic-driver` is the last entry in `FEATURES`, so the in-feature stamp write already sees the refreshed shared module. With the finalizer disabled (M3), this test still passes. Only the replay (keep) test fails.
- **Fix:** Optional. Add a test where the shared module is refreshed after the feature's own install step, or accept that the ordering is guaranteed by registry order and the finalizer covers the keep case.

### Issue 2: [severity: LOW]

- **File:** docs/installer.md (the paragraph "A run that installs one of those shared modules while `epic-driver` is *not* in the plan ... leaves the manifest stale on purpose"); docs/epic-driver.md section 1.
- **Problem:** The wording is broader than the behaviour. I tested `--only keep-awake` with epic-driver already installed. The feature takes the `keep` action and the stamps are rewritten. The stale case only arises if the feature is not recorded as installed, and then there is no manifest either. A reader could expect a stale manifest more often than it happens.
- **Fix:** Tighten the sentence: the manifest is refreshed on any run where the feature is recorded as installed, including `--only <other>`.

## Code Quality Notes

- `_epic_driver_place_files` is reused by both install and the keep path. Errors set the feature to failed through `_epic_driver_fail`.
- If a prerequisite feature fails, the missing file makes the stamp write fail loudly, naming the files. That is the safe behaviour.
- Docs say `--only epic-driver` pulls prerequisites in. Confirmed.

## Questions for User

None.
