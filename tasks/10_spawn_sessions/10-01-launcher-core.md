# 10-01 — Launcher core (`amux-spawn spawn`)

**Status:** todo (prereqs ✓) · **Depends on:** [task 12](../12_amux_extensions.md) —
**E1** (env via `update-environment`), **E2** (`--no-default-model`), **E4-floor**
(`--session-id` → `meta.json`), **E5** (`--no-attach`) — all four are **hard
prereqs**: the core launch structure is *create-detached-under-lock (E5) → attach*,
not just `--detach`. **These are DONE and chain-verified** (fork
`feat/epic-10-amux-extensions` @ `9b05d10`, installed at `/usr/local/bin/amux`;
E3/E4-full also landed). Build against that binary; pin the commit.
**Read first:** [brd.md](./brd.md) + [architecture.md](./architecture.md) (esp.
§3 Entry point, §5 Launch & env, **§6.0 handle schema**, and decisions D-Entry,
D-Workspace, D-Name, D-Tracked, D-Env, D-SessionId, D-RunId).

## Goal

Build the `amux-spawn` CLI skeleton and the `spawn` subcommand: create + seed a
new amux session for both humans (TTY) and agents (non-TTY), with workspace-anchored
naming, tracked-vs-plain handling, and model/env inheritance. (`status`/`last`/`ls`
are stubs here — filled by 10-03; `--wait`/notify by 10-04; `a|attach` by 10-05.)

## Scope

- CLI dispatch for `amux-spawn <subcommand>`; implement `spawn`:
  `spawn [suffix] [--detach] [--dir P] [--yolo] [--run-id R] [--stuck-after T]
  [claude-flags…] [-- "<prompt>"]`.
- **Dir resolution** (D-Workspace): `--dir` → parent `CC_DIR` (agent/non-TTY) →
  cwd (human/TTY) → cwd+warning. Parent resolved via the `amux-<name>` tmux trick
  (see `resolve_amux_session` in `.claude/hooks/notification_hook.py`) then read
  `CC_DIR` from `~/.amux/sessions/<parent>.env`.
- **Naming** (D-Name): `prefix = basename(resolved-dir)`; optional explicit
  `suffix`; else `<prefix>`, `<prefix>-2`, … with **atomic allocation** (avoid two
  concurrent spawns picking the same name).
- **Tracked vs plain** (D-Tracked): TTY/human launch ⇒ plain amux session (no
  handle, no minted id). Agent/non-TTY ⇒ tracked: mint a `--session-id` **valid
  UUID** (e.g. `uuid4` — Claude 2.1.185 rejects a non-UUID with *"Invalid session
  ID. Must be a valid UUID."* and exits at startup; confirmed in task-12 testing), write
  the handle `~/.amux/spawn/<name>.json` (**schema in architecture §6.0** — use that
  exact field list; persist `stuck_after_s` from `--stuck-after` here so 10-03 can
  read it). The minted id must end up in `<name>.meta.json`, **not `CC_FLAGS`** (task
  12 E4-floor) — otherwise `amux start-all` re-passes it and the session dies. **`--wait`/
  `--notify` (10-04) force tracked + detached even at a TTY** — you await the
  result, you don't interactively attach.
- **TTY behavior** (D-Entry): TTY ⇒ attach, or `switch-client` if `$TMUX` set
  (relies on task 12 E3); non-TTY ⇒ fire-and-return; `--detach` forces detach **via
  amux `--no-attach` (E5)** — don't rely on `attach-session` failing.
- **Launch via extended amux** (§5 + task 12): pass `--no-default-model` (E2).
  Env reaches the child via tmux **`update-environment`** (E1) — **not plain
  inheritance, which is broken on an already-running server** (Decision 1). amux owns
  the allowlist append, the denylist unsets (incl. `CLAUDE_CODE_SESSION_ID`), and
  auth precedence — so 10-01 just ensures the model/auth vars are present in its own
  environment (so the allowlist can copy them) and never inlines secrets. Seed the
  prompt as a **positional** arg (auto-submits — confirmed by the spike); strip
  amux-spawn's own `--` separator before forwarding.
- **run_id** (D-RunId): inherit from the parent handle if present; `--run-id`
  overrides; mint a new one if no parent.
- **Model inheritance, standard tier:** alt-model selection rides on inherited env,
  but the standard `--model` tier is a flag, not env. When the caller passes no
  `--model`, read the parent's `--model` from its `CC_FLAGS` (same parent `.env` you
  read `CC_DIR` from) and propagate it, so e.g. opus→opus works.
- **Fork-bomb backstop** (Decision 4): refuse to spawn beyond a **per-workspace** cap
  (keyed on absolute `CC_DIR`) on concurrent live **tracked** sessions — default
  **16**, env-overridable `AMUX_SPAWN_MAX_SESSIONS`; plain human sessions don't count;
  count only handles with `tmux has-session` true (dead handles linger — no
  auto-cleanup). **Critical section under one global `flock` (`~/.amux/spawn/.lock`),
  held by ALL spawns incl. plain:** cap-check (tracked only) → pick a free name
  against amux's **full** namespace (`amux ls` + `tmux has-session`) → **create the
  session DETACHED via amux `--no-attach` (E5)** → (tracked: write the handle). Then
  **release the lock**, and only afterwards attach / `switch-client` for the TTY path.
  Do **not** hold the lock across the attach — `amux exec`'s normal attach blocks
  until the human detaches, which would serialize every launch. Creating detached
  first also closes the TOCTOU window (the tmux session existence is the reservation;
  amux `exec` overwrites `<name>.env` unconditionally, so the name must be confirmed
  free inside the lock). Structurally: **every** spawn = create `--no-attach`, then
  conditionally attach — `--detach`/`--wait`/`--notify` just skip the attach step.
- Since every spawn creates detached (`--no-attach`) and attaches only as a separate
  TTY step, there's no non-TTY attach failure to swallow. Confirm liveness via
  `amux ls` before reporting success (amux runs under `set -e`; don't trust exit code
  alone); print the resolved session name.

## Implementation hints / watch-outs

- **Language:** the handle/JSON and later transcript work favor Python (consistent
  with `.claude/hooks/*.py`); the launcher can be Python or bash. Pick one and keep
  the producer (10-02) able to share helpers (e.g. name↔handle resolution).
- Secrets (model auth tokens) reach the child via tmux **`update-environment`**
  (amux/E1 copies them from the spawner's live env — read from the fd, never argv), so
  nothing is inlined into the launch command (inlining would leak via `ps`). **Not**
  plain tmux inheritance, which is broken on an already-running server (Decision 1).
- Never re-pass `--session-id` for restart (D-SessionId) — that's resume-aware in
  amux (task 12 E4-full; E4-floor keeps it out of `CC_FLAGS`); spawn only ever *creates*.
- `CC_DIR` may differ from the parent's actual cwd — read it from the `.env`, don't
  assume cwd.
- Deterministic transcript path = `~/.claude/projects/<enc-dir>/<uuid>.jsonl`, where
  `<enc-dir>` replaces **both `/` and `.`** with `-` (and keeps `_`) — verified
  against `~/.claude/projects/` (e.g. `/home/anton/.local/…` → `-home-anton--local-…`).
  A dir with a dot (e.g. `…/v3.2`) breaks a slash-only formula. **Safer: capture the
  real `transcript_path` from the first `Stop` payload** (10-02 writes it) rather than
  computing it; if you must compute it at spawn, encode dots too. Store it in the handle.
- Atomic handle writes: tmp file + rename.
- **Installation:** ensure `amux-spawn` lands on `PATH` system-wide (extend
  `install-claude-config.sh` or document); it must not depend on repo-local files
  at runtime.

## Done criteria

- [ ] Human: `amux-spawn spawn` at a TTY creates a plain amux session in cwd and
      attaches (switch-client inside tmux); `(claude_glm5_env && amux-spawn spawn)`
      runs the GLM model **(test from a second amux session on an already-running
      tmux server — the real topology)** with no sonnet override and no auth conflict.
- [ ] Agent: a non-TTY `amux-spawn spawn -- "<prompt>"` creates a tracked session
      (handle written, id minted), seeds+auto-submits the prompt, returns without
      blocking, and the session is live (`amux ls`).
- [ ] Dir/run_id inheritance works from a parent amux session; `--dir` overrides.
- [ ] Names auto-increment and survive a concurrent double-spawn without collision.

## Testing

- Manual TTY run + `amux ls`/`amux attach` to watch the seeded prompt execute.
- Headless: spawn into a temp dir, assert handle JSON, minted id, transcript path
  exists, and `tmux has-session` true; verify the seeded prompt produced a turn.
- Env: **from a second amux session on an already-running tmux server**, launch under
  an alt-model `_env` function; confirm the child's effective model (peek the session /
  its transcript) carried via `update-environment`, and that no secret appears in
  `ps -ww`. (A fresh-server test gives a false pass — Decision 1.)
- Negative: exceed the fork-bomb cap → refused with a clear message.
