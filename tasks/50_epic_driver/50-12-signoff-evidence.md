# 50-12 — Live verification: runbook and sign-off evidence

Template for the operator. Fill the Evidence and Timestamp lines as you go (paste commands, output, screenshots paths).
Spec of the checks: [50-12-live-verification_human.md](./50-12-live-verification_human.md). Guide: `docs/epic-driver.md`.

Signed off by: ______________________  Date: ______________

## 0. The rig (already built; nothing is enabled)

| What | Where |
|---|---|
| Workspace (git repo, own `.claude/roles.toml`, queue `docs/questions/questions.md`, `[questions] sync = "off"`) | `~/epic50-rig/ws` |
| Ledger | `~/epic50-rig/ws/plan/ledger.yaml` (run id `epic-rig50`, epic `rig50`, branch `epic/rig50`) |
| Cards / check scripts | `~/epic50-rig/ws/plan/ledger/{cards,checks}/` |
| Toy implementer / reviewer prompts | `~/epic50-rig/ws/prompts/toy-{implementer,reviewer}.md` |
| Scratch repo (primary checkout on `main`, one commit) and its bare remote | `~/epic50-rig/repo`, `~/epic50-rig/remote.git` |
| Alarm fixture flags (check 11) | `~/epic50-rig/flags/` |
| Run state once enabled | `~/.local/state/epic-driver/epic-rig50/` (`state.json`, `logs/`, `worktrees/`, `handoffs/`) |

Steps in the ledger: `driver-hello` (driver + check), `soak-watch` (watch, `after` 5m), `recurring-note` (recurring 20m),
`gated-step` (approval `RIG-A1`), `toy-worker` (worker over `~/epic50-rig/repo`), `ssh-reach` (watch; check 1),
`alarm-probe` (watch; check 11 fixture). All roles: model `sonnet`, profile `claude` (low cost), efforts driver medium,
workers low. Compaction pinned per OQ-2 (65 / 300000), `cap` 100000.

Before enabling:

1. **Fill in the ssh host.** Edit `~/epic50-rig/ws/plan/ledger/checks/ssh_reach.sh`, set `RIG_SSH_HOST="..."` (marked
   `>>> OPERATOR`). Until it is set the check exits 1 (not yet) and check 1 cannot pass.
2. Baseline of the scratch repo (for check 10):
   ```
   git -C ~/epic50-rig/repo status -sb; git -C ~/epic50-rig/repo rev-parse HEAD
   git -C ~/epic50-rig/remote.git reflog | head; git -C ~/epic50-rig/remote.git for-each-ref
   ```
   Baseline HEAD: ____________________
3. Validate: `epic-driver validate ~/epic50-rig/ws/plan/ledger.yaml` (3 `context_window_default_fallback` warnings are expected: the
   validator assumes a 1M window for the alias `sonnet`).

## 1. `enable`

Run from your own interactive shell (it snapshots PATH, Go, Rust, Node for the tick):

```
epic-driver enable ~/epic50-rig/ws/plan/ledger.yaml
```

It refuses on the first failing gate (exit 3, with the fix). In order: (1) no other run active; (2) stamps agree and
`EPIC_DRIVER_ALLOW_UNSTAMPED` unset; (3) ledger validates incl. `base_ref` `main` in the scratch repo; (4)
`claude-questions-listen.service` active; (5) `claude-questions --check --workspace-dir ~/epic50-rig/ws` passes
(this probes the relay with the installed token) and `sync` is not `"push"` (it is `"off"`); (6) a test notify reaches
Telegram ("test notify from epic-driver enable"); (7) the tmux server: **your shared tmux server (pid 16651 when this rig was
built) is inside `ptyxis-spawn-*.scope`, so `enable` will move it into `epic-driver-tmux.scope`** (check 3 records this); (8) env snapshot; (9) writes
`state.json`, `active.json` and the pointer file `~/epic50-rig/ws/plan/epic-driver.run.json`; (10) `systemctl --user enable --now
claude-epic-driver.timer claude-epic-driver-inhibit.service` and verifies the `sleep:idle` lock; (11) one Telegram line
"epic-driver on for rig50".

> **Note (50-13, after this run was enabled).** `enable` now has two more gates after (3), and the later ones shift by
> two (listener = 6 … notify = 13): (4) folder trust, (5) the claude.ai login. Before enabling a new run, trust once,
> per `docs/epic-driver.md` section 4 "Folder trust": the state root `~/.local/state/epic-driver/` (covers the worker worktrees) and the
> workspace. `enable` never writes
> `~/.claude.json`; it refuses and names the untrusted folders. Gate 5 refuses when the login expires within 7 days
> (`--login-days N`); the tick raises `login_expiring` 3 days before and `login_expired` after.

Evidence (full output):

```

```
Timestamp: ____________

Afterwards: `epic-driver status`, `systemctl --user is-active claude-epic-driver.timer claude-epic-driver-inhibit.service`,
`systemd-inhibit --list | grep -i epic`, `cat /proc/$(tmux display -p '#{pid}')/cgroup`.

Ticks run every 10 min (`OnCalendar=*:0/10`); force one with `epic-driver tick` (takes the lock) or `systemctl --user start claude-epic-driver.service`.
Session names: `ws-rig50-driver-g1`, `repo`-based worktree names like `rig50-toy-worker-rig50-implementer-toy-worker-g1`
(read the real ones from `epic-driver status`). Logs: `~/.local/state/epic-driver/epic-rig50/logs/{tick.log,notify.log,checks/<step>.log}`,
`journalctl --user -u claude-epic-driver.service`.

## 2. Checks

### Check 1 — Reboot, then login, no terminal opened (P0-6)

Prepare (driver on, one session mid-step): `epic-driver mark ssh-reach reset` so the ssh check re-runs after the reboot;
let a tick start `toy-worker` or `driver-hello` and note the session name, handle model/effort (`~/.local/state/amux/` handle or
`amux-spawn` listing) and the time. Reboot; unlock the disk; log in; open **no** terminal.

Observe, within one timer period (<= 10 min), from a first terminal opened only afterwards (or from another device over ssh):

- `systemctl --user list-timers claude-epic-driver.timer` and `journalctl --user -u claude-epic-driver.service --since "boot"` show a tick.
- `tail ~/.local/state/epic-driver/epic-rig50/logs/checks/ssh-reach.log` shows `ssh ok` with a sane `PATH`/`SSH_AUTH_SOCK` line
  (the script prints them). The `ssh-reach` step goes done.
- `cat /proc/$(tmux display -p '#{pid}')/cgroup` contains `epic-driver-tmux.scope`.
- The in-flight session is resumed in place: same name/dir, same model and effort. Compare the first post-restart assistant model in its transcript with the
  handle; `epic-driver status` shows no `model_mismatch` alarm.
- A session the tick spawned is still alive 15 min later (`tmux ls`).
- Inside a tick-spawned pane (`amux-spawn a <name>`, or `tmux capture-pane`): `go version && cargo --version` succeed (review M2).

Pass: all of the above. Evidence:

```

```
Timestamp: ____________

### Check 2 — No suspend during 8 h idle (driver on, logged in)

Leave the machine idle (no keyboard, screen may lock) for 8 h with the driver on. Record at start, middle and end:
`systemd-inhibit --list | grep -i epic` (the `sleep`/`idle` lock held by `claude-epic-driver-inhibit.service`),
`systemctl --user is-active claude-epic-driver-inhibit.service`, `uptime -s`, and
`journalctl -b --since "-9h" | grep -iE 'suspend|sleep'` (no suspend entries); `last_tick_at` in `epic-driver status` stays within 10 min.

Pass: lock present throughout, no suspend, ticks continued. Evidence:

```

```
Timestamp: ____________

### Check 3 — The tmux server outlives its terminal (review M1)

With the shared tmux server started from a terminal tab: before `enable`, `cat /proc/$(tmux display -p '#{pid}')/cgroup` (expected `ptyxis-spawn-*.scope`).
Run `enable`; record whether it printed "moved the tmux server (pid N) into epic-driver-tmux.scope" or refused with instructions.
Afterwards the cgroup must show `epic-driver-tmux.scope`. Close that terminal tab/window, then log out and back in:
`tmux ls` still lists the driven sessions; `epic-driver status` shows them alive.

Pass: moved (or refused with instructions, recorded) and the sessions survive close + logout/login. Evidence (which of the two happened):

```

```
Timestamp: ____________

### Check 4 — Attach to a tick-spawned session

`epic-driver status` to read a driven session name; `amux-spawn a <name>`; look at it; detach (`Ctrl-b d`) leaving the input line empty.
Check the transcript persists: `ls -l` the session's transcript under `~/.claude/projects/` (the path is in the handle) is unchanged/grows, nothing lost.

Pass: attach works, detach leaves the session running, transcript intact. Evidence:

```

```
Timestamp: ____________

### Check 5 — Kill a worker's pane (review B1)

While `toy-worker`'s implementer is alive: note its name, model and a fact from its context (e.g. what it said it would do). Then
`tmux kill-pane -t <session>` (or `amux-spawn` kill for that name). Wait one or two ticks (`epic-driver tick`).

Pass: the tick resumes it in place (`claude -r`, same session id/dir, same model in the new first assistant message), it remembers the
earlier context, no `model_mismatch`. Evidence:

```

```
Timestamp: ____________

### Check 6 — A forced rotate

Lower the role's `cap` below the session's context: edit `~/epic50-rig/ws/plan/ledger.yaml`, set `roles.driver.cap` to e.g. `20000`
(find the session's context in `epic-driver status`; if it is lower than 20000, drive the session with a few reading tasks first, or use a smaller value;
the cap must stay valid for `epic-driver validate`). The tick re-reads the ledger; give the driver an event (`epic-driver mark recurring-note reset`, tick) to trigger Rotate.

Observe: a handoff file appears in `~/.local/state/epic-driver/epic-rig50/handoffs/`; a fresh `...-g2` session is seeded with it; the old g1 is parked
until g2 starts, then reaped. Repeat with `amux-spawn a <g1 name>` attached: g1 must **not** be reaped while attached. Restore `cap: 100000` afterwards.

Pass: as above. Evidence:

```

```
Timestamp: ____________

### Check 7 — An event deferred while text sits in the input line (review M6)

Attach to the driver session, type some text without pressing Enter. (a) Stay attached: provoke an event (`epic-driver mark recurring-note reset`,
`epic-driver tick`); status/outbox shows it queued and the pane untouched. Delete the text; next tick delivers it **once**.
(b) Type text again, detach with the text still there, provoke another event: still deferred (`delivery_deferred` alarm after its threshold, see check 11);
clear the line (attach, Ctrl-U Ctrl-K, detach): delivered exactly once.

Pass: nothing typed over your text in either case; each event delivered exactly once (count `E-<n>` lines in the transcript). Evidence:

```

```
Timestamp: ____________

### Check 8 — A question through the relay

`gated-step` needs approval `RIG-A1` (condition `driver-hello` done). After `driver-hello` is done the driver asks it through
`mcp__questions__ask` **with buttons**. Answer in Telegram. Observe the answer reaches the step (`answer` event, the step proceeds),
`epic-driver mark --grant RIG-A1 ...` was run: `epic-driver status --json | jq '.approvals'` or `state.json` shows RIG-A1 granted. Then provoke a second
question (e.g. attach to the driver and ask it to ask one with buttons, or hold back an approval) and withdraw it (`claude-questions --withdraw Q-n --reason test`
with `--workspace-dir ~/epic50-rig/ws`): the question closes in Telegram. Use buttons only (the no-button path needs task 49's relay redeploy, review m7).

Pass: answer reaches the step, grant in state, a second question withdrawn closes. Evidence:

```

```
Timestamp: ____________

### Check 9 — A never-seen worktree (review m9)

`toy-worker` spawns its implementer unattended into `~/.local/state/epic-driver/epic-rig50/worktrees/rig50-toy-worker/` (a fresh path, never
trusted). Observe the pane (`tmux capture-pane -p -t <name>`) right after spawn.

Pass: the seeded prompt runs with no trust dialog stalling it. If a trust dialog appears, record it: the `startup_dialog` alarm must fire and nothing may have been typed into it
(`tmux capture-pane` shows it unanswered). Evidence:

> Open point (50-13), observe live: after the state root is trusted, does a new worker worktree still show the
> trust dialog? (Probe sessions on Claude Code 2.1.330 suggested a git worktree takes trust from its main repo.)
> `enable` does not gate on it; if the dialog shows, the tick's `startup_dialog` alarm reports it.

```

```
Timestamp: ____________

### Check 10 — The toy ledger end to end

Let the ticks run (a few). When `alarm-probe` is the only step left: `touch ~/epic50-rig/flags/pass`. Observe:

- `epic-driver status`: every step done (`recurring-note` shows its first occurrence done, then pending again by design).
- Verdicts in the log: `grep -i verdict ~/.local/state/epic-driver/epic-rig50/logs/tick.log`; each step's check re-run before done
  (`logs/checks/<step>.log` timestamps precede the done marks in `state.json`).
- Merge: `git -C ~/.local/state/epic-driver/epic-rig50/worktrees/integration/repo log --oneline --graph epic/rig50` shows the `--no-ff` merge of
  `epic-step/rig50/toy-worker`; `git -C ~/epic50-rig/repo merge-base --is-ancestor <sha> epic/rig50` is 0 (the sha check passed).
- Primary checkout untouched: `git -C ~/epic50-rig/repo status -sb` and `rev-parse HEAD` equal the baseline (section 0), still on `main`.
- Remote: `git -C ~/epic50-rig/remote.git reflog` / `for-each-ref` show no forced update (nothing is pushed unless the driver pushed `epic/rig50`; any push must be a plain fast-forward/new ref).

Pass: all of the above. Evidence:

```

```
Timestamp: ____________

### Check 11 — Alarms reach Telegram

- `check_error`: `touch ~/epic50-rig/flags/check-error` **before** `alarm-probe` passes (i.e. do this before check 10's `pass` flag, or `epic-driver mark alarm-probe reset` and `rm flags/pass` first).
  After three consecutive erroring ticks (about 30 min at check_every 5m / ticks every 10 min, or force with `epic-driver tick` x3) one `check_error` arrives with a ladder.
  Observe no 4-hourly repeat while the ladder is live (`logs/notify.log`), then `rm ~/epic50-rig/flags/check-error`: it clears.
- `delivery_deferred`: leave text in the driver pane's input line (as in check 7b) past the threshold; one alarm arrives, then clears when the line is emptied and the event is delivered.
- `listener_down`: `systemctl --user stop claude-questions-listen.service`; within a tick the driver tries one restart, then raises `listener_down` (via the local log and
  `status` if the relay path is down too). Restore with `systemctl --user start claude-questions-listen.service` (the tick may already have).

Pass: each alarm arrives once with a ladder, no repeat while live, clears; `listener_down` after one restart attempt. Evidence:

```

```
Timestamp: ____________

### Check 12 — Liveness by hand (P0-11)

`systemctl --user stop claude-epic-driver.timer`; wait 30 min; `epic-driver status`: `last tick` shows about 30+ min ago (stale `last_tick_at` in `--json`).
`systemctl --user start claude-epic-driver.timer`. Force a tick error (untested suggestion: `chmod 000 ~/.local/state/epic-driver/epic-rig50/state.json`, `epic-driver tick`, then `chmod 600` it back; any error that makes a pass fail works; a missing ledger gives `ledger_invalid`, not a tick error) and see `status` show `last error: ...`.
Confirm no daily "alive" line was ever sent (Telegram and `notify.log`).

Pass: stale `last_tick_at` visible; the forced error is shown; no daily line. Evidence:

```

```
Timestamp: ____________

### Check 13 — A startup dialog (lead note, 2026-11-22)

Needs a session spawned while Claude Code shows a startup dialog: the "Code Mode (experimental)" opt-in if the installed version still shows it
(your `~/.claude/profiles.toml` has `CLAUDE_CODE_EXPERIMENTAL_CODE_MODE` commented out in `[all-profiles]`; uncomment it to provoke the opt-in, and restore after),
otherwise any dialog that blocks the seed.

- **Unlisted:** remove the `startup_dialogs:` list from `ledger.yaml` (see the comment in the file), validate, provoke a fresh spawn (e.g. `epic-driver mark gated-step reset` or the next worker wave).
  Expect: `startup_dialog` reaches Telegram with the dialog's first lines, nothing answered or typed (`tmux capture-pane`), that pane never a delivery target.
- **Listed:** restore the entry (safe default `Enter` = "No, start without it"), provoke another fresh spawn. Expect: Enter sent exactly once, the seeded prompt runs, no alarm.

Pass: both cases. Evidence (note which dialog appeared and the claude version, `claude --version`):

```

```
Timestamp: ____________

## 3. Turning it off and removing the rig

```
epic-driver disable                 # stops the timer and the inhibitor; sessions and state kept; one notify
systemctl --user is-active claude-epic-driver.timer claude-epic-driver-inhibit.service   # inactive
# kill any leftover driven sessions: see `tmux ls` (names starting ws-rig50-... / rig50-...)
rm -rf ~/epic50-rig                 # the rig (workspace, scratch repo, bare remote, flags)
rm -rf ~/.local/state/epic-driver/epic-rig50   # optional: run state and worktrees (never deleted by the installer)
```

Remove the feature itself (the installer's real flag is `--uninstall`; `--without epic-driver` only skips it in a replay and keeps what is installed):

```
cd /data/sync/work/leangeeks-ai/claude-hooks && ./install.sh --uninstall epic-driver
```

It disables and removes the three units and the installed files, and leaves `~/.local/state/epic-driver/`. Also revert anything you changed by hand
(`CLAUDE_CODE_EXPERIMENTAL_CODE_MODE` in `profiles.toml`, a lowered `cap`). The enable step moved the shared tmux server into
`epic-driver-tmux.scope`; that persists until the server exits and is harmless.

## 4. Sign-off

All 13 checks passed: yes / no.  Failures and follow-ups:

```

```
