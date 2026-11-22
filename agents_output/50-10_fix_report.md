# Fix Report - 50-10 Docs and installation

## LOW 1: prove "stamps written last"

**Change:** new test `TestEpicDriverFeature.test_stamps_are_written_after_a_later_feature_changes_a_covered_module` in `tests/test_unit_installer.py`. No production hook was added.

Seam: the copy-the-repo approach the existing reinstall test uses (`CLAUDE_INSTALL_SCRIPT_DIR`), plus a patched `install.sh` in that copy, run directly with `bash` (`run_installer` always runs the repo's script, so it cannot run a patched one). Temp HOME, `CLAUDE_INSTALL_NO_EXTERNAL=1`.

Steps:
1. A first run installs everything.
2. In the copy, `FEATURES` is reordered so `statusline` runs AFTER `epic-driver`. `statusline` is also dropped from `epic-driver`'s requires, because `assert_registry_integrity` rejects a feature placed before its requirement.
3. `statusline.py` in the copy is changed.
4. A `--yes` replay runs the patched script.

The test asserts that the log shows `epic-driver` installing before `statusline`, that the installed `statusline.py` equals the changed file, and that `stamps.json` has the new hash, which matches the installed bytes.

I first tried `keep-awake`. It did not work: hook modules are refreshed up front by the module closure, before any feature runs. `statusline.py` is copied by the feature's own step, so it is the right stamped file to use.

**Mutation (removing the final `_epic_driver_finalize_stamps` call):** backed up with `cp` to `/tmp/install.sh.bak50-10`, mutated, restored from the copy, `diff` clean.
- `TestEpicDriverFeature`: FAILED (failures=2): `test_a_replay_keeps_the_stamps_current` and the new test.
- The new test passes on the unmutated code.

## LOW 2: stamps.json wording

`docs/installer.md` (the "stale on purpose" paragraph is replaced) and `docs/epic-driver.md` (the stamp-mismatch sentence) now say, matching the code:
- It is written on every run where the feature is installed, or recorded as installed (the `keep` action). That includes `--only keep-awake` and `--yes` replays. It is written by the feature's own step and again at the end of the run. A `keep` run first re-copies the feature's own files.
- A `keep` needs at least one install or update in the run. An uninstall-only run leaves the manifest as it is.
- Feature off (not installed, not in the plan): no manifest is written or kept.
- Uninstall: `stamps.json` is removed with the feature's other files, and `~/.local/state/epic-driver/` is kept.
- A missing covered file at write time fails the feature.

The old claim that `--only telegram` leaves the manifest stale was wrong for an installed feature (it is kept and rewritten). It is removed. The `epic-driver.md` sentence that blamed a "shared module re-installed without the feature in the plan" is replaced as well.

## Test results

- `python3 tests/test_unit_installer.py`: Ran 224 tests, OK.
- Full suite, foreground, last: `python3 tests/run_all_tests.py`
  - Summary: `Ran 3056 tests in 469.062s` / `OK (skipped=2)`
  - Exit code: 0
  - Baseline was 3055 OK, 2 skipped; the +1 is the new test.

## git status --short

```
 M architecture.md
 M docs/installer.md
 M install.sh
 M tasks/50_epic_driver/state.md
 M tests/test_unit_installer.py
?? docs/epic-driver.md
```
(`agents_output/` is gitignored.) `install.sh` is byte-identical to its state before this fix (the mutation was restored). Nothing was committed.

## Questions for User

None
