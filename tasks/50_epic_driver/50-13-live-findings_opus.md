# 50-13 — Fixes from the 50-12 live run (F-1..F-4)

**Status:** todo · **Depends on:** 50-10 (code); runs while 50-12 is in progress
**Read first:** the lead brief `agents_output/50-13_live_findings_brief.md` (the binding spec for this task) ·
`agents_output/50-12_live_log.md` "Findings for epic 50" (the evidence) · [architecture.md](./architecture.md) §6.2,
§12.11, §15.1, §16, §17, §21, §22 · [state.md](./state.md) P0-10, P0-12, P0-13 · `docs/epic-driver.md` §4 ·
[50-12-signoff-evidence.md](./50-12-signoff-evidence.md)

## Live-run constraint (binding)

The toy run `epic-rig50` is **enabled and live**: the timer is active, its sessions run on the driver's tmux server, and
the operator still has checks 1–3 to run.
- Never run `epic-driver enable|disable|tick|mark|queue` against it.
- Never read or write `~/.local/state/epic-driver/epic-rig50` or the rig's files.
- Never touch the driver's tmux server, its sessions or the systemd units.
- Tests use the fakes or throwaway dirs (temp HOME, temp state root) only.
- Do not run `./install.sh`: the manager does that after the commit.
- Do not write `~/.claude.json`.
- `~/.claude/.credentials.json` holds secrets: you may read its structure and its expiry fields, but never print,
  log, copy or commit token values. Fixtures must be synthetic.

## Scope

1. **F-1, important.** `BoundedWorld.git` (`.claude/hooks/epic_driver_lib.py`, ~l.7508) sets `expired` before every
   clamped call, so the ledger stage (`LEDGER_END_S` 30 == `GIT_TIMEOUT_S` 30) always defers. With the fix, `expired`
   is set only when:
   - a call is refused (< 1 s left), or
   - a clamped call actually timed out.

   A clamped call that returned in time is a normal result. Audit `prepare_worker_steps` (~l.7524) and every other
   `BoundedWorld` user for the same spurious deferral.
2. **F-2, minor.** No `due_not_started` while either of these holds:
   - the step's approval question is open;
   - it is answered, but the answer event to the driver has not settled (the grant is not yet recorded).

   It still fires when no question exists.
3. **F-3.** A new `enable` gate refuses (exit 3) unless both the workspace and the driver state root
   (`~/.local/state/epic-driver/`) are trusted. Trust means `~/.claude.json` → `projects["<abs>"]` has
   `hasTrustDialogAccepted = true` on the path itself or on any parent.
   - The fix text names the folder and how to trust it: open `claude` there once, or set the key.
   - `enable` never writes `~/.claude.json`.
   - A missing or unreadable `~/.claude.json` refuses, with the fix text.
   - Document the one-time trust of the state root in `docs/epic-driver.md` §4 and its setup steps, and note it in
     the 50-12 runbook.
4. **F-4.** Find a reliable machine-readable source for the login expiry, the one behind Claude Code's banner "Your
   login expires in N days · run /login to renew". It is not the short-lived access token, which refreshes itself.
   Report what you found and how you verified it.
   - **If a reliable source exists:**
     - `enable` warns, or refuses with a fix, when the login expires within N days (default 7, configurable).
     - The tick raises `login_expiring` (subject: the run) at ≤ 3 days, then `login_expired`.
   - **If it doesn't:** detect the banner in the startup pane capture of §12.11 at spawn and resume, and raise
     `login_expiring` with the days left.
   - Either way: add the alarm(s) to architecture §16's table, and the alarm clears after a re-login.
5. **Optional, low.** Fix the `ssh_without_connect_timeout` false positive only if the fix is trivial, for example
   skipping the warning when the script sources a lib that defines the options. Otherwise leave it and say so.
6. **Mid-run stamps.** Read §17 and the tick's stamp check. Report whether replacing the installed files and stamps
   while a run is live needs anything beyond `./install.sh --yes` followed by a tick. If the design says it does, the
   report says exactly what.

Architecture is amended where the fix changes stated behaviour (§16 table, §12.11 or the `enable` gate list, §6.2).

## Tests (from the brief)

- **F-1:**
  - A fast git call with `left` < `GIT_TIMEOUT_S`: not expired, and base refs are verified.
  - A clamped call that times out: expired, with the note.
  - Regression: a normal pass's notes carry no deferral message.
- **F-2:** both windows suppress the alarm; with no question, the alarm still fires.
- **F-3:** trusted directly, trusted through a parent, not trusted, key missing, `~/.claude.json` absent or unreadable.
- **F-4:**
  - the parser on synthetic fixture text or JSON;
  - the alarm thresholds;
  - the alarm clears after a re-login.

## Done when

- `python3 tests/run_all_tests.py` passes, run in the foreground.
- Every scope item is implemented, or is explicitly reported as not applicable with a reason.
