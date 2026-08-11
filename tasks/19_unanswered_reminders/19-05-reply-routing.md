# 19-05 — Reply-to-nudge routing

**Status:** todo · **Depends on:** 19-04
**Read first:** [brd.md](./brd.md) §5.6 · `app.py` `_handle_update`

## Goal

People reply to the message that notified them. After 19-04 that is often the
nudge — and today a reply aimed at it is **silently swallowed**. This task makes
a nudge a valid reply target for the prompt it points at.

**Ship with 19-04.** A build where nudges exist and replying to one does nothing
is worse than no nudges at all: it trains the operator that Telegram replies are
unreliable, which is the exact trust this whole system runs on.

## The bug it prevents

`_handle_update` resolves `reply_to_message_id` against `messages` by
`telegram_message_id`; a nudge is not a row (19-04, "nudges are not `messages`
rows"), so the lookup misses. The handler then **returns without falling
through** (`app.py:1345`) — deliberately, because falling through to the recency
heuristic is what caused the historical mis-routing. Correct as written; it just
means the answer vanishes with no error and no hint.

## Scope

### Reply target resolution

Extend the lookup: a `reply_to` that matches no message row is checked against
`messages.nudge_tg_message_id` in the same chat. A hit resolves to that row and
takes the ordinary `_apply_text_answer` path with `via="nudge_reply"` — worth
distinguishing in the answer payload so the logs can show whether nudges are
actually being answered.

Order matters: message rows first, nudges second. A `telegram_message_id` and a
`nudge_tg_message_id` cannot collide in practice, but the precedence should be
explicit rather than incidental.

A nudge whose target has since resolved resolves to a terminal row: keep the
existing behaviour for that case (do not fall through to the heuristic), and
prefer a short "that one's already handled" over silence now that we know which
message was meant.

### Ambiguity counter

`_distinct_open_targets` decides whether a loose, non-threaded reply can be
attributed. It counts open message rows — so it is unaffected by nudges as long
as nudges stay non-rows. **Verify that and test it**, because the failure is
nasty: if a nudge ever became countable, a single pending prompt plus its own
nudge would read as two targets and the relay would start refusing plain replies
with "multiple sessions are waiting" when only one is.

### Free-text answer shape

A nudge-reply must produce the same answer as a reply to the original — same
decision mapping, same `finalize_message` bake, same waiter wake. No new client
behaviour, nothing for the hooks to learn (state.md invariant 8).

**Verified during planning:** `relay_answer_to_decision`
(`router.py:944`) branches on `via` for `button_multi` and `button` only, then
**falls through to free-text for any other value** (`router.py:980`). So a new
`via="nudge_reply"` needs no hook change and cannot break the mapper. This was
the one place a server-side `via` addition could have silently broken an
installed client; it doesn't. Keep the free-text fallthrough test in
`tests/test_unit_decision_mapper.py` green as the guard on that.

## Testing

`relay-server/tests/test_webhook.py`:

- Reply to a nudge → the target message is answered, waiter woken, `via`
  recorded as the nudge path.
- Reply to a nudge whose target is already answered → no state change, a clear
  reply, no fall-through to the recency heuristic.
- One open prompt **plus its nudge** → a loose reply is still unambiguous and is
  attributed. (The regression guard.)
- Two open prompts each with a nudge → loose reply still refused as ambiguous,
  message unchanged.
- Reply to an unrelated bot message → unchanged behaviour, still ignored.
- Button tap on the original while a nudge exists → answers, and 19-04's cleanup
  deletes the nudge.

## Done criteria

- [ ] Replying to a nudge answers its prompt, with the same result as replying
      to the original.
- [ ] The ambiguity counter is provably unaffected by nudges.
- [ ] No answer path that worked before behaves differently.
- [ ] Landed together with 19-04, not after it in a separate release.
