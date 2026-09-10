# 29-02 — Feature registry, manifest, probes, test harness

**Status:** todo · **Depends on:** —
**Read first:** [brd.md](./brd.md) §4, D1, D2, D14 ·
[architecture.md](./architecture.md) §2, §3, §4, §10 · [state.md](./state.md)
invariants 2, 3, 4 and **the bootstrapping hazard section** ·
`install-claude-config.sh` (the whole file — it is what you are restructuring,
and after §0 it is the frozen reference copy you keep reading) ·
`tests/run_all_tests.py:38-75` (`TEST_MODULES`) ·
`tests/test_amux_pin.py` (the only precedent for testing a shell script)

## Goal

The data model the rest of the epic builds on: one verb-set per feature, the
module-owner map, the manifest, the probes, and the first automated coverage
`install-claude-config.sh` has ever had.

**This task adds no interactivity and changes no behaviour.** At the end of it,
the script still installs everything it installs today, by the same means, when
run with no arguments — but it does so *through* the registry, and it can report
what it detects. Behaviour changes land in 29-05.

## Scope

### 0. First commit: freeze the old script, build in a new one

**Do this first, before touching a line of installer code.** Not for commit
hygiene — the manager's committer lands this task as one commit either way — but
because the protection *is* the fact that `install-claude-config.sh` is never
edited. Copy first, then work only in the copy, and that holds by construction.

```bash
cp install-claude-config.sh install.sh   # a copy, not a git mv — both paths exist
chmod +x install.sh
git add install.sh
```

`install-claude-config.sh` is now **frozen** for the rest of the epic: nothing in
29-02 … 29-08 may edit it. All epic work happens in `install.sh`. Task 29-09
deletes the frozen copy and repoints the documentation.

Add a header comment to the frozen copy, directly under the shebang, saying so:

```bash
# FROZEN for epic 29 (tasks/29_installer_interactive/). This is the pre-epic
# installer, kept working and unmodified so that the documented workflow
# ("re-run ./install-claude-config.sh to make a hook edit live") stays safe
# while install.sh is under construction. Edit install.sh, not this file.
# Task 29-09 deletes this copy.
```

Three reasons this is not bureaucracy, and you should not skip it:

1. Every reference the project makes — `docs/prompts/implementer.md`,
   `reviewer.md`, `docs/prompts/permission-review-daily.md:251`, the developer's
   own habit — names `install-claude-config.sh`. Keeping that name pointing at a
   working script means the *default* action stays safe for anyone who never
   reads `state.md`.
2. Every later task in this epic cites line numbers into
   `install-claude-config.sh` as "read first" refs (29-03 `:536-602`, 29-04
   `:646-794`, 29-08 `:451`). Those citations describe **pre-epic** code. Frozen,
   they stay correct for the whole epic instead of rotting the moment you finish
   this task.
3. 29-09 has to validate the migration path against the real pre-epic script.
   Freezing it is what makes that fixture exist.

### 0.1 The real-`$HOME` guard

`install.sh` gets a guard at the top, before argument parsing, for the duration
of the epic:

- If `HOME` is the invoking user's real home directory (compare against
  `getent passwd "$(id -un)"`, not against `$HOME` itself), refuse to run.
- Exit non-zero with a message that names the two ways forward: run against a
  temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`, or run the frozen
  `./install-claude-config.sh` if what you actually wanted was to make a hook
  edit live.
- `CLAUDE_INSTALL_EPIC29_LIVE=1` overrides it, for 29-10's human verification.

Mark it unmistakably:

```bash
# TEMPORARY — remove in 29-09. Guards the developer's machine while install.sh
# is under construction. See tasks/29_installer_interactive/state.md.
```

This is belt-and-braces with the frozen copy: the copy protects anyone who runs
the *documented* command, the guard protects the one case the copy does not
cover — an agent deliberately running the script it has just edited. 29-09's
"Done when" includes deleting it; if it ships, every user's first run fails.

### 1. The registry

Implement `FEATURES` and the nine `feature_<id>_*` verbs exactly as
architecture §2 specifies, for all ten features. Registry order must satisfy
every `_requires()`.

For this task, `feature_<id>_install` may simply call the existing code path for
that feature, lifted out of the current straight-line script and into the
function. Do not redesign what each one does — move it. `_uninstall` bodies are
29-05's; stub them to a clear "not implemented until 29-05" error rather than a
silent no-op (`docs/prompts/implementer.md` critical rule 1: no stubs that
pretend to work — an explicit failure is not a stub).

### 2. `MODULE_OWNERS` and startup assertions

Implement the map from architecture §2.1. Then assert at startup, before any
work:

- every key of `MODULE_OWNERS` appears in `REQUIRED_HOOKS`
  (`install-claude-config.sh:163`) and vice versa — a mismatch is a hard error
  naming the offending module, because the failure it prevents is a hook file
  silently never being installed;
- every id in every `_requires()` exists in `FEATURES`;
- every feature appears in `FEATURES` after all of its `_requires()`.

These are cheap and they are the difference between a registry that rots and one
that cannot.

### 3. The manifest

`~/.claude/install-manifest.json`, shape per architecture §3. Implement read,
write and validate:

- Unknown `schema` → warn and ignore, never parse optimistically.
- `repo` differing from `SCRIPT_DIR` → honour, but warn that `daily-review` and
  the MCP registrations embed absolute paths that are now stale.
- Absent → not an error; that is a first run (brd §6.1/6.2).
- Written **once**, at the end of a successful run, in full. Never incrementally
  mutated mid-run, so an aborted run leaves the previous manifest intact.

Reuse the existing validate-then-replace discipline (`install-claude-config.sh:1004-1021`):
build, `jq empty`, write, re-validate.

### 4. Probes

`feature_<id>_probe` for all ten, per architecture §4, plus the four sub-toggle
probes. Every probe checks **artifact and wiring**, not artifact alone — a
`statusline.py` on disk with no `.statusLine` key is *not installed*, and getting
this wrong breaks brd success criterion 6 for every existing machine.

Probes must be side-effect-free and must not require the manifest, the repo, or
the network. `--list` (29-07) will render them, but this task must expose them.

### 5. Test harness

Create `tests/test_unit_installer.py` and register it in
`tests/run_all_tests.py:38` as `unit_installer`. Establish the harness every
later task extends, per architecture §10.1:

- a `tmp_home` fixture and a helper that runs the installer with `HOME` pointed
  at it, `CLAUDE_INSTALL_NO_EXTERNAL=1`, and captured output;
- helpers to read back `settings.json`, `~/.claude.json` and the manifest.

`CLAUDE_INSTALL_NO_EXTERNAL` is 29-03's to implement. For this task, the harness
sets it and the tests confine themselves to features that write nothing outside
`~/.claude` (`statusline`, `permission-hooks`, `profiles`,
`permissions-allowlist`, `context-mcp`). Do not write tests that exercise
crontab, systemd, tmux or `install-amux.sh` — architecture §10.2 lists exactly
why, and 29-03 is what makes them safe.

## Done when

- `install-claude-config.sh` is byte-identical to its pre-epic content except for
  the frozen-header comment, and `git log -- install-claude-config.sh` shows no
  other change from this task.
- `install.sh` run against the real `$HOME` without `CLAUDE_INSTALL_EPIC29_LIVE=1`
  refuses, non-zero, naming both alternatives.
- The script run with no arguments produces the same end state as `main` does
  today, via the registry.
- The three startup assertions fire on a deliberately broken registry and pass on
  the real one.
- Probes on a clean `HOME` report all ten not-installed; after a full run, all
  ten installed (bar any skipped for a missing dependency such as `uv`).
- Removing `.statusLine` from `settings.json` by hand flips the `statusline`
  probe to not-installed while the files remain.
- The manifest round-trips: written, re-read, unknown-schema rejected.
- `python3 tests/run_all_tests.py --module unit_installer` passes.

## Tests

In `tests/test_unit_installer.py`:

- Registry integrity: the three assertions, each with a deliberately broken
  fixture.
- Probe matrix: clean home → none installed; after `--only statusline` → only
  `statusline`; artifact-without-wiring → not installed.
- Manifest: round-trip; unknown schema warns and is ignored; a run that aborts
  part-way leaves the prior manifest byte-identical.
- No test in this module may run without `CLAUDE_INSTALL_NO_EXTERNAL=1` set —
  assert it in `setUp` so a later contributor cannot omit it silently.
- The real-`$HOME` guard: with `HOME` left at the real value and no override, the
  script exits non-zero and writes nothing. Run this one with a `tmp_home`-backed
  assertion that no file changed, so the test itself cannot be the thing that
  edits the developer's machine.
- A source test in the spirit of `tests/test_amux_pin.py`: assert
  `install-claude-config.sh` still carries the FROZEN header, so a later task in
  the epic cannot quietly start editing it.
