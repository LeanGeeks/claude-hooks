# 23-02 — Relay: per-message nudges + escalation

**Status:** todo · **Depends on:** — (independent root; shares files with 23-01,
so agree the migration number first)
**Read first:** [brd.md](./brd.md) §2.3, §2.4, D6 · [architecture.md](./architecture.md)
§2.2, §2.3 · [state.md](./state.md) invariants 4, 10 · `tasks/19_unanswered_reminders/brd.md`
§2, §4 · `relay-server/relay_server/availability.py`, `reaper.py` (nudge pass),
`render.py`

## Goal

Move reminder policy from the waiting process onto the message, so an async
question keeps nudging and escalates exactly once even though nobody is waiting
and the sending machine may be off.

## Scope

### 1. Repeating-tail ladders

`availability.py` parses comma-separated active-time intervals. Add: a trailing
`*` on the **last** rung means repeat that interval indefinitely, so
`4h,1d,3d,7d*` nudges at 4 h, 1 d, 3 d, then every 7 d forever. Without it, a
never-expiring question goes permanently silent — the exact failure the epic
exists to prevent (brd §2.4).

Keep the arithmetic in active time: windows and timezones must apply to the
repeating tail exactly as they do to a finite ladder. A `*` anywhere but the last
rung is a config error, reported not ignored.

### 2. Three nullable columns on `messages`

`nudge_schedule_override`, `escalate_at`, `escalate_to_token_hash`
(architecture §2.2). All NULL for every existing row and every existing sender —
absent means "behave exactly as today", which is invariant 9.

The nudge pass prefers `nudge_schedule_override` over the chat's
`recipients.nudge_schedule`. Note the interaction with `nudge_enabled`: a chat
with nudges **off** must still nudge a message that carries its own ladder, or an
async question in an unconfigured chat is silent. Make that precedence explicit
in code and in a test — it is the one place where the message legitimately
overrides the human's chat-level preference, and it is why the ladder is opt-in
per message rather than global.

### 3. The escalation pass

A fourth reaper pass beside the nudge pass: `state='open' AND escalate_at IS NOT
NULL AND escalate_at < now`. Send a duplicate of the row's **rendered body** (via
`render_body`, never a re-render from a stale payload) to the installation
identified by `escalate_to_token_hash`, record the duplicate's telegram message
id, and clear `escalate_at` so it fires once.

Both copies stay live; the first answered wins and the other is patched and
cancelled — mirror what `_finalize_losing_groups` does on the blocking path, but
server-side. Answering the escalated copy must record the answer against the
**original** message id, so the feed (23-01) and the client index stay keyed on
one id.

The relay resolves a token to an installation. It must not learn what a role is
(invariant 4).

**The escalation target may be a different machine.** Escalating to the operator
often means the same installation, but nothing guarantees it. Two consequences to
implement explicitly: the answer must be recorded against the **original**
message id whichever copy is tapped, so exactly one installation's feed (23-01)
carries it and the asking machine stays the one that writes the file; and the
duplicate row must not surface as its own answered row in the *other*
installation's feed. That listener would look up an unknown message id and skip
harmlessly (23-05 step 1), but relying on that as the mechanism is not the same
as choosing it — choose it, and test it with two installations.

### 4. Send-time acceptance

`POST /v1/messages` accepts `nudge_schedule`, `escalate_after_sec` and
`escalate_to_token`; the server hashes the token and computes `escalate_at` from
`now` in active time. Reject an escalation target that is not a bound
installation, at send time, with a clear error — a silently dropped escalation is
worse than a refused send.

## Done when

- A message with `4h,1d,3d,7d*` still nudges in week three, on windows.
- A message with its own ladder nudges in a chat with `nudge_enabled = 0`.
- Escalation fires once, to the right installation, and never re-fires.
- Answering either copy resolves both, attributed to the original id.
- Every existing message behaves byte-identically (all three columns NULL).

## Tests

Ladder parsing incl. the `*` position error; active-time arithmetic across a
window boundary for a repeating rung; override-vs-chat-config precedence
including `nudge_enabled = 0`; escalation fires-once, unknown-token refusal,
answer attribution from the escalated copy; a full reaper tick with all four
passes and rows in each state. Regression floor per invariant 9.
