# 50-12 rig report (2026-11-23)

## Dry-run and install
- `./install.sh --yes --with epic-driver --dry-run`: replay plan of 10 existing features, all `update`; the only change was `+ install Epic driver`
  (writes `~/.local/bin/epic-driver`, `~/.claude/epic-driver/`, 3 systemd unit files, none enabled). No removal, no other feature changed, no prompt.
- Real run (stdin `/dev/null`, no `--all`): same plan, ended "Hook modules are working correctly"; `epic-driver: installed`. amux update ran without a sudo prompt.
- Note: the docs' removal flag is `./install.sh --uninstall epic-driver`. `--without epic-driver` only marks it skipped in a replay and keeps the files (verified with a dry-run).

## Read-only post-install checks
- `~/.local/bin/epic-driver` -> `~/.claude/bin/epic-driver`: yes.
- `~/.claude/epic-driver/stamps.json`: exists (also `lib/`, `prompts/`).
- Units: `claude-epic-driver.timer` disabled, `claude-epic-driver-inhibit.service` disabled, `claude-epic-driver.service` static; all three `inactive`.
- `~/.local/state/epic-driver` does not exist yet (nothing enabled).

## Rig tree (`~/epic50-rig/`)
```
remote.git/                       bare, has main (initial commit)
repo/{README.md,test.sh}          primary checkout on main, 1 commit, origin=../remote.git
flags/                            empty; alarm-probe fixture flags (check-error, pass)
ws/                               git repo (1 commit)
  .claude/roles.toml              default=operator, [questions] sync="off", dir docs/questions, file questions.md
  docs/questions/questions.md     queue
  prompts/toy-{implementer,reviewer}.md
  plan/ledger.yaml
  plan/ledger/cards/{driver-hello,soak-watch,recurring-note,gated-step,toy-worker,ssh-reach,alarm-probe}.md
  plan/ledger/checks/{driver_hello,soak_watch,recurring_note,gated_step,worker_extra,ssh_reach,alarm_probe}.sh
```
Questions sync: `"off"`. The gate accepts off or commit and refuses push; off starts no git process for the queue and commits nothing. The workspace is still a git repo because `anchor="repo"` needs one.

## Ledger (`ws/plan/ledger.yaml`)
epic `rig50`, run `epic-rig50`, workspace `/home/anton/epic50-rig/ws`, plan_root `plan`, defaults check_every 5m, timeout 2h.
- Roles: driver (sonnet, medium), implementer and reviewer (sonnet, low, `epic_prompt` in `prompts/`), all `profile: claude`, `cap: 100000`,
  `pinned_env` autocompact 65 and idle compaction 300000 (OQ-2). Profiles defined in `~/.claude/profiles.toml`: claude is the plain Anthropic one; sonnet keeps cost low.
- `startup_dialogs`: `code-mode-optin`, match `Code Mode \(experimental\)(?s:.*)❯ 1\. No, start without it`, key Enter; comment says to delete the list for check 13's unlisted case.
- Approvals: `RIG-A1` (conditions driver-hello).
- Steps: `driver-hello` (driver + check); `soak-watch` (watch, needs driver-hello, after 5m); `recurring-note` (driver, every 20m);
  `gated-step` (driver, approval RIG-A1); `toy-worker` (worker implementer/reviewer over `/home/anton/epic50-rig/repo`, base_ref main);
  `ssh-reach` (watch; check runs `ssh -o ConnectTimeout=5 -o BatchMode=yes $RIG_SSH_HOST true`); `alarm-probe` (watch; flags `check-error` -> exit 3, `pass` -> 0).

## Validate
`~/.local/bin/epic-driver validate /home/anton/epic50-rig/ws/plan/ledger.yaml` (no override env), rc 0:
```
warning: [context_window_default_fallback] roles.driver: no known context window for model 'sonnet' ... default 1000000 tokens ...
warning: [context_window_default_fallback] roles.implementer: (same)
warning: [context_window_default_fallback] roles.reviewer: (same)
/home/anton/epic50-rig/ws/plan/ledger.yaml: valid (run epic-rig50, 7 steps, 3 warning(s))
```
The 3 warnings are benign: the alias `sonnet` is not in models.json, so a 1M window is assumed (cap 100000 is below 650K and 300K either way).
`claude-questions --check` was NOT run: `_check_tokens` does `GET /v1/installations/me` against the relay, which this task forbids.
Instead I loaded the workspace's `[questions]` config read-only via `questions_store`: it loads, sync `off`, no config errors.

## Operator must fill in
- `RIG_SSH_HOST` in `~/epic50-rig/ws/plan/ledger/checks/ssh_reach.sh` (marked `>>> OPERATOR`; placeholder `CHANGE_ME_SSH_HOST`, check exits 1 until set). Nothing contacted any host.

## What would make `enable` refuse / notable effects
- Gate 4: `claude-questions-listen.service` must be active (it is now).
- Gate 5: `claude-questions --check` needs the relay reachable and the installation token bound; I could not exercise this. Sync is off (accepted).
- Gate 6: test notify must dispatch (relay).
- Gate 7: the shared tmux server (pid 16651, many live sessions) is inside `ptyxis-spawn-*.scope`. `enable` will MOVE it into `epic-driver-tmux.scope`
  (or refuse with instructions). This affects all your sessions on that server (intended; check 3).
- Gate 2: refuses if `EPIC_DRIVER_ALLOW_UNSTAMPED` is set anywhere (not set in this shell); stamps ok after install.
- Gate 1: another active run (none). `CLAUDE_PROJECT_DIR` is unset here; if set in the enabling shell it would redirect roles lookup.
- `enable` writes `ws/plan/epic-driver.run.json` (pointer file) into the scratch workspace: it will make that repo dirty; harmless.

## Deliverable
Runbook: `tasks/50_epic_driver/50-12-signoff-evidence.md` (untracked; the only repo file created). Report: this file.

## Blockers
None. Unverified by design: gate 5/6 (relay), the 13 live checks, `epic-driver status` output.
