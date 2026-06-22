# 10-03 — Reads: `status` / `last` / `ls` + reason-context

**Status:** todo · **Depends on:** 10-01 (handles), 10-02 (handle state)
**Read first:** [architecture.md](./architecture.md) §6.0 (handle schema) + §6
(state machine) + §7 (reason-context) and decisions D-State, D-Stuck.

## Goal

Implement the read subcommands an orchestrator uses to observe tracked sessions:
honest state, last message, and cause-agnostic reason-context.

## Scope

- `status <handle>` → `running | idle | stuck | terminated` (+ for `terminated`,
  the last-known state, e.g. crashed-mid-run vs clean). Machine-readable output
  (e.g. `--json`) plus a human line.
  Precedence (architecture §6): `terminated` → `stuck` → `running` → `idle`.
  - `terminated` ⇔ `tmux has-session` false (or `SessionEnd` recorded).
  - Compute **`active`** = **live** `background_tasks` **or** an *open turn* in the
    transcript tail **or** no `Stop` recorded yet (`state == spawning` / no
    `mtime_at_stop`). The spawning clause keeps a just-launched session `active`
    (never `idle`, guarding `--wait`) **without** short-circuiting `stuck` — a first
    turn that hangs still ages into `stuck`. **Open-turn detection (load-bearing):** `Stop` only fires at turn
    end, so a session re-fed via `amux send` keeps a stale `idle`; but a bare mtime bump
    is NOT a new turn (a completing `run_in_background` task appends a notification while
    idle). Open turn = a **user message or a `tool_use` with no matching result, newer
    than the last `Stop`'s assistant message**; a lone background-completion
    notification does not count. Cheap gate: only parse the tail when `current_mtime >
    handle.mtime_at_stop` (same clock — both fs mtime; do **not** compare against
    wall-clock `updated_at`). **`background_tasks` self-corrects:** the experiment (§2.2)
    confirms background completion fires a fresh `Stop` with `background_tasks: []`, so
    the handle drains on its own. Optional defensive fallback (not needed on CC 2.1.185):
    reconcile entries against their output-file terminal status (you already read these
    for reason-context) in case a future CC skips that `Stop`.
  - `stuck` ⇔ `active` **and** `now − activity_mtime > stuck_after` (from the handle's
    `stuck_after_s`, default 10m; `status --stuck-after T` overrides per-query —
    Decision 4). `activity_mtime` = transcript mtime if the file exists, else handle
    `created_at` (so a hung pre-transcript boot still goes `stuck`). **Cause-agnostic**
    — do not infer the cause into the state.
  - `running` ⇔ `active` and not stuck.
  - `idle` ⇔ not `active`, and stored `state: idle`.
- `last <handle>` → the handle's `last_message`.
- `ls` → tracked sessions for the current workspace (resolved `CC_DIR`) / `run_id`,
  read from the registry (`~/.amux/spawn/*.json`). Mark/skip dead handles
  (`tmux has-session` false) — no auto-cleanup, so the registry accumulates them.
- **Reason-context** (attach to `status`, especially when `stuck`), assembled from
  files, presuming nothing (architecture §7):
  - background work — `background_tasks` + harness task output files (`{kind, id,
    command/label, output_file + tail + mtime, started_at/duration, pid if
    discoverable}`);
  - in-flight foreground tool — transcript tail = a `tool_use` with no result +
    frozen mtime;
  - pending permission — `pending` entry in `~/.claude/permission_requests.jsonl`
    matched by **`session_id`** (every row has `session_id`; use
    `permission_state_store.get_pending_request_for_session`). The tracked session's
    minted `session_id` is in the handle, so this is a precise match;
  - unknown — none resolved; still `stuck`.

## Implementation hints / watch-outs

- Activity = the live `transcript_path` mtime read at status time (path stored by
  10-01, refreshed from `Stop` by 10-02); compared against `mtime_at_stop` (snapshot
  written by 10-02). No heartbeat.
- Reason-context is **best-effort and additive** — list every signal found; never
  collapse to "it's the permission" (permission accuracy is bounded by
  [task 11](../11_permission_delivery_reliability.md)).
- Harness background task output files live under the per-session tasks dir (the
  path is reported when a background Bash/Agent is launched, e.g.
  `…/tasks/<id>.output`); tail a bounded number of lines.
- Tolerate partial/missing handles (a just-spawned `spawning` session); never crash.
- **Permission ↔ session correlation is precise** (review correction): the store
  **does** key by `session_id`, so match directly on the queried session's
  `session_id` — the old "may be coarse / report best-effort" hedge was wrong. What
  task 11 bounds is permission **delivery** reliability, not correlation.

## Done criteria

- [ ] `status` correctly returns running / idle / stuck / terminated across the
      lifecycle; `--stuck-after` is honored.
- [ ] A session stuck on a background task, a hung foreground command, **and** a
      permission prompt each surface as `stuck` with the matching reason-context.
- [ ] `last` returns the final assistant message; `ls` lists the workspace's
      tracked sessions.
- [ ] JSON output is stable enough for an orchestrator to parse.

## Testing

- Drive sessions into each state (idle; background `sleep` past `--stuck-after`;
  gated command; `amux rm` → terminated) and assert `status` + reason-context.
- Verify `--stuck-after 5s` flips a quiet-with-background session to `stuck`.
- **Re-activation:** after a session goes `idle`, `amux send` a follow-up and assert
  `status` reports `running` (not stale `idle`) while it works, then `idle` again.
- **Hung first turn:** a tracked session whose first turn never reaches a `Stop`
  (e.g. seeded a gated command, `--stuck-after 5s`) flips `spawning`→`stuck` (not stuck
  ⇒ booting bug); a just-spawned healthy session reads `running`, never `idle`.
- Confirm cause-agnostic output: a stuck session with both a background task and a
  pending permission lists both, not one.
