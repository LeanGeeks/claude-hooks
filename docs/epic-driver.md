# The epic driver

`epic-driver` runs one epic's **step ledger** unattended on this workstation. A systemd user timer starts a tick
every 10 minutes. The tick has no LLM in it: it reads the ledger, a runtime-state file, the amux registry and the
session transcripts, runs each step's check, and starts or wakes an LLM session **only on an event** (a step became
due, a worker finished, a check failed, an answer arrived, an approval is needed, a session died, an alarm cleared).
Between events it costs nothing.

Two kinds of session do the work, both `amux-spawn` sessions with a pinned model and effort:

- one **driver** session per epic does the epic's actions under its standing approvals, asks you questions through
  `mcp__questions__ask` (so they arrive in Telegram), and closes worker waves (tests, commit, merge, push);
- **worker** sessions (an implementer and a reviewer) run in driver-owned git worktrees and never commit.

Every turn a driven session ends on a one-line **verdict** (`ADVANCED`, `WAITING`, `ASKED`, `HALT`, plus `DONE` for
workers). The verdict is a claim: the tick re-runs the step's check before it marks anything done.

This guide is for two readers: the **epic author**, who writes the ledger and the step cards (sections 3 to 5), and
the **operator**, who turns the driver on and off and watches it (sections 1, 2 and 6 to 9). The design, with every
rule and number, is in [the epic's architecture](../tasks/50_epic_driver/architecture.md); you do not need it to use
the driver. Section references below (`§n`) point there for the curious.

## 1. Install, turn on, turn off

There are two separate switches, and neither is on by default.

**Switch 1, the installer feature.** The feature `epic-driver` is off on a fresh machine. It requires `amux`,
`statusline`, `keep-awake` and `questions` (the installer pulls them in). Select it in the interactive checklist, or:

```bash
./install.sh --yes --with epic-driver        # replay your recorded selection, and add this feature
./install.sh --only epic-driver --yes        # a fresh machine: the feature and its prerequisites
```

This places the CLI at `~/.claude/bin/epic-driver` (symlinked as `~/.local/bin/epic-driver`), its libraries and the
two role prompts under `~/.claude/epic-driver/`, a version manifest `stamps.json`, and three systemd user units. It
**enables none of the units**, so nothing runs yet. What exactly it writes: [installer.md](./installer.md#what-each-feature-writes).

The driver runs only from these installed copies. A repo edit is not live until you run `./install.sh --yes` again.
The CLI checks every file it imports against `stamps.json` before it starts; a mismatch (you edited an installed
file, or a file the manifest covers was changed outside the installer) stops it with `stamp_mismatch` and the fix is
`./install.sh --yes`. The check cannot be bypassed on an installed copy.

The installer rewrites `stamps.json` on every run in which the feature is installed or already recorded as installed,
at the very end of the run, after all other features have installed. That includes runs that name other features,
such as `./install.sh --only keep-awake`, so those do not leave it stale. While the feature is off (never installed,
or uninstalled) no manifest is written or kept; `./install.sh --uninstall epic-driver` removes it together with the
rest of the feature's files and leaves the run state in `~/.local/state/epic-driver/`.

**Switch 2, `epic-driver enable <ledger>`.** Run it from your own interactive shell (it snapshots your `PATH`, Go,
Rust and Node environment for the tick, which runs without a login shell):

```bash
epic-driver validate tasks/037_image_optimization/ledger.yaml   # lint only, changes nothing
epic-driver enable   tasks/037_image_optimization/ledger.yaml
```

Before the first `enable`, trust the folders the driven sessions start in, once (section 4, "Folder trust"): the
epic workspace and the state root `~/.local/state/epic-driver/` (the state root covers the worker worktrees).

`enable` refuses on the first gate that fails, says why, and says what to run (exit 3). In order: no other run is
active; the stamps agree; the ledger validates, including every `base_ref`; Claude Code trusts the workspace, the
state root (section 4); the claude.ai login does not expire within 7 days (`--login-days N`
changes the window, `--login-days 0` turns that refusal off; an expired login always refuses); the questions listener is active;
`claude-questions --check` passes for the epic workspace and its `[questions] sync` is not `"push"`; a test notify
goes out; the tmux server is not inside a terminal's scope (or it is moved into its own scope); then it snapshots
the environment, writes the run state, starts `claude-epic-driver.timer` and `claude-epic-driver-inhibit.service`,
verifies the sleep-inhibit lock, and sends one Telegram line, "epic-driver on for <epic>".

Only one ledger is active at a time.

**Turning it off:**

```bash
epic-driver pause [--reason TEXT]    # start nothing new; the tick still watches and raises alarms
epic-driver resume
epic-driver disable                  # stop the timer and the inhibitor; sessions and state untouched; one notify
```

`disable` leaves driven sessions running in tmux and the state under `~/.local/state/epic-driver/` alone, so
`enable` on the same ledger picks the run up again. To remove the feature itself, `./install.sh --uninstall epic-driver`
(it stops, disables and removes the units and installed files, and never deletes `~/.local/state/epic-driver/`).

After a reboot the user manager starts the enabled timer once you have unlocked the disk and logged in (lingering is
on); the first tick restarts the tmux server in its own scope and resumes the sessions that were mid-turn. You do not
need to open a terminal.

## 2. Command reference

```
epic-driver validate <ledger>                      lint only; exit 0/1
epic-driver enable <ledger> [--login-days N]       the gates above; exit 0, or 3 with the failed gate
epic-driver disable                                stop the timer and the inhibitor
epic-driver pause [--reason TEXT] | resume
epic-driver status [--json]                        the status screen (section 7)
epic-driver tick [--dry-run]                       one pass; --dry-run computes and prints, writes nothing
epic-driver mark <step> done|reset                 override a step by hand
epic-driver mark --grant <approval> [--question Q-n] [--note TEXT]
epic-driver queue <step> --to <role> --file <path> send feedback or an answer to a worker
```

Exit codes: 0 ok, 1 error, 2 usage, 3 refused. Every writing verb takes the tick lock and waits up to 300 s for a pass
in progress to finish.

`mark <step> reset` sets a step back to pending with its attempts cleared and its alarms gone (sessions untouched).
`mark <step> done` records it done by operator without running the check. Both are how you clear
`attempts_exhausted`, `halt` and the deadline alarms.

## 3. Writing the ledger

The ledger is YAML in the epic folder. The complete field list is architecture §5; a small example:

```yaml
version: 1
epic: "037"
workspace: /data/sync/work/activecdn/acdn-workspace     # the driver's --dir and the questions workspace
plan_root: tasks/037_image_optimization                 # cards and checks resolve against this
defaults: {check_timeout: 60s, check_every: 10m, timeout: 6h, max_attempts: 3}
roles:
  driver: {model: fable, effort: high, profile: claude-fable}
approvals:                       # map order is ask order; grants are never written here
  SA4: {conditions: [e1-build]}
steps:
  - id: monterey-export
    title: "Re-pull the Monterey access log"
    card: ledger/cards/monterey-export.md
    role: driver
    every: {period: 2d, from: "2026-11-22T09:28:20Z", deadline: 7d, warn_before: 2d}
    check: "ledger/checks/monterey_export_fresh.sh"
    timeout: 2h
```

A step with a `role` is an **action step**: the driver does it. A step with no `role` is a **watch step**: nothing
acts, only its `check` runs until it passes. A step may be `due` by `needs`, `after`, `not_before`, `every` / `at`
or a `trigger`, and is dispatched only when its `approval` is granted, it is inside its `window`, its `resource`
units are free and the run is not paused. `epic-driver validate` reports every structural mistake, and the tick
re-validates on each pass (an invalid edit raises `ledger_invalid` and the tick keeps running the last valid copy).

### Rules for what goes into a ledger

- **Deadlines shorter than your absence do not belong to the driver (D1).** Anything with a deadline shorter than the
  time you may be away (log TTLs, timed extracts, rollback triggers) must be a cron or timer on the server, and the
  server-side failure alert must reach you directly, never only through the driver. If this workstation is off,
  suspended or locked, the driver does nothing (section 8).
- **Checks.** A check is a shell command run by `/bin/sh -c` in `<workspace>/<plan_root>`. Its exit code means: `0`
  done, `1` not yet (silent, tried again at the next `check_every`), `2` failed (an event goes to the driver with
  the card and the output tail), anything else or a timeout is a check error (`check_error` alarm after three in a
  row). Checks must be **idempotent and read-only**: the tick never runs an action. For `ssh` use
  `$EPIC_DRIVER_SSH_OPTS` (`-o ConnectTimeout=5 -o BatchMode=yes`). Checks get a fixed environment (`PATH` from your
  snapshot, `HOME`, `LANG`, `EPIC_DRIVER_RUN_ID`, `EPIC_DRIVER_STEP`, `EPIC_DRIVER_STATE_DIR`) and about 150 s of
  total check time per tick.
- **Windows shorter than 20 minutes are rejected.** A `check_window` whose span is under 20 minutes (for example "0 to
  5 minutes after the swap") belongs in the step card, which the driver session runs itself, or on the server. Put the
  +0 verification in the card; use the `check` to re-verify the claim from ground truth afterwards.
- **Cards.** One markdown file per step, and the card must exist. It says: what to do, how, which checks to run, how
  to roll back, and what **not** to do. The driver follows the card and the epic's reviewed scripts; it writes
  production state only through them.
- **Approvals.** The `approvals:` map holds ids, `conditions` (steps that must be done first) and **order**: map order
  is ask order. The driver asks one approval at a time, through a question with a recommendation. Grants are recorded
  with `epic-driver mark --grant <id> [--question Q-n]`, which the driver runs after your yes. An approval you already
  gave by hand (037's SA1 to SA3) is recorded the same way after `enable`, before the first dispatch; there is no
  `granted` key in the ledger.
- **Worker steps.** `role: {worker: {implementer: <role>, reviewer: <role>, repos: [{path, base_ref}]}}`. The two
  roles must differ. The driver creates `epic/<epic>` and per-step branches in its own worktrees under the state
  directory from `base_ref`, never from the state of your checkout, and never touches a branch you have checked out.
  The worker prompt wraps the epic's own `epic_prompt` for the role and adds: never commit, never ask the operator
  (draft the question and end `HALT`), end `DONE: <step>` after writing the report.
- **Roles and the questions workspace.** The epic workspace needs a `.claude/roles.toml` and a queue file, with
  `[questions] sync` set to `"commit"` or off (never `"push"`). `enable` checks all of it.

## 4. Startup dialogs: `startup_dialogs`

Claude Code can open an interactive dialog when a session starts (an opt-in such as "Code Mode (experimental)",
folder trust, and others). Until someone answers it, the seeded prompt is blocked. By default **the tick never answers
a dialog**: it raises the `startup_dialog` alarm with the dialog's first lines, and that pane is never a delivery
target.

To let the tick answer a dialog you know to be safe, list it in the ledger:

```yaml
startup_dialogs:
  - id: code-mode-optin
    match: 'Code Mode \(experimental\)(?s:.*)❯ 1\. No, start without it'
    key: Enter
```

- `id`: `[a-z0-9-]+`, unique in the list.
- `match`: a regular expression that must match the captured pane text, **including the highlighted safe option**. Make
  it as specific as a human's judgement ("this dialog, and the safe answer is the one selected").
- `key`: `Enter` or `Escape`. Nothing else can be sent.

The tick sends the key once per spawn or resume and dialog id, with no alarm. If the same dialog is still there on the
next pass, `startup_dialog` is raised and no second key is sent. Folder trust is a security decision: put it in no
list. `validate` rejects a duplicate `id`, a `match` that does not compile and a `key` outside those two.

### Folder trust: a one-time setup

Claude Code asks "Do you trust this folder?" the first time it starts in a folder nobody trusted, and the tick never
answers that dialog. Every new worker worktree would stall on it. Trust these once, before the first `enable`:

1. **The state root `~/.local/state/epic-driver/`.** It holds the wave directories of multi-repo worker steps and,
   by default, the worktree root; trust on it covers every folder below it that is not inside a git repository.
2. **The epic workspace** (the driver's folder).

Trusting the state root once covers the worker worktrees below it. If a new worktree still shows the dialog, the
tick's `startup_dialog` alarm reports it (the tick never answers it); then trust that folder as above.

To trust a folder, open `claude` there once and answer "Yes, I trust this folder" (then exit), or set
`projects["<absolute path>"].hasTrustDialogAccepted = true` in `~/.claude.json` yourself. Inside a git repository a
trusted folder above the repository root does not count. `enable` checks the workspace and the state root (gate 4) and refuses with the
folders it found untrusted; a missing or unreadable `~/.claude.json` refuses too. It never writes that file: trust is
your decision.

## 5. Watching a run: status, attaching, talking to a session

### Attaching

Every driven session is an ordinary amux-spawn session on the driver's own tmux server, named
`<workspace-dir>-<epic>-<role>[-<step>]-g<generation>` (for example `acdn-workspace-037-driver-g1`; workers use their
worktree's directory, for example `037-t1-037-implementer-t1-g1`). `epic-driver status` lists the names. To look:

```bash
amux-spawn a acdn-workspace-037-driver-g2        # attach
```

You can attach to any of them and steer it. The tick never types into a pane that has text in its input line, whether
or not you are attached, and it does not rotate or kill a session you are attached to; it delivers once the input is
empty.

### What an operator message does

A message you type yourself carries no event id (`E-<n>`). The session answers it normally and **does not need to end
with a verdict**: turns without an event id are exempt from the verdict rule, so a correction or a question from you
never counts as a missing verdict. If you change something the ledger depends on, tell the session in plain words and
use `mark` for state the tick owns. Leave the pane's input line empty when you detach, or the next event waits.

## 6. Alarms

An alarm is raised when the tick finds a condition (a step overdue, a session stuck, a check erroring, a
stamp mismatch, ...) and cleared when the condition goes. Raising sends a Telegram notify that you must acknowledge,
repeated every 4 hours while it stays active. Alarms and the `enable`/`disable` and phase-transition lines are the
only notifies the driver sends. There is **no daily "alive" message** and no setting for one.

The full catalogue, with when each alarm is raised and when it clears, is architecture
[section 16.2](../tasks/50_epic_driver/architecture.md#162-catalogue). Everything the tick considers "not an alarm" is
in §16.3.

## 7. The status screen

`epic-driver status` is one screen, newest facts first (`--json` gives the same data):

```
epic-driver 037 · run epic-037 · ON (timer active, next 09:40:00Z; inhibitor held) · not paused
last tick 2026-11-24T09:30:05Z (2 min ago) ok · last error: none
tmux server 16651 in epic-driver-tmux.scope · listener active · stamps ok (v1.4-12-g2a2f966)
phase E1: 3/5 done
STEPS · SESSIONS · OUTBOX · QUESTIONS / APPROVALS · ALARMS · NOTIFY FAILURES (last 3)
```

Read the second line first: `last tick` (`last_tick_at` in `--json`) is how long ago the timer last ran the tick, and `last error` is why the
last pass failed, if it did. `stamps` says whether the installed files still agree. `status` still works when the stamps
disagree, as do `disable`, `pause` and `resume`. Alarms the notify path could not deliver are in `NOTIFY FAILURES` and
in `~/.local/state/epic-driver/<run_id>/logs/notify.log`.

Run state lives under `~/.local/state/epic-driver/<run_id>/`: `state.json`, `events/`, `handoffs/`, `reports/`, and
`logs/` (`tick.log`, `checks/<step>.log`, `notify.log`). Systemd's own log of a tick is
`journalctl --user -u claude-epic-driver.service`.

## 8. What the driver does not guarantee: the silent-stall gap

**A quiet Telegram means "nothing happened" or "the driver is dead", and only `epic-driver status` tells them apart.**

The driver reports through the tick. Nothing outside the workstation notices when the tick does not run, so these are
not covered, and the first epic is run with you watching by hand:

- a dead timer, or a tick that never runs;
- a tick that crashes before it can notify (an import error, or a stamp mismatch whose notify also fails);
- a machine that is suspended, or rebooted and not yet unlocked and logged in (reboots are rare and never unattended; the
  driver recovers once you have logged in);
- a workstation that is off;
- a dead notify path (relay down, token revoked): alarms then go only to the local log and `status`.

The deferred dead-man (a heartbeat the relay would watch) is out of scope. What you have instead:

- run `epic-driver status` and look at `last tick` and `last error`;
- there is no daily alive line; liveness is the phase-transition and alarm notifies plus `status`;
- anything with a deadline shorter than your absence runs on a server cron with its own alert path (section 3). For an
  epic like 037 that means rollback triggers and lead check-ins are server-side, and a driver step that only *warns* of
  a deadline (the Monterey re-pull) warns nothing if the workstation is dead.

## 9. Troubleshooting

| Symptom | Look at | Usual fix |
|---|---|---|
| `enable` refuses | its message names the gate and the fix | do what it says, run `enable` again |
| `stamp_mismatch` | `epic-driver status`, the message names the file | `./install.sh --yes` with the feature on |
| Step due but nothing starts | `status` STEPS and ALARMS; paused? approval question open? window closed? model limited? | resolve the named reason |
| An event is not delivered | the pane's input line has text | clear it (the tick waits for an empty line) |
| `startup_dialog` alarm | attach, read the dialog, answer it | add a `startup_dialogs` entry if it is safe; a folder-trust dialog means a folder was not trusted (section 4) |
| `login_expiring`, `login_expired` | the days left in the alarm | run `/login` in any `claude` session; the alarm clears on the next tick |
| `attempts_exhausted`, `halt` | the step's card and the driver's last report | fix, then `epic-driver mark <step> reset` |
| Quiet Telegram and a stale `last tick` | `systemctl --user status claude-epic-driver.timer` | section 8 |
