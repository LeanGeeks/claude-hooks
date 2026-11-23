# 50-13 — fixes from the 50-12 live run: manager summary (2026-11-23)

## Outcome
F-1..F-4 and the optional ssh warning are fixed, reviewed clean, committed and pushed. The toy run `epic-rig50` was not
disabled, paused or touched beyond the single `epic-driver tick` the brief asked for.

| Step | Result |
|---|---|
| Task file | `tasks/50_epic_driver/50-13-live-findings_opus.md` (one task, one opus implementer for F-1..F-4) |
| Implementation | `agents_output/50-13_implementation_report.md` |
| Review 1 | CLEAN, 2 LOW notes (`50-13_review_report.md`) |
| Fix | operator decision on F-3 applied, both LOWs addressed (`50-13_fix_report.md`); suite 3100 OK, 2 skipped |
| Review 2 | CLEAN, 2 LOW notes, accepted (`50-13_review_report_2.md`) |
| Commit | `482eca1` fix(epic-driver): 50-13 — live-run findings F-1..F-4 (13 files; Opus trailer) |
| Push | `fcca922..482eca1 main -> main`, fast-forward, not forced (it also carried `aec70d4`, the 50-12 runbook) |
| Install | `./install.sh --yes`, exit 0; epic-driver updated, stamps.json written last, revision `482eca1` (16 modules) |
| Tick | `epic-driver tick` 11:24:46Z, exit 0: pass 57 ok in 11 s, alarms +0 ~0 -0 |
| Status | `stamps ok (482eca1)`, no `stamp_mismatch`, ALARMS (none), NOTIFY FAILURES (none); timer still active and enabled, inhibitor held |

**F-1 confirmed live.** In `tick.log`, every pass up to 56 (old code) ends with "repos and base refs not checked with git
this pass (pass deadline)". Pass 57, the first on the new code, does not.

**Mid-run stamps.** `./install.sh --yes` followed by one tick was enough, as the implementer read from §17 and the
code. That held live: the stamps moved from `fcca922` to `482eca1` with no mismatch.

## What changed
- **F-1:** `BoundedWorld` marks a pass expired only when a call is refused (< 1 s left) or a clamped git call actually
  timed out. The implementer also audited `prepare_worker_steps` and the other `BoundedWorld` users.
- **F-2:** `due_not_started` is held while the step's approval question is open, and while it is answered but not yet
  granted. It still fires when no question exists.
- **F-3:** a new `enable` gate refuses with exit 3 unless both the workspace and `~/.local/state/epic-driver/` are
  trusted. Trust means `~/.claude.json` `projects[<path>].hasTrustDialogAccepted` is set on the folder itself or on a
  parent. `enable` never writes `~/.claude.json`. The gate is documented in `docs/epic-driver.md` §4 and the setup
  steps, and noted in the 50-12 runbook.
- **F-4:** the login expiry comes from `~/.claude/.credentials.json` `claudeAiOauth.refreshTokenExpiresAt` (no
  secret values in the code, fixtures or reports).
  - `enable` gate 5 refuses when the login has expired, or expires within `--login-days N` (default 7; 0 = never
    refuse).
  - The tick raises `login_expiring` (run subject) at ≤ 3 days, then `login_expired`. Both clear after a `/login`.
  - Both alarms are added to the architecture §16.2 table.
- **Optional:** `validate` no longer gives the `ssh_without_connect_timeout` false positive when ssh goes through a
  sourced lib.

## Decision and open point (operator)
- **Operator decision:** "We do need to trust the parent folder, to cover all the worktrees."
  - The implementer had also added a per-repo trust gate beyond the brief. It was removed, so the gate covers the
    workspace and the state root, as the brief says.
- **Open point for 50-12:** the implementer's throwaway-HOME probes indicated that Claude Code takes a git worktree's
  trust from its main repo, not from a trusted parent. Review 2 also noted that Claude Code ignores parent trust above
  a git root.
  - So a worker worktree may still show the trust dialog even with the state root trusted.
  - The runbook asks to observe this live. If it happens, the tick's `startup_dialog` alarm reports it and never
    answers the dialog.
- **Re-running `enable` now has two more gates:**
  - the workspace and the state root must be trusted;
  - the login must not expire within 7 days, or pass `--login-days`.

## Accepted LOW notes
- A leftover one-iteration loop in `trusted_by`.
- A worktree's own trust key counts as trusted. This is harmless now that repos are not gated.
