# 46-07 — Live verification (human)

**Status:** todo · **Depends on:** 46-06, and 46-01 deployed · **Owner:** human

What a fake cannot show. Run against the real relay and a workspace with
`[questions] sync = "push"`.

1. **Escalated copy.** Ask a role that escalates, wait for the copy to arrive in
   the operator's chat, withdraw. Both messages read "Withdrawn", neither has a
   keyboard, neither is nudged again. (Needs the relay redeployed with 46-01.)
2. **Late answer.** Stop the listener, answer the question, withdraw it, start
   the listener. The entry stays withdrawn and carries the late answer; the
   message ends `✅ … late answer`. (A tap made *after* the relay has cancelled
   the message is refused by the relay and never recorded — that is not this
   case, and is worth seeing once too.)
3. **Two machines.** Ask from two machines at the same moment; two ids, two
   commits upstream, neither machine published anything else.
4. **Ahead.** With the primary checkout tens of commits ahead of its upstream,
   ask; `git log origin/main` gains queue commits only. Then `git pull --rebase`
   and push as usual; record whether it stopped and on what.
5. **The listener at 03:00.** Answer a question with no session running; the
   answer is committed and pushed by the listener.
6. **Degraded.** Ask during a rebase and with the network down; the question
   arrives in Telegram both times, `sync.reason` names the state, and the debt
   is paid within five minutes of the cause going away.

Record in a sibling `46-07-signoff-evidence.md`.
