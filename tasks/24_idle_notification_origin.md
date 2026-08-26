# 24 — Idle notifications only for sessions the operator started

**Status:** todo · **Owner:** Anton · **Created:** 2026-08-26 · **Rev:** 3 · **Type:** standalone task
**Depends on:** nothing technical. **Hold until epics 22 / 23 land** — they are
in flight in the same checkout and touch neighbouring hooks.
**Read first:** [architecture.md](../architecture.md) §"Idle notification (current)" ·
[tasks/10_spawn_sessions/architecture.md](./10_spawn_sessions/architecture.md) §6.0 (handle schema)

Standalone rather than an epic: one predicate, one hook, one test class, a few
doc edits. Nothing here needs a brd or a dependency graph.

**Before you start.** Every `file:line` below was taken against **`ed0ba1b`**,
and this task is deliberately held behind two in-flight epics — expect drift.
Re-anchor by symbol, not by number: `grep -n` the function or the quoted line
rather than trusting the offset. If an anchor has moved far enough that the
surrounding logic changed (particularly `cmd_spawn`'s tracked/plain decision or
`main()`'s gate order), re-verify the §6 facts that depend on it before writing
code — the design rests on those, not on the line numbers.

## 1. Goal

Every Claude session on the machine currently pushes a Telegram **💤 Idle** card
when it finishes a turn. That is correct for a session *you* started and walked
away from. It is noise for a session a *machine* started — an agent's
`amux-spawn` child, or hyppie-flow's cron PM tick — because those report to their
parent, not to you.

After this task the idle hook can tell the two apart and stays quiet for the
second kind. Sessions you start by hand are untouched.

**This task ships no settings and no config file.** It hard-codes the policy
"notify operator-started sessions, stay silent for the rest", behind a named
function whose body the later settings epic replaces. See §10.

**The risk to manage is a false positive, not a false negative.** A spawned
session that pings you once too often is mildly annoying. A session *you* started
that silently stops reaching your phone is a trust failure you may not notice for
days. Every design choice below resolves in that direction, and §3's second
condition exists solely to close a path to the second outcome.

## 2. Background — where the noise comes from

`install-claude-config.sh:697–717` merges the `Notification` hooks into
**`~/.claude/settings.json`**, i.e. user-global. So `notification_hook.py` fires
in *every* session on the box — attached, detached, spawned, cron-started alike.
There is no per-project opt-out, and there shouldn't be: the point is that a
session left in another tmux window can still reach your phone.

The only suppression that exists today is `has_active_background_agents()`
(`notification_hook.py:195`), which mutes the ping while async Agent / Bash /
Monitor / Workflow work is still in flight — a parent looks "idle" while it waits
on children. Nothing in the hook knows *who started the session*.

Two things generate the noise in practice:

- **Agent spawn chains.** An agent calls `amux-spawn spawn` from its Bash tool;
  the child works, finishes its turn, goes idle, and pings your phone. The parent
  agent is already watching it with `amux-spawn status` / `amux-spawn last` — the
  Telegram card tells you nothing you asked for.
- **hyppie-flow's PM loop.** `scripts/pm-tick-watchdog.sh:111` runs
  `amux-spawn spawn pm-tick --dir "$REPO" --detach --profile claude` from cron
  every five minutes. Each tick ends its turn with a `PM-PARKED:` verdict line
  and is then reaped by the watchdog. Each of those is currently a card.

The PM loop is **not** a separate category needing its own mechanism — it is an
`amux-spawn` spawn like any other. That collapses the design.

## 3. The predicate — a tracked handle naming *this* session

`amux-spawn` already makes this judgement at spawn time and already writes it
down as a file. It classifies each spawn as **tracked** or **plain**
(`.claude/bin/amux-spawn:284`):

```python
# ILLUSTRATIVE — existing code, shown for orientation. Do not re-type it.
tracked = (not is_tty) or wait_mode
```

A tracked spawn gets a **handle** at `~/.amux/spawn/<name>.json`. A plain one
gets nothing. The classification keys off the **TTY**, not off amux — which is
the reason this works:

| how the session starts | tty? | classified | handle | idle ping |
|---|---|---|---|---|
| `claude` from your bashrc → `amux-spawn spawn --profile claude` | ✅ | plain | — | **sends** |
| `amux-spawn spawn --detach` by hand | ✅ | plain | — | **sends** |
| `amux new` / bare `claude` (never reaches `amux-spawn`) | — | — | — | **sends** |
| agent's Bash tool → `amux-spawn spawn` | ❌ | tracked | ✅ | silent |
| cron → `pm-tick-watchdog.sh` | ❌ | tracked | ✅ | silent |
| `amux-spawn spawn --wait` / `--notify` | ✅ | tracked | ✅ | silent |

The row that matters most is the first. Anton's bashrc sources
`~/.claude/shell/amux-spawn.bash`, which defines a shell function per model
profile routing `claude` through `amux-spawn spawn --profile claude "$@"`
(`amux_spawn_lib.emit_shell_functions`, `:1351`). So **hand-started sessions are
amux-wrapped too** — "is this an amux session?" would silence everything. "Does
this amux session have a handle?" silences the right set, because the TTY is what
separates them.

### 3.1 Why the name alone is not enough

"A handle exists under this pane's amux name" is *almost* the predicate. It has
one hole, and it fails in the dangerous direction:

- handles are removed **only** by an explicit `amux-spawn rm`
  (`amux-spawn:2121`) — the registry is documented as deliberately accumulating;
- `amux`'s own `rm` deletes the session's `.env` (`/usr/local/bin/amux:1815`);
- `name_in_use()` (`amux_spawn_lib.py:398`) decides a name is free from the
  `.env`, amux's name list, and live tmux — it **never consults the handle
  registry**.

So a tracked session that was cleaned up with plain `amux rm` instead of
`amux-spawn rm` leaves an **orphaned handle** and frees its name. A later spawn
in the same workspace — possibly one of yours — can be handed that name and
inherit the orphan. Under a name-only check your own session would go silent, and
nothing would tell you.

The fix is to require the handle to name **this** session. `amux-spawn` mints the
`--session-id` UUID for every tracked Claude spawn and stores it in the handle
(`new_handle`, `amux_spawn_lib.py:623`); the Notification payload carries the
running session's id, already extracted at `notification_hook.py:611`. Comparing
them is exact identity, not a heuristic:

```python
# ILLUSTRATIVE sketch of the two functions. Not a patch — write them against
# the file as it stands.

def session_started_by_agent(amux_name, session_id):
    """Fact: was THIS session spawned programmatically? Fail-OPEN."""
    if not amux_name:
        return False                      # not an amux session → operator's
    handle = lib.read_handle(amux_name)
    if handle is None:
        return False                      # plain session → operator's
    # Must name this exact session, not merely share its amux name (§3.1).
    return handle.get("session_id") == session_id


def should_notify_idle(amux_name, session_id):
    """Policy: may this session raise an idle notification?"""
    return not session_started_by_agent(amux_name, session_id)
```

Every failure mode of that predicate lands on "notify": no tmux, no handle, a
corrupt handle, a handle for a different session, an empty `session_id` in the
payload, a Codex handle whose `session_id` is `None`.

Keep them as **two** functions. The first is a fact about the session, the second
is what we do about it; §10's settings replace only the second. Collapsing them
into one inline `if` is what turns the settings epic into a rewrite instead of a
one-function edit.

This is the same handle-gate `spawn_producer_hook._resolve_tracked_handle()`
(`:74`) already uses to decide whether a lifecycle event is its business, so the
repo gains no new concept — one more caller of an established one, with an
identity check the producer does not need (it only ever writes, never suppresses).

## 4. Why the handle, and not the alternatives

Recorded so nobody re-opens these.

- **An env var (`CLAUDE_SESSION_ORIGIN=agent`) — rejected as the primary
  mechanism.** It cannot reach a spawned pane. `AMUX_ENV_ALLOWLIST` is a
  hard-coded bash array at `/usr/local/bin/amux:181`, and tmux
  `update-environment` copies *only* the listed names — anything else is not
  merely absent, it is actively **unset** in the child. Adding a name means
  forking amux and bumping `AMUX_PIN` in `install-amux.sh`, which is the friction
  epic 21 exists to avoid. `architecture.md:375` records the same fact for
  `CC_NAME`/`CC_DIR`. An env var stays viable only as a future per-session
  override for launchers that set it in the launched process's *own*
  environment — see §10.
- **An `origin` field on the handle — rejected as unnecessary.** It would let us
  distinguish a human's `--wait` spawn from a cron tick. Both are silenced (§7),
  so there is nothing left for it to distinguish, and it would cost a change to
  the §6.0 handle schema plus a new `amux-spawn` flag.
- **A marker store keyed by `session_id`** (like `session_yolo_store.py`) —
  rejected as premature. Once §3.1's identity check is in place, the handle
  registry already answers the question; a second registry would need its own
  writer, reaper and consistency story.
- **Config-file suppression by workspace** — wrong axis. hyppie-flow hosts both
  hand-started and cron-started sessions in the same directory.
- **Routing to a muted role chat instead of dropping** (epic 15 bindings) —
  keeps a paper trail, but `amux-spawn status` / `last` already *is* the paper
  trail for exactly these sessions.

## 5. Scope

### 5.1 `.claude/hooks/notification_hook.py` — the predicate and the policy seam

Add the two functions from §3.1, placed just after `resolve_amux_session()`
(`:530`) since they consume its output.

The module needs `amux_spawn_lib`, which it does not import today. Mirror
`spawn_producer_hook.py`: `sys.path.insert(0, str(Path(__file__).parent))` before
`import amux_spawn_lib as lib`. Strictly the insert is redundant — a hook invoked
as `python3 /home/anton/.claude/hooks/notification_hook.py` already has that
directory as `sys.path[0]` — but every other hook importing the lib does it, and
matching them costs nothing.

**No installer change is needed for the import.** `amux_spawn_lib.py` is already
in `REQUIRED_HOOKS` (`install-claude-config.sh:163`), so the flat install layout
already has it next to `notification_hook.py`.

Do **not** copy `spawn_producer_hook`'s provider gate (`is_claude_handle`). That
hook must not overwrite a Codex worker's state; this one only asks "was this
session spawned", and the §3.1 identity check already excludes a Codex handle
(its `session_id` is `None` at spawn time) — which is moot anyway, since a Codex
worker runs no Claude hooks.

### 5.2 `.claude/hooks/notification_hook.py` — wiring in `main()`

Placement carries the substance:

- The gate goes **after** the `notification_type != 'idle_prompt'` check
  (`:623`) and **before** `has_active_background_agents(transcript_path)`
  (`:629`). One tmux round-trip is far cheaper than replaying a transcript that
  can run to megabytes, and for a tracked session the replay's answer is
  irrelevant. `session_id` is already in scope from `:611`.
- `resolve_amux_session()` is currently called at `:654` for reply routing.
  Hoist that single call to the gate and reuse `amux_name` below rather than
  calling it twice — the second call is a second `tmux display-message`
  subprocess for an answer we already have.
- Log the skip through `debug_log()`, naming the amux session, so
  `~/.claude/notification_hook_debug.log` shows *why* a ping did not arrive.
  This is the only way to diagnose an over-eager gate in the field, and Anton's
  global settings already run this hook with `CLAUDE_HOOK_DEBUG=1`.
- Keep the hook fail-OPEN, as everything else here is. `read_handle` already
  returns `None` on `OSError`/`ValueError`, and the identity check turns every
  other surprise into "notify". A broken lookup must never silence a real idle
  prompt.

### 5.3 Docs

- `architecture.md:103` — the `Notification`/`idle_prompt` table row should say
  the forward is conditional on the session being operator-started.
- `architecture.md:351` "Idle notification (current)" — add the gate to the flow
  block, above the existing background-agent suppression line, in the same style.
- `.claude/README.md:117–118` — the `Notification` hook description.
- `install-claude-config.sh:857` — the summary line still reads
  "idle_prompt → Telegram (forwards agent's last message)" unconditionally.
- The module docstring's "Behaviour notes" list in `notification_hook.py` gains
  one bullet.

### 5.4 Not in scope for the code change

No change to `amux-spawn`, `amux`, the handle schema, the relay, or any other
hook. `install-claude-config.sh` is touched for its summary string only.

## 6. Important facts (verified 2026-08-26 against `ed0ba1b`)

1. `~/.amux/spawn/` on this machine held only `.lock` while three amux sessions
   (`amux-claude-hooks-58/59/60`) were live and attached — direct confirmation
   that hand-started sessions write no handle.
2. `amux-spawn:284` `tracked = (not is_tty) or wait_mode`; `:476`
   `kind = "tracked" if tracked else "plain"`. `--detach` is *not* part of the
   tracked decision — it only affects attachment.
3. **Names are reused, handles are not reaped with them.** `name_in_use()`
   (`amux_spawn_lib.py:398`) consults the `.env`, amux's name list and live tmux,
   never `~/.amux/spawn/`; `amux`'s `rm` deletes the `.env`
   (`/usr/local/bin/amux:1815`); `delete_handle` is called only from
   `amux-spawn rm` (`:2121`) and the launch rollback (`:675`). This is what §3.1
   defends against.
4. **The handle is written *after* the session is live** — `_amux_create_detached`
   → `_launch_confirmed` → `write_handle`, all inside the spawn lock
   (`amux-spawn:407–458`). There is therefore a sub-second window in which a
   tracked session exists with no handle. It is not worth closing: a seeded spawn
   is mid-boot and cannot have reached an idle prompt yet, and the failure mode
   is one stray notification, not a silenced session.
5. `lib.SPAWN_DIR` (`amux_spawn_lib.py:46`) derives from `AMUX_HOME` (`:43`),
   which reads `CC_HOME` **at import time**. Tests must patch the `lib.SPAWN_DIR`
   attribute; setting the env var after import does nothing.
6. `lib.read_handle` (`:578`) returns `None` for both a missing file and a corrupt
   one — the fail-open behaviour §5.2 relies on, no extra guarding needed.
7. `amux-spawn rm` stops the tmux session *and* deletes the handle
   (`:2118–2121`), so the ordered teardown leaves nothing orphaned. Only plain
   `amux rm` does.
8. **`amux-spawn resume` is Codex-only** (`:855–860`: "there is no bounded-turn
   resume for [Claude sessions]"), so a Claude tracked handle's `session_id` is
   written once at spawn and never rewritten. Nothing in the normal lifecycle
   invalidates the §3.1 comparison.
9. `--wait` / `--notify` are caller-side blocking with an exit-code contract
   (`amux-spawn:1012–1030`); `--notify` is an alias of `--wait`, not an
   independent Telegram send. Nothing downstream depends on the idle card.
10. **The minted `--session-id` reaches the Notification payload unchanged —
    measured, not assumed.** A throwaway tracked spawn (`amux-spawn spawn --dir
    <tmp> --profile claude --wait`) on 2026-08-26 produced:

    ```
    handle  session_id : 765d7b01-f3ce-4d6d-9ccf-4b6c24decfec
    payload session_id : 765d7b01-f3ce-4d6d-9ccf-4b6c24decfec
    ```

    The same run also confirmed the two other things §3.1 depends on:
    `resolve_amux_session()` resolves the name from a **detached** spawned pane
    (the hook logged `reply injector armed for amux:idle-probe`), and the
    session's real transcript is
    `…/-tmp-…-idle-probe/765d7b01-….jsonl` — Claude Code adopts the minted UUID
    as its own session id rather than minting a second one. amux additionally
    `unset`s `CLAUDE_CODE_SESSION_ID` in the child shell
    (`/usr/local/bin/amux:1321`), so a spawned session can never inherit its
    parent's id.
11. **`idle_prompt` lags `Stop` by a wide margin.** In the same run `--wait`
    returned (handle `idle`) while the Notification hook did not fire for
    another two minutes. This further de-risks fact 4: the handle is written
    seconds after launch, minutes before any idle prompt could race it.

## 7. Edge cases, and the decisions already taken

| case | behaviour | decided |
|---|---|---|
| `--wait` / `--notify` spawn from a TTY | silent | yes — the caller already receives `last_message` on stdout |
| adopting a spawned session with `amux-spawn a <suffix>` | silent (handle survives attachment) | yes — you are looking at the terminal |
| `amux-spawn spawn --detach` by hand | **notifies** | yes — plain session, and the ping is your only signal |
| orphaned handle whose name a later session reuses | **notifies** — session ids differ | §3.1 |
| `/clear` inside a tracked session (new session id) | **notifies** — ids no longer match | accepted; wrong in the harmless direction |
| nested `claude` inside a session's Bash tool | inherits the parent pane's `$TMUX_PANE`, so the name matches but the session id does not → **notifies** | acceptable |
| non-amux auto-starters (systemd unit, wrapper script) | **notifies** — not covered | residual gap, closed by §10 |

Two downstream consequences the implementer should know rather than discover:

- **You lose the ability to answer a spawned session from Telegram.** Tracked
  sessions currently take the force-reply + `reply_injector` path
  (`notification_hook.py:654–676`); no card means no reply affordance. There is
  no equivalent Telegram surface today — epic 16 assumes `amux send` from a
  terminal (`16-06-spawn-and-wizard.md:132`). This is accepted, and it also
  shrinks the surface of the known idle-reply misroute problem.
- **It slows a pre-existing leak.** Every idle notification from an amux
  session arms a detached `reply_injector.py` that outlives its session: 21 were
  alive on this machine during the §6 probe, most of them pointing at amux names
  that no longer exist (`claude-hooks-56`, `claude-hooks-58`). This task neither
  causes nor fixes that, but silencing tracked sessions removes a large share of
  the arming events. Worth its own ticket, not this one.
- **Epic 19's nudges follow automatically.** The nudge engine treats an idle
  notification with `reply_required=True` as nudgeable
  (`19-04-nudge-engine.md:145`). Suppressing at source removes tracked sessions
  from the ladder too — no coordination needed, but worth stating so 19's
  live-verification counts are not read as a regression.

## 8. Testing

`tests/test_unit_notification_hook.py` (`unittest`, driven by
`python3 tests/run_all_tests.py`). **Baseline: 53 tests in this file at
`ed0ba1b`** — re-measure the suite total rather than trusting a remembered
number, and report before/after.

**One hazard will bite you.** `TestMainRouting.setUp` (`:158`) patches
`resolve_amux_session` to `None`, but three of its tests override it with a real
name (`"hyppie-flow"`). Once the gate exists, those tests reach the *developer's
real* `~/.amux/spawn/` registry. They pass today only because that directory
happens to be empty — anyone with a live `hyppie-flow` handle would see them
flip. Add a default `patch.object(nh.lib, "read_handle", lambda name: None)` to
`setUp` (untracked = operator-started) so the class is hermetic, and let
individual tests override it.

Cases, as a new `TestSessionOrigin` class plus additions to `TestMainRouting`:

| # | Setup | Expect |
|---|---|---|
| 1 | temp `lib.SPAWN_DIR` holding `<name>.json` whose `session_id` **matches** the payload | agent-started; `should_notify_idle` false |
| 2 | temp `SPAWN_DIR` with no handle for the name | operator-started — the hand-started case |
| 3 | `amux_name is None` | operator-started; no tmux is not evidence of a spawn |
| 4 | handle file containing `{not json` | fails **open**: notifies |
| 5 | handle whose `session_id` is a **different** UUID | fails open: notifies — the §3.1 orphan-handle guard |
| 6 | handle with `session_id: None` (Codex-shaped); payload id present | fails open: notifies |
| 7 | payload `session_id` empty string, handle carries a real UUID | fails open: notifies |
| 8 | `main()`, matching tracked handle | exit 0, `send_idle_notification` never called, no injector spawned |
| 9 | `main()`, matching tracked handle, `transcript_path` pointing at a nonexistent file, `has_active_background_agents` patched to record its calls | exit 0 **and the recorder stays empty** — proves the gate precedes the replay |
| 10 | `main()`, amux session with no handle | still sends, still force-reply, still arms the injector with `(message_id, name)` |

Cases 1–7 should drive the **real** `lib.read_handle` against a temp directory
rather than stubbing it, so they break if the handle layout moves. Case 5 is the
regression guard for §3.1 and must not be dropped as redundant — it is the one
protecting Anton's own sessions. Case 9 guards the placement decision in §5.2.
Case 10 guards "hand-started sessions still work", which is the whole risk of
this task.

## 9. Done criteria

1. `python3 -m py_compile .claude/hooks/notification_hook.py` clean.
2. `python3 tests/run_all_tests.py` green, with the ten cases above added and the
   before/after suite counts reported.
3. Re-run the identity probe of §6 fact 10 **only if** `cmd_spawn`'s
   `--session-id` handling or amux's launch path has changed since `ed0ba1b`:
   spawn a throwaway tracked session and compare
   `jq -r .session_id ~/.amux/spawn/<name>.json` against that session's
   `Session ID:` line in `~/.claude/notification_hook_debug.log`. They matched
   on 2026-08-26. If they ever stop matching, the gate degrades to "always
   notify" — safe, but the task has not achieved its goal, and the cause must be
   found before closing. Reap the probe with `amux-spawn rm <name>` (never plain
   `amux rm`, per §3.1).
4. A tracked session going idle logs a skip line naming its amux session in
   `~/.claude/notification_hook_debug.log`, and sends nothing.
5. A hand-started session going idle still produces the Telegram card *and* an
   armed reply injector — verified live, not only in tests, because §3's first
   table row is the one that would silently break daily use.
6. `architecture.md` §103 and §351, `.claude/README.md:117–118` and
   `install-claude-config.sh:857` reflect the gate.
7. `./install-claude-config.sh` re-run — repo hooks do not take effect until the
   installer copies them into `~/.claude/hooks/`.

## 10. Out of scope — the settings surface that comes later

Anton's stated direction is a notification settings surface covering, at least:

- **Idle notifications** — on/off, default **on**.
- **Idle notifications for sessions not initiated by the user** — on/off,
  default **off**. This task is that setting, pinned to its default.
- Settings for nudges and escalations, alongside epic 19
  (`19_unanswered_reminders`) and epic 23 (`23_async_questions`).

That epic owns the questions this task deliberately does not answer: where the
settings live (the relay config at `~/.config/claude-tg-relay/config.toml` is the
natural home — `roles_config.load_bindings` already parses it), whether they are
global or per-workspace, and whether a per-session env override
(`CLAUDE_SESSION_ORIGIN` / `CLAUDE_IDLE_NOTIFY`) is worth adding for launchers
outside `amux-spawn` — the §7 residual gap.

The only obligation this task carries forward is the seam: `should_notify_idle()`
must be the single place the answer is decided, so that epic changes one function
body instead of re-deriving the policy.
