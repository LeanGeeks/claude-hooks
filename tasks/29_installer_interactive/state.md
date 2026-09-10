# Epic 29 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md)
and [architecture.md](./architecture.md). Each task file is written for a
fresh-context agent and carries its own "read first" refs, done criteria and
tests. This file owns **cross-task invariants**, **ordering**, and one safety
rule that applies to every task in the epic.

## ⚠ The bootstrapping hazard — read before spawning anything

The committed installer is safe to run; the **half-finished** one is not, and the
project's standing instructions cannot tell them apart. `implementer.md` §Step 5
and `reviewer.md` §Step 6 both tell agents to re-run `install-claude-config.sh`
to make a hook edit live — correct advice in every other epic, and in this one it
points straight at the script being rewritten. An agent running a partial
registry migration against the real `$HOME` can drop hook wiring from
`~/.claude/settings.json`, remove a module a still-installed feature imports, or
corrupt `~/.claude.json` (which the pre-epic script never backs up — 29-04 §4).
The failure is quiet: the file still validates as JSON, and the symptom arrives
later, in a live session, as a permission prompt that no longer routes.

This repo is self-hosting. The hooks in `~/.claude/hooks/` are what the agent's
own session runs on, so a bad run breaks the thing that would fix it.

**The structural mitigation — 29-02 §0, do not skip it.** The first commit of
29-02 copies the script to `install.sh` and **freezes**
`install-claude-config.sh`. From then on:

- All epic work happens in `install.sh`. `install-claude-config.sh` is not edited
  by any task before 29-09.
- The documented command therefore stays safe. An agent that follows
  `implementer.md` verbatim, a reviewer following `reviewer.md:58`, and the
  06:15 daily-review job all keep running a script that works.
- Task line-number citations into `install-claude-config.sh` (29-03 `:536-602`,
  29-04 `:646-794`, 29-08 `:451`) stay valid for the whole epic instead of
  rotting the moment 29-02 lands.
- `install.sh` additionally refuses to run against the real `$HOME` unless
  `CLAUDE_INSTALL_EPIC29_LIVE=1` (29-02 §0.1). 29-09 §6 removes the guard and
  collapses the two files back into one.

**Rule for every task in this epic, no exceptions:**

- Agents run `install.sh` **only** against a temporary `HOME`, with
  `CLAUDE_INSTALL_NO_EXTERNAL=1` set (architecture §10.1).
- No agent runs `install.sh` against the real `$HOME`. If a task seems to require
  it, that is a **BLOCKER** for the user, per
  `docs/prompts/implementation_manager.md` §59-68 — the same class as "needs the
  live relay".
- Running the **frozen** `./install-claude-config.sh` against the real `$HOME`
  stays permitted and is the right thing to do when a task needs a hook edit
  live — 29-01 is the case that needs it.
- 29-10 is where the real machine gets touched by the new script, by a human,
  deliberately.

`implementer.md:8`/§Step 5 and `reviewer.md:8`/§Step 6 now carry this rule
directly, so it survives a manager that forgets to relay it. Pass it along
anyway, alongside the task file.

## No Phase 0

Every cross-cutting decision is locked in brd §3 (D1–D17). Two were chosen by
recommendation rather than hard requirement and are flagged in D17 as one-line
changes if they prove wrong:

1. **Per-feature failure isolation** rather than abort-on-first-failure
   (29-05 §2).
2. **The `profiles-autosource` sub-toggle** (5a), which was not requested — added
   because the two `.bashrc` lines are mutually exclusive and that logic has to
   exist regardless. Deleting it does not disturb anything else.

If either is revisited, record the change here before the affected task starts.

## Tasks

**This table is the authoritative status.** Each task file carries a `Status:`
line in its header for a reader who opens it alone; those are the values as of
handover and are not updated during execution. Update the table, per
`docs/prompts/implementation_manager.md` §81-89.

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 29-01 | [Permission hooks: local mode](./29-01-permission-hook-local-mode_opus.md) | ready | — | **opus.** The only Python task, and it fixes a live defect: `permission_request_hook.py:1551` exits on "Telegram disabled" *before* the yolo/bypass auto-allow at `:1603`, so a relay outage turns a `--dangerously-skip-permissions` session back into a prompting session. Independent of all installer work, and the one task that may run the frozen `./install-claude-config.sh` against the real `$HOME` to make its hook edit live. |
| 29-02 | [Registry, manifest, probes, harness](./29-02-registry-manifest-probes.md) | ready | — | Foundation for everything below. **Its first commit (§0) freezes `install-claude-config.sh` and creates `install.sh`** — the epic's safety mechanism; everything after it edits only `install.sh`. Adds the **first automated coverage the installer has ever had**. Behaviour-neutral: the script still installs what it installs today, through the registry. |
| 29-03 | [External-surface seam](./29-03-external-surface-seam.md) | todo | 29-02 | The safety boundary. `ext_*` functions + `CLAUDE_INSTALL_NO_EXTERNAL`. Nothing else in the epic may touch crontab, systemd, tmux, symlinks or `.bashrc`. |
| 29-04 | [Settings ownership](./29-04-settings-ownership.md) | todo | 29-02 | Stops the two clobbering writes (`:665-669`, `:770`). Subtlest case is the shared `Notification` array — two matchers, two owners. |
| 29-05 | [Executors and uninstall](./29-05-executors-and-uninstall_opus.md) | todo | 29-02, 29-03, 29-04 | **opus.** The task that can destroy a machine. Module closure, refcounted removal, failure isolation. |
| 29-06 | [Interactive selector](./29-06-interactive-selector.md) | todo | 29-02, 29-05 | The checklist. Tri-state cycling, dependency promotion, disclosure lines. |
| 29-07 | [CLI and subcommands](./29-07-cli-and-subcommands.md) | todo | 29-02, 29-03, 29-05 | Flags, `--dry-run`, and `enable`/`disable` — the latter is what discharges rev 0's Fact 1. |
| 29-08 | [New toggles](./29-08-new-toggles.md) | todo | 29-02, 29-03, 29-05 | `claude-history` and daily permission review. The latter installs **no files** — it only schedules. Reverses a documented stance in `docs/permission-review-daily.md`. |
| 29-09 | [Migration and docs](./29-09-migration-and-docs.md) | todo | 29-01 … 29-08 | The once-only path every existing user walks. Validate against the **real** pre-epic script — 29-02 §0 leaves it frozen in the tree for exactly this. Also owns the cutover (§6): remove the guard, collapse the two scripts, strip the epic-29 paragraphs from `implementer.md` / `reviewer.md`. |
| 29-10 | [Live verification](./29-10-live-verification_human.md) | blocked | 29-09 | **human** — the four surfaces no automated test may touch, plus the migration path on a real machine. |

## Dependency graph

```
29-01 ──────────────────────────────────────────────────────┐
                                                            │
29-02 ──┬──► 29-03 ──┬──► 29-05 ──┬──► 29-06 ──┐            │
        │            │            │            │            │
        └──► 29-04 ──┘            ├──► 29-07 ──┼──► 29-09 ──┴──► 29-10
                                  │            │                 (human)
                                  └──► 29-08 ──┘
```

Read as: **29-01 is fully independent** — it is Python, touches no installer
code, and could run at any point. Everything else funnels through 29-02, and
29-05 is the join. 29-06, 29-07 and 29-08 are siblings that do not touch each
other's code.

## Recommended order

Numeric order is the execution order — the table above is the queue.

1. **29-01 first**, even though it is independent. It is the riskiest change in
   the epic and the only one with runtime consequences; getting it landed and
   reviewed early means everything after it is pure installer work.
2. **29-02 next and alone.** Nothing else can start until the registry exists,
   and every later task extends its test harness.
3. **29-03 before 29-04.** Both depend only on 29-02, but 29-03 is the safety
   boundary — having the seam in place before the executors exist means no
   intermediate commit can write a real crontab.
4. **29-05** is the join and the highest-risk installer task. Do not start it
   until 29-03 and 29-04 are both reviewed and committed.
5. **29-06, 29-07, 29-08** in any order (sequentially — the manager does not run
   parallel agents). They share no code.
6. **29-09**, then **29-10** with the user.

## Implementer model

Per `docs/prompts/implementation_manager.md` §5-16, the filename suffix decides.
Two tasks carry `_opus`:

- **29-01** — changes control flow in the permission hook, which is the component
  where a subtle bug means a permission is silently auto-allowed or a session
  hangs. It also has to preserve every existing relay-present path byte-identically.
- **29-05** — deletes files and settings keys, with a refcount that has to be
  right across seven shared modules and ten features. A mistake here removes a
  module a still-installed feature imports, and the symptom appears later, in a
  different feature, as an ImportError.

Everything else is `sonnet`. Reviewer stays `sonnet` throughout.

**29-02 is the largest single task in the epic** — it restructures a 1186-line
script into the registry while keeping its behaviour identical, and builds the
manifest, the probes and the test harness on top. It is broad rather than
intricate (each feature's install code already exists as a contiguous block; the
work is lifting it into a function), which is why it stays `sonnet`. But if the
implementer reports it as too large, **split it rather than accepting a partial
registry**: the natural seam is registry + lifted features (behaviour-neutral,
verifiable on its own) versus manifest + probes + harness. **§0 and §0.1 — the
freeze and the guard — go with the first half**, whatever else moves; they are
what makes the rest of the epic safe to run at all. A half-migrated
registry — some features lifted, some still inline — is the one intermediate
state that must not be committed, because invariant 3's closure computation is
wrong for any feature still outside it.

## Cross-task invariants

1. **No path fabricates a permission decision.** With no relay and neither yolo
   nor bypass, the hook declines to decide and Claude Code's terminal prompt
   runs. Epic 23 brd §2.1 established this; 29-01 must not weaken it.
2. **The manifest records intent; the disk is truth.** Probes read the disk and
   never the manifest (brd D2). A probe that consults the manifest breaks every
   existing machine's first run.
3. **Hook modules install as a dependency closure and are always refreshed.**
   Including under `Keep` (brd D3). `install-claude-config.sh:852`: *"partial
   installs must never occur."*
4. **A module is removed only when no still-installed feature owns it**
   (brd D4, `MODULE_OWNERS`). Seven modules have more than one owner.
5. **Every write outside `~/.claude` goes through an `ext_*` function that
   honours `CLAUDE_INSTALL_NO_EXTERNAL`.** No exceptions, no direct calls to
   `crontab`, `systemctl`, `loginctl`, `tmux set` or `ln -s` anywhere else
   (architecture §8). 29-03 ships a grep test that enforces this.
   One deliberate exemption, worth writing down so it is not rediscovered as a
   bug: the legacy-daemon `kill` at `install-claude-config.sh:141-150` is gated on
   a pidfile **under `$HOME`**, so a temp `HOME` makes it inert and it needs no
   seam. It is not on the grep list; if that gating ever changes, it goes on the
   list.
6. **Not-installed means inert, never broken.** A feature that is absent leaves
   no import error, no dangling settings entry, no symlink to nothing. This is
   what makes the toggles independent at all, and it is what 29-01 fixes for the
   permission hook.
7. **Every external write is marked, idempotent, backed up and reversible.** The
   marker carries its own undo instruction (architecture §8.1). `crontab -r` is
   never called, under any circumstance.
8. **The installer never destroys configuration it does not own.** Foreign hook
   entries, foreign permission patterns and foreign MCP servers survive install,
   update and uninstall (brd constraint 2.7).
9. **JSON writes are validate-then-replace, all-or-nothing per run.** Build,
   `jq empty`, write, re-validate, restore from backup on failure — the existing
   discipline at `:1004-1021`, extended to `~/.claude.json`.
10. **Uninstall removes machinery, never user data.** `profiles.toml` (API
    tokens), `history.jsonl`, `~/.claude/projects/`, the state stores and
    `/usr/local/bin/amux` are never deleted by any uninstall path.
11. **Nothing is asked after the confirmation.** probe → plan → confirm →
    execute → report (brd D6). `install-amux.sh` can block for 900 s inside
    `execute`; a prompt behind it would strand the run.

## Log

- **2026-09-10** — Epic scoped with the user across four rounds of decisions.
  Rev 0's single fact (the `[questions_listen]` opt-in) is preserved as brd
  constraint 2.2/D15 and is discharged by 29-07.
- **2026-09-10** — Two defects found during scoping, both now owned by tasks:
  the yolo/bypass ordering bug (29-01 §2) and the absence of any installer test
  coverage together with four `HOME`-escaping side effects (29-03, architecture
  §10.2).
- **2026-09-10** — Bootstrapping hazard restructured after review. The blanket
  "never run the installer" rule was replaced by 29-02 §0's frozen copy plus
  29-02 §0.1's guard, and the rule was written into `implementer.md` and
  `reviewer.md` rather than relying on the manager to relay it. 29-09 §6 owns the
  cutover.
- **2026-09-10** — Three further defects found in the same review, now owned:
  `~/.claude.json` is written with no backup while 29-04 assumed one existed
  (29-04 §4); the daily-review seed prompt runs the installer unattended from the
  working tree, which after this epic must mean `--yes` manifest replay and not a
  defaults install (29-08 §2.1, 29-07 §1.1); and the legacy-daemon `kill` is an
  unlisted-but-safe external call (invariant 5).
