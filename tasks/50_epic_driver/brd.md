# Epic 50 — Epic driver: run a multi-day epic unattended, resumable after crashes and reboots

**Status:** ready for implementation (Phase 0 locked, nothing pending) · **Owner:** Anton · **Created:**
2026-11-22 · **Rev:** 3 (the folder form of spec rev 2.2)

> Broken down into tasks — see [state.md](./state.md). The design is in [architecture.md](./architecture.md).
> Sources: spec rev 2.2 (verbatim in [spec_rev2.2.md](./spec_rev2.2.md), which the review cites by line number) and
> the handover-readiness review `agents_output/50_spec_review_report.md` (3 blockers, 10 majors, 10 minors, all
> folded in here or in Phase 0).

## 1. Problem

An epic that runs for days has to keep moving while the operator is away and the workstation reboots: code waves,
releases, canary soaks, data collection and analysis. Today that needs a long-lived LLM supervisor session, and both
reference projects showed that this fails (§3). The first user is activecdn epic 037
(`/data/sync/work/activecdn/acdn-workspace/tasks/037_image_optimization/`), whose workflow decisions WF-1..WF-9 are
in its `state.md` log of 2026-11-22.

## 2. Goal

- **No silent stalls.** Every stop is a verdict, a question the operator has seen, or an alarm. §6 lists the stops
  this epic *cannot* make visible without the deferred dead-man.
- **Resumable from disk.** A crash, a reboot or a lost context loses no decided work.
- **Reasonably autonomous.** The operator is asked only what the epic's rules reserve for him.
- **Attachable.** The operator can attach to any driven session and steer it.

## 3. Background: what two reference projects taught

Studied 2026-11-22: `natasha/hyppie-flow` (PM ticks) and `tradingtips/leads-platform` (fleet controller).

1. **Both converged on the same core:** durable state in files, short sessions, a fixed verdict per turn, and async
   questions while other work continues.
2. **Long-lived supervisor sessions failed in both.** The hyppie-flow PM idled through a 19 h build and re-read its
   context cold on every wake (its ADR-0027). leads-platform ran 23 blocking Monitors in one wave and had units idle
   for 7 h 44 m unnoticed (its ADR-012).
3. **Both left their simple external watchdog unbound.** That is the piece that matters most; here it is designed and
   deferred (D11, §6).
4. **Derived status lies.** An API-killed child read `running` for 2 h 25 m; implementers reported `DONE` over red
   gates. So the driver re-measures from ground truth.
5. **The longest stalls were transport, not logic:** a permission prompt open for 14.6 h, a 20 h usage freeze, a
   4 h 50 m Fable limit.
6. **Cost is context size.** Main sessions were 60–71 % of spend.

## 4. Operator decisions (fixed; not reopened by any task)

| # | Decision | Where it binds |
|---|---|---|
| OD-1 | **Workstation-hosted.** The driver runs on this workstation; safety lives on the servers (D1). | architecture §1 |
| OD-2 | **Full YOLO with text rules, no hook fencing.** Driven sessions get `--yolo`; rules are prompt text (Anton: "proven during the past few months; strict fencing creates more issues than it prevents"). | §19 |
| OD-3 | **amux-spawn sessions, never `claude -p`**, so the operator can attach and steer. | §12, §13 |
| OD-4 | **On for one epic, then off.** Outside it the operator works the old way (a foreground planning session, a foreground implementation manager). | §18 |
| OD-5 | **Home is this repo** (`leangeeks-ai/claude-hooks`), where amux-spawn's source lives. The epic's ledger and cards stay in the epic's folder. | §17 |
| OD-6 | **The D4 cost-based reuse rule** (WF-6): reuse a warm session under its cap, rotate a cold or over-cap one, resume a dead one, keep warm only when the next event is close. | §11 |
| OD-7 | **A handoff only on rotation** (WF-7); a dead session is resumed in place, even when cold. The old session stays parked until the new one has started. | §11.3 |
| OD-8 | **The operator's manual messages don't pause events** (WF-8). Work he controls himself runs in sessions he starts by hand, outside the run id. | §9 |
| OD-9 | **Implementers never commit.** The driver commits, and decides whether and when to push; never a force-push (Anton: "the driver would know better than me"). | §14 |
| OD-10 | **Dead-man (D11): designed, implementation deferred.** No task builds it; nothing depends on it. The operator checks the driver manually during the first epic. | §6 |
| OD-11 | **037's ledger compile, its `roles.toml` and queue file, and its plan edits are 037's work**, not epic 50's. Epic 50's live verification uses a toy ledger. | §8 |

## 5. What the epic delivers

- `epic-driver`, a CLI whose `tick` subcommand a systemd user timer runs every 10 minutes. The tick has no LLM in it.
  It reads a YAML **step ledger** (the epic's program), a runtime-state JSON, the amux registry and the transcripts,
  runs the steps' checks, and starts or wakes an LLM session **only on an event**.
- Driven sessions are amux-spawn sessions with pinned model, effort, profile and compaction environment, deterministic
  names, and a fixed **verdict** line at the end of every event turn.
- A **driver** session (one per epic) does the epic's actions under its standing approvals, asks the operator through
  `mcp__questions__ask`, and closes worker waves (tests, commit, merge, push). **Worker** sessions (implementer,
  reviewer) run in driver-owned worktrees and never commit.
- `amux-spawn resume` gains a Claude branch; the lifecycle reducer's `terminated` latch clears on a later turn.
- An installer feature `epic-driver`, off by default, and `docs/epic-driver.md` with the epic-author guidance.

## 6. The silent-stall gap (review M8)

"No silent stalls" cannot hold for the following without the deferred dead-man. They are accepted for the first epic,
which the operator watches by hand:

- a dead timer, or a tick that never runs;
- a tick that crashes before it can notify (e.g. on an import error or a stamp mismatch whose notify also fails);
- a machine that is suspended, or rebooted and not yet unlocked and logged in (P0-6: reboots are rare and never
  unattended, and with disk encryption the machine does not finish booting until the operator unlocks it; the driver
  recovers once he has logged in);
- a workstation that is off;
- a dead notify path (relay down, token revoked): alarms go to the local log and `status` only.

D11's old fallback — noticing the absence of phase-transition notifies — does not work when a phase lasts days (037's
R4 runs until Monterey leaves). What the epic does instead:

- `epic-driver status` shows `last_tick_at` and the last tick error (architecture §20); the operator checks it
  himself.
- **No daily "alive" line** (P0-11, operator: "No daily line"). Liveness is phase-transition and alarm notifies only,
  plus `status`. A quiet Telegram therefore means "nothing happened" *or* "the driver is dead"; only `status` tells
  them apart.
- `docs/epic-driver.md` tells epic authors (D1): anything with a deadline shorter than the operator's absence moves to
  a server cron; server-side failure alerts must reach the operator directly, never only through the driver. For
  037 this concerns the SA1–SA3 rollbacks ("the lead is notified") and its lead check-ins, which are the only watcher
  of the server watchdog. Checking where those notifications really go is 037's work.
- Example from 037: the Monterey access-log re-pull is a driver step with a deadline warning (architecture §5.6). With
  the workstation dead, that warning never fires.

## 7. Success criteria

- [ ] A toy ledger runs end to end: steps become due, the driver session acts and ends with verdicts, the tick
      re-runs each step's check before marking it done.
- [ ] A reboot, then the operator unlocks the disk and logs in **without opening a terminal**: the timer fires, the
      tick runs with a working `PATH` and ssh, the sessions that should resume do, the tmux server sits in its own
      scope, a session it spawns is alive 15 min later, and `go version && cargo --version` work in its pane.
- [ ] A killed worker pane is resumed in place on the same model and effort, with its context.
- [ ] A forced rotation produces a handoff, a fresh session seeded with it, and the old session parked until the new
      one has started.
- [ ] An event is never typed into a non-empty input line, attached or not, and is delivered once it is empty.
- [ ] A question asked by the driver reaches Telegram; its answer reaches the step; `withdrawn` closes it.
- [ ] Every alarm in the catalogue (architecture §16) has a unit test that raises it and one that clears it.
- [ ] Nothing in the operator's primary checkouts is moved, reset, checked out or committed by the driver.

## 8. Out of scope

- **The dead-man (D11).** Its design stays in [spec_rev2.2.md](./spec_rev2.2.md) D11: a heartbeat endpoint on the
  relay (`POST /v1/heartbeats/<name>`), a reaper alert when overdue. No relay task is in this epic.
- **037's own preparation:** compiling its ledger and step cards, adding `.claude/roles.toml` and a queue to
  acdn-workspace (still missing on 2026-11-22), moving C10's SA3 rollback trigger to h01, replacing "AskUserQuestion"
  in its plan with `mcp__questions__ask`.
- **Adopting orphan sessions.** v1 alarms on an orphan and leaves it (review m4).
- **Hook fencing** of driven sessions (OD-2).
- **Running two ledgers at once.** One active ledger per workstation.

## 9. Prerequisites (state on 2026-11-22)

- Re-install so the installed question tools match the repo: **done** (installed hooks byte-identical to the repo,
  per the review). `claude-questions-listen`: **active**.
- The epic workspace needs `.claude/roles.toml`, a queue file, and `[questions] sync` set to `"commit"` or off:
  **037's work**, checked by `enable` (architecture §18.1).
- Live check 7's `withdrawn` path for a question without buttons needs task 49's relay redeploy (still pending);
  50-12 uses a question with buttons (review m7).
