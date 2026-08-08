# 16-03 — `amux-spawn spawn --plain` and `--json`

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §2.3, §5.2 · epic 10 [brd](../10_spawn_sessions/brd.md) D-Tracked, D-SessionId

## Goal

Make tracked-vs-plain an explicit choice instead of an inference. Today
`cmd_spawn` decides with

```python
tracked = (not is_tty) or wait_mode          # .claude/bin/amux-spawn:189-191
```

which encodes "non-TTY ⇒ an agent is spawning ⇒ track it". The Telegram listener
(16-05/16-06) is non-TTY but is spawning a **human** session: it must produce a
plain, restartable session with no handle, no minted `--session-id`, and no
consumption of the per-workspace fork-bomb cap.

Small task, no dependencies, and it blocks 16-06 — build it early.

## Scope

Two mutually exclusive flags on the `spawn` subparser:

- `--plain` — force plain, whatever the TTY says.
- `--track` — force tracked, whatever the TTY says.

Resolution, in order:

1. `--plain` and `--track` together → usage error, exit 2, nothing created.
2. `--wait` / `--notify` with `--plain` → usage error. Supervision reads the
   handle; a plain session has none. Say that in the error.
3. `--plain` → plain. `--track` → tracked.
4. Neither → today's inference, unchanged.

`--plain` does **not** imply `--detach`: detachment is still `--detach or
wait_mode or (not is_tty)`, so a human at a TTY typing `--plain` still attaches.

Nothing else changes: a plain spawn already skips the cap check, the minted UUID,
the handle write and the `session_id`/`run_id` echo — all of them already sit
behind `if tracked:` in `cmd_spawn`.

### `--json`

`spawn --json` prints one machine-readable object on stdout instead of the human
line, and nothing else:

```json
{"name": "claude-hooks-2", "dir": "/data/sync/work/leangeeks-ai/claude-hooks",
 "tracked": false, "session_id": null, "run_id": null, "profile": "glm5"}
```

16-06 consumes this. The alternative — regex over
`amux-spawn: spawned plain session 'X' in DIR` — makes the listener depend on a
human-readable string that nobody would think twice about rewording.

Rules: `--json` suppresses the human lines on **stdout** only (warnings stay on
stderr); with `--wait` the result payload still goes to stdout after the object,
separated by a newline, and `WAIT_TIMEOUT_MARKER` behaviour is unchanged; on
failure nothing is printed on stdout and the exit code carries the result, as
today.

### Help and completion

- `--help` text for the flags names the consequence, not the mechanism:
  `--plain: restartable human session, no tracking handle (default at a TTY)`.
- `shell/amux-spawn-completion.bash` gains `--plain`, `--track` and `--json`
  wherever the `spawn` subcommand's options are completed (it currently
  special-cases `--profile` at line 61 and offers no general flag list — add one
  rather than extending the special case).

## Implementation notes

- Put the resolution in one small helper (`resolve_tracked(args, is_tty) ->
  bool`) so the tests can cover the matrix without launching anything. `cmd_spawn`
  calls it exactly once, where the current expression sits.
- Keep the error text on stderr through `_eprint` and return the usage exit code
  the parser already uses for bad arguments; do not print anything on stdout —
  orchestrators capture stdout as the result payload (`cmd_spawn` wait_mode).
- Do not touch the `detach` expression, the lock, or the handle schema. This
  task's diff should be a flag, a helper, one call site, help text and completion.

## Testing

`tests/test_unit_amux_*` conventions, no real tmux:

- Matrix over (`--plain`, `--track`, `--wait`, TTY) → resolved tracked/detach or
  usage error, covering: TTY default plain, non-TTY default tracked, `--plain` at
  a non-TTY, `--track` at a TTY, both flags, `--plain --wait`, `--plain --notify`.
- `--plain` at a non-TTY: no handle written, no `--session-id` passed to amux, cap
  not consulted (assert the cap helper is not called — a fake workspace at the cap
  must still spawn).
- `--plain` at a TTY still attaches; `--plain --detach` does not.
- `--json`: stdout parses as a single object with the documented keys for both a
  plain and a tracked spawn; no human lines leak onto stdout; a failed spawn
  prints nothing on stdout; `--json --wait` still emits the result payload after
  the object and still returns 3 with the timeout marker on timeout.
- Existing tracked-spawn tests stay green unchanged — that is the regression
  signal that the inference path was not disturbed.

## Done criteria

- [ ] `--plain` / `--track` exist, are mutually exclusive, and override the TTY
      inference in both directions.
- [ ] `--plain` with `--wait` or `--notify` is a usage error that explains why.
- [ ] A `--plain` spawn writes no handle, mints no session id, and is not capped.
- [ ] Detachment behaviour is unchanged.
- [ ] `spawn --json` prints exactly one machine-readable object on stdout, for
      both plain and tracked spawns, and nothing else.
- [ ] Completion offers `--plain`, `--track` and `--json`; `--help` explains each
      in one line.
- [ ] The existing epic-10 spawn tests pass untouched.
