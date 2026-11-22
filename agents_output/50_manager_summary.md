# Epic 50 — implementation manager summary (2026-11-23)

## Outcome

All engineering tasks are done, reviewed until clean, and committed on `main` (not pushed; `main` is 11 commits
ahead of `origin/main`). 50-11 was dropped (P0-6). **50-12 (live verification, `_human`) is blocked on the
operator**; nothing in it was run or simulated.

| Task | Commit | Reviews / fixes | Suite after |
|---|---|---|---|
| lead note: startup dialogs folded into 50-04/05/06/12 + architecture | 807b817 | — | — |
| 50-01 amux-spawn flags, pinned env, Claude resume | 889a9cb | 2 / 2 | 2482 OK |
| 50-02 lifecycle `terminated` latch | 0e2c8b8 | 1 / 1 | 2489 OK |
| 50-03 measurement helpers | 34e7739 | 1 / 1 | 2520 OK |
| 50-04 ledger, schedule, checks, state | 7e59110 | 2 / 2 | 2685 OK |
| 50-05 reconcile and stalls | efad5af | 2 / 3 | 2784 OK |
| 50-06 dispatch policy and delivery | ac0ce85 | 2 / 2 | 2861 OK |
| 50-07 worker waves | 4b24190 | 2 / 1 | 2908 OK |
| 50-08 tick, CLI, alarms, notify, tmux | aa1fde1 | 2 / 2 | 3030 OK |
| 50-09 role prompts | 80ce028 | 2 / 2 | 3046 OK |
| 50-10 docs and installation | 76d1c6b | 1 / 1 | 3056 OK (2 skipped) |

Per-task reports: `agents_output/50-0N_*`, `50-10_*`. Decisions taken on review findings are logged in
`tasks/50_epic_driver/state.md` (Log).

## Things the operator should know

- **Installed state changed.** `./install.sh --yes` (manifest replay, lead-allowed) was re-run during 50-10: the ten
  selected features were refreshed, so 50-01…50-09's changes to amux-spawn, the lifecycle reducer, the status line,
  context-mcp and questions-mcp are **live**. `epic-driver` itself is **not installed**; the install manifest now
  records it as `skipped`. Nothing was enabled; no `epic-driver*` unit, state dir, tmux scope or pointer file exists.
- **Spec amendments made from verified findings** (architecture): §13.3 (resume prompt without `--`; only a missing
  model refuses; growth check only with a prompt), §8 (state keys `by`, `check.last_error`), §12.11 + §16.2 + §5.2
  (startup dialogs, lead note), §14.3 (`queue --to <role>`), §16.1 (first repeat 4 h after the ladder ends).
- **Fixture provenance.** The "Code Mode (experimental)" opt-in pane is synthesised (not reproducible here); the
  folder-trust and input-box panes are real captures from a throwaway tmux server. 50-12 check 13 covers it live.
- **Accepted limits** (reviewed, left as is): `_git_env` does not scrub `GIT_CONFIG_*`; the git allowlist checks
  flags, not argv shape, for read-only verbs; the push-without-`-C` prompt test is per-line (a second test covers it).
- **Commit trailers.** The haiku committer's harness signs with its own model; the 50-04, 50-07 and 50-08 commit
  messages were amended locally (message only, unpushed HEAD) to the project's `Claude Opus 5.5` trailer.

## What 50-12 needs from the operator

`tasks/50_epic_driver/50-12-live-verification_human.md`, 13 checks, evidence in `50-12-signoff-evidence.md`:

1. `./install.sh --yes --with epic-driver` (installs the feature; enables nothing).
2. A scratch workspace with `.claude/roles.toml`, a question queue (`sync = "commit"` or off), a toy ledger (driver
   step with a check, watch step with `after`, recurring step, one approval, one worker step over a scratch repo with
   a bare remote, pinned compaction per OQ-2, optionally a `startup_dialogs` entry) — never 037's.
3. `epic-driver enable <ledger>`, then the checks: reboot + login without a terminal, 8 h no-suspend, the tmux
   server outliving its terminal, attach, killed worker resumed, forced rotate, deferred delivery, a relay question,
   a never-seen worktree, the toy ledger end to end, alarms on Telegram, liveness by hand, a startup dialog.
