# 22-05 — Daily reviewer + store compaction

**Status:** todo · **Depends on:** 22-01 (reviews the new classifications),
22-04 (a queue to drain)
**Read first:** [brd.md](./brd.md) §2 D6/D8, §4 H6/H7/H8 ·
[state.md](./state.md) invariants 4, 6, 8, 9 · `docs/prompts/` (house home for
agent prompts) · epic 21 brd §1 (why the spawn must pin)

## Goal

The scheduled consumer that closes the loop: a daily session in the
claude-hooks workspace that drains its queue, reviews yesterday's permission
traffic, applies or rejects proposals with commits, files/fixes parser issues,
and keeps the stores from growing without bound.

## Scope

### 1. Compaction — a store-level function, not agent freelancing

`permission_state_store.py` gains `compact(max_age_days=30) -> dict` under the
existing flock protocol (invariant 6): move **terminal** rows older than the
cutoff from `permission_requests.jsonl` to
`permission_requests.archive.jsonl` (append), rewrite the hot file with the
rest, return counts. Pending rows are never moved regardless of age (expiry
handles them first). A small CLI entry (`python3 -m permission_state_store
compact` or a `--compact` arg in `__main__`) so the reviewer — and a human —
runs it without writing JSONL by hand. Rotate `bash_manual_confirm.log` the
same run: rename to a dated file beside it when over a size threshold; the
pretool hook recreates it on next append (verify `open(..., 'a')` behavior —
it does).

Tests: terminal-old rows move, pending-old rows stay, archive appends across
two runs, hot file remains valid JSONL, lock held for the rewrite.

### 2. The reviewer prompt — `docs/prompts/permission-review-daily.md`

Written for a fresh-context session started **in the claude-hooks checkout**.
Contents, in order:

1. **Drain the queue** for `resolve_project_key(cwd)` (the claude-hooks key):
   move each entry to `processed/` *before* acting on it, then act.
   - `allowlist_proposal` (scope `user`): judge the pattern (tighter is
     better; check deny/ask collisions; prefer `Bash(git log:*)` over
     `Bash(git:*)`), apply accepted ones to the repo's
     `.claude/settings.json`, reject with a written reason in the commit
     message otherwise.
   - `parser_issue`: reproduce with the replay harness
     (`CLAUDE_HOOK_REPLAY=1`, or `--dry-run`) against the current parser;
     small clear fixes may be made directly with tests, anything structural
     becomes a task-doc stub in `tasks/` for a human to triage.
2. **Review yesterday's traffic** via `permission_history(days=1)` +
   `bash_manual_confirm.log`: flag deny false positives (H1 watch), repeated
   asks that deserve a pattern (propose or apply per scope rules), and
   anything anomalous — an unexplained agent decision, a burst of denials —
   surfaced to the human in the summary.
3. **Apply + propagate:** commit the settings/queue outcomes (conventional
   message, queue entry ids referenced), then run the
   `install-claude-config.sh` merge so user-scope changes reach the global
   file (H6; invariant 8 — the run is named in the summary). Push only if the
   standing repo policy says so; otherwise leave the commit for the human.
4. **Compact** (§1) and report counts.
5. **Summarize** — a short report; the idle-notification hook forwards the
   last message to Telegram, which is the delivery mechanism for free.

Constraints restated inside the prompt: never edit a foreign checkout
(invariant 4); every applied pattern names its evidence
(`evidence_request_ids` or log lines); reject rather than guess.

**Workspace-scoped reviewers for other repos** reuse the same prompt shape —
document in the prompt header that steps 1–2 are workspace-generic (scope
`workspace`, no installer merge) so another repo can adopt it by copying the
file and its cron line. Shipping those adoptions is not this task.

### 3. The schedule

A system crontab line (survives reboots, no new infrastructure), documented in
the prompt header and in `docs/`:

```
15 6 * * *  <repo>/shell/permission-review-daily.sh
```

with a small launcher script in `shell/` that execs `amux-spawn spawn --dir
<claude-hooks> --model <alias> --effort <level> --no-attach -- "$(cat
docs/prompts/permission-review-daily.md)"` — **model and effort pinned**
(invariant 9 / brd H8; the epic-21 warning fires on this exact path if they
are not). amux-spawn gives the run a tmux home, Telegram notification on idle,
and `amux peek` for post-hoc inspection. **Cron's environment is nearly
empty** — the launcher must set `PATH` (amux, uv, `~/.local/bin`) and `HOME`
explicitly and not assume the interactive profile ran; log its own stderr to a
dated file under the repo's `temp/` so a silent 6:15 failure is diagnosable. The script is installed to `PATH` by
the installer like the other `shell/` entries — actually **verify** which
shell entries the installer copies and match that mechanism; if none fits, the
crontab line calls the repo path directly (the repo is stable on this
machine).

Installing the crontab line itself is a **human step** — the task documents
the exact line; 22-06 verifies a real run.

## Testing

- Compaction unit tests (§1 list).
- Prompt dry-run: start the reviewer session manually against a fixture queue
  (2 proposals — one good, one deny-colliding — and 1 parser issue with a
  known-reproducible false ask). Verify: entries moved to `processed/`, one
  pattern applied + committed, one rejected with reason, the parser issue
  reproduced and stubbed or fixed, compaction counts reported, summary
  produced.
- **The reviewer's own permission surface:** during the dry-run, record every
  permission prompt the reviewer itself triggers (git, jq, the installer, the
  compaction CLI) and add the missing patterns to this repo's
  `.claude/settings.json` — a scheduled session that blocks on Telegram at
  06:15 for its own tooling defeats the point. The dry-run is not done while
  the run still prompts.

## Done criteria

1. Suites green with the compaction tests; counts reported.
2. The fixture dry-run above, reported with the commit hash it produced.
3. The crontab line + launcher documented; **not installed** unless the epic
   manager asks (human step).
4. No edits outside `permission_state_store.py`, `docs/prompts/`, `shell/`,
   `docs/`, tests — plus whatever `tasks/` stubs the dry-run legitimately
   files.
