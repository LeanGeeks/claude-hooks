# 10-02 — Producer hook & state machine

**Status:** todo · **Depends on:** 10-01 (handle registry exists)
**Read first:** [architecture.md](./architecture.md) §6.0 (handle schema) + §6
(producer & state) + §2 (verified mechanics) and decisions D-Producer, D-State,
D-Stuck, D-Bg, D-Idle.

## Goal

Keep each **tracked** session's handle (`~/.amux/spawn/<name>.json`) current via
hooks, so 10-03 reads can report `running | idle | stuck | terminated`. Producer is
`Stop`-based; the activity clock is the transcript mtime (no per-tool heartbeat).

## Scope

- A **`Stop`** hook: for sessions that have a handle, write `last_message ←
  last_assistant_message`, `background_tasks ← payload`, set `state = idle` iff
  `background_tasks == []` else `running`, clear `permission_pending`, and capture the
  real `transcript_path` from the payload (preferred over computing it — §6.0).
  **Also snapshot `mtime_at_stop` = the transcript file's current mtime (epoch float)
  at processing time** — 10-03's open-turn / re-activation check compares the live
  mtime against this (same clock); do NOT compare against wall-clock `updated_at`.
  Update `updated_at`. Requires CC ≥ 2.1.145 for
  `background_tasks`/`last_assistant_message`. Write only fields in the §6.0 schema.
- A **`SubagentStop`** hook (recommended — confirmed to fire and carry
  `background_tasks`, §2.2): treat like `Stop` for **freshness only** (refresh
  `background_tasks` + `mtime_at_stop`), but **do not** let it set `state: idle` — a
  subagent finishing is not the main turn ending; leave `idle` to `Stop`.
- A **`Notification`** hook (matcher `permission_prompt`): set
  `permission_pending=true` in the handle (reason-context for 10-03). **The next
  `Stop` must clear it** (`permission_pending=false`) so the marker can't go stale
  after the gate resolves. `idle_prompt` is informational only (NOT the idle signal —
  `Stop` is). Do not duplicate the existing Telegram idle notification.
- Optional **`SessionEnd`** hook: mark `terminated` promptly + reason, preserving
  the last-known state.
- Handle-gated: all hooks must no-op quickly for sessions without a handle (resolve
  this session's amux name like `resolve_amux_session` in
  `.claude/hooks/notification_hook.py`, then check for the handle file).
- Register the hooks **user-global** (extend `install-claude-config.sh` /
  `~/.claude/settings.json`) so they fire in any workspace (spike Q7).

## Implementation hints / watch-outs

- **RESOLVED by experiment (2026-06-22, CC 2.1.185 — architecture §2.2):** a
  `run_in_background` Bash **does** appear in `Stop.background_tasks` as `type:"shell"`,
  and **background completion fires a fresh `Stop` with `background_tasks: []`** (the
  harness injects the completion as a new user turn). So `idle ⇔ last Stop bg==[]` is
  reliable and the handle self-drains. `SubagentStop` also fires around the bg-shell
  lifecycle and carries `background_tasks` — **also hook `SubagentStop`** (treat like
  `Stop`: refresh `background_tasks`/`mtime_at_stop`) for extra freshness. Re-confirm
  if the pinned CC version changes.
- **CONFIRMED by experiment (2026-06-22, CC 2.1.185):** a **permission block produces
  no `Stop`** while parked (`PreToolUse → Notification{permission_prompt}`, then ~65s
  parked with zero `Stop`); on resolution `PostToolUse → Stop` (`background_tasks:[]`).
  So state stays `running` with frozen mtime ⇒ `stuck` after the timeout, and the
  `permission_pending` marker is cleared by that post-resolution `Stop`. Re-confirm if
  the pinned CC version changes.
- Reuse existing transcript helpers conceptually (`extract_last_agent_message`,
  `has_active_background_agents` in `notification_hook.py`) but the `Stop` payload
  should make most scraping unnecessary — prefer the payload.
- Do **not** add a per-tool heartbeat; activity is `transcript_path` mtime, read by
  10-03 at status time.
- Atomic handle writes (tmp + rename); tolerate concurrent reads.
- Fail safe: a hook error must never disrupt the session (mirror the
  fail-open style of the existing hooks); but never silently corrupt the handle.
- Keep this additive — must not regress task 09 (reply-injection) or the existing
  idle Telegram notification.

## Done criteria

- [ ] After a tracked session finishes its first turn, its handle shows
      `state: idle`, the correct `last_message`, `background_tasks: []`, and a fresh
      `mtime_at_stop`.
- [ ] While the session has a live background child, the handle shows
      `state: running` with non-empty `background_tasks`; **after the child completes
      the handle self-drains to `state: idle`** (a `Stop` fires on completion — §2.2).
- [ ] A permission-blocked session has `permission_pending: true` set by the
      `Notification` hook and is not reported idle; the post-resolution `Stop` clears it.
- [ ] `SubagentStop` refreshes `background_tasks`/`mtime_at_stop` but never sets `idle`.
- [ ] Hooks no-op for non-tracked (plain/human) sessions and other repos' sessions.
- [ ] Behavior reconfirmed against the **pinned CC version** (≥ 2.1.145): background
      `shell` in `background_tasks`, completion fires a `Stop`, permission-block fires
      none (all confirmed on 2.1.185 — re-verify if the pin moves).

## Testing

- Spawn a tracked session that replies once → assert handle `idle` + `last_message` +
  `mtime_at_stop` set.
- Spawn one that launches a background subagent (and separately a background bash
  `sleep`) → assert `running` + `background_tasks`, then **self-drains to `idle`** once
  the child completes (no `Stop`-less stall — the completion fires a `Stop`, §2.2).
- Gated command (no `--yolo`) → assert `permission_pending` set, not idle; approve →
  next `Stop` clears it.
- Confirm a plain human session produces no handle writes.
