# Lead brief: fixes from the 50-12 live run (2026-11-23)

Evidence and context: `agents_output/50-12_live_log.md` (section "Findings for epic 50"). The operator wants F-1..F-4
fixed now, in parallel with the remaining 50-12 checks. Run them through the usual process of
`docs/prompts/implementation_manager.md`:
- one implementer per fix, or one for F-1+F-2 and one for F-3+F-4, your call;
- review rounds until clean;
- you commit;
- push main fast-forward only, never force.

File them as task 50-13 (one task file) and log the decisions in `tasks/50_epic_driver/state.md`.

## Live run constraint
The toy run `epic-rig50` is **enabled and live**: timer active, sessions on the driver's tmux server, and the
operator's checks 1–3 still to come (reboot, logout, 8 h idle).
- Do not disable it, pause it, or touch its sessions or state.
- Tests must use the fakes or throwaway dirs, never `~/.local/state/epic-driver/epic-rig50`.
- After the commit, re-run `./install.sh --yes` so the installed copy and the stamps match. Then run one
  `epic-driver tick`, confirm `epic-driver status` shows `stamps ok (<new sha>)` and no `stamp_mismatch`, and report
  what happened.
- If the design says a mid-run stamp change needs something else, follow the design and say so.

## F-1, important: the tick never verifies repos or base refs
- `.claude/hooks/epic_driver_lib.py` `BoundedWorld.git`, around line 7508: `if left < timeout_s: self.expired = True`
  is set before the call. At the ledger stage `LEDGER_END_S` (30) equals `GIT_TIMEOUT_S` (30), so `left` is always
  < 30.
  - Every live pass logged "repos and base refs not checked with git this pass (pass deadline)" and re-loaded with
    `assume_git=True`, even 11-second passes.
- **Fix:** `expired` only when a call is refused (< 1 s left) or a clamped call actually timed out. A clamped call
  that returned in time is a normal result.
- **Audit** the other user, `prepare_worker_steps` (around line 7524), for the same spurious deferral.
- **Tests:**
  - a fast git call with `left` < `GIT_TIMEOUT_S` → not expired, base refs verified;
  - a clamped call that times out → expired and the note;
  - a regression test that a normal pass's notes don't carry the message.

## F-2, minor (alarm noise): `due_not_started` between "answered" and "granted"
- Live sequence:
  - Q-001 (RIG-A1) was answered via the relay at 06:55:41.
  - Before the driver's turn that ran `mark --grant RIG-A1` (06:59), the tick raised `due_not_started gated-step`
    ("approval (RIG-A1 ungranted)"), which sent Telegram message 9211.
- §6.2 / §16 say not to alarm for a reason the operator already sees, and an answered approval question is one.
- **Fix:** while the step's approval question is open, or answered with the answer event to the driver not yet
  settled, `due_not_started` is not raised.
- **Tests:** cover both windows, and that the alarm still fires when no question exists.

## F-3: folder trust is neither documented nor gated
- Every new workspace and every worker worktree hits Claude Code's "Do you trust this folder?" dialog. The tick
  correctly never answers it (`startup_dialog`), but the step stalls until the operator attaches.
- The operator's decision (11-23): **trust `~/.local/state/epic-driver/` once**. Claude Code inherits trust to every
  folder below; this was verified live with a probe session.
  - The setting is `~/.claude.json` → `projects["<abs path>"].hasTrustDialogAccepted = true`.
  - Lookup: the path or any parent with `true`.
- **Fix:**
  - a new `enable` gate that refuses (exit 3) unless the workspace and the driver state root
    (`~/.local/state/epic-driver/`) are each trusted, themselves or through a parent. The fix text names the folder
    and how to trust it (open `claude` there once, or the `~/.claude.json` key).
  - `enable` must **not** write `~/.claude.json` itself, because trust is the operator's security decision.
  - `docs/epic-driver.md` §4 and the setup steps: document the one-time trust of the state root.
  - the 50-12 runbook: note it.
- **Tests:** trusted directly, through a parent, not trusted, missing key, and an unreadable or absent
  `~/.claude.json` (refuse with the fix).

## F-4: login expiry is not detected
- A fresh Claude session showed "Your login expires in 3 days · run /login to renew". A multi-week run would lose
  every session at expiry.
- **Fix:**
  - Find a reliable source for the expiry: look at `~/.claude/.credentials.json` and at what Claude Code reads for
    that banner. Don't confuse it with the short-lived access token, which refreshes itself.
  - If there is a reliable machine-readable source:
    - `enable` warns, or refuses with a fix, when the login expires within N days (default 7, configurable);
    - the tick raises a `login_expiring` alarm (run subject) at ≤ 3 days, then `login_expired`.
  - If there isn't: detect the banner text in the startup pane capture the tick already does at spawn and resume
    (§12.11), and raise `login_expiring` with the days left.
    - Banner variants seen: "Your login expires in N days · run /login to renew".
  - Add the alarm(s) to architecture §16's table.
- **Tests:** the parser on fixture text or JSON, the alarm thresholds, and that the alarm clears after a re-login.

## Optional, low: `ssh_without_connect_timeout` false positive
- The 037 ledger's checks call ssh through a shell function (`acdn037_ssh` in `driver/checks/lib.sh`, which applies
  `$EPIC_DRIVER_SSH_OPTS`), yet `validate` warns on `m1-sa3-close` and `ops-checkin`.
- Leave it as is unless the fix is trivial, for example skipping the warning when the script sources a lib that
  defines the options. It is a warning, not a gate.

Report back in `agents_output/50-13_summary.md`, with the commits and the installed stamp.
