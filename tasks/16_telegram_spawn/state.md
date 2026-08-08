# Epic 16 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md)
and [architecture.md](./architecture.md). Each task file is written for a
fresh-context agent and carries its own "read first" refs, done criteria and
tests. This file owns **cross-task invariants** and **ordering**.

**No Phase 0.** Every cross-cutting decision is locked below. The one genuine
unknown (multi-line seeding through amux) is scoped inside 16-06 and cannot block
anything before it.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 16-01 | [Relay command queue](./16-01-relay-command-queue.md) | todo | — | `commands` table, long-poll + result endpoints, two waiter registries, reaper, client methods. **Touches no Telegram code.** |
| 16-02 | [Relay command surface](./16-02-relay-command-surface.md) | todo | 16-01 | `/new` + `/ls`, target resolution and fan-out, `c:` callbacks, every chat-visible string |
| 16-03 | [Launcher `--plain` / `--json`](./16-03-launcher-plain-flag.md) | todo | — | Explicit tracked/plain, machine-readable spawn output |
| 16-04 | [Seen-store](./16-04-seen-store.md) | todo | — | `telegram_workspaces.py`, three writers, allowlist semantics |
| 16-05 | [Listener runtime](./16-05-listener-runtime.md) | todo | 16-01, 16-04 | `amux-spawn listen`: loop, lock, dispatch, `resolve`/`ls`, caps ledger, `--status` |
| 16-06 | [Spawn and wizard](./16-06-spawn-and-wizard.md) | todo | 16-03, 16-04, 16-05 | Modifiers, workspace resolution, wizard, preflight, subprocess spawn |
| 16-07 | [Installer, diagnostics, docs](./16-07-installer-diagnostics-docs.md) | todo | 16-02, 16-06 | systemd unit, installer step, `docs/telegram-spawn.md`, top-level `architecture.md` |
| 16-08 | [Live verification](./16-08-live-verification_human.md) | todo | 16-07 | **human** — needs two bound machines, a real relay and a laptop that actually sleeps |

## Dependency graph

```
16-01 ─┬──────────────► 16-02 ──────────────┐
       │                                    ├─► 16-07 ─► 16-08
       └─► 16-05 ─► 16-06 ──────────────────┘
16-03 ──────────────► 16-06                    (16-08 is human)
16-04 ─┬─► 16-05
       └─► 16-06
```

## Recommended order

1. **Start 16-01, 16-03 and 16-04 together** — three independent roots. 16-03 and
   16-04 are small; 16-01 is the critical path.
2. **16-02 and 16-05 in parallel** once 16-01 lands (16-05 also needs 16-04).
   They are opposite ends of the same wire and share only the envelope in
   architecture §2.3 — fix that shape before either starts and they cannot drift.
3. **16-06** after 16-05. Answer the multi-line seeding question *first*, in ten
   minutes, before writing the wizard: the answer can change what the spawn step
   does.
4. **16-07**, then **16-08** live.

The first end-to-end moment is the end of 16-05: `/new` reaches the machine and
comes back with an honest "not implemented". Demonstrate that before starting
16-06 — it proves the whole channel independently of the feature.

## Implementer model

16-01, 16-02, 16-05 and 16-06 carry real concurrency or protocol risk and should
go to the stronger implementer model; 16-03, 16-04 and 16-07 are mechanical.
Reviewer stays consistent throughout.

- **16-01** — atomic claim under concurrent pollers, two-phase TTL, waiter
  lifecycle. A race here is invisible until it duplicates a session.
- **16-02** — parsing, resolution and *every user-facing string*; the fan-out is
  async and must not block the webhook.
- **16-05** — a long-lived process with threads, locks, backoff and signals; the
  failure modes are sleep/wake and restart overlap, not logic.
- **16-06** — talks to humans over a lossy channel and then executes; every
  branch must end in either a session or a sentence.

## Decisions locked

Settled in design review. Do not relitigate without a brd revision.

1. **Only `/new` and `/ls` carry commands.** A loose message is not inert — it is
   injected into a single open session (`app.py:1355`) — so the slash prefix is a
   safety mechanism, not a style choice (brd §2.2).
2. **The relay routes; the listener decides.** No directory, profile, prompt or
   template ever reaches the server. `resolve` answers `{claim, ambiguous}`, and
   a machine with two same-named workspaces disambiguates with the user itself.
3. **Live-only delivery, and refusal when not live.** A spawn aimed at a machine
   whose `last_seen_at` is stale is refused and **not inserted**; nothing is
   stored and forwarded. One command, one outcome, one message (brd §5.4).
4. **Telegram-spawned sessions are plain**, which is why `--plain` exists.
   Tracked sessions are the agent path (epic 10), not the phone path.
5. **The seen-store is the allowlist.** No path typed into Telegram is ever
   accepted; a picker choice is an index into a list the listener built.
6. **The spawn runs as a subprocess.** `cmd_spawn` mutates `os.environ`
   (`amux-spawn:173-180`); a listener that lives for weeks must never do that.
7. **`+tokens` are one namespace**, resolved profile-first then tier, with a
   collision refused rather than guessed. `@` stays reserved for epic 15 roles.
8. **Wizard state is in-memory.** A restart loses in-flight wizards and their
   messages expire. Never resume a half-specified spawn.
9. **No `[listen]` config, or `enabled = false`, = today's behaviour byte for
   byte.** Every task asserts this; it is the regression floor.

## Cross-task invariants

- **Command envelope** = architecture §2.3 (single source). 16-02 writes it,
  16-05/16-06 read it. `workspace`, `prompt` and `modifiers` are all optional —
  absence is what invokes the wizard.
- **Result envelope** = `{ok, summary, detail, data}` (16-01). The listener
  supplies facts; 16-02 owns every rendered string. The queue layer posts nothing.
- **Timeouts** = architecture §6. Do not invent a new one in a task file.
- **State files**: `~/.claude/telegram_workspaces.json` (seen-store),
  `telegram_spawns.json` (ledger), `amux-spawn-listen.lock`,
  `amux-spawn-listen.status.json`. Atomic tmp+rename under `flock`, corrupt reads
  as empty, everywhere.
- **New hook modules must join `REQUIRED_HOOKS`** in the task that creates them
  (`telegram_workspaces.py` in 16-04, `amux_listen_lib.py` in 16-05) — the
  installer copies only what that list names, and a mid-epic install would
  otherwise ship a router that cannot import its own dependency.
- **Fail open, never wedge.** A store, a listener or a relay that is down must
  degrade to today's behaviour, never to a broken session.
- **No new logs under `~/.claude`.** The listener logs to the journal; task 11
  documents what happens when hook logs grow unattended.

## Verification gates

Unit tests cover the logic. Three things can only be confirmed live, and 16-08
batches them: a **second machine bound to the same chat**, a **laptop that
actually sleeps**, and **systemd restarting a killed listener**. Nothing about
sleep/wake, reconnect or `kill -9` mid-wizard is provable against fakes.

Also re-run `install-claude-config.sh` on both machines after 16-07 — until then
`telegram_workspaces.py` and `amux_listen_lib.py` are invisible to the installed
hooks, and an import of a module the installer never copied fails in exactly the
way these hooks are built to swallow silently.

## Standing risks

- **This is a remote-code-execution channel.** Every task that widens what a
  Telegram message can reach — a path, a flag, a new command kind — needs the
  brd §5.6 bounds re-checked, not assumed.
- **Multi-line seeding through amux is unverified** (16-06). It is the common
  case, not an edge case; if it breaks, the fix belongs in the amux fork
  (epic 12), not in a flattening hack.
- **Epic 15 lands in the same files.** `telegram_permission_router.py` gains a
  client registry there and seen-store writes here; whichever lands second
  rebases. The listener uses the **default role's** token — if roles change that
  assumption, this epic's brd §4.1 needs revising.
- **Epic 11 (permission delivery) is still open** and a resident listener is
  plausibly part of its answer. Decide that deliberately; do not let this epic
  absorb it by accident.
