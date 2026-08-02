# 15-05 — Escalation to the default destination

**Status:** todo · **Depends on:** 15-04
**Read first:** [brd.md](./brd.md) §5.4 (escalation), §5.6 (attribution), §2.2 (why call-level)

## Goal

A question sent to the designer must not strand the session if the designer is
asleep. When `escalate_after` elapses, send the same call to the default
destination as well, keep both live, and let the first *complete* answer win.

Waiting stays unbounded by default (brd §5.4): with no `escalate_after` this
task changes observable behaviour not at all.

## Scope

15-04 built the loop this task plugs into: one daemon thread per open child
message, results on a `queue.Queue`, a main thread that owns terminal detection
and every state-store write, and a group counted as **won** once all of its
children have answers. Read that task's Part 1 before starting; this one adds
exactly three things to it.

### 1. The escalation deadline

A new step in the main loop's tick, between terminal detection and draining the
queue: `escalate_at` reached and not yet escalated → send the escalation group
and start its threads. `escalate_at = start + destination.escalate_after`.

Drive the tick from `queue.Queue.get(timeout≈2)`, never `time.sleep` — with a
sleep the deadline drifts by up to a full long-poll chunk.

Terminal detection already covers "any child of any group", so it needs no
change here beyond the escalation children being in the set it scans.

### 2. Sending the escalation group

- The destination is `resolve_destination(catalog, bindings, alias=None)` — the
  default *role*, which is not always the top-level `installation_token`: the
  default role can carry its own explicit binding (brd §3.3). Use its `.token`
  and `.title`, not the router's implicit default client.
- New `group_id`, `group_total = len(questions)`, one **new state-store row per
  question** with `role = <default role id>`.
  Distinct `request_id`s give distinct idempotency keys
  (`req:{request_id}:send`) and keep `set_telegram_message_id` writing to the
  right row, so the router needs no new parameters.
- `banner` (15-02) on every message, not just the first — whichever one the
  operator opens should explain itself:
  `@ux (UX/UI designer) hasn't answered in 15m — you can decide instead.`
  The alias is the one the agent actually wrote (`@design` if that is what it
  used), paired with the role's title. The duration must be **humanized**:
  `parse_duration` returns seconds, so 900.0 has to render as `15m`, not
  `900s` and certainly not `900.0`.
- `role_title` is the **default** role's title: this copy really is addressed to
  the operator.
- Same cleaned headers, same options, same `multi_select` flags as the original.

### 3. Deciding between two groups

- **Role group wins** → answers returned verbatim.
- **Escalation group wins** → append ` (answered by Operator)` — the default
  role's title — to each answer string (brd §5.6).
- Loser, per child: `finalize_message(msg_id, body, winning_answer_for_that_question,
  prefix="✅ ", token=<that group's token>)`. Both groups carry the same questions
  in the same order, so pair them by index. Patch the **unannotated** answer —
  the ` (answered by Operator)` suffix is for the agent, not for the chat, where
  it would be both redundant and confusing.
  The winning group finalizes itself server-side (`app.py:1203`); only the loser
  needs client-side cleanup.
- Mark every state-store row of both groups terminal — winners `REPLY` with
  `resolution_source=telegram`, losers `REPLY` too, so `posttool_hook`'s
  pending-request sweep skips them all (the reason `_mark_relay_resolved` exists,
  `permission_request_hook.py:330`).

### Escalation does not fire when

- `destination.escalate_after is None` — including every case where 15-01
  already suppressed it: no policy configured, the role *is* the default, the
  role's binding resolves to the default token anyway (same human), or the send
  fell back to the default because the role was unreachable (brd §5.4).
- The call is already answered or terminal-resolved.
- `escalate_after` exceeds the remaining `REQUEST_TTL` window.

## Timeline

```
t0      -> designer's chat:  group A (2 msgs), live
t0+15m  -> operator's chat:  group B (2 msgs), live, ⏳ banner
t0+16m     operator answers both -> group B finalizes server-side
           group A: each msg patched "✅ <answer>", keyboard stripped
           returned answers: "Sidebar (answered by Operator)", ...
```

If the designer had finished first, group A wins, group B is the one patched,
and the answers are returned unannotated.

## Implementation notes

- One thread per child, capped by the number of questions — `AskUserQuestion`
  tops out at 4, so at most 8 threads once both groups are live. No pool needed.
- 15-04 treats "every child of the only group is non-answerable" as the call
  dying. With two groups that becomes **per group**: one group expiring is that
  group losing, not the call failing — the other may still win. Only when *both*
  are non-answerable does the call `return None` and leave the native UI
  standing. Widening that check is the one edit this task makes to 15-04's
  termination logic; make it deliberately, not incidentally.
- The escalation copy is sent from the main thread, mid-loop. Its send can fail
  like any other (brd §5.1): log it, leave the role group live, and do not
  retry — the role group is still the real destination and the operator can
  answer at the keyboard.

## Testing

Extend `tests/test_integration_permission_request.py`, reusing the scripted
per-message-id fake relay from 15-04. Use a sub-second `escalate_after` (or a
monkeypatched clock) so tests do not actually wait minutes, and keep the hard
per-test timeout 15-04 introduced.

## Done criteria

- [ ] No `escalate_after` → one group, no second send, behaviour identical to
      15-04.
- [ ] `escalate_after` elapsing sends exactly `len(questions)` messages to the
      default token, with a new `group_id`, correct `group_total`, and the ⏳
      banner on every one.
- [ ] Role group answering first → verbatim answers; escalation messages patched
      and cancelled.
- [ ] Escalation group answering first → answers suffixed
      ` (answered by Operator)`; role messages patched and cancelled.
- [ ] A partially answered losing group is still fully patched and cancelled.
- [ ] Terminal resolution at any point cancels **both** groups via 15-04.
- [ ] No escalation when the role fell back to the default, when the role is the
      default, or when `escalate_after` exceeds the remaining TTL.
- [ ] Every state-store row from both groups is terminal when the hook exits.
- [ ] One group expiring does **not** end the call; both expiring → `return None`,
      native UI intact.
- [ ] A failed escalation send leaves the role group live and the call waiting.
- [ ] The whole suite still passes with no `roles.toml` present.
