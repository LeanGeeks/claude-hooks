# 16-02 — Relay command surface (`/new`, `/ls`)

**Status:** todo · **Depends on:** 16-01
**Read first:** [brd.md](./brd.md) §3, §5.1, §5.4–5.6 · [architecture.md](./architecture.md) §2.4–2.6

## Goal

The Telegram-facing half: parse `/new` and `/ls`, decide which machine a command
is for, render the one keyboard the relay owns, and turn a listener's result into
a chat message. Everything the user reads in the chat that is *not* a wizard step
is written here.

## Scope

### Command entry — `_handle_update`

Two branches, placed **before** the existing `if text.startswith("/")` early
return (`app.py:1316`) which today swallows every non-`/bind` command:

```
/bind …   → unchanged (app.py:1288)
/new …    → this task
/ls …     → this task
other /…  → unchanged: ignored
```

The bound-user check that currently guards only loose replies
(`app.py:1329-1333`) is hoisted into a helper and applied to commands too: a
command from anyone but the chat's `bound_user_id` is dropped silently, exactly
as a loose reply from a stranger is.

### `/new` grammar

`/new [target] [+token …] [prompt …]`, scanned left to right from the start:

- a `+`-prefixed token is a **modifier** (the `+` is stripped, nothing else);
- the first token that is neither `+`-prefixed nor already taken as the target is
  the **target**;
- the first token that is neither of those **starts the prompt**, and everything
  from there on is prompt text, verbatim.

So `/new claude-hooks +opus fix the bug` and `/new +opus claude-hooks fix the bug`
are the same command, while `/new claude-hooks fix the +opus bug` puts `+opus`
in the prompt, where the user clearly meant it. One rule, no lookahead, no
guessing from prose.

Modifiers are collected in order into `payload.modifiers` and passed through
**unparsed** (brd §5.3 — profile-vs-tier is the listener's call; the relay has no
profile list). `payload` also carries `workspace` and `prompt`, and any of the
three may be absent — that is what puts the wizard into play.

| Input | Parsed |
|---|---|
| `/new` | wizard from the top |
| `/new claude-hooks` | target only; listener asks for the prompt |
| `/new claude-hooks fix the flaky login test` | target + prompt |
| `/new workstation.claude-hooks fix …` | machine + workspace + prompt |
| `/new claude-hooks +glm5 +opus fix …` | target + two modifiers + prompt |
| `/new .` / `/new . fix …` | last target used in this chat |
| `/new workstation. fix …` | machine fixed, workspace chosen in the wizard |

A target containing `.` splits on the **first** dot; the prefix is matched
case-insensitively against the `label` of the installations bound to this chat.
No label match → the whole token is a workspace name (a directory named
`foo.bar` still works). A trailing dot means "this machine, wizard for the rest".

`.` as the whole target reuses the most recent `done` spawn command for this chat
— its `installation_id` and its `workspace`. No new table: query the `commands`
table. Nothing found → the wizard, with a note saying there was no previous
target.

### Target resolution

```
machine.workspace          → route directly
bare workspace, 1 bound    → route directly
bare workspace, N bound    → fan-out resolve (below)
no target                  → machine picker if N>1, else route directly
```

**Fan-out `resolve`.** Insert one `resolve` command per bound installation
(payload `{workspace}`), 10 s delivery TTL, and await their result waiters with a
~5 s deadline. Listeners answer `{claim: bool, ambiguous: bool}` (16-05); the
listener resolves its own same-basename collisions later, so the relay never sees
a path.

| Claimants | Behaviour |
|---|---|
| exactly 1 | insert the `spawn` command targeted at it |
| >1 | machine picker listing only the claimants |
| 0, some machines answered | `⚠️ No machine has a workspace called <ws>.` + which answered, which were offline |
| 0, none answered | the offline notice below |

### Machine picker — the only relay-owned keyboard

Buttons carry `c:{command_id}:o:{idx}` (architecture §2.6). `callback_data.decode`
is strict on `m:…:o:…` and returns `None` for anything else (`callback_data.py:41`),
so `_handle_callback_query` branches on the `c:` prefix **before** the existing
path and the message flow is untouched. Add `encode_command` / `decode_command`
next to the existing helpers rather than overloading `CallbackData`.

On tap: set `installation_id`, flip `targeting` → `pending`, notify that
installation's waiter, edit the picker message to show the choice and drop its
keyboard. A tap on a command that has since expired answers the callback with a
short toast and edits the message to say so.

### Liveness and the offline notice

An installation is **live** when `last_seen_at` is within 90 s — a listener
long-polling every 25 s keeps it fresh through the existing `_touch`
(`app.py:180`), so the window tolerates three missed polls.

**A `spawn` for a machine that is not live is refused, not inserted.** Inserting
it would give one action two messages ("offline" now, "expired" in two minutes),
or worse, start a session moments after the user was told nothing would happen.
One command, one outcome, one message:

| Machine | Behaviour |
|---|---|
| not live | refuse immediately, insert nothing |
| live, claims it | normal path; the ack is the only message |
| live, never claims | expires at the delivery TTL, "did not pick this up" |

`resolve` and `ls` are broadcast to every bound installation regardless of
liveness — they are harmless reads, and a non-answer is exactly the signal the
fan-out wants.

Refusal must be unmistakable and must say that nothing happened (brd §5.4):

```
⚠️ workstation is offline — nothing was started.
Last seen 14 minutes ago. Start the listener there and send this again.
```

With several machines bound and none live:
`⚠️ No machine is online (thinkpad, workstation) — nothing was started.`

When a targeted spawn expires unclaimed despite the machine looking live:
`⚠️ workstation did not pick this up within 2 min — nothing was started.`

### Results → chat

`spawn` result, `ok`:

```
▶ claude-hooks-2 · workstation
/data/sync/work/leangeeks-ai/claude-hooks · claude +opus
```

`spawn` result, not `ok`: `⚠️ ` + `summary`, with `detail` in a `<pre>` block when
present. The listener supplies both strings (16-06); this task owns the shape,
the HTML escaping and the emoji.

`resolve` and `ls` results are **not** posted to the chat — they are consumed by
the fan-out. `/ls` renders its own message from the collected `data`:

```
workstation
  claude-hooks-2   claude-hooks    idle 4m
  hyppie-flow      hyppie-flow     active
thinkpad — offline
```

Ordering is by machine label, then session name. A machine that answers with no
sessions renders `— no sessions`. `/ls <machine>` scopes to one and still says
"offline" rather than showing nothing.

## Implementation notes

- All chat text goes through the existing HTML escaping helper (`_esc`,
  `app.py:1019`); a workspace name or a failure detail is user-controlled text.
- The fan-out awaits result waiters concurrently (`asyncio.gather` with a single
  deadline), never sequentially — five bound machines must not cost 5× the
  deadline.
- Do not block the webhook handler on a fan-out longer than its deadline;
  Telegram retries slow webhooks and a retry must not double-insert. Guard by
  keying an in-flight marker on the Telegram `update_id`, matching the
  idempotency instinct already in `POST /v1/messages`.
- Command wording lives in one module-level table so 16-08 can assert on exact
  strings and a future locale change touches one place.

## Testing

`relay-server/tests/`, fake backend, no network:

- Parsing table above, row by row, including `+tokens` passed through unparsed
  and a prompt that itself contains a `.` or a `+`.
- `machine.workspace` matches labels case-insensitively; an unmatched prefix is
  treated as a workspace name.
- Resolution: 1 bound / N bound with 1 claimant / N bound with 2 claimants /
  0 claimants with some answers / 0 answers.
- Picker: `c:` callbacks route to the command path and `m:` callbacks still route
  to the message path; a tap on an expired command edits rather than crashes.
- A spawn aimed at a stale-`last_seen_at` machine posts the offline notice
  (assert the exact string) and inserts **no** command row — then, when that
  machine's listener starts polling, it receives nothing.
- Unclaimed targeted spawn at a live machine → expiry notice posted exactly once,
  and never together with the offline notice.
- `/ls` renders grouped output including an offline machine; `/ls <machine>`
  scopes.
- A command from a non-bound user is ignored; a `/new` in a chat with no
  installation is ignored.
- Regression: a loose text reply still reaches `_apply_text_answer`, and `/bind`
  still binds.

## Done criteria

- [ ] `/new` and `/ls` are handled before the slash-command early return, and
      every other `/command` is still ignored.
- [ ] Bound-user check guards commands as it guards replies.
- [ ] The parsing table is reproduced exactly, modifiers passed through unparsed.
- [ ] Target resolution covers all five branches, fan-out is concurrent and
      bounded by one deadline.
- [ ] Machine picker works on the `c:` namespace with no effect on `m:` callbacks.
- [ ] Every failure path says, in words, that nothing was started.
- [ ] Spawn ack, spawn failure, `/ls` table and offline notices render escaped.
- [ ] No new knowledge of directories, profiles or prompts on the server.
