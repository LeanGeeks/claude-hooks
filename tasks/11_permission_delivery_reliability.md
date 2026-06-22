# Task 11 — Permission-delivery reliability (bug)

**Status:** open · **Type:** bug · **Created:** 2026-06-21 · **Priority:** high
**Relation:** independent of, but relied upon by, [Epic 10](./10_spawn_sessions/) —
spawned/unattended sessions depend on permission requests actually reaching the
operator. This is the *shipped product* (tasks 02/03/05/08/09), so it stands on
its own.

## Symptoms (observed live, 2026-06-21)

While a background sub-agent and the main session issued gated commands:

1. **Terminal-less delivery.** A permission request reached **only Telegram**,
   never the terminal — no local prompt, no local indication one was pending.
2. **Delayed / non-proactive delivery.** The request surfaced on Telegram **only
   after a later, unrelated hook fired** (a subsequent bash command), not when it
   was created. Implication: on a quiet/idle pipeline, a pending request can sit
   **undelivered indefinitely**.

A concrete instance: a sub-agent's cleanup `rm -rf …` sat at the gate as
`permission_requests.jsonl` id `a8ad14c3` (state `pending`) for ~33 minutes; no
`rm` process ever started — it was parked awaiting approval, invisible locally.

## Evidence / leads

- **The hook side is prompt.** The spawn spike (Epic 10) confirmed the event
  chain `PreToolUse → Notification{notification_type:"permission_prompt"}` fires
  **immediately** when a command is gated. So the latency/terminal-less behavior
  is downstream — in the **relay / Telegram forwarding path**, not in hook firing.
- **Config possibly not honored.** The spike agent moved
  `~/.config/claude-tg-relay/config.toml` aside believing it disabled Telegram,
  yet requests were **still forwarded** — suggesting the permission path reads a
  different config, or a long-running daemon caches stale config.
- **Background sub-agents have no TTY**, so for *those* the terminal cannot prompt
  — Telegram is inherently the only channel. But the operator reported
  terminal-less behavior for **main-session** commands too, so this is broader.
- **Log hygiene red flags** (possible symptom or side-issue):
  `telegram_daemon.log` ≈1.1 GB, `bash_hook_debug.log` ≈735 MB,
  `permission_telegram_errors.log` ≈1.6 MB. Worth scanning the errors log and
  capping/ rotating the debug logs.

## Hypotheses (to confirm, not assume)

- H1 Forwarding is performed **lazily inside a later hook invocation** (a queue
  flushed on the next hook run) rather than pushed when the request is created.
- H2 The **local/terminal prompt is suppressed** by design (routed to Telegram),
  removing any local fallback when Telegram is slow/missed.
- H3 The permission path uses a **config source or daemon** that ignores the
  relay config file edits (stale cache / different path).
- H4 A daemon/queue processes requests only on an external trigger.

## Scope

- Diagnose why permission requests are (a) not surfaced locally and (b) not
  delivered promptly/proactively; fix delivery to be **prompt and push-based**.
- Ensure a **fallback** when the remote channel is unavailable (local prompt, or
  at minimum a detectable pending state — see Epic 10's `stuck` reason-context).
- Verify behavior for **both** main-session and background-sub-agent commands.
- Confirm enable/disable config is honored without a daemon restart, or document
  the restart requirement.

## Acceptance criteria

- [ ] A gated command's permission request is delivered to the operator
      **promptly** (push, not coupled to later activity); measure latency.
- [ ] When the remote channel is down/disabled, there is a **fallback** or a
      clearly detectable pending state (no silent indefinite block).
- [ ] Verified for main-session **and** background-sub-agent commands.
- [ ] Relay enable/disable via config behaves as documented.
- [ ] Debug/daemon logs are bounded (rotation/cap) or the growth cause is fixed.

## Investigation findings (2026-06-22) — proven facts

Investigated live against the code, the on-disk logs/state, and the production
relay DB (`ssh anton@h02.activecdn.net`, sqlite at `/var/lib/relay/relay.db`),
plus two controlled experiments with background sub-agents.

**The original symptoms did not reproduce; the cited evidence does not hold up.**

- **`a8ad14c3` (the flagship example) worked correctly.** Debug log: created
  `23:10:28.545`, **Telegram sent 508 ms later** (relay msg 1028), resolved
  **via terminal** 66 s later. Not "33 min pending", not "terminal-less". The
  state store rewrites rows in place and stores UTC while debug logs were
  local-time (`+0400`) — correlating the two produced a false ~4 h skew, the
  likely source of the "sat for N minutes" misreading.
- **The 31 "pending" rows were noise:** 24 were test pollution (`test-*`
  sessions writing into the real `~/.claude/permission_requests.jsonl`); the 3
  real ones all had `telegram_message_id`s (delivered) and were left `pending`
  only because the hook exits without finalizing the row.
- **Experiment A — background sub-agent, `cowsay …` (auto-context):** the
  `PermissionRequest` hook **fired in ~5 s** (payload carried `agent_id` /
  `agent_type`) and delivered to Telegram (relay msg 1052). Refutes "the hook
  doesn't fire for background sub-agents".
- **Experiment B — background sub-agent, parked (not resolved):** request
  parked `pending` ~81 s with the relay message **OPEN and carrying a live
  2-row keyboard** (Allow/Deny/Stop/Whitelist), i.e. Telegram is a fully
  working approval channel during a park; a **parent-CLI prompt also appeared**
  (so background sub-agents are not terminal-less when the parent is attended).
  It then resolved `resolved_terminal` and the relay message was cancelled.
- **Buttonless Telegram prompt** (observed by operator): the keyboard *is*
  sent; it gets **stripped when the request resolves via the CLI** (cancel →
  `editMessageReplyMarkup` with no keyboard). So a phone prompt can go dead
  before you open it — a race, working as designed, not a delivery failure.

**Conclusion on root cause:** delivery itself is prompt and push-based. The only
genuine exposure is the original Epic-10 scenario: an **unattended parent** (no
human at the CLI) **plus** a missed Telegram tap → the request parks up to the
12 h TTL with no auto-escalation. That is a policy gap, tracked separately.

**Secondary defects confirmed and fixed here:**
- Hook exits without transitioning the state-store row → rows stay `pending`
  for up to the 12 h TTL, making `pending` unreliable for stuck-detection.
- Timestamp zone mismatch: store=UTC, debug/error logs=local. Now all UTC.
- `request_id` was 32 bits (`uuid4()[:8]`) → collision-prone at current row
  counts; widened.
- Relay message ids restart after a relay-DB reset, so the long-lived client
  store can hold duplicate `telegram_message_id`s from different eras (found
  relay id 1052 on a May-25 `chmod` row and today's `cowsay` row). Documented;
  any logic keying on that id across a reset is unsafe.
- Tests wrote into the real `~/.claude/permission_requests.jsonl`
  (`test_unit_state_store.py` saved but never repointed `STATE_FILE`). Isolated.
- Debug logs unbounded (no rotation). Now rotated+gzipped on each
  `install-claude-config.sh` run.

### Decision — unattended-parent escalation (2026-06-22)

"Missed tap" is **not** a detectable event (Telegram gives the bot no read
receipts), so we don't design around it. Spawned/unattended sessions are
**interactive tmux panes** (not `claude -p`), and the native terminal prompt is
shown **concurrently** with the Telegram prompt for the hook's whole lifetime —
either channel can answer. So no attended/unattended detection is needed.

Agreed behavior (implemented):

- **Keep the full ~12h TTL** for both permission requests and AskUserQuestion —
  overnight prompts answerable in the morning. Unchanged.
- **Permission requests:** race the terminal for the full TTL **even when the
  Telegram send fails** (the terminal is still live, so an attended operator can
  approve at the CLI). If *nobody* answers within the TTL → **auto-deny with an
  agent-facing note** (fail safe; never auto-allow). The note distinguishes a
  *delivery failure* ("couldn't reach operator; the same command may work if
  retried") from *delivered-but-unanswered* ("no response within 12h; re-run"),
  and includes the non-allowlisted command parts.
- **AskUserQuestion:** never auto-resolve (no safe synthetic answer). On no
  Telegram answer the native terminal UI persists indefinitely; a missing
  Telegram answer is **not** treated as a failure (only an explicit send error
  is). Its rows intentionally stay `pending`.
- A delivery failure is surfaced only at the eventual auto-deny (plus the
  error log), not as an immediate deny — to avoid cutting off an attended CLI.

**Reference:** relay server is remote — `ssh anton@h02.activecdn.net`, docker
compose at `~/.bin/claude-hooks/relay-server/`, container `relay-server-relay-1`,
sqlite `/var/lib/relay/relay.db`. In `messages`, `id` = relay id (what the
client stores as `telegram_message_id`); `telegram_message_id` = real Telegram id.

## Out of scope

- Epic 10's spawn tooling. Note, however: **permission-block is only one of
  several reasons a session/background task can appear stuck** — do not treat
  this bug as *the* cause of stuck sessions (Epic 10 handles stuck generically).
