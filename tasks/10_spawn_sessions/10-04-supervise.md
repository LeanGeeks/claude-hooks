# 10-04 — Supervise (fan-out): `--wait` + background-notify

**Status:** todo · **Depends on:** 10-01 (spawn), 10-02 (state), 10-03 (status)
**Read first:** [brd.md](./brd.md) §2 (topologies) + [architecture.md](./architecture.md)
§3–§4 and decisions D-Handoff (chains do NOT use this), D-State.

## Goal

Give a **live orchestrator** (the secondary, fan-out topology) two ways to learn a
spawned session reached first-turn idle: block inline (`--wait`), or be notified
asynchronously via the harness's native background-task channel (`--notify`).

## Scope

- `spawn … --wait [--timeout T]`: after creating the tracked session, **block**
  until it reaches `idle` (handle `state: idle`, i.e. a `Stop` with empty
  `background_tasks`) **that occurs after the seeded turn**, then print its
  `last_message` to stdout. Honor `--timeout` (return a clear timeout result).
- `--notify`: not a new mechanism — document and support the pattern where the
  orchestrator runs `amux-spawn spawn … --wait` as a **background Bash** task
  (`run_in_background`); the harness then emits its standard completion
  notification, and the orchestrator `Read`s the output-file for the result.
  Ensure `--wait`'s stdout is exactly the payload worth capturing there.

## Implementation hints / watch-outs

- `--wait`/`--notify` imply a **tracked + detached** session (override TTY-attach):
  the caller awaits a result, it does not attach. Detach is via amux `--no-attach`
  (task 12 E5), even at a TTY — not by relying on `attach-session` failing.
- **False-idle guard:** do not return on a boot-time / pre-seed idle. The §6 state
  model already covers this — a session with no `Stop` yet is `active` (`state ==
  spawning`) ⇒ reads `running`, never `idle` — so polling `status` until `idle` is
  sufficient. (Belt-and-suspenders: also require a `running`→`idle` transition / ≥1
  assistant message.)
- This is **consumer-side polling of the handle / `status`** — fine, and NOT the
  "producer polling" we deferred.
- `idle` here means truly idle (empty `background_tasks`); if the seeded turn left a
  background child running, wait for it to drain (matches D-Bg). **The experiment (§2.2)
  confirms background completion fires a fresh `Stop` with `background_tasks: []`** — so
  polling `status`/the handle until idle drains correctly and `--wait` will not hang.
  (The output-file reconciliation in §6 is a defensive fallback only.)
- Fan-out only. Chains (D-Handoff) deliberately do not block — keep `--wait`
  opt-in; default spawn stays fire-and-return.
- Keep timeout behavior distinct from `stuck`: `--timeout` is the caller's patience;
  `stuck` is a separate observable.

## Done criteria

- [ ] `spawn --wait -- "<prompt>"` blocks, then prints the session's first-turn
      final message; exits promptly after idle.
- [ ] A prompt that launches a background child causes `--wait` to return only after
      the child drains (true idle), not at the first `Stop`.
- [ ] `--timeout` returns a clear, distinguishable result without hanging.
- [ ] Run as background Bash, completion produces a harness notification whose
      output-file contains the result (manually verified).

## Testing

- `--wait` on a quick prompt → returns correct `last_message`.
- `--wait` on a prompt that does `sleep`-in-background then finishes → returns after
  drain; assert it didn't return at the early non-empty-`background_tasks` `Stop`.
- `--timeout 5` on a long task → timeout result.
- Background-Bash form: launch via `run_in_background`, confirm the completion
  notification + output-file content.
