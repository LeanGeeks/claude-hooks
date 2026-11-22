# 50-08 — Tick, CLI, alarms, notify, tmux server

**Status:** todo · **Depends on:** 50-04, 50-05, 50-06, 50-07
**Read first:** [brd.md](./brd.md) §5, §6 · [architecture.md](./architecture.md) §1, §2, §4, §12.10, §15.1, §16
(all), §17, §18 (all), §20, §21 (all), §22 (the service's environment), §23 · [state.md](./state.md) P0-2, P0-5,
P0-8, P0-11, P0-12, P0-13, OQ-3, OQ-4, OQ-5, invariants 1, 5, 9 · review M1, M2, M7, M8, m5, m8 ·
`questions-mcp/questions_mcp_lib.py` (`notify`, `_resolve_workspace_dir`, `_resolve_hooks_dir`) ·
`.claude/hooks/questions_listen_lib.py` (`read_index`) · `shell/claude-questions` (`--check`, `--workspace-dir`)

## Goal

Assemble the pieces into the one-pass tick the timer runs, the CLI the operator and the driver use, the alarm engine
with its notify path, and the tmux-server ownership that keeps driven sessions alive when the terminal closes.

## Scope

1. **`.claude/bin/epic-driver`**: argparse for every verb of §18; path setup and the stamp check of §17 before any
   non-stdlib import (`EPIC_DRIVER_ALLOW_UNSTAMPED=1` only for tests); `CLAUDE_HOOKS_REPO=$HOME` set in-process
   before importing `questions_mcp_lib`; exit codes 0/1/2/3; `tick.lock` handling (non-blocking for `tick`, up to
   300 s for the other writers).
2. **The tick** (§23): the pass in order, executing 50-06's `(mutation, effect)` sequence with a commit between each;
   `SSH_AUTH_SOCK` unset when the socket is gone; `last_tick_at`, `last_tick_ok_at`, `last_tick_error` (P0-11: the
   operator's only liveness view besides notifies); an unhandled exception ⇒ `tick_error`, commit, exit 1; `--dry-run` writes nothing.
3. **tmux server** (§21.1): detect; start in its own scope with the snapshot environment when none runs; verify via
   `/proc/<pid>/cgroup`; the `AttachProcessesToUnit` move used by `enable` (creating the transient scope if needed);
   `tmux_server` alarm when the tick finds the server in a terminal's scope. Never let an amux call start a server in
   the service's cgroup.
4. **Environment snapshot** (§21.2) written by `enable`; used for the server and for checks.
5. **Infrastructure checks** (§12.10): listener and inhibitor, one start attempt each, then the alarm; the inhibitor
   also verified in `systemd-inhibit --list`.
6. **Alarm engine** (§16.1): raise/repeat/clear over the conditions from 50-04/05/06/07; `ack=True` for every alarm;
   no repeat while the ladder is live (index membership + the nudge span — confirm in `questions_listen_lib` whether
   an acknowledged message leaves the index and implement accordingly), then every 4 h; `alarm_cleared` items for
   driver steps; `due_not_started` from 50-04's hold reasons (§6.2). The catalogue of §16.2 is implemented in full;
   `halt` and `usage_limit` as written there (OQ-5).
7. **Notify path** (§16.4): add keyword-only `workspace_dir: Optional[str] = None` to `questions_mcp_lib.notify`
   (None keeps today's resolution); the tick calls it in process with the ledger's workspace; failures go to
   `logs/notify.log`, `state.notify_failures` and `status`.
8. **Heartbeats** (§16.5, `ack=False`): enable, disable, phase transitions. Nothing else: no daily alive line and no
   flag for one (P0-11).
9. **Verbs** (§18): `validate`; `enable` with the gates of §18.1 in order (including `claude-questions --check
   --workspace-dir` and the `sync = "push"` refusal, the test notify, the tmux move, the snapshot, the pointer file,
   `systemctl --user enable --now` of the timer and the inhibitor, the `systemd-inhibit --list` verification with
   rollback); `disable` (stop and disable both units; sessions and state untouched; one notify); `pause`/`resume`
   (the pause file; the tick still watches and alarms); `status` and `status --json` (§20); `mark <step>
   done|reset`; `mark --grant` (P0-13); `queue` (OQ-4: stores the request in the state for 50-06).
10. `systemctl`, `systemd-run`, `systemd-inhibit`, `busctl`/D-Bus and `tmux` calls all go through `World`.

## Tests

A fake `World` throughout (no real systemd, tmux or relay):

- A full pass on a toy ledger: due → dispatch (spawn) → confirmed → verdict → check → done → next step, across
  several simulated ticks with an advancing clock.
- Lock: a second `tick` while one holds the lock exits 0 quietly; a lock file left by a dead process is acquired;
  `mark` waits for the lock.
- Crash: an exception mid-pass records `last_tick_error` and `tick_error`; the next pass clears it.
- Stamps: missing manifest, changed file, unstamped repo run refused unless the test env var is set.
- tmux: no server ⇒ the `systemd-run --scope` command with the snapshot environment; a server in a
  `ptyxis-spawn-*.scope` ⇒ `enable` moves it, and refuses when the move fails; the tick raises `tmux_server`.
- Alarms: every §16.2 id raised and cleared at least once across this task's and the earlier tasks' tests; no repeat
  while the ladder is live; a repeat at 4 h after it; a failed notify lands in the log and `status`.
- Notify: `workspace_dir` honoured; the MCP tool path (no `workspace_dir`) unchanged.
- `enable`: each gate failing in turn refuses with its reason and leaves no timer enabled; `sync = "push"` refused; a
  missing inhibitor lock rolls back.
- Heartbeats: one per phase transition, none on a quiet day.
- `status` text and JSON on a fixture state.

## Done when

- The unit's `ExecStart` (`epic-driver tick`) does one pass and exits within the 5 min `TimeoutStartSec` on the toy
  ledger with the check budget saturated.
- No code path starts a tmux server from the tick's own process tree.
