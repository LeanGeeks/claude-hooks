# 29-09 — Migration from the old installer, and docs

**Status:** todo · **Depends on:** 29-01 … 29-08
**Read first:** [brd.md](./brd.md) §6.2, §7, D15 ·
[architecture.md](./architecture.md) §3, §4, §8.4 · [state.md](./state.md)
invariant 6 · `install-claude-config.sh:940-1001` (the `config.toml` opt-in being
retired) · `architecture.md` (top level) `:49-53`, `:108`, `:509`, `:586` · `.claude/README.md`

## Goal

The path every existing user walks exactly once, and the documentation that stops
this epic from being folklore.

## Scope

### 1. First run on a machine the old script installed

This is brd §6.2 and success criterion 6. Probes find artifacts; there is no
manifest. Every feature detected as installed offers `Update / Keep / Uninstall`
at `Update`, so accepting the defaults reproduces the old behaviour — arrived at
by consent, and leaving a populated manifest behind.

Verify this against a real pre-epic layout rather than a hand-built one. The
fixture is sitting in the tree: 29-02 §0 froze the pre-epic script at
`install-claude-config.sh` and it has not been edited since. Run **it** into a
`tmp_home`, then run `install.sh` over the result and assert the end state
matches a fresh `--all --yes` install, modulo the manifest.

Capture the frozen script into the test as a fixture (copy it into the test's
temp tree, or read it from its git blob) **before** §6 deletes it, so the
migration test keeps working after the cutover.

That fixture is the whole point of the task. A migration path validated only
against a fixture the same author invented proves nothing.

### 2. Retire the `config.toml` opt-in

`[questions_listen] enabled` in `~/.config/claude-tg-relay/config.toml` is read
today at `:970-998` and is the only thing that enables the listener unit. Per brd
D15 / architecture §8.4 the `questions-listen` sub-toggle replaces it.

On first run:

- if the key is present and true, seed the sub-toggle from it and tell the user
  the key is now inert and where the setting lives instead;
- if present and false, seed `false`, same message;
- never leave two sources of truth. After migration the installer does not read
  the key again.

Do not edit the user's `config.toml` — it is theirs, it holds relay credentials,
and a stale inert key is harmless once it is announced.

### 3. Docs

**`docs/installer.md`** — new, and the file the installer's `--help` points at:
the ten features and four sub-toggles with what each writes; the tri-state model
and what `Keep` does and does not do (brd D3); the manifest and where it lives;
the full CLI; `enable`/`disable`; how to uninstall a feature; and the
`CLAUDE_INSTALL_NO_EXTERNAL` seam, documented as a testing aid so a future
contributor does not mistake it for a user-facing dry-run.

**Top-level `architecture.md`** — `:53` currently reads *"install-claude-config.sh
merges permissions + wires hooks into global settings.json"*, which stops being
the whole truth. Update it and the other install references (`:49`, `:108`,
`:412-418`, `:509`, `:586`) to point at `docs/installer.md`.

**`.claude/README.md`** — check and update the install instructions.

**`tasks/29_installer_interactive/brd.md`** — flip `Status:` to `done` when the
epic closes. Leave rev 0's Fact 1 in place; it is the historical record of why
the epic exists.

### 4. The bootstrapping hazard, documented

`docs/prompts/implementer.md:8` tells every future implementer that *"editing a
repo hook does NOT take effect until `install-claude-config.sh` re-runs"*. That
instruction now has a sharp edge: an agent re-running the installer to make a
hook live will hit an interactive prompt, and an agent running it non-interactively
without a manifest will hit the brd §5 error.

Add to `docs/installer.md`, and reference from `docs/prompts/implementer.md` §Step 5:
the way to refresh installed hooks non-interactively on a machine that is already
set up is `./install-claude-config.sh --yes`, which replays the manifest. Say
explicitly that an agent must never run `--all` on a developer's machine, because
that installs features the developer deliberately skipped.

### 5. Uninstall documentation

There has never been a way to undo this installer. Document, in
`docs/installer.md`: per-feature uninstall, what is deliberately never removed
(`profiles.toml`, `history.jsonl`, state stores, `/usr/local/bin/amux` — 29-05
§4), and that the live `tmux set -g` options are not reverted (29-03 §5).

### 6. The cutover: retire the frozen copy and the guard

29-02 §0 left the tree with two installers. This task collapses them, and it is
the last thing to do — after §1's fixture is captured and green.

1. **Remove the temporary real-`$HOME` guard** from `install.sh` (29-02 §0.1,
   marked `TEMPORARY — remove in 29-09`) along with its
   `CLAUDE_INSTALL_EPIC29_LIVE` override. If this ships, every real user's first
   run refuses. Grep for the marker; do not rely on memory.
2. **Decide the final filename and say so in the commit.** Two defensible
   choices, and this is the one place to pick:
   - keep `install.sh` and `git rm install-claude-config.sh` — the name is
     shorter and the script is no longer config-only, but every existing user's
     habit, every doc reference and any crontab line breaks at once;
   - `git mv install.sh install-claude-config.sh` over the frozen copy — nothing
     external breaks and no reference needs repointing, at the cost of a name
     that undersells what the script now does.

   **Recommendation: the second.** This epic is already changing what the command
   *does*; changing what it is *called* in the same release doubles the surface a
   user has to relearn, and the rename is available any time later as a one-line
   change plus a symlink. If you take the first, a compatibility shim at the old
   path that `exec`s the new one — not a bare deletion — is the minimum.
3. **Repoint every reference** to whichever name survives. Known set, verify with
   a fresh grep rather than trusting this list: `docs/prompts/implementer.md`
   (the epic-29 paragraph at `:8` **and** the Step 5 exception — both were added
   for the epic and both must go, not just be edited), `docs/prompts/reviewer.md`
   (same two places), `docs/prompts/permission-review-daily.md` step 3,
   `docs/permission-review-daily.md`, `shell/permission-review-daily.sh:89` (a
   `FATAL` message naming the script), `.claude/README.md`, top-level
   `architecture.md`, and this epic's own task files.
4. **Delete `state.md`'s bootstrapping-hazard section**, or rewrite it in the
   past tense. It describes a hazard that no longer exists once there is one
   installer again, and a stale safety rule teaches the next reader to ignore
   safety rules.

## Done when

- The real-old-installer migration fixture passes (§1).
- A machine with `[questions_listen] enabled = true` migrates to the sub-toggle
  and is told the key is inert.
- `docs/installer.md` exists and covers all ten features, four sub-toggles, the
  CLI, uninstall, and the seam.
- Top-level `architecture.md` and `.claude/README.md` no longer describe the
  installer as unconditional.
- `docs/prompts/implementer.md` §Step 5 tells agents to use `--yes`, and the
  epic-29 paragraphs added to `implementer.md:8` and `reviewer.md:8` are gone.
- Exactly one installer script exists in the tree.
- `grep -rn 'CLAUDE_INSTALL_EPIC29_LIVE\|TEMPORARY — remove in 29-09' .` returns
  nothing.
- A run against the real `$HOME` is no longer refused (the guard is gone) — assert
  this by running `--dry-run` with `HOME` unset from the harness override and
  checking the exit status, not by installing anything.
- No reference anywhere in the repo names an installer path that does not exist.
- `python3 tests/run_all_tests.py` passes in full.

## Tests

- The §1 migration fixture, as an actual test: run the pre-epic script from
  `git show`, then the new one, then diff against a fresh `--all --yes`.
- `config.toml` migration: true, false, absent, and malformed TOML.
- A docs test: every feature id and sub-toggle id in the registry appears in
  `docs/installer.md`. Enumerate from the registry, so a feature added later
  without documentation fails the suite.
- A source test: no file in the repo references an installer filename that is not
  present on disk. This is what catches a half-done cutover, and it is cheap —
  grep the tree for `install-claude-config.sh` and `install.sh`, and assert every
  hit resolves.
