# Epic 10 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md)
(Rev 6) and [architecture.md](./architecture.md) (Rev 6). Each task file is written
for a fresh-context agent and lists its own "read first" refs, done criteria, and
tests. This file owns **cross-cutting decisions** and **ordering** — the optimal
order is *not* purely sequential. The four review decisions and the two empirical
confirmations are recorded under **Resolved during review** below; treat them as
settled.

## Tasks

| id | title | status | depends on |
|----|-------|--------|-----------|
| [12](../12_amux_extensions.md) | amux extensions (external/fork) | **done** (E1–E5 + E4-full; fork `feat/epic-10-amux-extensions` @ `9b05d10`, installed to `/usr/local/bin/amux`, chain-verified) | — |
| [10-01](./10-01-launcher-core.md) | launcher core (`spawn`) | **done + E2E-verified** (code+review+tests @ `765fb36`; 244 green; GLM-on-running-server chain-verified: env via `update-environment`, `model: glm-5.2`, no `ps` leak, handle==§6.0, transcript path matches). Only remaining gate: live `install-claude-config.sh` re-run [user runs separately] | 12 (E1, E2, E4-floor, E5) ✓, Phase 0 |
| [10-02](./10-02-producer-and-state.md) | producer hook + state | **done** (code+review+tests; 259 green; committing. Live re-confirm gate batched with `install-claude-config.sh` [user runs separately]) | 10-01 |
| [10-03](./10-03-reads.md) | reads: status/last/ls + reason-context | **done** (code+review+tests @ `db4545a`; 289 green) | 10-01, 10-02 |
| [10-04](./10-04-supervise.md) | supervise (`--wait`/notify) | **done** (code+review+tests @ `0b899ad`; 310 green; `--wait`/`--notify`/`--timeout` + false-idle guard; live background-Bash verification batched into acceptance) | 10-01, 10-02 |
| [10-05](./10-05-human-ergonomics.md) | attach/completion/shell | **done** (code+review+tests @ `5fa072b`; 334 green. Opt-in shell snippet — bashrc untouched. Live switch/TAB/integrated-launch batched into acceptance) | 10-01, 12 (E3) ✓ |

**All five 10-0x implementation tasks DONE (each Implementer→Reviewer PASS→Fixer→Committer). Suite 334 passing.**

## BRD §9 acceptance — live run 2026-06-23 (installer re-run by user; hooks now live)

Driven live against installed hooks + real amux + GLM/Claude:
- **A1 producer Stop hook (10-02 live, was blocked):** ✅ tracked spawn → handle `spawning→idle`, `last_message='PONG'`, `mtime_at_stop` set.
- **A6 re-activation / open-turn (10-03 load-bearing):** ✅ `amux send` to idle session → `status` reports `running` then `idle` (`PONG2`).
- **A2 stuck + reason-context:** ✅ background `sleep 180` → `state=stuck` (age>stuck_after), `live_background_tasks=true`, reason_context lists the shell task. Cause-agnostic; stuck derived not persisted.
- **A5 cross-workspace producer (user-global hooks):** ✅ Stop hook fired for `formdr_replacement-accxw` in a different workspace (`last_message='XWORK-OK'`).
- **A7 no regression:** ✅ suite 334 OK with installed config; task-09/permission untouched.
- **C10 GLM env inheritance via update-environment:** ✅ (10-01 prior live verify: `model: glm-5.2`, no `ps` leak).
- **A3 chain (3+ link autonomous, no human):** ✅ **FULLY VERIFIED** with a real-task chain (run_id `9fa40ce8`): link1 created `calc.py:add()` → spawned link2 → appended `multiply()` → spawned link3 → wrote+ran `test_calc.py` → **TESTS-PASS / CHAIN-DEPTH-3-OK**. All 3 links share `run_id` + workspace-root `CC_DIR` (subdir-independent), no human after seeding link1. NB: a *synthetic* recursive-spawn seed is refused by both GLM and Claude (model safety) — use real task-context chains (see memory).
- **TTY items (handed to user as checklist):** `claude` launcher, `a <suffix>` switch (root+subdir, switch-client inside tmux), TAB completion, integrated `claude-glm5` — **awaiting user run.**

All acceptance test sessions cleaned up (registry empty; user's 5 plain sessions intact).
| [11](../11_permission_delivery_reliability.md) | permission delivery (bug) | open | independent |

## Phase 0 — lock these BEFORE parallel work (decide once, all tasks inherit)

These are not in any single task because divergence between agents would break
integration. The orchestrator must fix them up front and record the choices here.

1. **Runtime/language.** One choice for the `amux-spawn` binary and the hooks. The
   hooks parse JSON event payloads and transcripts → Python is the path of least
   resistance and matches `.claude/hooks/*.py`. Recommended: **Python** for
   everything; bash only for the completion script. Lock it.
2. **Shared module.** A single helper module used by 10-01/02/03/05 for:
   name ↔ handle ↔ workspace-prefix resolution, the `amux-<name>` parent lookup
   (cf. `resolve_amux_session`), handle read/write (atomic), and transcript path/
   mtime. Define its location/API in 10-01; later tasks import it (no re-impl).
3. **Packaging / install.** How `amux-spawn` reaches `PATH` system-wide, how the
   bash-completion installs, and how the producer hooks register **user-global**
   without clobbering existing hooks — all via `install-claude-config.sh`. Decide
   the mechanism once (10-01 sets it up; 10-02/10-05 extend the same path).
4. **Test placement.** New automated tests join the existing suite
   (`tests/run_all_tests.py`); keep the fail-open / `FakeTelegram` conventions.
5. **Handle schema (single source).** architecture §6.0 now defines the explicit
   handle JSON schema. 10-01 creates, 10-02 transitions, 10-03 reads — all against
   that one field list. Do not let any task invent its own fields.
6. **Env mechanism (Decision 1).** Model/auth env reaches the child via tmux
   **`update-environment`** (curated allowlist, no `ps` leak) — **not** plain
   inheritance, which is broken on an already-running server (task 12 E1).
   **Confirmed:** the re-spike (and a GLM end-to-end run through amux) verified the
   curated vars reach the pane from a second amux session on a live server, with
   `DISPLAY`/`SSH_*` preserved and no `ps` leak. Locked.

## Dependency graph

```
            ┌────────── 10-03 reads ─────────┐
12 ─▶ 10-01 ─▶ 10-02 producer/state ─┼────────── 10-04 supervise ──┐
   └───────────────────────────────┘                              ├─▶ ACCEPTANCE (BRD §9)
        └─▶ 10-05 ergonomics (also needs 12 E3) ──────────────────┘
11 (parallel; bounds permission-*delivery* reliability, not 10-03's correlation —
   the store keys by session_id, so reason-context matching is already precise)
```

## Recommended order (with parallelism)

1. **Phase 0** decisions (above).
2. **task 12** — amux extensions. ✅ **DONE** (fork `feat/epic-10-amux-extensions`
   @ `9b05d10`, installed to `/usr/local/bin/amux`; pin this commit, not merged).
   E1–E5 **and** E4-full all landed and were chain-verified against real tmux 3.5a +
   real Claude (create → `send` → `--resume` → `rm`; GLM alt-model confirmed
   `model: glm-4.7`). Three bugs surfaced + fixed during that testing (`send-keys`
   pane target, `rm` meta cleanup, `cmd_start` abort on tmux 3.5a). 10-01 can now be
   built and validated against the installed binary.
3. **10-01** launcher core (critical path).
4. **10-02** producer + state — build right after 10-01. The hook-behavior questions
   are **already settled by experiment** (CC 2.1.185, see *Resolved during review*):
   background `shell` shows in `Stop.background_tasks`, completion fires a draining
   `Stop`, a permission block fires none, and `SubagentStop` fires + carries
   `background_tasks`. 10-02's job is to **re-confirm against the pinned CC version**
   and wire the `Stop`/`SubagentStop`/`Notification`/`SessionEnd` hooks accordingly —
   not to re-discover the behavior. Hook `SubagentStop` for freshness (never `idle`).
5. **Parallel track A:** 10-03 then 10-04 (10-04 consumes status/state).
   **Parallel track B:** 10-05 (independent of 02/03/04; needs 10-01 + 12 E3).
6. **Acceptance** — run BRD §9 success criteria end-to-end (human launch +
   switch; GLM-inherits-GLM; 3-link autonomous chain; stuck/reason-context across
   background/permission/hung-foreground; `--notify` like a background `sleep`;
   cross-workspace producer; no regression to task 09 / permission gating).

## Cross-task invariants (keep consistent)

- **Handle schema** = architecture §6.0 (single source). 10-01 creates (`spawning`);
  10-02 transitions; 10-03 reads. Atomic writes (tmp+rename) everywhere.
- **Tracked vs plain** (D-Tracked): only tracked sessions get handles/producer;
  plain human sessions rely on the existing idle Telegram hook. `--wait`/`--notify`
  ⇒ tracked + detached even at a TTY.
- **Naming/prefix** algorithm is shared by 10-01 (create) and 10-05 (attach) —
  mismatch breaks `a <suffix>`.
- **Stop is the producer signal; `idle ⇔ background_tasks == []`; activity = transcript
  mtime; stuck is cause-agnostic.** No per-tool heartbeat. **But `Stop` only fires at
  turn end** — a session re-activated via `amux send` shows stale `idle` until its
  next `Stop`, so 10-03 re-derives state via **open-turn detection** in the transcript
  tail (a user msg / dangling `tool_use` after the last `Stop`; a background-completion
  notification does NOT count — else an idle session false-flips to running→stuck),
  gated by `current_mtime > handle.mtime_at_stop` (architecture §6). Compare fs mtime
  to the `mtime_at_stop` snapshot, never to wall-clock `updated_at`.
- **Minted `--session-id` MUST be a valid UUID** — confirmed during task-12 testing:
  Claude (2.1.185) rejects a non-UUID with *"Invalid session ID. Must be a valid
  UUID."* and exits at startup. 10-01 must mint a real UUID (e.g. `uuid4`); amux just
  passes it through.
- **Never re-pass `--session-id`** (restart is resume-aware in amux, task 12 E4-full;
  E4-floor keeps the minted id out of `CC_FLAGS` so `start-all` can't re-pass it).
- **Fail safe, not silent**; never regress task 09 or existing permission gating.

## Resolved during review (2026-06-22)

- **Env mechanism** → `update-environment` allowlist (Decision 1); plain inheritance
  is broken on a running server. Task 12 E1 rewritten.
- **E4 split** (Decision 2): E4-floor blocks 10-01; E4-full is post-v1.
- **Detached launch** (Decision 3): amux `--no-attach` (task 12 E5).
- **Fork-bomb cap** (Decision 4): **16, per-workspace** (keyed on absolute `CC_DIR`),
  live+tracked only, env-overridable (`AMUX_SPAWN_MAX_SESSIONS`), flock-atomic with
  name allocation.
- **`--stuck-after`** (Decision 4): persisted in the handle at spawn; `status
  --stuck-after T` overrides per-query.
- **Background Bash in `Stop.background_tasks`**: **CONFIRMED by experiment**
  (2026-06-22, CC 2.1.185): appears as `type:"shell"`, **and background completion
  fires a fresh `Stop` with `background_tasks: []`** (harness injects it as a user
  turn) — so the handle self-drains and `--wait` won't hang. `SubagentStop` also fires
  + carries `background_tasks` (hook it too). Pin CC ≥ 2.1.145.
- **Permission↔session correlation**: `permission_requests.jsonl` **does** key by
  `session_id` (and `permission_state_store.get_pending_request_for_session`); 10-03
  matches directly — drop the "may be coarse" hedge.
- **Handle schema** now defined in architecture §6.0 (was missing — every task
  referenced a schema that didn't exist).

## Open items (flagged, not blockers)

- `--wait` false-idle guard → 10-04.
- Trust dialog in fresh dirs → post-v1 only (chains stay in the trusted workspace).
  (NB: experiments confirmed **no** trust dialog under OAuth for fresh `/tmp` dirs at
  CC 2.1.185 — both with and without `--dangerously-skip-permissions`.)
- **Permission block fires no `Stop`** — **CONFIRMED by experiment** (2026-06-22,
  CC 2.1.185): ~65s parked at a gate, zero `Stop`; `PostToolUse → Stop` on resolution.
  The §6 stuck-on-permission and re-activation logic stands. Re-confirm on CC bumps.
