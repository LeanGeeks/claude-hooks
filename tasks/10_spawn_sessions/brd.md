# Epic 10 — Agent-spawned amux sessions

**Status:** planning · **Owner:** Anton · **Created:** 2026-06-21 · **Rev:** 6

> Rev 4 made `amux-spawn` the unified human+agent entry point. **Rev 5** resolves
> the review: extend amux (task 12) for the launch controls we need; **human
> sessions are plain/restartable, agent sessions are tracked**; restart is
> resume-aware; nested-tmux uses switch-client; activity is read from transcript
> mtime; env denylist + auth precedence fixed; run-id auto-inherits.
> **Rev 6** (adversarial review): env via tmux **`update-environment`** (plain
> inheritance is broken on a running server — spike-verified, no `ps` leak);
> **E4 split** (E4-floor blocks 10-01); detached launch via amux **`--no-attach`**
> (E5); fork-bomb cap **16 / per-workspace**, flock-atomic; `--stuck-after` in the
> handle; **re-derive `running` from transcript mtime** (Stop only fires at turn
> end); permission↔session match is precise (`session_id`); handle schema now
> defined (architecture §6.0); transcript encoding maps `.`→`-` too.

## 1. Problem & thesis

Claude Code supports only **one level of agent nesting**, and a single long-lived
orchestrator overflows its context window. **Thesis:** each amux session is a
*fresh Claude process* with its **own context window** and its **own one-level
`Agent` budget**, so spawning amux sessions **re-roots the nesting limit** and
**escapes context overflow** via handoff:
`iter-1 planner → iter-1 impl orchestrator (spawns workers) → iter-2 planner → …`

Secondary: amux is multi-step, so the operator forgets it and runs bare `claude`
(losing Telegram follow-up) and piles up tmux tabs. Making `amux-spawn` the
one-command launcher **and** quick-switcher fixes both, for humans and agents.

## 2. Two topologies (chain primary)

| topology | shape | who stays alive | result channel |
|----------|-------|-----------------|----------------|
| **Chain / handoff** (primary) | link N seeds link N+1 then **steps aside** | nobody waits | handoff artifact the successor reads (caller's convention) |
| **Supervised fan-out** (secondary) | one live orchestrator launches N workers and collects | orchestrator stays alive | `--wait` / background-notify |

## 3. Design principle: mechanism, not policy

`amux-spawn` launches+seeds, reports **facts**, exposes last-message +
cause-agnostic reason-context, and switches sessions. It enforces **no** handoff
format, permission policy, stop conditions, or orchestration. Only policy scalar:
`--stuck-after`.

## 4. Goals

- Spawn a new amux session, optionally seeded, **no live parent required**.
- Be the **`claude`-replacement launcher** (no prompt at a TTY → create + attach).
- **Quick-switch** by suffix with bash completion.
- **Track** agent-spawned sessions: `running|idle|stuck|terminated`, last message,
  cause-agnostic reason-context.
- Optionally **supervise**: `--wait` / native background notify.
- **Model/env transparency**: children inherit the spawner's model + env.
- Spawned sessions inherit the **user-global** hooks (Telegram/permission/reply).

## 5. Dependencies & non-goals

- **Depends on [task 12](../12_amux_extensions.md)** — amux gains env passthrough,
  `--no-default-model`, nested-tmux switch-client, resume-aware restart. Epic 10
  does not reimplement amux's launch.
- **Non-goals:** enforcing handoff format; permission policy (`--yolo` passthrough
  only, not an autonomy lever — D-Yolo); orchestration logic; managed worktrees /
  parallel fan-out (v1 = chains-only; explicit `--dir` to a pre-made worktree is
  fine); the `Agent` tool; amux MCP; cross-machine; live output streaming.

## 6. Decisions

| # | Decision | Rationale / evidence |
|---|----------|----------------------|
| D-Topo | Two topologies, chain primary; spawn never requires a live parent. | Parent dies by design. |
| D-Handoff | Handoff = fire-and-return + step-aside, not wait. | `--wait` would pin the predecessor's context. |
| D-Amux | **Extend amux (task 12)** for env passthrough, model-default suppression, nested-tmux switch, resume-aware restart. | amux 0.3.0's CLI can't do these; cleaner than reimplementing its launch. |
| D-Tracked | **Human-launched = plain, restartable amux sessions** (no handle, no minted id; Telegram idle still works via the existing hook). **Agent / `--wait` / `--notify` = tracked** (handle + minted `--session-id`). | Humans want normal restart; only tracking needs a deterministic transcript path. |
| D-Entry | `amux-spawn` is the unified entry point. **TTY ⇒ attach** (inside tmux → **switch-client**), **non-TTY ⇒ fire-and-return**. `--detach` overrides at a TTY (`--attach` is meaningless off a TTY). Prompt optional. | One tool; replaces `claude`; works from inside tmux. |
| D-Workspace | Dir resolution: (1) `--dir`; (2) agent/non-TTY → inherit spawning session's `CC_DIR` (resolve parent via `amux-<name>`), not the agent's cwd; (3) human/TTY → cwd; (4) fallback → cwd + warning. | Stable "current workspace" down a chain; subdir-proof; no git detection. |
| D-Name | prefix = `basename(resolved-dir)`; name = `<prefix>` then `<prefix>-2/-3…`. `spawn [suffix]` takes an optional memorable suffix; auto-increment only when omitted; **atomic name allocation** (registry/amux lock) against concurrent spawns. | Unifies human/agent naming; matches existing names. |
| D-Switch | `a|attach <suffix>` resolves `<prefix>` from cwd and fuzzy-attaches `<prefix>-<suffix>`; if cwd-prefix yields no match (e.g. run from a subdir), **fall back to fuzzy match across the session list**. Bash completion over live sessions. | Subdir-proof switching. |
| D-RunId | `run_id` **auto-inherits** from the parent handle; `--run-id` overrides. | Chains stay grouped without the agent passing it. |
| D-Env | Child gets the curated model/auth env via tmux **`update-environment`** (allowlist copied from the spawner's live env — no `ps` leak; **plain inheritance does NOT work on an already-running server**, spike-verified — Decision 1). amux (task 12 E1) appends the allowlist (`ANTHROPIC_*`, curated `CLAUDE_CODE_*`, `API_TIMEOUT_MS`), keeps `CLAUDE_CODE_SESSION_ID` **out** of it and `unset`s it in `shell_setup` (also unsets `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT`; `TMUX`/`TMUX_PANE` reset by tmux), and preserves **auth precedence** in `shell_setup` (drop `ANTHROPIC_API_KEY` when `AUTH_TOKEN`+`BASE_URL` set). `--env` is a non-secret manual override only (it inlines ⇒ `ps` leak). Suppress amux's `--model sonnet` (E2). **Also propagate the parent's explicit `--model`** (read from parent `CC_FLAGS`) when the caller gives none — the standard model tier is a flag, not env. | Model inheritance for both alt-model env and the standard `--model` tier; no auth conflict; no `ps` leak; honors claude.bashrc. |
| D-SessionId | Tracked sessions mint `--session-id` (transcript path), stored in `<name>.meta.json` **not `CC_FLAGS`** (task 12 E4-floor — else `start-all` re-passes it and the session dies "already in use"), and are **not restarted by re-passing it**; resume-aware restart via `--resume` is task 12 E4-full (post-v1). Human sessions are plain (no minted id → restart fresh, no collision). | Spike Q6; D-Tracked. |
| D-Idle | `idle` = not running, not blocked, not waiting (exact existing definition). | — |
| D-State | Enum = **running \| idle \| stuck \| terminated** (terminated reports last-known state, e.g. crashed-mid-run vs clean). `Stop` only fires at turn end, so the handle's `idle` goes stale after an `amux send` follow-up; 10-03 re-derives via **open-turn detection** (user msg / dangling `tool_use` after the last `Stop`, gated by `current_mtime > mtime_at_stop`) — a lone background-completion notification stays idle, so an idle session never false-flips to running/stuck. | Facts; AI interprets. |
| D-Producer | Producer = the **`Stop`** hook (`last_assistant_message` + `background_tasks[]`); **activity clock = transcript mtime** (no per-tool heartbeat); a `Notification` hook records the permission-block marker. | Spike: Stop ~2s; transcript mtime is a free activity signal. |
| D-Stuck | **Cause-agnostic**: not idle/terminated + `now − transcript_mtime > --stuck-after` (default 10m), with reason-context, never assuming the cause. `--stuck-after` is set at spawn and **persisted in the handle**; `status --stuck-after T` overrides per-query (Decision 4). "not idle" must account for re-activation via `amux send` — see D-State / architecture §6. | Background work / hung foreground tool / permission / unknown. |
| D-Bg | Background **work** = Agents *and* Bash; idle requires `background_tasks == []`. | Spike. |
| D-Actions | Orchestrator: wait · `amux send` follow-up · `amux rm` + respawn. AskUserQuestion/permission → user via existing hooks. | Tool is non-AI. |
| D-Yolo | `--yolo` passthrough does **not** bypass the gate and is not an autonomy lever. | Spike Q5. |
| D-Scope | v1 = chains-only. | Drops merge-back + most trust-prompt. |
| D-Cleanup | No auto-cleanup; orchestrator uses `amux ls` + `amux rm`. | Stance A. |
| D-Hooks | Hooks-first/-only; producer is **user-global** (fires in any workspace). | Spike Q7. |

## 7. Capabilities

- **C0 Spawn / C1 Handoff** (fire-and-return; no live parent).
- **C2 Status / C3 Last / C4 Reason-context** (tracked sessions).
- **C5 Wait / C6 Notify** (fan-out).
- **C7 Lifecycle** (`amux send` / `amux rm`).
- **C8 Launcher (human)** — `amux-spawn spawn` no-prompt at TTY → create + attach
  (switch-client inside tmux); one-step `claude` replacement.
- **C9 Quick-switch (human)** — `amux-spawn a <suffix>` with completion.
- **C10 Model/env inheritance** — `(claude_glm5_env && amux-spawn spawn)` works
  like `claude-glm5`; agent children inherit automatically.

## 8. Functional requirements

- FR1 Spawn launches in the resolved workspace (D-Workspace, absolute) and seeds
  the prompt atomically (positional; multi-line; auto-submits).
- FR2 Stable, run-scoped handle (tracked sessions) valid across calls and after
  the spawner is gone; **the non-TTY path never blocks unless `--wait`** (TTY
  attach blocks by nature).
- FR3 TTY ⇒ attach / switch-client; non-TTY ⇒ fire-and-return; `--detach` override.
- FR4 State via `Stop` payload (idle ⇔ `background_tasks == []`); `stuck`
  cause-agnostic via transcript mtime + `--stuck-after`; status bundles
  reason-context.
- FR5 `--wait`/notify resolve at first-turn idle (guard against pre-seed false idle).
- FR6 Tracked sessions never restarted by re-passing `--session-id`; human sessions
  plain; restart is resume-aware (task 12).
- FR7 Env passthrough per D-Env via tmux **`update-environment`** (curated allowlist;
  `CLAUDE_CODE_SESSION_ID` unset; auth precedence; `--model sonnet` suppressed) →
  model inheritance with no `ps` leak. (Not plain inheritance — broken on a running
  server.)
- FR8 `a <suffix>` resolves prefix from cwd with cross-list fuzzy fallback; bash
  completion shipped.
- FR9 Fork-bomb backstop: cap on concurrent live **tracked** sessions —
  **per-workspace** (keyed on absolute `CC_DIR`), default **16**, env-overridable
  (`AMUX_SPAWN_MAX_SESSIONS`), checked flock-atomically with name allocation; deep
  chains and plain human sessions never capped. (Recursion inherits the parent
  `CC_DIR`, so per-workspace catches the fork-bomb vector; a deliberate
  multi-`--dir` fan-out is not globally bounded — accepted.)
- FR10 Fail safe, not silent: non-progress surfaces as `stuck` + reason-context.

## 9. Success criteria

- [ ] `amux-spawn spawn` at a shell (incl. inside tmux) creates + switches to an
      amux session in the cwd; Telegram follow-up works.
- [ ] `(claude_glm5_env && amux-spawn spawn)` launches a GLM session; an agent in
      it spawns GLM children with no extra config (no sonnet override, no auth
      conflict). **Test on an already-running tmux server** (the agent-chain topology
      where plain inheritance fails and `update-environment` is what carries the env).
- [ ] `amux-spawn a <suffix>` switches sessions (incl. from a subdir); completion
      lists the workspace's sessions.
- [ ] An agent spawns a successor inheriting the parent `CC_DIR` and `run_id`
      (ignoring its own subdir cwd) and ends its turn; a 3+ link chain runs with
      no human.
- [ ] `status` distinguishes running/idle/stuck/terminated; background-task,
      permission, and hung-foreground blocks surface as `stuck` w/ reason-context.
- [ ] `--notify` behaves like a background `sleep`; cross-workspace spawn still
      reports state; no regression to task 09 / permission gating.

## 10. Risks & dependencies

- **amux fork maintenance** (task 12): pin the version; prefer upstreamable changes.
- **Permission-delivery reliability** ([task 11](../11_permission_delivery_reliability.md)):
  permission-block is one cause of stuck, not special-cased.
- **Secrets in env** (claude.bashrc): reach the child via tmux `update-environment`
  (read from live env, never inlined ⇒ no `ps` leak); `--env` must not carry secrets;
  consider rotation.
- **`background_tasks` coverage**: **confirmed by experiment** (CC 2.1.185) — background
  Bash appears as `type:"shell"`, and completion fires a fresh `Stop` with empty
  `background_tasks`, so the handle self-drains. Pin CC ≥ 2.1.145.
- **Bare-`claude` parents** can't inherit workspace/model (fallback + warning); the
  C8 launcher habit keeps sessions amux-managed.
