# 21-02 — Close the "carry `CLAUDE_CODE_EFFORT_LEVEL` in amux" proposal

**Status:** todo · **Depends on:** the fork's `02-01` (the guard) · **Human task**
**Read first:** [brd.md](./brd.md) §1 findings 5–6, §3 H2/H3/H4 · the fork's
[`tasks/02_effort_env_allowlist/`](../../../amux/tasks/02_effort_env_allowlist/)

## Why this is human

Not because it is hard — because it is a **decision that reverses a written
proposal**, and because everything downstream of it (a pin bump, a root-owned
install, a release that carries six unrelated commits) is the operator's to
authorise. The engineering half is already specified and needs no install.

## The decision, and the evidence for it

The proposal, as filed downstream (hyppie-flow DW-68 item 2): *add
`CLAUDE_CODE_EFFORT_LEVEL` to `AMUX_ENV_ALLOWLIST`, so a profile can pin effort
the way it already pins a model; belt-and-braces for the per-spawn `--effort`
pins.*

**Measured 2026-08-23, and it refutes the proposal** (brd §1 finding 6): the env
var **overrides** an explicit `--effort`, in both polarities. Since the allowlist
feeds tmux `update-environment`, which is sampled at session creation and so
reaches every session spawned from then on, carrying the var means a
single stale export re-levels every descendant session — beating the flag each
spawn site passes, while the spawn command still reads as if it pinned the value.

⇒ **Recommendation: do not add it.** Record the omission as deliberate (the
fork's `02-01` does exactly that: a comment plus a regression test), and give the
capability its safe shape instead — [21-03](./21-03-effort-inheritance-by-flag_sonnet.md),
which inherits effort **as a flag**.

## Checks — before accepting the recommendation

Re-measure rather than trusting this file; it is one machine and one CLI version,
and the finding is counter-intuitive enough to deserve a second pair of hands.
Read the served effort out of the child's transcript — the newest `*.jsonl` under
`~/.claude/projects/<mangled-workspace-path>/` records `message.model` and a
top-level `effort` per assistant message. **A session that *says* it is running
at some effort is not evidence.**

```bash
cd "$(mktemp -d)"
CLAUDE_CODE_EFFORT_LEVEL=high claude --effort low  -p 'Reply with exactly: ok' </dev/null
CLAUDE_CODE_EFFORT_LEVEL=low  claude --effort high -p 'Reply with exactly: ok' </dev/null
# expect: served high, then served low — i.e. the env var wins both times
```

A full seven-arm harness, including the project-settings arms, is committed
downstream at `hyppie-flow/docs/analysis/dw-069-model-effort-precedence/probe.sh`.

## If the decision goes the other way

If the operator still wants the var carried (for instance because a future CLI
inverts the precedence), these are the facts that make it a release rather than a
line — none of them changed, they were simply not the deciding factor:

- `install-amux.sh:27` carries `AMUX_PIN="${AMUX_PIN:-9b05d10}"`; the fork branch
  is **6 commits ahead** of it and `/usr/local/bin/amux` is byte-identical to the
  pin, so the install ships the fork's codex-provider epic too (brd §3 H4).
- The install needs `sudo`; no agent session here may run it.
- Verify a candidate SHA the way the fork's own state.md documents:
  `git -C ~/.bin/amux merge-base --is-ancestor <SHA> feat/epic-10-amux-extensions`.
- H3 (fresh-server asymmetry) is still unverified and would need stating.

## Done criteria

1. A one-line decision, with its date and its reason, in [state.md](./state.md)'s
   Log — whichever way it goes.
2. If the recommendation is accepted: the fork's `02-01` is landed, and 21-01's
   comment about the env var still matches the reason (brd §1 finding 6).
3. The downstream entry that raised it (hyppie-flow DW-68 item 2) is told the
   outcome, so it is not re-opened from its original wording.
