# 50-12 — Live verification (human)

**Status:** todo · **Depends on:** 50-10 · **Owner:** human (operator present)
**Read first:** [brd.md](./brd.md) §6, §7 · [architecture.md](./architecture.md) §9.3, §11.3, §11.4, §12.5, §15,
§18.1, §21, §22 (after a reboot) · [state.md](./state.md) P0-3, P0-5, P0-6, P0-11

What a fake cannot show. Run with the feature installed (`./install.sh --yes`, `epic-driver` on) and a **toy ledger**
in a scratch workspace that has its own `.claude/roles.toml` and queue (`sync = "commit"` or off) — never 037's
ledger or queue (OD-11). The toy ledger has a driver step with a check, a watch step with an `after` delay, a
recurring step, one approval, and one worker step over a scratch repo with a bare remote. Pinned compaction values per
OQ-2. (Task 50-11 was dropped with P0-6; this task keeps its id.)

1. **Reboot, then login, no terminal opened** (P0-6). Reboot with the driver on and one session mid-step; unlock the
   disk, log in, open no terminal. Within one timer period: the timer fires; the tick runs with a working `PATH` and
   ssh (a check that runs `ssh -o ConnectTimeout=5 <host> true`); the tmux server pid shows `epic-driver-tmux.scope`
   in `/proc/<pid>/cgroup`; the session that had a step in flight is resumed in place on the same model and effort
   (compare the first post-restart assistant model with the handle; no `model_mismatch`); a session the tick spawns
   is alive 15 min later; `go version && cargo --version` succeed inside a tick-spawned pane (review M2).
2. **No suspend** during 8 h of idleness while logged in, with the driver on (`systemd-inhibit --list` shows the lock
   throughout).
3. **The server outlives its terminal** (review M1). With the shared tmux server started from a terminal tab,
   `enable` moves it into its own scope (or refuses with instructions; record which). Close that terminal, then log
   out and back in: the driven sessions survive.
4. **Attach** to a tick-spawned session (`amux-spawn a <name>`), detach; its transcript persists.
5. **Kill a worker's pane**: it is resumed in place, with its context, on the same model (review B1).
6. **A forced rotate** (lower the role's `cap` below the session's context): handoff written, a fresh gen + 1 session
   seeded with it, the old one parked until the new one starts, then reaped; not reaped while attached.
7. **An event deferred** while text sits in the input line — once attached, once after detaching with the text still
   there (review M6); delivered once the line is empty, exactly once.
8. **A question through the relay**: the driver asks an approval with buttons; the answer reaches the step; `mark
   --grant` lands in the state; a second question withdrawn closes (use a question with buttons: the no-button path
   needs task 49's relay redeploy — review m7).
9. **A never-seen worktree** (review m9): a worker spawned unattended into a fresh worktree path runs its seeded
   prompt (no trust dialog stalls it; if one does, record it — `startup_dialog` must fire and nothing is typed into it).
10. **The toy ledger end to end**: every step done, verdicts in the log, each check re-run before done, the worker step
    merged into `epic/<epic>` with the sha check passing, nothing in the scratch repo's primary checkout changed, no
    force-push in the remote's reflog.
11. **Alarms reach Telegram**: provoke `check_error` and `delivery_deferred`; each arrives once with a ladder, no
    4-hourly repeat while the ladder is live, and clears. Stop the listener: `listener_down` after one restart
    attempt.
12. **Liveness by hand** (P0-11): with the timer stopped for 30 min, `epic-driver status` shows the stale
    `last_tick_at`; after a forced tick error it shows that error. No daily line is ever sent.
13. **A startup dialog** (lead note, 2026-11-22): a session spawned while Claude Code shows a startup dialog (the
    "Code Mode (experimental)" opt-in if the installed version still shows it, otherwise any dialog that blocks the
    seed). Unlisted: `startup_dialog` reaches Telegram, nothing is answered or typed. Listed in the toy ledger's
    `startup_dialogs` with its safe default: answered once, the seeded prompt runs, no alarm.

Record in a sibling `50-12-signoff-evidence.md` (commands, timestamps, screenshots where useful).
