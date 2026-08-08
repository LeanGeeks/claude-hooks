# Epic 16 — Start a session from Telegram

**Status:** planning · **Owner:** Anton · **Created:** 2026-08-02 · **Rev:** 1

## 1. Problem & thesis

[Task 09](../09_reply_from_telegram.md) closed the *continue* loop: an idle
session forwards its last message, a threaded reply is injected as the next user
turn, and the conversation runs from a phone. What is missing is the *start*.
With no session running there is nothing to reply to, so the phone can only
continue work that the keyboard began. The most common away-from-desk want —
"start on this now, I'll read it later" — has no path at all.

Every Telegram interaction in the system today is **response-shaped**: a hook
sends a message and then long-polls its answer. An unsolicited message has no
sender to answer and, worse, is not inert (§2.2).

**Thesis:** add a narrow **inbound command channel** — a queue on the relay and
one **resident listener per machine** — and spend it first on `/new`, which
seeds a plain amux session in a workspace that already talks to Telegram. From
that session's first idle notification, the existing task-09 loop owns the
conversation; this epic only has to bootstrap. The channel is generic, so `/ls`
ships alongside at near-zero marginal cost.

**Division of knowledge:** the relay learns nothing new about your machines. It
routes commands to installations and renders one picker; workspaces, prompts,
directories, profiles and history stay on the machine. Every wizard step the
user sees is an ordinary relay message *sent by the listener* through the
existing `POST /v1/messages` + long-poll machinery.

## 2. Constraints discovered

Recorded because they are not obvious and each one closed off an option.

**2.1 — There is no background drain.** `waiters.py` wakes only an in-flight
`GET /v1/messages/{id}/answer`; an answer is acted upon exactly when a process
on the originating machine is parked on that message (`architecture.md:144`).
The old client daemon was deleted and hooks long-poll directly. A spontaneous
Telegram message therefore has **no listener at all** — and the primary case for
this epic is a machine with *zero* sessions running, so no hook will ever fire to
piggyback on. Something resident is unavoidable.

**2.2 — A loose message is not inert.** In `_handle_update` a non-threaded
message from the bound user is either **injected into the single open session**
(`app.py:1355`, `"fallback"`) or refused with a "use Reply" nudge when several
are open (`app.py:1335`). So "just message the bot to start a session" would, on
a machine with one idle session, land the spawn request as a *turn in that
session*. Slash-prefixed text early-returns before any of this (`app.py:1316`),
which is what makes **`/new` the only safe carrier**.

**2.3 — tracked-vs-plain is inferred from TTY.** `cmd_spawn` sets
`tracked = (not is_tty) or wait_mode` (`.claude/bin/amux-spawn:189-191`). The
listener is non-TTY but human-intent, so today it would mint a `--session-id`,
write a handle, and consume the per-workspace fork-bomb cap — while producing a
session that is *not* restartable (epic 10 D-SessionId). The launcher needs an
explicit `--plain`.

**2.4 — The listener has no login shell.** Under systemd it inherits none of
`claude.bashrc`'s exported auth. Model selection must therefore go through
`~/.claude/profiles.toml` and `--profile`, which `cmd_spawn` resolves and exports
explicitly before the spawn lock (`.claude/bin/amux-spawn:173-180`). Ambient env
is not a fallback here; it is simply absent.

**2.5 — "Workspaces that have talked to Telegram" is not recorded.**
`permission_requests.jsonl` does carry `cwd` (`permission_state_store.py:81`),
but its update paths rewrite the whole file and it holds a working set, not a
history — live sample: **3 rows, 2 workspaces, one of them `/test`**. Idle
notifications record nothing at all; they derive a display name from `cwd` and
discard it. The pick list needs its own small store (§4.2).

**2.6 — One chat, many machines.** Several installations routinely bind to one
chat (`architecture.md:76-82`) and the same workspace name can exist on more than
one of them. Every command surface in this epic is therefore machine-addressed,
and `/ls` is a fan-out, not a lookup.

**2.7 — Relay-owned keyboards need a callback namespace.** `callback_data.decode`
is strict on `m:{message_id}:o:{idx}` and returns `None` for anything else
(`callback_data.py:41`), so a command-scoped `c:{command_id}:o:{idx}` can be
added without touching the message path.

## 3. Command surface

### 3.1 `/new`

Tokens are read left to right: `+`-prefixed ones are modifiers, the first other
token is the **target**, and the next token after that starts the **prompt**,
which runs verbatim to the end. Nothing is guessed from prose — a `+opus` inside
the prompt stays inside the prompt.

| Input | Behaviour |
|---|---|
| `/new` | wizard: machine picker (only if >1 bound) → workspace picker → prompt |
| `/new claude-hooks` | target resolved; force-reply asks for the prompt |
| `/new claude-hooks fix the flaky login test` | one message, spawns |
| `/new workstation.claude-hooks fix …` | explicit machine, never ambiguous |
| `/new workstation. fix …` | machine fixed, workspace chosen in the wizard |
| `/new .` / `/new . fix …` | last target used in this chat |
| `/new claude-hooks +glm5 +opus fix …` | profile and/or model tier (§5.3) |

`/new <workspace>` with the prompt omitted is the expected shape for real work:
the prompt arrives as a normal force-reply, so it can be as long and as multi-line
as you like. Recall of past prompts and templates is deliberately **not** in v1 —
see [epic 17](../17_prompt_recall/brd.md).

Every wizard step carries `[✖ Cancel]` and expires with its message TTL.

### 3.2 `/ls`

Fans out to every installation bound to the chat and renders one message grouped
by machine, listing live amux sessions: name, workspace, last activity, and —
for tracked sessions only — the derived state from the epic-10 handle. Machines
that do not answer within the deadline are listed as offline rather than omitted.
`/ls <machine>` scopes to one.

### 3.3 Acknowledgement

On success the bot posts the session name, the machine, the directory and the
resolved profile/tier:

```
▶ claude-hooks-2 · workstation
/data/sync/work/leangeeks-ai/claude-hooks · claude +opus
```

which is also how you learn the session name for `amux a` at the keyboard. Everything after that is the existing idle-notification and reply-
injection loop, unchanged: the seeded turn ends, the notification arrives with
force-reply, the injector is armed, and the conversation continues.

## 4. Configuration

### 4.1 `[listen]` in `~/.config/claude-tg-relay/config.toml`

The listener is a relay client, so its settings live with the other relay client
settings — the same file epic 15 extends with `[roles]`. No new config location.

```toml
server_url         = "https://relay.example.com"
installation_token = "rly_aaa..."      # unchanged

[listen]
enabled         = true
default_profile = "claude"             # profiles.toml name; no ambient env (§2.4)
default_model   = ""                   # "" = the profile's ANTHROPIC_MODEL governs
model_tiers     = ["fable", "opus", "sonnet", "haiku"]
max_live        = 8                    # Telegram-spawned live sessions, per machine
min_interval_s  = 10                   # floor between two spawns
```

The **machine name** used in `machine.workspace` is the relay installation
`label`, already served by `GET /v1/installations/me` (`app.py:297`). Nothing new
to name or keep in sync.

With epic 15 in flight: the listener uses the **default role's** installation
token, consistent with roles' §6 keeping permissions and notifications
default-only.

### 4.2 The seen-store — `~/.claude/telegram_workspaces.json`

A small map `abs_dir → {last_seen, count, name}`, upserted by the three paths
that actually emit Telegram messages: the permission send, the AskUserQuestion
send, and the idle notification. It is the workspace pick list, ranked by
recency, and it doubles as the **allowlist**: v1 accepts no arbitrary paths from
Telegram, only entries that are already in this store.

This is a deliberate reading of "workspaces that previously emitted Telegram
messages" — the evidence for that claim does not survive anywhere today (§2.5),
so this epic starts recording it. A workspace enters the list the first time it
asks you anything from that machine.

## 5. Behaviour

### 5.1 Target resolution

1. `machine.workspace` → that installation, no ambiguity.
2. Bare `workspace`, one installation bound → that machine.
3. Bare `workspace`, several bound → the relay **broadcasts a `resolve`** to
   every bound installation and collects claims within a short deadline (a
   machine that is asleep simply does not answer). Exactly one claimant → route
   to it. Several → machine picker naming only the claimants. None → "no machine
   has `foo`" naming which machines answered and which were offline.

Broadcast keeps workspace names off the server and makes the one-message fast
path stay one message in the common case. The picker is relay-rendered on a
`c:` callback namespace (§2.7); every other keyboard in this epic belongs to the
listener and rides the existing message API.

### 5.2 The session is plain

Plain, seeded, detached, name auto-derived by the existing prefix algorithm
(`workspace_prefix` → `claude-hooks`, `claude-hooks-2`, …). Plain because a
session you will later attach to and restart is a human session (epic 10
D-Tracked), and because a minted `--session-id` cannot be re-passed on restart.
This requires the new `--plain` flag (§2.3).

Consequence: plain sessions are outside the fork-bomb cap, so the listener
enforces its own (`max_live`, `min_interval_s`).

### 5.3 Profile and model are separate axes

A profile is the **backend** — base URL, auth, `ANTHROPIC_MODEL` — and it maps
tiers through `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`
(`shell/profiles.example.toml`). The tier is a `--model` flag forwarded to
Claude Code, and amux already passes `--no-default-model` (epic 12 E2) so an
unspecified tier leaves the profile's `ANTHROPIC_MODEL` in charge.

Both are `+`-prefixed and resolved by the listener: **profile names first, then
model tiers**; a token matching both is an explicit error. One sigil to remember,
and `@` stays free for epic 15's role aliases.

**`+tokens` are the way to choose**, and the wizard offers rows only on a bare
`/new` — the "I don't remember the syntax" path: a model row `[default] [fable]
[opus] [sonnet] [haiku]` from `model_tiers`, and a profile row only when more
than one profile is configured. `/new <ws> <prompt>` never asks; it uses the
configured defaults, because a phone user should not tap through a question they
already answered in config.

Tiers are passed through verbatim — the listener does not validate a tier against
the profile's env, because a profile that does not map a tier still resolves
*something*, and guessing here would be worse than passing through.

### 5.4 Delivery is live-only

A spawn command is delivered to a listener that is **currently connected**;
commands expire after ~120 s and are never stored and forwarded. If the machine
is asleep the bot says so immediately.

The alternative — queue it until the laptop wakes — means opening the lid at 9am
to a session that started itself from a message sent at midnight. Surprise
sessions are worse than an honest "workstation is offline".

### 5.5 Failure behaviour

| Situation | Behaviour |
|---|---|
| Machine offline / no listener | `⚠️ workstation is offline — nothing was started.` The command is **not** queued, so it cannot fire later |
| Machine online but never claims | Expires at the delivery TTL: `⚠️ workstation did not pick this up — nothing was started.` |
| Unknown workspace | List the machine's known workspaces as buttons |
| Unknown `+token` | Name it, list valid profiles and tiers, do not spawn |
| Both a profile and a tier match a `+token` | Explicit error; ask for disambiguation |
| Spawn fails (amux error, dir gone) | Report the failure text; nothing half-created |
| `max_live` / `min_interval_s` hit | Refuse with the current count, do not queue |
| Wizard step unanswered | Expires with the message TTL, no session created |
| Listener dies mid-wizard | Steps expire; systemd restarts it; no partial spawn |

### 5.6 Security

This is a remote-code-execution channel by construction, so it is bounded on
purpose:

- Bound user only, reusing the existing sender check (`app.py:1329-1333`).
- Workspaces are limited to the seen-store; **no arbitrary paths** from Telegram.
- **No `--yolo` from Telegram**, ever. A spawned session runs under the normal
  hooks, so its gated commands come back through the permission flow.
- Commands carry a target and prompt text — never a shell command.
- Rate and concurrency caps per machine (§4.1).

The residual risk is unchanged from what the product already accepts: a
compromised Telegram account can approve permissions. This epic lets it also
*start* work, which is why the workspace allowlist is not optional.

## 6. Components

| # | Task | Changes |
|---|---|---|
| 16-01 | [Relay command queue](./16-01-relay-command-queue.md) | `commands` table, `GET /v1/commands?wait=N`, `POST /v1/commands/{id}/result`, waiters, reaper, client methods |
| 16-02 | [Relay command surface](./16-02-relay-command-surface.md) | `/new` + `/ls` parsing, target resolution and fan-out, `c:` callback namespace, every chat-visible string |
| 16-03 | [Launcher `--plain`](./16-03-launcher-plain-flag.md) | explicit tracked/plain override (§2.3), `--json` output, completion |
| 16-04 | [Seen-store](./16-04-seen-store.md) | `telegram_workspaces.py`, three writers, allowlist semantics |
| 16-05 | [Listener runtime](./16-05-listener-runtime.md) | `amux-spawn listen`: loop, lock, dispatch, `resolve`/`ls` responders, caps ledger, `--status` |
| 16-06 | [Spawn and wizard](./16-06-spawn-and-wizard.md) | modifier resolution, workspace resolution, wizard steps, preflight, subprocess spawn |
| 16-07 | [Installer, diagnostics, docs](./16-07-installer-diagnostics-docs.md) | systemd unit, installer step, `docs/telegram-spawn.md`, top-level `architecture.md` |
| 16-08 | [Live verification](./16-08-live-verification_human.md) | **human** — two bound machines, a real relay and a laptop that actually sleeps |

## 7. Success criteria

- [ ] From a phone, with **no session running** on the target machine, `/new
      claude-hooks fix the flaky login test` produces a live plain amux session
      seeded with that prompt, and its first idle notification arrives with a
      working reply injector.
- [ ] `/new claude-hooks` force-replies for the prompt and accepts a long
      multi-line one.
- [ ] `/new` bare walks machine → workspace → prompt, with cancel at each step.
- [ ] With two machines bound to one chat, a bare workspace name that exists on
      both produces a picker naming both; `machine.workspace` never does.
- [ ] `+glm5 +opus` reaches the session as the right backend and tier; an unknown
      token refuses instead of spawning.
- [ ] A `/new` to an offline machine reports it and creates nothing, then works
      after the listener starts — without the old command firing.
- [ ] `/ls` reports both machines' live sessions grouped, with offline machines
      listed as such.
- [ ] Restarting the listener mid-wizard loses only the wizard; no orphan session.
- [ ] No regression: existing permission, question, idle and injection flows are
      byte-identical with the listener stopped.

## 8. Out of scope

- **Prompt recall** — templates, history, editing before send: [epic 17](../17_prompt_recall/brd.md).
- **Mini App** — one-screen composer with search and pickers: [epic 18](../18_telegram_miniapp/brd.md).
- **`/kill`, `/attach`, follow-up injection into an arbitrary session.** v1 starts
  and lists; killing a session remotely waits until the channel has proven itself.
- **Store-and-forward delivery** (§5.4).
- **Tracked sessions from Telegram.** No handles, no `status`, no `--wait`;
  those are the agent-orchestration path (epic 10), not the phone path.
- **Arbitrary directories, new workspaces, git clone/worktree creation.** A
  workspace must already exist and have talked to Telegram once.
- **Non-amux hosts.** Same limitation as task 09: injection and spawn both
  require amux.
- **Cross-machine session migration** and any streaming of live session output.
