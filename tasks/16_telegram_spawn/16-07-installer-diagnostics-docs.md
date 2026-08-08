# 16-07 — Installer, service unit, diagnostics, docs

**Status:** todo · **Depends on:** 16-02, 16-06
**Read first:** [brd.md](./brd.md) §4.1, §5.6 · [architecture.md](./architecture.md) §3.1

## Goal

Make the listener a thing that exists on a machine after
`install-claude-config.sh` runs, and a thing a human can diagnose without reading
this epic.

## Scope

### Service unit — `shell/amux-spawn-listen.service`

```ini
[Unit]
Description=amux-spawn Telegram listener
After=network-online.target

[Service]
ExecStart=%h/.local/bin/amux-spawn listen
Restart=always
RestartSec=5
# Journal only: the listener must not add to ~/.claude's log growth (task 11).

[Install]
WantedBy=default.target
```

`%h/.local/bin` because that is where step 3 of the installer puts `amux-spawn`
(`install-claude-config.sh:302-308`) — not `/usr/local/bin`, which is where the
**amux fork** lives (epic 10 state.md).

### Installer step

A new step, after the amux-spawn launcher step:

1. Copy the unit to `~/.config/systemd/user/`, `systemctl --user daemon-reload`.
2. Read `[listen].enabled` from `~/.config/claude-tg-relay/config.toml`:
   - true → `enable --now`, then report `systemctl --user is-active`;
   - false or absent → leave it installed and disabled, and print the two commands
     to turn it on. Never enable a remote-command channel because a file was
     copied.
   - already enabled but now false → `disable --now`, and say so.
3. Print the lingering hint when `loginctl show-user "$USER" -p Linger` is `no`:
   without it the listener dies at logout, which looks exactly like "Telegram
   stopped working". Print the command; do not run it (it may need polkit auth).
4. No systemd user session (`systemctl --user` unavailable, e.g. a container):
   skip cleanly with instructions for running `amux-spawn listen` under whatever
   supervisor exists. This must not fail the installer.
5. Warn when `amux` is not on `PATH`: the listener will start and every spawn will
   fail. Same shape as the existing amux-spawn PATH warnings.

Idempotent, like the rest of the script: re-running must not duplicate anything
or restart a healthy listener without cause.

### Docs — `docs/telegram-spawn.md`

One page, written for the operator:

- What the channel does and its one hard rule: **a spawn is delivered to a
  machine that is online right now; nothing is queued** (brd §5.4).
- The `[listen]` config block, every key, with defaults and what changing it does.
- The command reference: the `/new` grammar table from brd §3.1, `/ls`, and the
  `+profile` / `+tier` modifiers.
- How a workspace becomes spawnable (it must have talked to Telegram once — brd
  §4.2) and how to check: `amux-spawn listen --status`.
- Troubleshooting table: nothing happens / "offline" although the machine is up /
  workspace not offered / spawn fails / session starts on the wrong machine.
  Each row names the command that shows the truth.
- The security boundary in three lines: bound user only, seen-store workspaces
  only, no `--yolo`, and the note that spawned sessions still gate their commands
  through the normal permission flow.

### `architecture.md` (top level)

Add the inbound channel to the repository's architecture document — it currently
describes a system where every Telegram interaction is outbound-first, which
stops being true with this epic:

- the relay table gains `commands`, and the API table gains the two endpoints;
- a new section for the listener next to the existing "Host environment: amux";
- the end-to-end flows section gains "Spawn from Telegram", alongside the
  permission / AskUserQuestion / idle flows;
- the `Repository layout` block gains `amux_listen_lib.py`,
  `telegram_workspaces.py` and `shell/amux-spawn-listen.service`.

Keep the existing tone: what it is, why it is that shape, what breaks if you
assume otherwise.

## Implementation notes

- The installer parses TOML with the same approach used elsewhere in the script
  (a small `python3 -c` is fine; do not add a dependency).
- Everything new is guarded so a machine without systemd, without amux, or
  without a relay config still installs the hooks successfully. The installer's
  existing contract is that it never leaves a working setup broken.

## Testing

The installer has no unit-test harness; verification is by running it. What the
task must demonstrate, recorded in the task file:

- Fresh install with no `[listen]`: unit installed, disabled, instructions
  printed, nothing started.
- `enabled = true`: unit enabled and active, `--status` healthy.
- Flip to `false` and re-run: unit disabled, said out loud.
- Re-run twice with no changes: no duplicate units, no needless restart.
- No-systemd path: skipped with instructions, installer exits 0.
- Docs and `architecture.md` reviewed for consistency with the shipped behaviour
  (this is the last task before live verification, so they must describe what was
  actually built, not what was planned).

## Done criteria

- [ ] `shell/amux-spawn-listen.service` ships and points at `%h/.local/bin`.
- [ ] Installer installs, enables/disables per config, and never enables the
      channel implicitly.
- [ ] Lingering, missing-systemd and missing-amux cases are handled with clear
      output and a zero exit.
- [ ] `docs/telegram-spawn.md` covers config, grammar, allowlist, troubleshooting
      and the security boundary.
- [ ] Top-level `architecture.md` describes the inbound channel, the listener and
      the spawn flow.
- [ ] Re-running the installer is idempotent.
