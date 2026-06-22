# Task 12 — amux extensions (prerequisite for Epic 10)

**Status:** v1 implemented (E1/E2/E3/E4-floor/E5 done in fork branch
`feat/epic-10-amux-extensions`; commits `76cc680`, `f86fb62`, `fa6ccc1`) ·
E4-full + deploy outstanding · **Type:** upstream/external · **Created:**
2026-06-22 · **Rev:** 5
<!-- Rev 3 (adversarial review): E1 rewritten around tmux `update-environment` (plain
inheritance is broken on a running server); E4 split into E4-floor (blocks 10-01) and
E4-full (post-v1); E5 `--no-attach` added. -->
<!-- Rev 4 (build kickoff): fork chosen (aDorofeev/amux at ../amux); Decision 4
(E4-floor ships E4-full's meta schema + create/restart fork now, merge-safe write,
deferring only the `--resume` branch); Decision 5 (`--no-default-model` persists via a
backward-compatible `.env` key). -->

**Repo:** amux — own fork **`github.com/aDorofeev/amux`**, local clone at
**`../amux`** (sibling of this repo; remote `origin` = the fork). Forked rather than
waiting on upstream because Epic 10-01 is blocked on E4-floor + E5. Pin the fork
commit `amux-spawn` targets; deploy by replacing `/usr/local/bin/amux` with the fork
build (the installed 0.3.0 binary is **not** the fork HEAD — the fork carries
`amux-remote` and other features, so the deploy ships the whole fork build, not a
patch). Prereq for [Epic 10](./10_spawn_sessions/). Evidence for each item is in Epic
10's [architecture.md](./10_spawn_sessions/architecture.md) §2 (lifecycle spike).

## Why

`amux-spawn` (Epic 10) needs launch behaviors amux 0.3.0's CLI does not expose.
Rather than reimplement amux's registration/quoting/OAuth-unset/launch and risk
drift, extend amux.

## Where (orientation for a fresh agent)

Relevant functions in the amux script: `cmd_exec` (register+start), `cmd_start`
(builds the `claude …` command and runs `tmux new-session -d … "<shell_setup>cd …;
$cmd"` then attaches), and `parse_claude_flags` (which routes **unknown** flags
into `CC_EXTRA_ARGS`, i.e. on to `claude`). Per-session config: `<name>.env`
(`CC_DIR`, `CC_FLAGS`) and `<name>.meta.json` (already stores `codex_session_id` —
the model for E4-floor/E4-full). **New amux-level options (E1 `--env`, E2
`--no-default-model`, E5 `--no-attach`) must be parsed as amux options, not
forwarded to `claude`** — extend the option parsing ahead of the claude-flag
passthrough.

## Extensions (prioritized)

### E1 — Correct env propagation *(required, v1)*
**Plain tmux inheritance does NOT work** when a tmux server is already running — the
agent-chain case, since the spawner is itself inside tmux. A `new-session` pane on an
existing server takes its env from the **server's global env** (frozen at server
start), *not* from the spawning client. **Spike-verified:** a var exported by the
caller arrives **empty** in the new pane without explicit propagation:
`new-session` (no `-e`) → `noenv=[]`. So model/auth vars do **not** reach the child
by inheritance alone, and the previous "inheritance is the primary path" plan is
wrong for the topology that matters.

**Mechanism (decided — Decision 1): tmux `update-environment` allowlist.** It copies
the listed vars from the spawner's **live environment** (read via the inherited fd,
**never argv ⇒ no `ps` leak**) and works on an already-running server
(spike-verified: `update=[via_update]`). The changes amux needs:
- **Append the curated set to the server-global `update-environment`** before
  `new-session`: `ANTHROPIC_MODEL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_DEFAULT_*`, curated `CLAUDE_CODE_*`,
  `API_TIMEOUT_MS`. **Append-and-preserve**: union into the existing list, never
  overwrite (keep `DISPLAY`/`SSH_*`/…). Note `update-environment` *unsets* a listed
  var that is absent from the spawner's env — desirable (a clean alt-model spawner
  with no `API_KEY` yields no key in the pane).
- **Add `unset CLAUDE_CODE_SESSION_ID`** to the spawn `shell_setup` (it already
  unsets `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT`); keep `CLAUDE_CODE_SESSION_ID`
  **out of** the `update-environment` list. Belt-and-suspenders so a child never
  believes it *is* the parent session.
- **Keep/extend auth precedence in `shell_setup`** (not via `update-environment`):
  drop `ANTHROPIC_API_KEY` when `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` are
  present. Today amux only drops it for OAuth.
- **`--env KEY=VAL` (repeatable)** stays available as a manual override **but must
  NOT carry secrets** — it inlines onto the tmux command line (`ps` leak).
  Non-secret targeted overrides only.
- **Re-spike (replaces the misleading §2.9 evidence):** confirm the curated vars
  land in the pane **from a second amux session on an already-running server** — not
  a fresh server / login shell that already had the vars in global env. That false
  baseline is exactly why the original spike looked like plain inheritance worked
  (and why §2.9 wrongly reported `CLAUDE_CODE_SESSION_ID` leaking — a per-session var
  that could only appear under full inheritance).

### E2 — Suppress the default model *(required, v1)*
- A `--no-default-model` amux option so `cmd_start` does **not** append
  `--model sonnet` when no `--model` is given. Under alt-model env that default
  wrongly selects `ANTHROPIC_DEFAULT_SONNET_MODEL` instead of `ANTHROPIC_MODEL`.
  Scope to the **claude** provider; leave codex's model defaulting untouched.
- **Persist, don't pass transiently (Decision 5).** `register`/`exec` write a
  `CC_NO_DEFAULT_MODEL=1` key to `<name>.env`; `cmd_start` reads it and skips the
  sonnet injection on **every** start (including `start-all`). Transient (re-passed
  each spawn, never remembered) would reintroduce the exact E2 bug on any later
  human `amux start` whose shell still has `ANTHROPIC_MODEL` set — sonnet alias
  wrongly resolving the alt model — for the session's whole lifetime, not just first
  launch. **Backward compatible:** the guard is
  `[[ "${CC_NO_DEFAULT_MODEL:-}" == "1" ]] || cmd="$cmd --model sonnet"`, so an
  existing `.env` with no such key falls through to today's behavior; the key is
  written only when `--no-default-model` was explicitly passed. (Cold-restart caveat:
  if the alt-model env is gone, neither choice reconstructs `ANTHROPIC_MODEL` —
  persisting just never wrongly injects sonnet; full reconstruction is E4-full /
  spawn-env-persistence territory, out of scope.)
- Note: `--no-default-model` only stops amux *injecting* sonnet. The standard
  `--model` tier (opus/sonnet/haiku) is a **flag, not env**, so it isn't inherited
  via env — propagating the parent's `--model` is `amux-spawn`'s job (10-01 reads it
  from the parent's `CC_FLAGS`), not amux's.

### E3 — Nested-tmux attach (switch-client) *(required for Epic 10 C9)*
- `cmd_start`/attach should detect `$TMUX` and use `tmux switch-client` instead of
  `tmux attach-session` (the latter refuses/misbehaves from inside tmux — the normal
  case when switching). Assumes same tmux server; otherwise fall back gracefully.

### E4-floor — Keep `--session-id` out of `CC_FLAGS` *(required, v1 — blocks 10-01)*
`parse_claude_flags` currently routes `--session-id` into `CC_FLAGS` (line 210),
which amux persists to `<name>.env` and **re-passes on every `start`/`start-all`** ⇒
Claude dies *"Session ID already in use"*, killing the tracked session. amux-spawn
mints a `--session-id` for every tracked session, so this is **not optional polish —
it blocks 10-01** (Decision 2).
- Store the minted id in **`<name>.meta.json`** (like `codex_session_id`), never in
  `CC_FLAGS`. The initial `exec`/first start applies `--session-id <id>` (create).
- This alone makes `start-all` safe — it can't re-pass an id it never stored.
- **Ship E4-full's meta schema + create/restart fork now (Decision 4).** The "apply
  on create only" property already forces `cmd_start` to know "is this the first
  start?", and driving that off a meta flag (not off "which command") correctly
  handles `register` + plain `start`, where the *first start* — not `exec` — is the
  create. So write `{"claude_session_id":"<id>","started":false}` and fork in
  `cmd_start`: id present **and** `started==false` → apply `--session-id`, then flip
  `started=true`; `started==true` → **(E4-floor)** apply nothing → **(E4-full)** apply
  `--resume <id>`. Building the `started` flag now costs nothing and makes E4-full a
  one-line gated branch instead of a meta-schema migration over the same delicate
  launch fork. Only the literal `--resume` invocation is deferred.
- **Bash meta-write MUST be merge-safe.** `amux-server.py` (the codex path) is the
  only writer today; the bash side now becomes a second writer of the same file. Do a
  read-modify-write of the JSON (preserve `codex_session_id` and anything else
  present), never a clobbering `cat >`. A meta.json without `claude_session_id` /
  `started`, or no meta at all, behaves exactly as today (no `--session-id`).

### E4-full — Resume-aware restart *(post-v1; removes the remaining footgun)*
- A *subsequent* `start` of a session that has run before uses **`--resume <id>`**
  (mark "has-run" in the meta after first start) instead of `--session-id`. Never
  re-pass `--session-id` on restart.
- Epic 10 never restarts tracked sessions itself; this just makes manual restart
  resume cleanly for everyone. May follow v1; does not block it.

### E5 — `--no-attach` start option *(required for Epic 10 detached launch)*
`cmd_start` always ends with `tmux attach-session` (line 383) — there is no way to
create a session without attaching. amux-spawn needs detached launch **even at a
TTY** (`--detach`, and `--wait`/`--notify` which force detached). Add a `--no-attach`
amux option that makes `cmd_start` skip the trailing `attach`/`switch-client`.
amux-spawn passes it on every non-interactive spawn (the agent path too) instead of
relying on `attach-session` failing. Note amux runs under `set -euo pipefail`, so a
swallowed attach failure exits non-zero — callers confirm liveness via `amux ls`,
not the exit code (Decision 3).

## Deployment

- Build against a local clone/fork; **pin the amux version** `amux-spawn` targets.
- Deploy by replacing `/usr/local/bin/amux` with the extended build; `amux-spawn`
  calls `amux` on `PATH` (no hard-coded path). Document the install step.
- **⚠ Outstanding — not yet deployed.** The fork branch is built and tested but
  the installed `/usr/local/bin/amux` is untouched. Deploy ships the whole fork
  `amux` (carries `amux-remote` etc., not just E1–E5) — confirm that's intended,
  pin the commit (`fa6ccc1` or its merge), then replace the binary. `amux-server.py`
  is unchanged so it need not be redeployed.

## Acceptance criteria

- [x] A child session gets the parent's model/auth env (alt-model included) via the
      `update-environment` allowlist, **from a second amux session on an
      already-running server**, with no secret in `ps`, `CLAUDE_CODE_SESSION_ID`
      unset, and no auth conflict. *(Re-spike automated as
      `test_propagation_end_to_end_on_running_server`: model+token reach the pane,
      `DISPLAY` preserved, absent key not leaked, union dedups.)*
- [x] With `--no-default-model`, an alt-model session runs `ANTHROPIC_MODEL` (not
      the sonnet alias) when no `--model` is passed; codex defaults unaffected. The
      suppression is **persisted** (`CC_NO_DEFAULT_MODEL=1` in `<name>.env`) and
      survives a later `start`/`start-all`; an `.env` without the key still injects
      sonnet as before.
- [x] `amux attach <name>` from inside tmux switches cleanly. *(tmux_attach uses
      `switch-client` when `$TMUX` is set, falls back to `attach-session`.)*
- [x] **(E4-floor)** Create stores `--session-id` in `<name>.meta.json`, never in
      `CC_FLAGS`; `start-all` with a tracked session present does not re-pass it / die.
- [ ] **(E4-full, post-v1)** A later restart of the same session resumes via
      `--resume` without collision. *(Deferred. Meta schema + create/restart fork
      already shipped; only the `--resume` branch — the `started==true` arm — is
      outstanding.)*
- [x] **(E5)** `--no-attach` creates a live session without attaching, at a TTY and
      off it; default behavior (no flag) still attaches.
- [x] New amux options are parsed by amux (`parse_amux_opts`), not forwarded to
      `claude`.
- [x] **Backward compatible:** all existing amux behavior is unchanged (additive
      only) — existing sessions, `exec`/`start`/`attach`/`ls`/`send`/`start-all`,
      and the codex path behave exactly as before when the new options are absent.
      *(Full suite 181 passed: 159 upstream + 22 new; no regressions.)*

## Testing

Use throwaway sessions in a temp dir and `amux rm` everything created (mirror the
Epic 10 spike's hygiene). For each extension:
- **E1:** with a tmux server **already running**, from a second amux session spawn
  under an alt-model `_env`; assert the child's effective model (peek its
  transcript), that `ANTHROPIC_AUTH_TOKEN`/`BASE_URL` reached it via
  `update-environment`, that `CLAUDE_CODE_SESSION_ID` is unset in the pane, and that
  no secret appears in `ps -ww` for the launch command. Verify the existing
  `update-environment` entries (`DISPLAY`/`SSH_*`) are preserved (append, not
  overwrite).
- **E2:** with `--no-default-model` and an alt-model env, confirm `ANTHROPIC_MODEL`
  is used (not the sonnet alias); confirm codex sessions still get their defaults.
- **E3:** from inside a tmux session, `amux attach <name>` switches without the
  nested-attach warning; from outside tmux it still attaches normally.
- **E4-floor:** create a session; assert `--session-id` is in `<name>.meta.json` and
  **not** in `CC_FLAGS`; run `start-all` with a tracked session present and confirm
  it doesn't die.
- **E4-full (post-v1):** stop a session that has run, then `amux start` it; assert
  `--resume`, no "already in use".
- **E5:** `amux exec … --no-attach` from a TTY creates a live session without
  grabbing the terminal; without `--no-attach`, it still attaches.
- **Regression:** a plain `amux exec`/`start`/`attach` with no new options behaves
  identically to upstream.

## Notes

- Prefer minimal, upstreamable changes; fork only if needed.
