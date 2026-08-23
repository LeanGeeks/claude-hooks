# Epic 21 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, done criteria and tests. This file owns **cross-task invariants**,
**ordering**, and the **two-repo interlock**.

**No Phase 0.** The decisions that would have needed one are locked in brd §2.2
and 21-01 §2: warn rather than refuse; key on `not is_tty` rather than `tracked`;
never count `CLAUDE_CODE_EFFORT_LEVEL` as a pin. The one genuinely open question
belongs to an optional task and is called out there (21-03, question 2: is effort
inheritance wanted by default at all?).

## Two repos

| Half | Repo | Where the spec lives |
|---|---|---|
| A — the `amux-spawn` warning | **this repo** (`~/.bin/claude-hooks`) | [21-01](./21-01-spawn-pin-warning_sonnet.md) |
| B — guard the deliberate absence of `CLAUDE_CODE_EFFORT_LEVEL` from `AMUX_ENV_ALLOWLIST` | **the fork** (`~/.bin/amux`, branch `feat/epic-10-amux-extensions`) | the fork's `tasks/02_effort_env_allowlist/02-01-guard-effort-env-absence.md` |
| B's decision record | **this repo** | [21-02](./21-02-allowlist-decision_human.md) |

**One spec per change, no duplication.** The fork's task is authoritative for
what changes in `amux`; 21-02 is authoritative for the decision behind it. The
precedent for driving fork work from this repo is epic 12
(`Type: upstream/external`).

**Nothing in this epic requires an install of `amux`.** That is deliberate — see
brd §3 H4. Only 21-01 needs an install at all, and it is the user-owned one
(`install-claude-config.sh`, no sudo).

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 21-01 | [amux-spawn pin warning](./21-01-spawn-pin-warning_sonnet.md) | done | — | The load-bearing half. Three files, eight tests, no policy. **Independently shippable**, no sudo. |
| — | fork `02-01` (guard the omission) | done | — | Lives in `~/.bin/amux`. A comment + a regression test; independent of 21-01, runnable in parallel by an agent in that checkout. |
| 21-02 | [Allowlist decision](./21-02-allowlist-decision_human.md) | done | fork `02-01` | **human** — closes the downstream proposal as refuted (or overturns the refutation with fresh measurement). No install unless it overturns. |
| 21-03 | [Effort inheritance by flag](./21-03-effort-inheritance-by-flag_sonnet.md) | todo · **optional** | 21-01 | The safe shape of what DW-68 item 2 wanted. **Ask before running** — its question 2 is a policy call. |

## Dependency graph

```
21-01 ─────────────► 21-03 (optional, and only after its two questions are answered)
fork 02-01 ────────► 21-02 (human: record the decision)
```

21-01 and the fork's 02-01 are independent roots; nothing in this epic blocks on
an `amux` install.

## Recommended order

1. **Ship 21-01 alone.** It is the half that matters (brd §1: it reports the
   *next* unpinned spawn site, which no audit can), it needs no root, and it is
   testable offline. Install it with `install-claude-config.sh` and live with it.
2. **The fork's 02-01 whenever convenient** — a comment and an assertion.
3. **21-02** once 02-01 is committed: one line in this Log, and tell the
   downstream entry (hyppie-flow DW-68 item 2) how it closed.
4. **21-03 only if asked.** Its question 2 ("is inheriting effort wanted by
   default?") is the operator's, and a wrong default is expensive in the
   `max`-flows-downhill direction.

## Cross-task invariants

1. **The warning never breaks a spawn.** stderr only; no exception it can raise;
   no change to any exit code; no subprocess, no filesystem. (brd §3 H1)
2. **Nothing here invents a model or an effort.** The launcher reports; the
   caller decides. A default would reproduce the original defect with a new
   author. (brd §2.2)
3. **An environment-carried effort is never a pin.** It cannot travel today, and
   if it could it would override the flag rather than support it. (brd §1
   finding 6; 21-01 §2 decision 2)
4. **A repo edit is not live.** `.claude/bin/amux-spawn` reaches `PATH` only via
   `install-claude-config.sh`; `/usr/local/bin/amux` only via `install-amux.sh`
   (sudo). Any claim that something "works now" must say which of those ran.
5. **Served model and effort are read from transcripts, never from a session's
   own report.** Every measurement in brd §1 was made that way; anything added
   to the record follows it.

## Log

- **2026-08-23 — epic created.** Filed from hyppie-flow's HF-027 (2026-08-22) and
  its follow-ups DW-68/DW-69 (2026-08-23), which measured brd §1's findings and
  re-verified both defects at the installed versions. Three bookkeeping
  corrections came out of that re-read and are folded in here: the `tracked` line
  is `amux-spawn:191` (not `:181`); the allowlist is in the **fork**, not this
  repo, at `:144` installed / `:173` on the branch; and the fork branch is 6
  commits ahead of the installed pin. Epic **20** is reserved by the fork's
  cross-repo reference to `20_codex_background_workers`, so this epic is 21.
- **2026-08-23 — half (B) inverted before anyone wrote code.** The epic was filed
  asking for `CLAUDE_CODE_EFFORT_LEVEL` to be *added* to the allowlist. Measuring
  the conflict case first — both polarities — showed the env var **overrides**
  `--effort` (brd §1 finding 6), which turns the addition from belt-and-braces
  into a silent override of every per-spawn pin. (B) is now a guard on the
  omission, 21-02 records the reversal, and 21-03 carries the safe shape of the
  capability. **No code was written in either repo during this sitting** —
  operator's instruction was specs only.
- **2026-08-23 — fork `02-01` done** (`aDorofeev/amux` `66ebd54`, log `db7e29d`). A comment at
  `AMUX_ENV_ALLOWLIST` giving the reason for the omission, plus a negative assertion in the existing
  propagation test. 398 tests green before and after — no behaviour change, the same 15 vars
  propagate. The guard was proven to bite by a reviewer independent of the implementer: added the
  var, saw red, reverted, confirmed green with an unchanged tree. Nothing installed, per brd §3 H4.
- **2026-08-23 — 21-01 done.** `extract_flag_value()` added to `amux_spawn_lib.py` with
  `extract_model_flag()` refactored to delegate to it (signature and behaviour unchanged); a warning
  block in `cmd_spawn` after the `inh_model` block, guarded by `not is_tty`; 8 cases in a new
  `TestSpawnPinWarnings`. Suite **724 passed** (baseline 716, +8), 1 pre-existing skip.
  Acceptance §4.1/§4.2 demonstrated against the repo copy: an unpinned non-TTY spawn printed one
  warning per unpinned knob **and still started the session**; the same spawn with `--model` and
  `--effort` printed neither. Invariant 3 holds in the code — `CLAUDE_CODE_EFFORT_LEVEL` is
  deliberately not consulted, with brd §1 finding 6 cited at the line. Note the deliberate asymmetry
  that a reader may mistake for a bug: `ANTHROPIC_MODEL` **does** count as a model pin (epic 12
  verified alt-model selection riding on it end-to-end) while the effort env var does **not** count
  as an effort pin — invariant 3 is about effort specifically, and the two vars are not symmetric.
  Review PASS with two LOW findings, neither fixed and both deliberately: one flags this file as a
  fourth changed path, which is the manager's bookkeeping rather than the implementer exceeding
  done criterion 5; the other observes that 3 of 8 cases survive neutering because they are negative
  (`assertNotIn`) or bare-helper tests. The neutering experiment is the load-bearing one and it
  passed — 5 of 8 went red with the block stubbed out, all green on revert.
- **2026-08-23 — 21-01 is LIVE on this machine.** `install-claude-config.sh` was re-run (the
  user-owned half, no sudo; settings backed up to
  `~/.claude/backups/settings.json.20260823_140446.bak`, and `model`/`effortLevel` among the keys it
  preserved). `~/.local/bin/amux-spawn` is now byte-identical to `.claude/bin/amux-spawn`. Naming
  the install is invariant 4's requirement, and this is the claim it licenses — acceptance §4.1/§4.2
  re-run against the **installed** binary on `PATH`, not the repo copy:
  - unpinned non-TTY spawn → both warnings printed, session still spawned, **exit 0**;
  - `--model=sonnet --effort=medium` → **neither** warning, session spawned.

  All three probe sessions (`tmp`, `tmp-2`, `tmp-3`) were killed and removed; `tmux ls` shows none
  left. `install-amux.sh` was **not** run and needs no running — nothing in this epic requires it
  (brd §3 H4), so the fork's six unrelated commits stay unshipped.
- **2026-08-23 — 21-02 decided: DW-68 item 2 is REFUTED. `CLAUDE_CODE_EFFORT_LEVEL` stays off
  `AMUX_ENV_ALLOWLIST`.** This is a decision, not a preference: the operator declined to accept the
  recorded finding on its own and asked for it to be re-measured first. It was, independently, on
  CLI **2.1.231** — env `high` + `--effort low` → served **high**; env `low` + `--effort high` →
  served **low**; both single-channel controls confirmed each channel works in isolation; served
  effort read from each child's transcript, never self-reported. Both polarities were needed, since
  one arm cannot tell *"env wins"* from *"the higher tier wins"*. Evidence:
  [21-02_remeasurement_report.md](../../agents_output/21-02_remeasurement_report.md). brd §1
  finding 6 therefore stands **re-confirmed, not merely cited**. Because the recommendation was
  accepted, no pin bump and no root-owned install follow (21-02 §"If the decision goes the other
  way" is moot); H3, the fresh-server asymmetry, remains **unverified** and is untouched by this.
  Downstream told: `hyppie-flow` `docs/backlog.md`, DW-68 entry, "ITEM 2 CLOSED" — worded so the
  entry is not re-opened from its original phrasing (21-02 done criterion 3).
- **2026-08-23 — 21-03 declined for now, and left `todo · optional` rather than closed.** Its
  question 2 is the operator's, and the operator answered it by deferring the whole task: ship
  21-01's warning and live with it before adding an inheritance whose wrong default is expensive in
  the `max`-flows-downhill direction. Nothing in 21-01 forecloses it — `extract_flag_value`, the
  generalization 21-03 depends on, lands with 21-01.
