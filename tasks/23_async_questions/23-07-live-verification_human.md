# 23-07 — Live verification (human)

**Status:** todo · **Depends on:** 23-06 · **Owner:** human
**Read first:** [brd.md](./brd.md) §7 · [state.md](./state.md) invariants

## Why this is human-only

Every remaining risk is one a fake cannot reproduce: a real human answering days
later, a laptop that actually suspends, a real 300 KB queue file with real git
history, and a worktree that really was deleted. The unit suites prove the
mechanisms; this proves the epic.

Run against a real relay, a real bot and the reference workspace.

## Gates

Walk brd §7 end to end and record evidence for each. Specifically:

1. **The long wait.** Ask a question from a session, exit the session, and answer
   it **at least 24 h later** (brd §7) — ideally after the machine has slept. Confirm the
   entry resolves in the file, the Telegram message shows `✅`, and no session ran.
2. **The dead worktree.** Ask from a sibling worktree, delete the worktree
   (and, separately, switch its branch), then answer. Confirm the answer lands in
   the primary checkout's queue file.
3. **The nudge tail.** Leave a question unanswered past its third rung and
   confirm it is still nudging on the repeating tail, inside availability windows.
4. **Escalation.** Confirm it fires exactly once at `escalate_after`, that both
   copies are live, and that answering either resolves both.
5. **Crash safety.** `kill -9` the listener between apply and PATCH; restart;
   confirm exactly-once application.
6. **Ack-notify.** Confirm it nudges, that it writes no file, and that idle-session
   notifications are unchanged.
7. **Regression floor.** With the listener stopped, run a normal permission
   prompt, a blocking `AskUserQuestion`, an idle notification and a Telegram reply
   injection. Confirm each behaves exactly as before.
8. **Adoption.** Add `[questions]` to a second, unrelated workspace and confirm a
   working `ask` with no code changes.

## Evidence

Record in a sibling `23-07-signoff-evidence.md` (the epic-20 convention):
timestamps, the resulting file diffs, screenshots of the Telegram thread for the
nudge tail and escalation, and the `claude-questions --check` output before and
after. Sign off only when every gate has evidence attached.
