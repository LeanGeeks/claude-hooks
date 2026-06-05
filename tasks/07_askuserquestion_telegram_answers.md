# Task 07: AskUserQuestion — Telegram Answer Integration

## Goal

When Claude Code calls `AskUserQuestion`, the user should be able to answer **either** from the Claude Code terminal (native UI, unchanged) **or** from Telegram. Both channels must work independently; whichever the user answers first wins.

---

## Desired behavior

### Terminal (unchanged from default Claude Code behavior)
- The native `AskUserQuestion` UI appears in the terminal immediately, without any delay.
- Multiple-choice questions show their options.
- The user can select an option or type a free-text answer.
- This path must work even when Telegram is not configured or is unreachable.

### Telegram (new)
- The same question is forwarded to Telegram at the same time as the terminal UI appears.
- Multiple-choice options are shown as inline keyboard buttons.
- The user can also reply with free text (not just pick a button).
- When the user answers in Telegram, Claude Code receives that answer and proceeds — **without the user needing to do anything in the terminal**.

### Free-text answers
- Any option labelled with a free-text trigger (e.g. "Let me type it…", "Other…", "Custom") should follow up with a prompt for the actual text, in whichever channel the user is using.
- Free-text replies to a Telegram question message should also be accepted as answers (not just button clicks).

---

## Constraint: native UI must not be touched

Previous attempts used a `PreToolUse` hook that blocked before the tool ran. This prevented the native terminal UI from appearing and required a timeout before the terminal became usable. **That approach is not acceptable.**

The native Claude Code UI for `AskUserQuestion` must display immediately and without modification.

---

## Key technical challenge

The fundamental conflict:
- **To inject a Telegram answer**, we need to intercept the tool call before it runs (`PreToolUse` with `updatedInput`).
- **To show the native terminal UI**, the tool must run normally (no `PreToolUse` interception).

These two requirements are mutually exclusive with the current hook API, unless there is a way to:
1. Let the tool start and show its UI, **and**
2. Externally inject a response into the running tool call.

---

## Approaches to investigate

### Option A — PostToolUse re-run (rejected, probably)
Let `AskUserQuestion` run normally. In `PostToolUse`, detect that the tool ran and send the question to Telegram. If the user responds via Telegram, somehow trigger another question call with the answer pre-filled. Likely too late and too roundabout.

### Option B — Parallel PreToolUse + stdin injection
`PreToolUse` immediately exits 0 (so native UI shows), but concurrently sends the question to Telegram. If the user answers in Telegram before answering in the terminal, inject the answer by writing to the terminal's stdin (`/dev/tty`). Fragile and system-specific.

### Option C — Short-circuit with instant PreToolUse, late answer delivery
`PreToolUse` blocks only briefly (e.g. checks if a Telegram answer for this session is already queued). For a fresh question, exits immediately (native UI shows). The Telegram answer is stored but used in a different way — e.g. by having the hook output the answer to a sidecar that Claude Code picks up. Not currently supported by the hook API.

### Option D — Two-pass flow
`PreToolUse` sends to Telegram and exits 0. Native UI shows. `PostToolUse` captures the terminal answer. On the **next** tool call in the session, if a Telegram answer for the previous question is pending, it gets surfaced. Requires state across calls; semantics are awkward.

### Option E — Telegram answers as a separate input channel (most promising?)
Accept that terminal and Telegram answers are two separate events, and build a mechanism where:
1. Native UI shows immediately.
2. Telegram notification is sent in the background (fire-and-forget from the hook).
3. If the user answers in Telegram, the daemon captures the answer.
4. The answer is delivered back to Claude via a follow-up message or injected into the session transcript somehow (e.g. a synthetic user message saying "My answer to your question was: …").

This avoids the hook API limitation entirely but requires a PostToolUse or session-level integration to close the loop.

### Option F — Claude Code feature request
The ideal solution would be a hook event specifically for `AskUserQuestion` that can supply answers while the native UI is also active. This does not currently exist. Worth checking the latest Claude Code changelog or filing a feature request.

---

## What has already been built (current state)

- `ask_user_question_hook.py` — `PreToolUse` hook that intercepts `AskUserQuestion`, registers each question in the permission state store, sends it to Telegram (buttons for options, force-reply for free text), and polls the state store for an answer. Returns `updatedInput` with answers injected.
- `telegram_daemon.py` — extended to handle `qa{N}:{request_id}` button callbacks and route answers into the state store.
- `telegram_permission_router.py` — `_format_command_summary` improved to show readable question text if `AskUserQuestion` falls through to `PermissionRequest`.
- **Known problem**: `PreToolUse` blocking suppresses the native terminal UI entirely. The 5-minute timeout before terminal fallback is unacceptable for interactive use.

---

## Definition of done

- [ ] Native Claude Code `AskUserQuestion` terminal UI appears immediately, with no delay and no modification.
- [ ] The same question is forwarded to Telegram at the same time.
- [ ] Answering via Telegram button (option selection) is accepted by Claude Code.
- [ ] Answering via Telegram free-text reply is accepted by Claude Code.
- [ ] Free-text trigger options prompt for actual text in Telegram.
- [ ] If both channels answer, first answer wins (no double processing).
- [ ] Works correctly when Telegram is unreachable (graceful degradation to terminal-only).
