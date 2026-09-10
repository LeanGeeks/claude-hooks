# 29-07 — Non-interactive CLI and `enable` / `disable` subcommands

**Status:** todo · **Depends on:** 29-02 (registry, manifest, probes), 29-03
(external seam), 29-05 (executors)
**Read first:** [brd.md](./brd.md) §5, D15, §7 criterion 2 ·
[architecture.md](./architecture.md) §9, §5.2, §5.3 · [state.md](./state.md)
invariants 5, 11 · `tasks/29_installer_interactive/brd.md` §1 (rev 0's Fact 1 —
this task is what discharges it)

> **Which file you edit:** `install.sh`. 29-02 §0 froze
> `install-claude-config.sh` as the pre-epic reference copy — every
> `install-claude-config.sh:NNN` citation below points into that frozen file and
> describes code as it was before the epic. Do not edit it. Run `install.sh` only
> against a temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`
> ([state.md](./state.md), bootstrapping hazard).

## Goal

Everything the checklist does, without a terminal — plus the per-toggle commands
that make changing one's mind cheap.

## Scope

### 1. Flags

Per brd §5:

```
--all                  every feature on, sub-toggles at their defaults
--only a,b,c           exactly these
--with x --without y   adjust the manifest/default selection
--yes                  non-interactive
--list                 feature table with detected state, exit 0
--dry-run              render the plan, touch nothing
--help
```

Argument parsing runs **before** the dependency checks (architecture §9), so
`--help` and `--list` work on a machine missing `jq`. Today the `jq` check at
`:104-107` exits before any argument is read.

Three combinations are errors, not conveniences:

- no TTY and no `--yes`;
- `--yes` with neither a manifest nor an explicit selection;
- `--only` naming an unknown feature (list the valid ids).

The old script's behaviour — install everything without asking — must not be
reachable by accident. `--all` is how you ask for it on purpose.

### 1.1 `--yes` is a replay, and something unattended depends on it

`--yes` with a manifest present means **re-apply exactly what this machine
already chose** (brd §6.3). Spell that out in the implementation and in `--help`,
because it is load-bearing for a caller that is not a human:

- Every feature's action comes from its manifest `state`. `"skipped"` stays
  skipped — a recorded skip is a decision, and a re-run is not an opportunity to
  revisit it. Sub-toggle values in `options` are replayed the same way.
- The D14 defaults are consulted **only** for a feature id absent from the
  manifest entirely (a feature the epic added after that manifest was written).
  Report those as newly-offered rather than folding them in silently.
- `--with` / `--without` adjust the replayed selection; that is the supported way
  to change one thing non-interactively without re-running the checklist.

The caller that depends on this is `docs/prompts/permission-review-daily.md` step
3: once `daily-review-cron` is enabled, a 06:15 job runs the installer unattended
to propagate user-scope permission changes (29-08 §2.1). If `--yes` fell back to
defaults there, that job would quietly install every feature the user had
declined, on a machine nobody is looking at. The already-specified error — `--yes`
with neither a manifest nor an explicit selection — is what makes the failure
loud instead; do not soften it into a warning.

### 2. `--list`

Renders every feature with its detected state, its default, its `_writes()` and
its sub-toggle states. Read-only, exit 0, safe on any machine. This is the
command a user runs to answer "what is this machine actually running?", so it
must be truthful about state it detects rather than state it intended.

### 3. `--dry-run`

Renders the plan (the same rendering the selector's `p` uses — architecture §5.3)
and exits 0 **having written nothing**: no manifest, no backup, no settings
change, no external surface. A dry run that creates a backup file has already
failed the test.

### 4. `enable` / `disable`

Per brd D15 — this is rev 0's Fact 1, discharged. Enabling the questions listener
becomes:

```
install-claude-config.sh enable questions-listen
```

instead of hand-editing `~/.config/claude-tg-relay/config.toml` and re-running the
entire installer.

The four sub-toggle ids are `amux-autowrap`, `profiles-autosource`,
`questions-listen`, `daily-review-cron` (architecture §2).

Both subcommands:

- load the manifest and verify the parent feature is installed — if not, exit
  non-zero **naming the parent**, never cascading into installing it;
- apply only that one surface, through 29-03's `ext_*` functions;
- rewrite the manifest;
- touch nothing else — no files, no settings, no other feature. That narrow blast
  radius is the entire reason the subcommand exists.

An unknown id exits non-zero listing the valid ones. `enable` on an
already-enabled toggle is a no-op that says so.

Enabling `amux-autowrap` disables `profiles-autosource` and reports it
(architecture §2, mutual exclusion).

## Done when

- `--help` and `--list` succeed with `jq` removed from `PATH`.
- `--list` on a machine installed by the old script reports the true detected
  state of all ten features and four sub-toggles.
- `--dry-run` leaves the machine byte-identical, including no new file in
  `$BACKUP_DIR`.
- The three error combinations exit non-zero with an actionable message.
- `--yes` against a manifest containing at least one `"skipped"` feature and one
  disabled sub-toggle reproduces that exact state — nothing installed that the
  manifest did not record as installed.
- A feature id present in `FEATURES` but absent from the manifest is reported as
  newly-offered by a `--yes` run rather than installed by default.
- `enable questions-listen` with `questions` installed enables the unit and changes
  nothing else — asserted by diffing the whole `$HOME` before and after, modulo
  the manifest and the unit's enabled state.
- `enable questions-listen` with `questions` **not** installed fails, naming
  `questions`.
- `enable amux-autowrap` with `profiles-autosource` on flips both and says so.
- `--only telegram` promotes `permission-hooks` (29-05 §5 / 29-06 §3 rules apply
  identically here).

## Tests

Extend `tests/test_unit_installer.py`.

- Flag parsing: each error combination; unknown feature id; `--only` with
  dependency promotion.
- `--help` / `--list` with a `PATH` lacking `jq`.
- `--dry-run` byte-identity: snapshot the whole `tmp_home` tree before and after
  and assert equality. This is a stronger assertion than checking individual
  files, and it is the one that catches a stray backup or a manifest write.
- `enable`/`disable` for all four sub-toggles: parent-not-installed refusal,
  idempotency, mutual exclusion, and a whole-`$HOME` diff proving nothing else
  moved.
- `--yes` replay: run `--all --yes`, then `--yes` alone, and assert the second
  run is a no-op producing an identical manifest.
- `--yes` replay of a **partial** selection: `--only statusline --yes`, then
  `--yes` alone; assert the nine unselected features are still not installed.
  This is the assertion that protects the unattended daily-review path — write it
  even though it looks like a duplicate of the one above, because that one passes
  when everything is installed and cannot catch a defaults fallback.
- A hand-edited manifest with an unknown feature id, and one missing a known id:
  neither crashes, the second reports newly-offered.
