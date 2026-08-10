# 10-06 — `rm`: reap a tracked session (registry end-of-life)

**Status:** todo · **Depends on:** 10-01 (handles), 10-03 (derived state; the
"no auto-cleanup" decision), 12 (amux `rm` behavior)
**Read first:** [architecture.md](./architecture.md) §6.0 (handle schema) + §6
(state machine). Downstream consumer:
`~/hyppie-flow/hyppie-flow/scripts/amux-reap.sh` and ADR-0027 in that repo
(`docs/decisions/0027-pm-tick-lifecycle.md`).

## Why (external driver)

hyppie-flow's PM loop (ADR-0027, 2026-08-10) runs its project-manager as
watchdog-spawned **ticks**. The watchdog predicate reads this layer's spawn
registry: a handle in `idle`/`terminated` under the workspace root means "a
verdict awaits reconcile" → spawn a PM tick. Reconcile must therefore **end a
handle's life**, or every reconciled session looks forever-due and the watchdog
spawns ticks in a loop. 10-03 deliberately shipped "no auto-cleanup — the
registry accumulates"; that decision stands — but there is no *explicit*
cleanup either. Today the consumer reaches into this layer's private state by
hand (`amux-reap.sh`: `amux stop` + `amux rm` + `rm -f ~/.amux/spawn/<name>.json`),
which is exactly the kind of cross-layer improvisation the spawn layer exists
to prevent.

## Goal

`amux-spawn rm <handle>` — first-class end-of-life for a tracked session: tear
down the session at the amux layer and delete the registry handle, so
`ls`/`ls --all` stop reporting it.

## Scope

- `rm <handle>`: resolve via `_resolve_handle` (name-global, cwd-independent —
  parity with `status`/`last`). Then:
  1. Derive state (10-03 semantics). `running`/`stuck` → **refuse** with the
     derived state and a hint, non-zero exit — unless `--force`. Reaping a
     live session is a kill; the caller must mean it. (The hyppie-flow
     consumer only ever reaps `idle`/`terminated`.)
  2. `amux rm <name>` if amux tracks the session — task 12's `cmd_rm` stops
     tmux, removes the registration, and deletes `<name>.meta.json`; tolerate
     "unknown session" (the tmux side may already be gone).
  3. Delete `~/.amux/spawn/<name>.json`.
  - Output: human one-liner; `--json` → `{name, state_at_rm, killed}`. Exit 0
    on success, 1 on no-handle (stderr message, parity with `status`/`last`).
- **Non-goals:** no bulk `--all-terminated` (YAGNI until a consumer asks); no
  auto-cleanup in `ls` (the 10-03 decision stands — cleanup stays explicit);
  no archiving of `last_message` (consumers that care log it before reaping —
  the hyppie-flow watchdog already does).

## Done criteria

- `rm` on an `idle`/`terminated` handle: registry file gone, `ls --all` no
  longer lists it, exit 0.
- `rm` on `running`/`stuck`: refuses without `--force`; with `--force` kills
  and removes.
- `rm` on an unknown handle: exit 1, stderr message.
- Unit tests beside the existing suites (`tests/test_unit_amux_reads.py`
  style, tmux stub): refuse-live, force-kill, idle-reap, unknown-handle.
- Downstream follow-up (happens in the hyppie-flow repo after this ships, and
  is noted in that script's header): `scripts/amux-reap.sh` reduces to
  `exec amux-spawn rm "$1"`.
