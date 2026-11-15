# Task 49 — Withdrawing a question that has no buttons

**Status:** done (relay redeploy pending) · **Type:** fix · **Created:** 2026-11-15 · **Rev:** 1

## Problem

Reported from hyppie-flow on the first live use (`[questions] sync = "push"`):
`ask` without `options`, then `withdraw`. The entry was marked `[withdrawn]`,
committed and pushed, but the result was `relay_cancelled: false`, "relay HTTP
502: telegram_error", on every retry. The relay log, each attempt:

```
patch_message: telegram rejected edit for 8826: Bad Request: message can't be edited
"PATCH /v1/messages/8826 HTTP/1.1" 502 Bad Gateway
```

## Cause

Three things, each harmless alone:

1. A question with no buttons that wants a typed answer is sent with Telegram's
   `force_reply` prompt (`HttpTelegramBackend.send_message`). Telegram never
   lets a bot edit a message sent with a reply markup that is not an inline
   keyboard: text and markup edits both answer "message can't be edited".
2. `questions_ops._relay_withdraw` sent the text as a `PATCH` first and the
   cancel second. The refused `PATCH` raised, so **the cancel was never sent**:
   the message stayed open on the relay with its reminders and its escalation
   armed. `relay_cancelled: false` was therefore true, and the error text
   ("buttons are still live") was not the point.
3. Had the cancel been sent, the relay would have closed the row and then
   answered 502 anyway, because the keyboard strip that ends every cancel is
   also an edit.

Every test of the relay passed because `FakeTelegramBackend` accepted every
edit. A question with options has an inline keyboard and was never affected.

## Fix

**Relay** (`relay-server/relay_server/`, needs a redeploy):

- `is_not_editable(exc)` beside `is_not_modified`.
- `POST /v1/messages/{id}/cancel`: a refused keyboard strip with "message can't
  be edited" is a success (there is no inline keyboard on such a message). When
  the text edit is refused that way and the call carries a `final_text`, the
  text is posted as a reply under the original (best-effort, like the edit).
- With a `final_text` the reply body is `{"final_text_shown": …}`; without one
  it is `{}` as before. `RelayClient.cancel_message` returns that body.

  | Value | Meaning | The client then |
  |---|---|---|
  | `edit` | the message reads the text | nothing |
  | `reply` | uneditable; text posted under it | `note` |
  | `refused` | uneditable; the reply could not be sent | `note` |
  | `failed` | the edit failed for a passing reason | sends the text as a `PATCH` |
  | `already_closed` | this call did not close the row; its text (perhaps an answer) is left alone | `note` |
- `PATCH` is unchanged: a refused edit is still a 502, and the payload still
  never claims a text the chat did not show.
- `FakeTelegramBackend` refuses edits of a message it sent with the prompt, the
  way Telegram does.

**Client** (`.claude/hooks/questions_ops.py`, takes effect on install + session
restart, with any relay):

- The cancel goes first and carries the text. It is the act that matters.
- A current relay says how the text was shown; nothing more is sent. One call
  where there were two.
- A relay that says nothing (older), or says `failed`, gets the text as a
  `PATCH` afterwards; a failure there is a `note`, not a failed cancel.
- A 502 from the cancel is a failure only if the relay still has the message
  open (asked through the answer endpoint). The relay deployed before this fix
  closes the row and then answers 502; that is a cancel.
- `relay_cancelled` means what callers need it to mean: the message is closed
  on the relay, so no reminder and no escalation will fire. How the chat shows
  it is in `note`.

## Outcomes

| Relay | Question | Result |
|---|---|---|
| current | with buttons | message edited to the withdrawn text; `relay_cancelled: true` |
| current | no buttons | closed; withdrawn text as a reply under it; `note` |
| deployed before this fix | no buttons | closed; chat keeps the original text; `note` |
| before task 46-01 | with buttons | cancelled, then edited |

## Tests

- `relay-server/tests/test_cancel_cascade.py`, section "a question without
  buttons": the PATCH is refused and leaves the row open; cancel closes it,
  clears the reminder, posts the reply once, reaches the escalated copy; a
  plain cancel is quiet; a cancel of an answered one is a 200; a failed reply
  does not fail the cancel.
- `relay-server/tests/test_relay_client.py`: `cancel_message` returns the body.
- `tests/test_unit_questions_ops.py`: the four relay shapes above, and a 502
  that did leave the message open.
- Both groups fail with their half of the fix reverted.

## Review

`agents_output/49_review_report.md` (sonnet, independent): PASS, 2 MEDIUM,
4 LOW. No path was found that reports `relay_cancelled: true` for an open row.

- MEDIUM, fixed: with the cancel first, a text edit that failed for a passing
  reason inside the relay was reported like a refusal and the text was given up
  on. The relay now says `failed` and the client sends the text again.
- MEDIUM, fixed: a cancel that found the row already closed was reported like a
  refusal ("the chat still shows the original text"). It now says
  `already_closed`, and the note says that.
- LOW, fixed: the note for a 502 from a relay that did close the row no longer
  asserts the cause; `relay_cancelled` is documented as "closed on the relay",
  not "buttons gone".
- LOW, accepted: the closed-or-not question can take tens of seconds against a
  relay that hangs (the client's own retries); the answer is still "not
  closed", which is the safe one.
- LOW, accepted: the fake backend records an edit before refusing it, so a test
  that only counts calls does not prove the chat changed.
- LOW, fixed: tests for the closed states, the passing failure and the 502.

Live, against the relay as deployed before this fix (throwaway workspace,
operator's chat, relay #8829): `ask` without options, `withdraw` →
`relay_cancelled: true` with the 502 note; the relay row `cancelled`, no
reminder, no escalation; the relay log shows both refusals ("text edit" and
"keyboard strip": "message can't be edited"). The reply under the original
needs the redeployed relay and was not seen live.

## Not changed

- The listener's finalize (`✅ <answer>`) of an answered question without
  buttons is the same refused edit. It is best-effort and logged; the human's
  own reply sits under the question, so nothing is missing from the chat.
- The `#unanswered` tag is likewise never put on such a message.
