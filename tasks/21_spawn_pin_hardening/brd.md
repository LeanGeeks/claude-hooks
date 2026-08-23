# Epic 21 — No automated spawn runs on an unpinned model or effort

**Status:** todo · **Owner:** Anton · **Created:** 2026-08-23 · **Rev:** 1
**Type:** this repo + the amux fork (two repos — see [state.md](./state.md))

> Broken down into tasks — see [state.md](./state.md) for ordering, the cross-repo
> interlock and the invariants. The amux-side half has its own task in the fork:
> [the fork's epic 02 task directory](../../../amux/tasks/02_effort_env_allowlist/)
> (`~/.bin/amux/tasks/02_effort_env_allowlist/`).

**Provenance.** Filed from downstream evidence: the hyppie-flow project audited
every automated spawn it owns (its hotfix HF-027, 2026-08-22) and re-measured the
tooling side the next day (its backlog items DW-68 / DW-69, 2026-08-23). Every
claim in §1 was measured on this machine; the two that were not are labelled
**unverified** and stay that way until someone measures them.

## 1. Problem & thesis

A project that spawns Claude sessions from automation — watchdogs, schedulers,
`amux-spawn` chains — wants those sessions to run on a model and an effort level
it *chose*. HF-027 found that none of its spawn sites pinned either, and that the
tooling's own documentation was wrong about why they were nonetheless "fine".

**Measured** (five probe children, each reporting the model and effort it was
actually served, read out of its own transcript rather than self-reported):

1. A spawn passing neither `--model` nor `--effort` serves **whatever the
   operator's last interactive `/model` wrote into the harness settings file**.
   That value floats: it moved twice inside one day.
2. `--profile` pins **neither** knob. The `claude` profile carries only the
   `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` alias→id map and no
   `ANTHROPIC_MODEL`, so it selects nothing. (A profile *can* pin the model —
   `claude_glm_env` does, via `ANTHROPIC_MODEL=glm-4.7`, verified end-to-end in
   epic 12 — but the default profile does not.)
3. The **model** inherits only from a parent that passed `--model` itself: by
   flag, through `inherited_model_flag()`, never by environment.
4. **Effort** has no inheritance path through this tooling at all.
5. (2026-08-23, this epic's addition) **The CLI does honour
   `CLAUDE_CODE_EFFORT_LEVEL`**: a session started with it set to `high` against
   a `medium` machine default serves `high`. So finding 4 is not a CLI
   limitation — the var dies at *our* boundary, `AMUX_ENV_ALLOWLIST`, which
   carries 15 vars and not that one.
6. (2026-08-23, and it inverted this epic's second half) **That env var
   overrides an explicit `--effort` flag.** Both polarities, because one arm
   cannot tell *"env wins"* from *"the higher tier wins"*:

   | env `CLAUDE_CODE_EFFORT_LEVEL` | `--effort` | served |
   |---|---|---|
   | `high` | — | `high` |
   | — | `high` | `high` |
   | `high` | `low` | **`high`** |
   | `low` | `high` | **`low`** |

   The usual CLI-args-beat-environment layering does not hold for this pair.

The downstream fix was to state both values at every spawn site it owns. That
closes the sites that exist **today** and does nothing for the next one somebody
adds — including in other projects on this machine, which get no audit at all.

**Thesis.** The non-TTY path *is* the agent path — `amux-spawn` decides exactly
that at `tracked = (not is_tty) or wait_mode`. So the launcher already knows,
at spawn time, when it is serving automation rather than a human. Two changes
make an unpinned automated spawn **loud** instead of silent:

- **(A) `amux-spawn` warns** when a non-TTY spawn resolves to no pinned model or
  no pinned effort. It is the belt to HF-027's braces: HF-027 fixed the sites
  that exist, this reports a new one the first time it runs.
- **(B) amux keeps `CLAUDE_CODE_EFFORT_LEVEL` *out* of its env allowlist — on
  purpose, and says so.** This is the half that inverted. The downstream entry
  that prompted this epic (DW-68 item 2) asked for the opposite: add the var, so
  a profile can pin effort the way it can already pin a model. Finding 6 refutes
  that. `AMUX_ENV_ALLOWLIST` feeds tmux `update-environment`, which copies each
  listed var into **every session it creates from then on** (and every pane
  inside them; epic 12 E1) — so a carried
  `CLAUDE_CODE_EFFORT_LEVEL` would let one stale export in the operator's shell
  silently re-level every descendant session, **overriding the explicit
  `--effort` that (A) exists to encourage**, while each spawn command still reads
  as if it pinned the value. The omission is a protection. What is owed is a
  comment saying so and a test keeping it that way, so the next reader does not
  "complete" the allowlist.

**(A) is the load-bearing half.** (B) is now a guard rather than a feature, and
it costs one comment plus one assertion. The capability DW-68 actually wanted —
effort that survives a spawn chain without being restated — has a safe shape
that does not go through the environment at all: task 21-03.

## 2. Scope

### 2.1 In scope

- A stderr warning from `amux-spawn spawn` on the non-TTY path when the effective
  model or the effective effort is unpinned (task 21-01).
- Guarding the deliberate absence of `CLAUDE_CODE_EFFORT_LEVEL` from amux's
  `AMUX_ENV_ALLOWLIST` — specified and implemented in the fork's own epic 02.
- The decision record that closes DW-68 item 2 as refuted, plus the deployment
  facts for whoever eventually ships a fork build for other reasons (task 21-02,
  human).
- **Optional:** effort inheritance down a spawn chain **by flag** — the safe
  shape of what DW-68 item 2 was reaching for (task 21-03).

### 2.2 Out of scope — deliberately

- **Refusing an unpinned spawn.** `amux-spawn` is on `PATH` for every project on
  this machine; a hard refusal would break other repos' automation on upgrade,
  and the failure mode it prevents (a session on the wrong model) is recoverable
  while a launcher that will not launch is not. Warn. If a project wants a
  refusal, it can grep its own stderr — or ask for an opt-in flag in a later
  revision, with a default of warn.
- **`amux-spawn` inventing a default model or effort of its own.** A launcher
  that silently substitutes its own choice reproduces the original defect with a
  different author. It reports; the caller decides.
- Any opinion about *which* model or effort a caller should pick. That is the
  caller's policy (downstream: hyppie-flow workflow §2.2).
- Retro-fitting the warning into `amux` itself. `amux` does not know whether its
  caller is automation; `amux-spawn` does.

## 3. Constraints & hazards

- **H1 — the warning must not be able to break a spawn.** It runs on the path
  every automated session takes. Pure list/dict/env reads, no subprocess, no
  filesystem, no exception it can raise, no effect on the exit code, stderr only.
  A guard whose failure mode is "no session starts" is worse than the defect it
  reports (downstream precedent: a naive `set -e` + `symbolic-ref` guard that
  aborted printing nothing).
- **H2 — an env-carried effort would beat the flag, not lose to it.** This was
  written the other way round when the epic was filed ("acceptable, because the
  flag still wins") and measuring it flipped the conclusion — finding 6. Anyone
  proposing to add the var must re-measure both polarities first, and must state
  what happens to the pins in §1 when a stale export exists. The same caution
  applies to the model half: `ANTHROPIC_MODEL` vs `--model` in conflict is
  **unmeasured**; do not assume it mirrors effort in either direction.
- **H3 — fresh-server asymmetry (unverified).** The allowlist is consulted via
  `update-environment` on an **already-running** tmux server; on a fresh server
  `new-session` seeds the pane from the spawner's environment directly. So the
  same spawn can behave differently depending on whether a server was already up.
  Consistent with tmux semantics and with amux's own comment at the allowlist,
  **not re-measured**. Whatever lands in (B) should state it rather than imply
  uniform behaviour.
- **H4 — no amux change is ever a one-line deploy.** The fork's branch
  `feat/epic-10-amux-extensions` is **6 commits ahead** of the pin
  `AMUX_PIN=9b05d10` that `install-amux.sh` defaults to, and the installed
  `/usr/local/bin/amux` is byte-identical to that pin (50 KB installed vs 101 KB
  on the branch). Shipping *anything* from the fork therefore also ships those
  six commits — its codex-provider epic — through a root-owned install. Epic 12
  said it in advance: *"the deploy ships the whole fork build, not a patch."*
  This is why (B) is deliberately a **source-level guard that needs no install**,
  and why 21-02 — the only task that would touch a deploy — is human.
- **H5 — a repo edit is not live.** `.claude/bin/amux-spawn` reaches `PATH` only
  when `install-claude-config.sh` re-runs (step 3 copies it to
  `~/.local/bin/amux-spawn`). No `sudo` for this half.

## 4. Acceptance

The epic is done when, on this machine:

1. `amux-spawn spawn` from a non-TTY with neither knob pinned prints one warning
   per unpinned knob to stderr, starts the session anyway, and returns the exit
   code it would have returned before.
2. The same spawn with `--model <alias> --effort <level>` prints neither.
3. `python3 tests/run_all_tests.py` is green, with new cases covering both.
4. The fork's allowlist carries a comment explaining why
   `CLAUDE_CODE_EFFORT_LEVEL` is absent, and a test that fails if someone adds
   it. (The fork's epic 02; nothing needs installing for the test to guard the
   source.)
5. `state.md` records DW-68 item 2 as refuted, with the measurement, so the next
   person to read the entry does not re-open it from the original wording.
