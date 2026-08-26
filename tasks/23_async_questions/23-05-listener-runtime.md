# 23-05 — Answer listener runtime (`questions-listen`)

**Status:** todo · **Depends on:** 23-01 (the feed), 23-03 (the store)
**Read first:** [brd.md](./brd.md) §2.2, §6.2, §6.3, D5, D10 ·
[architecture.md](./architecture.md) §5 · [state.md](./state.md) invariants 2, 3,
5, 7, 8 · `.claude/hooks/reply_injector.py` (long-poll + fail-open shape) ·
`tasks/16_telegram_spawn/architecture.md` §3.1–3.2 (lock, status file, backoff —
the same lifecycle problems, solved once already)

## Goal

The resident half of the epic: with no session running and no AI in the loop,
turn an answer given in Telegram into a resolved entry in a workspace file.

**This is the task where a bug loses a human decision silently.** Everything
below is shaped by that.

## Scope

### 0. The index is this task's

`~/.claude/async_questions.json` — its shape (architecture §5), its `flock` +
atomic-replace protocol, and its reader/writer helpers — is owned here, in
`questions_listen_lib.py`. 23-04 imports them. Never two hand-rolled parsers for
one file.

### 1. Binary and library split

`.claude/bin/questions-listen` is process lifecycle only; the logic lives in
`.claude/hooks/questions_listen_lib.py` (installed through `REQUIRED_HOOKS`,
importable, testable) — the epic-10/16 layout.

Single instance via `flock` on `~/.claude/questions-listen.lock`; a second copy
exits rather than double-applying. The loop refreshes
`~/.claude/questions-listen.status.json` so `--status`, a separate process that
cannot hold the lock, has something truthful to read.

### 2. Loop

Enabled by `[questions_listen] enabled = true` in
`~/.config/claude-tg-relay/config.toml`; absent or false, the binary exits 0
immediately so the unit can be installed without being active.

`GET /v1/answers?after=<watermark>&wait=25`, jittered backoff on network error,
`401` fatal-with-slow-retry and surfaced in `--status`. Laptops sleep: a dropped
connection is the normal case, not an error worth logging loudly.

### 3. Apply

Per answer, in order:

1. Look up `message_id` in the index. **No entry** → not ours (or the index was
   lost); log and skip without advancing past it destructively.
2. **No `qid`** → an ack-notification: finalize the message, no file touched
   (brd D7).
3. Re-resolve the workspace path **now** from `workspace_id` + anchor mode
   (invariant 7) — the path captured at ask time may be stale or gone.
4. `questions_store.apply_answer(...)`, idempotent on `message_id`, with the
   answer text escaped by the store (invariant 11). `answered_by` is the index
   entry's `role` rendered as its alias, plus the answer timestamp and the relay
   message id — the provenance that makes an injected line traceable (brd §8).
5. `PATCH /v1/messages/{id}` to `✅ <answer>` — the finalization the blocking hook
   would have done. Epic 19's cleanup sweep covers the window if we die here.
6. **Then** advance the watermark (invariant 2).

An answer whose apply fails moves to `pending` and the watermark advances past it
— it is now tracked by the pending list, and blocking the watermark on one
unappliable answer would stall every later one.

### 4. Pending retries and the sidecar fallback

Retry every `pending` entry on each wake with backoff to roughly hourly. After
~10 attempts, write the answer to `<dir>/answered/<qid>.md` with a note that the
original entry could not be located, and drop it from `pending`. Visible, never
silently lost (brd D10, §6.3).

Expose the pending count so 23-04 can return it on every `ask`.

### 5. `--status`

Connection state, watermark, last applied answer, pending count with the oldest
entry's last error, and the resolved config. Never token material.

### 6. What this must not do

No spawning, no `amux send`, no git operations, no branch checkouts, no moving or
deleting queue entries (invariant 8, architecture §3.5). If an answer cannot be
applied, the outcomes are retry and the sidecar — never "make the tree fit".

## Done when

- An answer given while the machine was **off** is applied on the next start.
- `kill -9` between apply and PATCH: on restart the answer is applied exactly
  once and the message still finalizes.
- An answer for a deleted worktree's question lands in the primary checkout.
- An answer for an entry that exists nowhere retries, then lands in the sidecar.
- Ack-notifications finalize and touch no file.
- Two listeners cannot run; the second exits cleanly.

## Tests

Fake relay client, temp workspaces, no network: watermark advance only after a
terminal outcome; crash-injection at each of the six steps with a replay
assertion; pending retry and sidecar; missing-index-entry handling; anchor
re-resolution after moving a checkout; ack-notification path; single-instance
lock; backoff and `401` behaviour. Include a full replay from `after=0` to prove
index recovery.
