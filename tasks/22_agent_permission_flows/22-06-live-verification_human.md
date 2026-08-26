# 22-06 — Live verification (human)

**Status:** todo · **Depends on:** 22-01 … 22-05, installed
**Read first:** [brd.md](./brd.md) §5 (this task walks it) ·
[state.md](./state.md) invariant 8

## Why human

Every step below crosses a boundary a test double hides: the installed hooks
(not the repo copies), the real relay and a real Telegram chat, real spawned
sessions under amux, the real crontab. The precedent is 19-07/20-06: the epic
does not close on suite-green alone.

## Preconditions

- `./install-claude-config.sh` re-run after 22-01…22-04 landed (hooks copied,
  MCP registered, allowlist merged). Name the backup file it printed.
- The crontab line from 22-05 installed, or the launcher run by hand for §6.

## Walkthrough — brd §5, in order

1. **Deny is final.** In a scratch workspace with `Bash(curl:*)` denied, have a
   session run `true && curl http://example.com`. Expect: hard deny with the
   pattern named in the agent-visible reason; **no** Telegram message; a
   `decision: "deny"` line in `~/.claude/bash_manual_confirm.log`.
2. **Ask asks.** Add `Bash(git push:*)` to `permissions.ask` with `Bash(git:*)`
   allowed; run `git push --dry-run`. Expect: Telegram prompt whose body names
   the ask pattern; answer it from the phone; the command proceeds.
3. **Agent decides.** Spawn a worker (`amux-spawn`, pinned model/effort) that
   runs a not-allowlisted command; from a second session, list → decide allow
   via the MCP. Expect: worker unblocked ≤ ~30 s; Telegram message finalized
   with the `🤖 … by agent …` line (no live buttons, no `#unanswered`); row
   carries `actor_agent` + `resolution_source: "agent"`; audit entry present.
4. **Guard.** From the manager session, attempt to decide one of its **own**
   main-agent pending rows (create one by running a gated command from a
   background subagent's parent — or simply fabricate via a second terminal).
   Expect refusal with the guard wording. Then confirm the same session **can**
   decide a row that carries an `agent_id`.
5. **Human-only tier.** Attempt to decide the §2 ask-matched request via the
   MCP before answering it. Expect the human-only refusal naming the pattern.
6. **Queue + worktree key.** From a `git worktree` of a scratch repo, call
   `allowlist_add` targeting that repo (foreign to the caller's workspace).
   Expect the entry under the **main checkout's** key in
   `~/.claude/permission-queue/`. Then run the daily reviewer (launcher by
   hand is fine): queue drained to `processed/`, decision committed, installer
   merge run and named, compaction counts in the summary, summary arrived in
   Telegram via the idle notification.
7. **Races stay safe.** Repeat §3 but answer in Telegram a moment before the
   MCP decide. Expect exactly one effective decision, the loser told honestly.

## Record

Append the outcome (with literal snippets: reasons shown to agents, the
patched Telegram text, audit lines, the reviewer commit hash) to
[state.md](./state.md)'s Log. Any step that fails reopens its task rather than
being patched around here.
