# 15-07 — Live verification

**Status:** done · **Depends on:** 15-06
**Agent:** human — do **not** spawn Implementer / Reviewer / Fixer / Committer
**Read first:** [brd.md](./brd.md) §5 (routing behaviour), [state.md](./state.md) (gates)

## Why this is a task and not a checklist

Everything else in this epic is verified with `FakeTelegramBackend` and a patched
`RelayClient`. Three behaviours cannot be: they need a **real relay** and a
**second Telegram chat with its own installation token**, which is precisely the
thing the epic exists to support and precisely the thing no agent can provision.

Per `docs/prompts/implementer.md` §5, an agent that cannot reach these must
report a BLOCKER rather than substitute a mock and call it verified. This task
exists so that blocker has an owner instead of surfacing as a surprise at the end
of 15-06.

## Preconditions (human)

1. A second installation on the relay: `relay-admin` issues a token, then
   `relay-client bind --config-path <tmpfile>` and `/bind <code>` **in a
   different chat** from the operator's.
2. That token recorded under `[roles]` or `[workspace.<id>.roles]` in
   `~/.config/claude-tg-relay/config.toml`.
3. A `.claude/roles.toml` in the test workspace with at least the default role
   and one other, the second one carrying `escalate_after = "1m"`.
4. `./install-claude-config.sh` re-run — repo hook edits are not live until it
   does, and `roles_config.py` is only copied once 15-06 adds it to
   `REQUIRED_HOOKS`. A hook importing a module the installer never copied fails
   in exactly the way these hooks are built to swallow silently, so this step is
   load-bearing, not hygiene.
5. `claude-roles --check` reports both roles `bound`.

## Gates

**G1 — routing.** Ask a question tagged `@<role> …`. It arrives in the second
chat, headed `for: <title>`, with the alias stripped from the rendered header,
and the terminal chip still showing the raw `@<role> …`. Ask an untagged one: it
arrives in the operator's chat. Answer each from Telegram; both answers reach the
agent.

**G2 — escalation race, both directions.** With `escalate_after = "1m"`:
- Let it elapse. A duplicate group appears in the operator's chat with the ⏳
  banner. Answer it → the second chat's messages get the answer patched in and
  lose their keyboards, and the agent's answer carries ` (answered by <default
  title>)`.
- Repeat, this time answering in the second chat before the minute is up, then
  let the minute pass → no duplicate group appears.
- Repeat once more, letting the duplicate appear and then answering in the
  **second** chat → the operator's duplicate is patched and stripped, and the
  answer reaches the agent unannotated.

**G3 — terminal win.** Ask a tagged question, answer it at the keyboard. The
second chat's messages show `✅ <answer>` and lose their keyboards. Confirm the
answer text is the real one, not the `Answered in the terminal` fallback.

Do this twice: once picking an option, once leaving **notes without selecting an
option** — the second chat should then show the notes text, not `(notes only)`.

Scope note: the `tool_response` *shape* is no longer in question. It was captured
from a live `PostToolUse` payload on 2026-08-02 and committed as
[`fixtures/posttool_askuserquestion.json`](./fixtures/posttool_askuserquestion.json),
so 15-04's parser is tested against real data before this gate runs. What G3
still proves is the end-to-end wiring: that the answers reach the *other chat's*
messages, through that role's token, and that the keyboards actually come off.

If the generic fallback does fire, capture the payload rather than guessing —
add a `PostToolUse` hook to `.claude/settings.local.json` that dumps its stdin
(the epic used exactly this trick; the built-in debug log truncates at 500 chars
and is useless here).

**G4 — nothing regressed.** One permission prompt and one idle notification
still arrive in the operator's chat and behave exactly as before.

## Evidence to record

Append the outcome to `state.md` under a *Live run* heading: date, which gates
passed, and for any failure the observed behaviour plus the chat it happened in.
Mark this task `done` only when G1–G4 all pass.

## If a gate fails

File it back as a fix against the owning task (G1 → 15-03, G2 → 15-05,
G3 → 15-04, G4 → 15-02 or 15-03), and re-run the whole gate set afterwards —
these paths share the wait loop, so a fix in one routinely moves another.
