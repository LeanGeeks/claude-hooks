# Epic 10 — Architecture: agent-spawned amux sessions

Companion to [brd.md](./brd.md) (Rev 6). **Rev 5** applies the review: launch via
**extended amux** ([task 12](../12_amux_extensions.md)); human=plain /
agent=tracked; resume-aware restart; nested-tmux switch-client; activity from
transcript mtime (no heartbeat); env denylist + auth precedence; run-id inherit.
**Rev 6** (adversarial review): env via tmux `update-environment` (not plain
inheritance); handle schema §6.0; mtime re-derivation of `running`; E4-floor / E5
amux changes; per-workspace fork-bomb cap. See the repo's top-level
[architecture.md](../../architecture.md).

---

## 0. Design stance: thin mechanism

`amux-spawn` launches+seeds, reports state honestly, exposes last-message +
cause-agnostic reason-context, switches/attaches, defers continue/terminate to
`amux send`/`rm`. No handoff format, permission policy, stop conditions, or
orchestration. Only policy scalar: `--stuck-after`.

## 1. Substrate recap

- amux wraps each session in tmux `amux-<name>`; recover from inside via
  `tmux display-message -p '#{session_name}'` then strip `amux-`.
- amux is CLI/Bash only. `Bash(amux:*)` allowlisted. `amux send`/`attach`/`ls`.
- `~/.amux/sessions/<name>.env`: `CC_NAME`, **`CC_DIR`**, `CC_FLAGS`; sibling
  `<name>.meta.json` (already holds `codex_session_id` — model for E4-floor/E4-full).

## 2. Verified mechanics (lifecycle spike — Claude Code 2.1.185, amux 0.3.0)

1. **`Stop` = turn-done (~2s)**: `last_assistant_message`, `background_tasks[]`,
   `permission_mode`, `session_id`, `transcript_path`. Idle `Notification` lags ~60s.
2. **Idle ⇔ `Stop.background_tasks == []`** — **confirmed by experiment (2026-06-22,
   CC 2.1.185, haiku)**: a `run_in_background` Bash appears in `Stop.background_tasks`
   as `{type:"shell", status:"running", id, command, description}`. Crucially, **when
   the background task completes the harness injects it as a new user turn**, and that
   turn ends with a **fresh `Stop` carrying `background_tasks: []`** — so the handle
   drains to `idle` on its own; no `Stop`-less stall. `SubagentStop` also fires around
   the background-shell lifecycle (no real subagent in the transcript) and likewise
   carries `background_tasks`. The idle `Notification` lagged ~60s, as in §2.1.
3. **Permission-block distinct — confirmed by experiment (2026-06-22, CC 2.1.185):**
   a gated tool fires `PreToolUse → Notification{permission_prompt}` and **no `Stop`
   while parked** (verified: session held ~65s at the gate, zero `Stop`). On
   resolution: `PostToolUse → Stop` (`background_tasks: []`). So a permission block
   keeps the last state (`running`) with frozen mtime ⇒ `stuck` after `--stuck-after`,
   cause-agnostic, with the pending-permission reason-context (§7). Idle =
   `Notification{idle_prompt}`.
4. Positional prompt auto-submits in all combos.
5. `--yolo` does **not** bypass the gate.
6. Hooks are user-global; fire anywhere.
7. `--session-id` restart collides; resume needs `--resume` ⇒ task 12 E4-full
   (E4-floor keeps the minted id out of `CC_FLAGS`).
8. Trust dialog unobserved headless (post-v1 concern).
9. Deterministic transcript path holds (but the dir→key encoding maps **both `/` and
   `.`** to `-`, keeping `_` — see §6). **amux force-appends `--model sonnet`**
   (⇒ task 12 E2) and unsets `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT`/`ANTHROPIC_API_KEY`
   (OAuth). **⚠ Misleading evidence:** this spike ran where the model vars (and
   `CLAUDE_CODE_SESSION_ID`) were already in the tmux server's global env, so plain
   inheritance *looked* like it worked. Re-spike confirms it does **not** on an
   already-running server — env must be carried via `update-environment` (task 12 E1,
   Decision 1). The `CLAUDE_CODE_SESSION_ID` unset is still added defensively.
10. `SubagentStop` carries `agent_id/agent_type/agent_transcript_path`; `SessionEnd`
    fires on exit (usable to mark `terminated`).

## 3. Entry point: one tool, two callers

`amux-spawn spawn [suffix] [--wait] [--detach] [--dir P] [--yolo] [--run-id R]
[--stuck-after 10m] [cc-flags] [-- "<prompt>"]`

- **TTY ⇒ attach**; **inside tmux (`$TMUX`) ⇒ `switch-client`** (task 12 E3), not
  `attach-session`. **non-TTY ⇒ fire-and-return.** `--detach` overrides at a TTY.
  No prompt = plain interactive session (C8 `claude` replacement).
- **Workspace-dir resolution:** (1) `--dir`; (2) agent/non-TTY → parent
  `CC_DIR` (resolve via `amux-<name>`, read `~/.amux/sessions/<parent>.env`); (3)
  human/TTY → cwd; (4) fallback → cwd + warning.
- **Naming:** `prefix = basename(resolved-dir)`; `<prefix>` then `<prefix>-N`;
  optional explicit `suffix`; **atomic allocation** vs concurrent spawns.
- **Tracked vs plain:** human launches = **plain** amux sessions (no handle, no
  minted id). Agent / `--wait` / `--notify` = **tracked** (handle + minted id).

`amux-spawn a|attach <suffix>`: prefix from cwd → fuzzy-attach
`<prefix>-<suffix>`; **if no cwd-prefix match (subdir), fall back to fuzzy match
across the full session list**. Bash-completion over live sessions.

## 4. Components

```
amux-spawn (CLI)                     ← unified human+agent entry point, thin
   ├─ spawn [suffix] …               → create (+attach/switch if TTY) (C0/C1/C8)
   ├─ a|attach <suffix>              → fuzzy switch in current workspace (C9)
   ├─ status <handle> / last <handle> / ls
+ bash-completion (suffixes)
+ spawn handle registry  ~/.amux/spawn/<name>.json   (tracked sessions only; §5)
+ spawn producer hooks   Stop (+ Notification)        (§6)
extended amux            (task 12: env, --no-default-model, switch, --resume)
```

`--notify` = `spawn … --wait` with `run_in_background:true` (native completion
notification). Continue = `amux send`; terminate = `amux rm`.

## 5. Launch & env (all spawns; minted id is tracked-only)

- Drive **extended amux** with **`--no-default-model`** so `ANTHROPIC_MODEL`
  governs (and propagate the parent's explicit `--model` when the caller gives none
  — D-Env); **for tracked sessions, also a minted `--session-id`** (stored by amux in
  `<name>.meta.json`, not `CC_FLAGS` — task 12 E4-floor). **Env reaches the child via
  tmux `update-environment`** — amux (task 12 E1) appends the curated allowlist
  (`ANTHROPIC_*`, curated `CLAUDE_CODE_*`, `API_TIMEOUT_MS`) so those vars are copied
  from the spawner's **live env** (no `ps` leak). **Plain inheritance does NOT work
  on an already-running server** (spike-verified) — that is the whole reason for the
  allowlist. amux also `unset`s the denylist in `shell_setup`
  (`CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT`/`CLAUDE_CODE_SESSION_ID`; `TMUX`/`TMUX_PANE`
  reset by tmux) and enforces auth precedence there (drop `ANTHROPIC_API_KEY` when
  `AUTH_TOKEN`+`BASE_URL` present). `--env` is a **non-secret** manual override only
  (it inlines ⇒ `ps` leak).
- Listed model/auth vars carry over via the allowlist ⇒ **model inheritance with no
  per-spawn secret handling**. (No env-persistence sidecar: human sessions are plain
  and re-launched via their alias.)
- **Detached launch** (`--detach`, and `--wait`/`--notify`) uses amux `--no-attach`
  (task 12 E5) — do not rely on `attach-session` failing.
- `run_id` defaults from the parent handle (D-RunId).
- Every spawn **creates detached** (`--no-attach`), then the TTY path attaches /
  `switch-client` as a separate step — so there is no `attach-session` failure to
  swallow on the non-TTY path. Confirm liveness via `amux ls` regardless (amux runs
  under `set -e`; don't trust its exit code alone).

## 6. Producer & state (Stop-based, hooks-only, tracked sessions)

### 6.0 Handle schema (single source — `~/.amux/spawn/<name>.json`)

Authoritative field list; 10-01 creates, 10-02 transitions, 10-03 reads. Atomic
writes (tmp+rename) everywhere. **No task invents fields outside this list.**

```jsonc
{
  "name":            "claude-hooks-2",      // amux session name (handle key)
  "session_id":      "<uuid>",              // minted --session-id (== transcript stem)
  "run_id":          "<uuid>",              // inherited from parent handle or minted
  "dir":             "/abs/CC_DIR",         // resolved workspace (absolute)
  "transcript_path": "/home/anton/.claude/projects/<enc-dir>/<session_id>.jsonl",
  "stuck_after_s":   600,                   // from --stuck-after at spawn (Decision 4)
  "state":           "spawning|running|idle|terminated", // last producer-written state
  "last_state":      "running|idle",        // for terminated: last-known before exit
  "last_message":    "…",                   // last_assistant_message from Stop
  "background_tasks": [ /* Stop.background_tasks verbatim */ ],
  "permission_pending": false,              // last Notification marker; cleared on next Stop
  "mtime_at_stop":   1719000000.0,          // transcript mtime (epoch float) observed at last Stop
  "created_at":      "<iso8601>",
  "updated_at":      "<iso8601>"            // last producer write (wall clock; for audit only)
}
```

Notes: `transcript_path` — the `<enc-dir>` encoding maps **both `/` and `.`** to `-`
and keeps `_` (verified against `~/.claude/projects/`); prefer capturing the real
path from the first `Stop` payload over computing it. `stuck` is **never persisted**
— it is derived at read time. `state` never holds `stuck`.

### 6.1 Producer hooks

Spawn-aware hooks, firing only for handle-bearing sessions:
- **`Stop`** (authoritative): `last_message ← last_assistant_message`,
  `background_tasks ← payload`; `state = idle` iff empty else `running`; clear
  `permission_pending`; snapshot `mtime_at_stop ← current transcript mtime`; capture
  the real `transcript_path` from the payload.
- **`SubagentStop`** (recommended): the experiment (§2.2) shows it fires around the
  background-shell lifecycle and carries `background_tasks`. Treat it like `Stop` for
  freshness — refresh `background_tasks`/`mtime_at_stop` — but do **not** let it set
  `state: idle` (a subagent finishing ≠ the main turn ending); leave `idle` to `Stop`.
- **`Notification`**: set `permission_pending=true` (reason-context; not idle). The
  **next `Stop` clears it** (the gated tool resolved and the turn ended), so the
  marker can't go stale.
- **`SessionEnd`** (optional): mark `terminated` promptly + reason, preserving
  `last_state`.

No per-tool heartbeat. **Activity clock = `transcript_path` mtime**, read at
`status` time — it advances on every event and freezes during a hung foreground
tool or background-only quiescence, which is exactly the stuck signal.

**State (derived at read time — §6.0 never stores `stuck`).** Precedence:
`terminated` → `stuck` → `running` → `idle`.

1. `terminated` ⇔ `tmux has-session` false (or `SessionEnd`), reported with `last_state`.
2. Compute **`active`** = (live `background_tasks`) **or** (an *open turn* in the
   transcript tail) **or** (no `Stop` recorded yet — `state == "spawning"` /
   `mtime_at_stop` absent). The spawning clause makes a freshly-launched session
   `active` until its first `Stop` (guards `--wait` against a pre-seed false idle) —
   **but it does not short-circuit `stuck`**, so a session whose *first* turn hangs
   still ages into `stuck` (step 3). **Open-turn detection (load-bearing):** `Stop` fires only at turn
   end, so a follow-up via `amux send` leaves a stale `state: idle`. But a bare mtime
   bump is *not* a new turn — a completing `run_in_background` task appends a
   notification while the session is genuinely idle. So an open turn = a **user message
   or a `tool_use` with no matching result, newer than the last `Stop`'s assistant
   message**; a lone background-completion notification does **not** count. Cheap gate:
   only parse the tail when `current_mtime > mtime_at_stop` (same clock — both fs mtime;
   do **not** compare against wall-clock `updated_at`). **`background_tasks` freshness:**
   the experiment (§2.2) shows background completion fires a fresh `Stop` with the drained
   list, so the last-`Stop` `background_tasks` self-corrects to `[]` — the simple model
   holds. **Defensive only:** if a future CC version ever skips that `Stop`, reconcile
   each entry against its output-file terminal status (§7) so a drained fan-out can't read
   `active` forever; not required against CC 2.1.185.
3. `stuck` ⇔ `active` **and** `now − activity_mtime > stuck_after` (**cause-agnostic**) —
   catches a stalled background task, a hung / permission-blocked follow-up whose `Stop`
   never came, **and a first turn that hangs before any `Stop`**. `activity_mtime` =
   the transcript file's mtime if it exists, else the handle's `created_at` (so a
   session that hangs before writing any transcript still ages into `stuck`).
4. `running` ⇔ `active` and not stuck.
5. `idle` ⇔ not `active` **and** stored `state: idle` (a `Stop` recorded empty
   `background_tasks`). A post-`Stop` background-completion notification still reads idle
   (it's not an open turn and the task reconciles as not-live).

## 7. Reason-context (cause-agnostic — C4)

When not idle (esp. `stuck`), `status` bundles every collectible signal without
presuming the cause, from files (no extra producer state):
- **background work** — `Stop.background_tasks` + harness task files: `{kind, id,
  command/label, output_file+tail+mtime, started_at/duration, pid if discoverable}`.
- **in-flight foreground tool** — transcript tail = a `tool_use` with no result +
  frozen mtime (which tool/command + age).
- **pending permission** — `pending` rows in `~/.claude/permission_requests.jsonl`
  matched by **`session_id`** (the store *does* key by session — see
  `permission_state_store.get_pending_request_for_session`; the tracked session's
  minted `session_id` is in the handle, so this is a precise match, not a coarse
  guess). Delivery reliability (not correlation) is bounded by
  [task 11](../11_permission_delivery_reliability.md).
- **unknown** — none resolves it; still surfaced as `stuck`.

## 8. Guards & safety

- **Fork-bomb backstop:** cap on concurrent live **tracked** sessions —
  **per-workspace** (keyed on absolute `CC_DIR`), default **16**, env-overridable
  (`AMUX_SPAWN_MAX_SESSIONS`); never cap chain length/depth, never count plain human
  sessions. Count only handles with `tmux has-session` true (filters dead handles —
  no auto-cleanup, D-Cleanup). The cap check is **flock-atomic with name allocation**
  (a single global lock, e.g. `~/.amux/spawn/.lock`, held by all spawns) across
  [cap-check → pick free name → create **detached** via `--no-attach` → tracked: write
  handle]. **Release before attaching** — never hold the lock across `amux exec`'s
  blocking attach (it would serialize every interactive launch). Create-detached-then-
  attach also closes the TOCTOU window; the tmux session existence is the reservation
  (amux `exec` overwrites `.env` unconditionally).
- **Fail safe, not silent:** non-progress ⇒ `stuck` + reason-context.
- **Permission-agnostic:** no permission flags unless `--yolo`; user-global gate
  governs regardless (§2.5).
- **Secrets:** env reaches the child via tmux **`update-environment`** (copied from
  the spawner's live env, never inlined ⇒ no `ps` leak). `--env` must not carry
  secrets. Plain inheritance is *not* the mechanism — it fails on a running server.

## 9. Open seams to validate during build

- Background **Bash** in `Stop.background_tasks`? Docs say `type:"shell"` (CC ≥
  2.1.145) — likely yes; confirm in 10-02 and pin the CC version. (§2.2)
- `--wait` false-idle guard: require a `Stop` *after* the seeded turn.
- task 12 E1 (Decision 1): confirm the curated vars reach the pane via
  `update-environment` **from a second amux session on an already-running server**,
  and survive the `unset` line. (Plain inheritance already disproven for this case.)
- Whether `Stop` truly never fires during a permission block (spike claim) — the §6
  re-activation rule and stuck-on-permission both depend on it; re-verify in 10-02.
- Parent-`CC_DIR` resolution when amux but `CC_DIR` ≠ cwd; bare-`claude` fallback.
- `a <suffix>` cross-list fuzzy fallback ranking (avoid wrong-workspace matches).
- Trust-dialog human-TTY check (only if spawning into fresh dirs — post-v1).
- Permission reason-context **delivery** (not correlation) depends on
  [task 11](../11_permission_delivery_reliability.md).
