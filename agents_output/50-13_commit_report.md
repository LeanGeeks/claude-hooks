# 50-13 commit report

## Commit hash

482eca1fac1cea092ade8ff1905567f59decb9fc

## Full message as committed

```
fix(epic-driver): 50-13 — live-run findings F-1..F-4

- F-1: BoundedWorld marks a pass expired only on a refused call or a
  clamped git call that actually timed out; the tick verifies repos
  and base refs again.
- F-2: due_not_started is held while the step's approval question is
  open or answered but not yet granted.
- F-3: enable gate: the workspace and ~/.local/state/epic-driver/ must
  be trusted in ~/.claude.json (path or a parent); enable never writes
  it. Docs and the 50-12 runbook describe the one-time trust.
- F-4: login expiry from claudeAiOauth.refreshTokenExpiresAt; enable
  refuses within --login-days (default 7); the tick raises
  login_expiring (<= 3 days) and login_expired.
- validate: no ssh_without_connect_timeout false positive for
  ssh wrapped in a sourced lib.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

## Committed files

- .claude/bin/epic-driver
- .claude/hooks/epic_driver_lib.py
- agents_output/50-13_fix_report.md
- agents_output/50-13_implementation_report.md
- agents_output/50-13_live_findings_brief.md
- agents_output/50-13_review_report.md
- agents_output/50-13_review_report_2.md
- docs/epic-driver.md
- tasks/50_epic_driver/50-12-signoff-evidence.md
- tasks/50_epic_driver/50-13-live-findings_opus.md
- tasks/50_epic_driver/architecture.md
- tasks/50_epic_driver/state.md
- tests/test_unit_epic_driver.py

## Remaining git status --short

(empty, working tree clean)
