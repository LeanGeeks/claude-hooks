# 15-03 — Alias routing in the send path

**Status:** todo · **Depends on:** 15-02
**Read first:** [brd.md](./brd.md) §4 (agent UX), §5.1–5.3 (routing, rejection)

## Goal

Wire 15-01's resolver into `handle_ask_user_question()`
(`permission_request_hook.py:450`) so a question tagged `@ux` reaches the
designer's chat, everything else reaches the default destination as before, and
a call addressing two roles is rejected rather than half-delivered.

## Scope

### Resolution, once per call

At the top of `handle_ask_user_question`, before any request is created:

```python
catalog  = roles_config.load_catalog(cwd)          # None => legacy mode
bindings = roles_config.load_bindings()
```

Then per question: `parse_header_alias(q["header"])` → `resolve_destination` →
collect.

`catalog is None` is the compatibility floor (brd §1) and is carried as
`destination = None` through the **same** code path — do not fork the function.
A duplicated legacy copy would have to be patched again by 15-04 and 15-05 and
would drift from the real one within an epic.

Two `None`s that must not be confused:

| | meaning | behaviour |
|---|---|---|
| `destination is None` | legacy mode, no roles configured | `token=None` → the router's default client (15-02), `role=None`, no `for:` line, no notes |
| `destination.token is None` | roles configured but nothing is reachable | log and `return None`; native terminal UI only |

### Mixed-role rejection

Skipped entirely in legacy mode (no catalog, no resolution, nothing to mix).
Otherwise, distinct `Destination.role_id` across the call's questions must be
exactly one. Two or more → return a deny decision instead of sending anything:

```python
{"action": "deny_mixed_roles", "reason": (
    "This AskUserQuestion call addressed 2 different human roles "
    "(@ux -> UX/UI designer, @arch -> Tech lead / architect). "
    "Each call must target exactly one role, because a question group cannot "
    "span two Telegram chats. Split this into one AskUserQuestion call per role."
)}
```

`build_output_decision` (`permission_request_hook.py:155`) gains a
`deny_mixed_roles` branch emitting `behavior: deny` with that `reason`. Nothing
is sent to Telegram and no state-store rows are created — bail before the
send loop.

This compares **resolved roles**, not literal aliases: two unknown aliases both
falling back to the default are one role and pass (brd §5.3).

### Sending

For the single resolved destination (or for `destination is None` in legacy
mode — see the table above):

- `token = destination.token if destination else None`. A `destination` that
  exists but has no token means nothing is reachable: log to the error log and
  `return None` — native terminal UI only, exactly as an unreachable relay
  behaves today.
- `create_request(..., role=destination.role_id if destination else None)` so
  `posttool_hook` can find the right client later (15-02).
- Strip the alias: the child's stored `tool_input["header"]` is the *cleaned*
  header. The terminal keeps the raw `@ux Layout` chip — `updatedInput` is built
  from the original `tool_input` (`permission_request_hook.py:569`), so this
  never leaks back into what Claude Code sees.
- `send_question_message(..., token=token, role_title=..., notes=...)`.
  `role_title` is `destination.title` whenever there is a destination —
  including for the default role, since a workspace that has declared roles
  should say which one is being asked — and `None` in legacy mode.
- Keep each child's rendered body (`render_question_body`, 15-02) in the
  `children` list. 15-04 and 15-05 both need the exact text to PATCH.
  `children` becomes a list of records, not tuples — give it a small dataclass
  (`child`, `question`, `message_id`, `body`) so later tasks can add fields
  without rewriting every unpack site.
- Every later call on those messages passes the same token:
  `wait_for_relay_answer(..., token=token)` and, in the terminal-resolution
  branch (`permission_request_hook.py:532`), `remove_inline_buttons(..., token=token)`.
  Those two call sites are the **only** change this task makes to the wait loop.
  Leave its sequential structure alone — 15-04 replaces it wholesale, and
  threading it here would be work thrown away and reviewed twice.

### Send failure is a fallback too

15-01 resolves *static* unreachability — no binding on this machine. A binding
that exists but fails at send time is just as real: the designer blocks the bot,
the relay auto-unbinds them (`app.py:613`) and returns `not_bound`. brd §5.1
promises the same reroute, and today's code just gives up
(`permission_request_hook.py:494`).

When any `send_question_message` of the group returns `None`:

1. Cancel the siblings already sent in this attempt, through the same token, and
   mark their rows terminal so `posttool_hook`'s sweep leaves them alone.
2. Destination was already the default → `return None`. Terminal-only, as today.
3. Otherwise retry the whole group **once** against the default destination,
   adding the note
   `Intended for UX/UI designer — the relay could not deliver to them (not bound or send failed).`
   This is the one note 15-03 owns rather than 15-01: it describes a runtime
   failure the static resolver cannot see. Match 15-01's voice — same
   `Intended for <title> — <reason>.` shape.
4. Suppress escalation on the retry, for the same reason a static fallback
   suppresses it (brd §5.4): the default role is already the one being asked.
   `Destination` is frozen, so pass the escalation deadline to the wait phase as
   its own value rather than mutating the destination — on this path that value
   is `None`.

The retry needs **fresh `request_id`s and a fresh `group_id`**, not a re-send of
the originals. `_send_relay` keys idempotency on `req:{request_id}:send`
(`telegram_permission_router.py:345`) and the retry body differs by one note, so
reusing an id earns a 422 `idempotency_key_reused_with_different_body` instead of
a message.

### Rendering

`render_question_body` (15-02) inserts, directly after the
`<b>Question</b> — <workspace>` line:

```
<i>for: UX/UI designer</i>
⚠️ Intended for Product lead — not reachable from this machine.
```

The `for:` line appears only when `role_title` is given; one `⚠️ ` line per note,
HTML-escaped, in the order 15-01 produced them; then the existing blank line and
the rest of the message. With neither argument the output is byte-identical to
today (15-02 done criteria).

## Worked example

```
AskUserQuestion({ questions: [
  { header: "@ux Layout",  question: "Sidebar or top nav?",     options: [...] },
  { header: "@ux Density", question: "Compact or comfortable?", options: [...] },
]})
```

→ both resolve to `ux` → one group of 2 in the designer's chat, `group_total=2`,
each headed `for: UX/UI designer`, headers rendered as `Layout` / `Density`.
Terminal chips stay `@ux Layout` / `@ux Density`.

Swap the second to `@arch Storage` and nothing is sent: the call is denied with
the reason above.

## Implementation notes

- Import `roles_config` lazily inside `handle_ask_user_question`, matching how
  `permission_request_hook` already defers optional imports
  (`permission_request_hook.py:524`). A missing or broken `roles_config` must
  degrade to the legacy path, not raise.
- Resolve every question before sending anything. Sending question 1 and *then*
  discovering question 2 targets another role would leave a live orphan
  keyboard.
- `catalog.errors` and `bindings.errors` go to `error_log()` once per call, not
  into the Telegram message — `Destination.notes` is what the human sees.
- Do not touch the permission or idle-notification paths (brd §6).

## Testing

Extend `tests/test_integration_permission_request.py`.

Patch `roles_config.load_catalog` / `load_bindings` to return fixtures built in
the test; do not create real config files under `$HOME`.

## Done criteria

- [ ] No `roles.toml` → identical behaviour and identical Telegram payloads to
      before this epic (assert on the recorded relay calls, not just the return
      value).
- [ ] `@ux Layout` sends to the `ux` token with `for: UX/UI designer` and a
      rendered header of `Layout`.
- [ ] Untagged questions in a roles-configured workspace go to the default token
      and say so.
- [ ] Unknown alias → default token, `⚠️ Unknown role @uxx — routed to Operator.`
      in the body.
- [ ] Role with no binding → default token, `⚠️ Intended for …` in the body.
- [ ] Two distinct roles in one call → `behavior: deny` with the mixed-roles
      reason, **zero** relay sends and **zero** state-store rows.
- [ ] Two unknown aliases in one call → one role, sends normally.
- [ ] `destination.token is None` → returns `None`, nothing sent, error logged.
- [ ] A failed send to a role token cancels the partial group, retries once
      against the default token with fresh ids and the delivery-failure note,
      and suppresses escalation on the retry.
- [ ] A failed send to the *default* token returns `None` without a retry loop.
- [ ] The state-store row carries `role`; `updatedInput.answers` keys are the
      original question strings and the raw headers are untouched.
- [ ] Terminal resolution cancels every sibling through the role's token.
