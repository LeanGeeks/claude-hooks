# 16-05 — Listener runtime (`amux-spawn listen`)

**Status:** todo · **Depends on:** 16-01 (client methods), 16-04 (seen-store)
**Read first:** [brd.md](./brd.md) §2.1, §2.4, §4.1 · [architecture.md](./architecture.md) §3.1–3.2, §3.5

## Goal

The resident process: connect, long-poll for commands, dispatch them, answer the
two read-only kinds (`resolve`, `ls`), and stay alive across sleep, network loss
and relay restarts.

Spawning itself is **16-06**. This task ends with a dispatcher that hands a
`spawn` command to a handler stub and reports a not-implemented failure — which
is a complete, testable milestone: the channel works end to end and says so.

## Scope

### Layout

- New `.claude/hooks/amux_listen_lib.py` — everything importable and testable.
- `cmd_listen` in `.claude/bin/amux-spawn` — argument parsing and process
  lifecycle only, matching how `cmd_spawn` delegates to `amux_spawn_lib`.
- Add `amux_listen_lib.py` to `REQUIRED_HOOKS` in `install-claude-config.sh:163`
  **in this task** (same reasoning as 16-04): the binary imports it, and the
  installer copies only what the list names.

### Config

```toml
[listen]
enabled         = true
default_profile = "claude"
default_model   = ""
model_tiers     = ["fable", "opus", "sonnet", "haiku"]
max_live        = 8
min_interval_s  = 10
```

Read from `~/.config/claude-tg-relay/config.toml` — the same file `RelayClient.
from_config` reads (`client.py:22`). `load_listen_config(path=None) ->
ListenConfig` returns a frozen dataclass with these defaults; an absent `[listen]`
table yields `enabled=False` and `listen` exits 0 with a one-line explanation, so
installing the unit before configuring it is harmless.

With epic 15 present, the listener uses the **top-level** `installation_token`
(the default role) — roles' §6 keeps non-question traffic default-only.

### Single instance

`flock` (non-blocking) on `~/.claude/amux-spawn-listen.lock`, held for the
process lifetime. A second copy prints who holds it and exits 0 — systemd
restarts overlap, and a double-claimer is the one failure mode the queue's
exclusive claim cannot fully hide.

### Loop

```
connect: RelayClient.from_config()
loop:
    cmd = client.poll_command(wait=25)      # 204 -> None -> immediately re-poll
    if cmd: dispatch(cmd)                   # in a worker thread
```

- **Backoff** on transport errors: jittered exponential 1 s → 60 s, reset on the
  first success. A laptop lid closing produces read timeouts and connection
  resets; those are normal and must not spam the journal (log at debug after the
  first, log the recovery at info).
- **401 / revoked token:** retry every 300 s, record the reason in `--status`,
  never busy-loop.
- **Concurrency:** a worker thread per command, capped by a module constant
  (4 — deliberately not a config key; it bounds threads, not policy). Over the
  cap, refuse with `ok=false, summary="listener busy"` rather than queueing
  behind a wizard that may sit for half an hour.
- **Shutdown:** SIGTERM sets a stop flag, stops polling, and gives in-flight
  workers a short grace period. A wizard interrupted this way simply leaves its
  message to expire (architecture §3.3).

### `resolve` responder

Payload `{workspace}` → `telegram_workspaces.find(workspace)`:

```json
{ "claim": true, "ambiguous": false }
```

`claim` is true when at least one seen-store entry's basename matches
case-insensitively; `ambiguous` is true when more than one does (the listener
resolves that with the user in 16-06 — the relay never learns the paths).
Report via `report_command_result(ok=True, data=…)`; nothing reaches the chat.

Must answer fast: the relay's fan-out deadline is ~5 s (16-02).

### `ls` responder

```json
{ "sessions": [
  {"name": "claude-hooks-2", "workspace": "claude-hooks",
   "tracked": false, "state": null, "idle_for_s": 240} ] }
```

- Sessions and their dirs from `lib.list_amux_sessions_with_dirs()`.
- `idle_for_s` from tmux: `tmux list-sessions -F '#{session_name} #{session_activity}'`,
  which is the only activity signal a **plain** session has (no handle, no minted
  session id, so no transcript path).
- For tracked sessions (`lib.list_handles()`), `state` comes from
  `amux-spawn status <name> --json` as a subprocess — that is already the public
  read interface (10-03); do not duplicate `_derive_status`.
- No tmux, no amux, or a failing call → `{"sessions": []}` with `ok=true` and a
  `summary` naming the reason. An empty list is a fact; an exception is not.

### Spawn ledger

`~/.claude/telegram_spawns.json` — `[{name, dir, created_at}]`, written by 16-06
after a successful spawn, read here for the caps:

```python
def ledger_record(name: str, abs_dir: str) -> None
def ledger_live(abs_dir: str | None = None) -> list[Spawn]   # filtered by tmux liveness
def caps_check(cfg, abs_dir) -> str | None                   # None = ok, else refusal text
```

`ledger_live` drops entries whose tmux session is gone (`lib.tmux_has_session`)
and rewrites the file, so the ledger self-drains. `caps_check` counts
**machine-wide** against `max_live` — `abs_dir` is there for the refusal wording,
not to scope the count — and enforces `min_interval_s` as a floor since the last
spawn, persisted (wall-clock timestamp, not `monotonic`) so a restart cannot
reset it.

Plain sessions are outside epic 10's fork-bomb cap (it counts tracked handles),
which is exactly why this exists.

### `listen --status`

Prints, for a human on the machine: config path and whether `[listen].enabled`,
relay URL, installation label and `chat_bound` (via `client.me()`, 16-01),
connection state (connected / backing off / unauthorised, with the last error and
its age), commands handled since start, live spawn count vs `max_live`, the
number of workspaces in the seen-store, and the lock holder's PID when another
instance owns it. Exit 0 when healthy, 1 otherwise, so it can be used as a check.

`--status` runs as a **separate process** from the listener (it cannot take the
lock), so its connection state comes from
`~/.claude/amux-spawn-listen.status.json`, which the loop rewrites atomically
each pass — not from shared memory. An absent or stale status file is
itself a diagnosis: "listener not running / not polling since <time>".

## Implementation notes

- The lib must not import hook-only modules beyond `telegram_workspaces` and
  `amux_spawn_lib`; keep `httpx` usage inside `RelayClient`.
- One `RelayClient` for the poll loop; workers that send wizard messages (16-06)
  get their own client instance — `httpx.Client` is not safe to share across
  threads for long-lived concurrent use, and a wizard's 30-minute long-poll must
  not sit in the same connection pool slot as the command poll.
- Never `os.environ.update()` in this process (16-06 explains why); the listener's
  environment must stay pristine for its whole life.
- Log to stdout/stderr for journald. No debug file in `~/.claude` — the existing
  hook debug logs grew to hundreds of megabytes (task 11 "log hygiene").

## Testing

New `tests/test_unit_amux_listen.py`, registered in `tests/run_all_tests.py`.
Fake the relay client (a stub returning scripted commands), fake tmux/amux by
patching the `lib` helpers and `subprocess.run`:

- Dispatch routes each kind to its handler; an unknown kind reports
  `ok=false` and does not crash the loop.
- Poll loop: 204 → immediate re-poll; transport error → backoff grows and resets
  on success; 401 → slow retry, reflected in `--status`.
- Concurrency cap refuses the (cap+1)-th command with a clear summary.
- `resolve`: no match → `claim=false`; one match → claim; two basename matches →
  `claim=true, ambiguous=true`.
- `ls`: mixed plain and tracked sessions; tracked state read from a stubbed
  `status --json`; tmux missing → empty list with a summary, `ok=true`.
- Ledger: records, self-drains dead sessions, `max_live` refusal text,
  `min_interval_s` refusal survives a simulated restart.
- Lock: a second instance exits 0 without polling.
- SIGTERM stops the loop and does not report results for commands never claimed.

## Done criteria

- [ ] `amux-spawn listen` runs, connects, long-polls and dispatches.
- [ ] `[listen]` config parsed with documented defaults; absent table → clean exit.
- [ ] Single-instance lock; second copy exits 0.
- [ ] Backoff, 401 handling and SIGTERM shutdown behave as specified.
- [ ] `resolve` answers within the fan-out deadline, including the ambiguous case.
- [ ] `ls` reports plain sessions with `idle_for_s` and tracked sessions with
      derived state, degrading to an empty list rather than an error.
- [ ] Ledger and caps implemented and self-draining.
- [ ] `listen --status` reports config, identity, connection, counters and lock
      holder from the status file, and diagnoses a dead listener correctly.
- [ ] `amux_listen_lib.py` in `REQUIRED_HOOKS`; new suite registered and green.
