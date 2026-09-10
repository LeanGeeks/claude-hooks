# 29-06 — Interactive tri-state selector

**Status:** todo · **Depends on:** 29-02 (registry, probes), 29-05 (so the plan
it produces can execute)
**Read first:** [brd.md](./brd.md) D1, D3, D5, D6, D14, D16 ·
[architecture.md](./architecture.md) §5, §2 · [state.md](./state.md) invariants
5, 6, 11 · `install-claude-config.sh:479-505` (the current printed opt-in boxes,
which this replaces)

> **Which file you edit:** `install.sh`. 29-02 §0 froze
> `install-claude-config.sh` as the pre-epic reference copy — every
> `install-claude-config.sh:NNN` citation below points into that frozen file and
> describes code as it was before the epic. Do not edit it. Run `install.sh` only
> against a temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`
> ([state.md](./state.md), bootstrapping hazard).

## Goal

The checklist: what the user actually sees. Detected state in, a confirmed plan
out, nothing asked after the confirmation.

## Scope

### 1. The checklist

Render as architecture §5.1 specifies. Plain printed output redrawn on each
toggle — no `dialog`, no `whiptail`, no cursor addressing, no new dependency
(brd §9). It must work over SSH on a machine carrying only `bash`, `jq` and
`python3`.

Each line shows: number, current action, title, and — on a continuation line —
what it writes outside `~/.claude`, from `feature_<id>_writes()`. That
disclosure **is** the consent step (brd D5); there are no separate consent
prompts, so a feature whose `_writes()` is non-empty must never render without
it.

Sub-toggles render indented under their feature and are addressed as `5a`, `9a`.

### 2. Tri-state cycling

Per brd D1, the offered states depend on the **probe**, not the manifest:

- not installed → `Install` ⇄ `Skip`
- installed → `Update` → `Keep` → `Uninstall` → `Update`

Initial selection: the manifest if present, else `feature_<id>_default()`
(brd D14 — `questions` and `daily-review` start at `Skip`; every other feature
starts at `Install`). A feature detected as installed starts at `Update`
regardless, so the default action on a re-run is "carry on as you were,
refreshed".

### 3. Dependencies in the UI

- Setting a feature to `Install` when its `_requires()` are unmet also selects
  those prerequisites, and prints one line naming them. Never silently.
- Setting a feature to `Uninstall` while an installed feature requires it is
  refused, naming the dependent (29-05 §5 enforces the same rule for the
  flag-driven paths).
- `amux-autowrap` and `profiles-autosource` are mutually exclusive; enabling one
  visibly clears the other (architecture §2).

### 4. `Keep` says what it means

When the user selects `Keep`, print the one-line consequence: wiring and external
surfaces are left alone, **shared hook modules are still updated** (brd D3,
29-05 §3). A user who reads `Keep` as "freeze this at the version I have" and is
not corrected will file a bug against a deliberate invariant.

### 5. Confirmation and the plan

`Enter` renders the plan (architecture §5.3 — the same rendering `--dry-run`
uses) and asks once. That confirmation is the **only** gate: after it the run is
unattended (brd D6), which matters because `amux` can block for up to 15 minutes
inside `install-amux.sh` (brd constraint 2.6).

`p` previews the plan without accepting. `q` exits 0 having changed nothing.

The plan lists every file, settings key and external surface that will change,
grouped by feature, with uninstalls called out separately — those are the
destructive ones and they should not be scannable past.

### 6. TTY detection

Interactive mode requires a TTY. No TTY and no `--yes` is an error (brd §5), not
a silent fallback to installing everything. Provide
`CLAUDE_INSTALL_ASSUME_TTY=0|1` so tests can drive both paths deterministically
(architecture §10.1 already sets it).

## Done when

- Every feature renders with its `_writes()` line; no out-of-tree write is
  reachable without appearing there and in the plan.
- Cycling matches brd D1 in both detected states.
- A fresh machine shows `questions` and `daily-review` at `Skip` and everything
  else at `Install`.
- A machine with a manifest shows the manifest's choices.
- A machine installed by the old script shows installed features at `Update`.
- Selecting `Install` on `telegram` with `permission-hooks` at `Skip` promotes
  the prerequisite and says so.
- Selecting `Uninstall` on `permission-hooks` with `telegram` installed is
  refused, naming it.
- Enabling `amux-autowrap` with `profiles-autosource` on clears the latter
  visibly.
- `q` and `--dry-run` both leave the machine byte-identical, manifest included.

## Tests

Extend `tests/test_unit_installer.py`. Drive the selector by piping a keystroke
script to stdin with `CLAUDE_INSTALL_ASSUME_TTY=1`, and assert on the rendered
output and the resulting plan rather than on the executed state — execution is
29-05's coverage.

- Cycling for both detected states; initial selection from default, from
  manifest, and from probe.
- Prerequisite promotion and uninstall refusal, asserting the message names the
  other feature.
- Mutual exclusion of the two bashrc sub-toggles.
- `_writes()` appears for every feature that has one — enumerate the registry in
  the test rather than hard-coding the list, so a new feature cannot be added
  without its disclosure.
- `q` and `--dry-run`: no manifest written, no backup created, no file changed.
- No-TTY without `--yes` exits non-zero with a message naming `--yes`.
