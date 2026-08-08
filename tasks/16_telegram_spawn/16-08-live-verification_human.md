# 16-08 — Live verification (human)

**Status:** todo · **Depends on:** 16-07
**Read first:** [brd.md](./brd.md) §7 · [architecture.md](./architecture.md) §5–6

## Why this is a human task

No agent can provision what this needs: a real relay with a real bot token, a
phone, **two machines bound to the same chat**, and a real amux on both. Every
mechanism below was built against fakes; this is where the fakes are checked
against Telegram's actual behaviour, systemd's actual behaviour, and a laptop
that actually sleeps.

Run it after `install-claude-config.sh` has been re-run on both machines.

## Setup

- Machine A (`workstation`) and machine B (`thinkpad`), each with its own
  installation token, both bound to one chat.
- `[listen].enabled = true` on both; listener active; `amux-spawn listen --status`
  healthy on both.
- At least two workspaces on A that have already emitted a Telegram message, one
  of them sharing a name with a workspace on B.

## Checklist

### Cold start — the point of the epic

- [ ] **C1.** With **no session running anywhere** on A, send
      `/new <ws> fix the flaky login test`. A session appears within seconds,
      seeded, and the ack names it, the machine and the directory.
- [ ] **C2.** The seeded turn ends → the idle notification arrives → reply to it
      → the reply lands as the next turn (task 09 loop, unbroken).
- [ ] **C3.** `amux a <name>` at A's keyboard attaches to that session, and it is
      an ordinary restartable session: no handle in `amux-spawn ls`, and killing
      and restarting it via amux works.

### Grammar and wizard

- [ ] **G1.** `/new <ws>` → prompt request → send a **multi-line** prompt →
      it reaches the session **verbatim** (the open question in 16-06).
- [ ] **G2.** Bare `/new` → machine picker → workspace picker → prompt → spawn.
- [ ] **G3.** `/new .` reuses the last target.
- [ ] **G4.** `/new <machine>.<ws> …` starts on the named machine even when both
      machines have that workspace.
- [ ] **G5.** Bare `/new <shared-ws> …` where both machines claim it → picker
      naming both; choosing B starts on B.
- [ ] **G6.** `+glm5 +opus` reach the session: check the session's model and that
      the profile's backend is in use. An unknown `+token` refuses and starts
      nothing.
- [ ] **G7.** `[✖ Cancel]` at each wizard step starts nothing and says so.

### Failure feedback — must be unmistakable

- [ ] **F1.** Stop B's listener. `/new thinkpad.<ws> …` → **"thinkpad is offline
      — nothing was started"** with the last-seen age. Start the listener: the
      old command does **not** fire (brd §5.4).
- [ ] **F2.** Suspend B (lid closed) rather than stopping the service, and repeat
      F1 — the notice must be the same, not a hang or a silent drop.
- [ ] **F3.** `SIGSTOP` A's listener so it looks live but claims nothing → the
      targeted spawn expires with the "did not pick this up" notice, exactly once.
- [ ] **F4.** `kill -9` A's listener mid-wizard → systemd restarts it; the wizard
      messages expire; no session is created; a fresh `/new` works.
- [ ] **F5.** Spawn into a workspace, then `rm -rf` its directory and spawn again
      → preflight refuses with the directory reason, no session.
- [ ] **F6.** Hit `max_live` → refusal naming the count; hit `min_interval_s` →
      refusal; neither queues.
- [ ] **F7.** A message from a Telegram account that is not the bound user is
      ignored entirely.

### Coexistence — nothing else may regress

- [ ] **R1.** `/ls` from the phone lists both machines' sessions grouped, with an
      offline machine labelled; `/ls <machine>` scopes.
- [ ] **R2.** A permission request from a *spawned* session reaches Telegram and
      is answerable, as from any session.
- [ ] **R3.** With several sessions idle, a **loose** (non-threaded) message still
      produces the "use Reply" nudge and is not swallowed by the command path.
- [ ] **R4.** A threaded reply still routes to the right session on the right
      machine.
- [ ] **R5.** `/bind` still works; other slash commands are still ignored.
- [ ] **R6.** Stop both listeners: permissions, questions, idle notifications and
      reply injection behave exactly as before the epic.

### Endurance

- [ ] **E1.** Leave both listeners up overnight through sleep/wake and a network
      change (wifi → tether). Next morning: `--status` healthy, `/new` works
      first time, journal shows reconnects at debug and no error spam.
- [ ] **E2.** Restart the relay server while listeners are parked → they
      reconnect without intervention.
- [ ] **E3.** After a week of normal use, `~/.claude/telegram_workspaces.json`
      holds the workspaces actually used and no junk; `telegram_spawns.json` has
      self-drained dead sessions.

## Recording

Note the result of every box in this file (pass, or what happened), the CC and
amux versions, and anything that had to be fixed. F2, F4 and E1 are the ones most
likely to reveal a design mistake rather than a bug — a surprise there is worth a
brd revision, not a patch.

## Done criteria

- [ ] Every box above ticked or explicitly waived with a reason.
- [ ] The multi-line seeding question from 16-06 answered by observation.
- [ ] Any wording change to a user-facing notice fed back into 16-02's string
      table and its tests.
