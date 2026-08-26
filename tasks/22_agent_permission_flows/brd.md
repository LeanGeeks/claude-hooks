# Epic 22 — Agent-facing permission flows

**Status:** todo · **Owner:** Anton · **Created:** 2026-08-26 · **Rev:** 1

> Broken down into tasks — see [state.md](./state.md) for ordering and
> cross-task invariants.

## 1. Problem & thesis

The PreToolUse/PermissionRequest stack widens allowlisted rules and routes
prompts to a human over Telegram. It has no AI-facing surface at all: an
orchestrator agent cannot see that its worker is blocked on a permission, cannot
answer for it, cannot study why prompts keep firing, and cannot promote a
pattern into versioned settings. Six wanted capabilities (Anton, 2026-08-26):

1. agents see current permission requests (same workspace, and — because of git
   worktrees — sometimes a different one);
2. agents approve/deny a request;
3. agents review/analyze request history;
4. agents allowlist commands into the **workspace's** git-versioned settings;
5. agents allowlist commands into the **claude-hooks** git-versioned settings
   (the user-scoped allowlist);
6. agents report parser issues and submit suggestions to claude-hooks.

**Thesis: the data layer already exists; what is missing is a surface, one
wait-loop gap, and policy.** Findings from the 2026-08-26 review:

1. **Visibility is nearly free.** `~/.claude/permission_requests.jsonl` is
   user-scoped (one file for every workspace and worktree on the machine); each
   row carries `cwd`, `session_id`, `agent_id`, `tool_name`, full `tool_input`,
   `permission_suggestions`, state and expiry.
   `get_all_pending_requests()` already exists
   (`permission_state_store.py:829`). Nothing reads it on behalf of an agent.
2. **The wait loop only honors external decisions on the relay-less path.**
   `_wait_state_store_only` (`permission_request_hook.py:490`) returns a
   decision written into the store by anyone; the main path — relay message
   live, hook long-polling (`permission_request_hook.py:352-387`) — checks the
   store only for `RESOLVED_TERMINAL`. An external `ALLOW` write there is
   silently ignored: the hook keeps polling until the human answers or the 12h
   TTL lapses, and the Telegram message stays open collecting `#unanswered`
   nudges.
3. **History is rich.** Three feeds: the state store (every request, resolution
   source, actor, timestamps), `~/.claude/permission_actions.jsonl` (audit
   trail), `~/.claude/bash_manual_confirm.log` (every command **not**
   auto-approved, with per-sub-command validation results — the "why did this
   prompt" signal). `pretool_hook.py` has a replay mode
   (`CLAUDE_HOOK_REPLAY=1`) for testing parser changes against logged commands.
4. **Whitelisting exists but targets the wrong file.** The Telegram Whitelist
   button writes `.claude/settings.local.json`
   (`telegram_permission_router.py:860`) — unversioned, and gone with a cleaned
   worktree. Its pattern choice (`generate_whitelist_pattern`, `:838`) is
   `permission_suggestions[0]` or a coarse `Bash(<head>:*)`.
5. **The claude-hooks repo settings file is already the de-facto user-scoped
   allowlist.** `install-claude-config.sh:640-651` **overwrites** the global
   `~/.claude/settings.json` permissions with the repo's on every install.
   Consequence in both directions: user-scope patterns MUST go through the
   repo (manual additions to the global file are clobbered on the next
   install), and a repo edit reaches other workspaces only when the installer
   merge re-runs.
6. **Native semantics, verified against Claude Code docs (2026-08-26):**
   `permissions.ask` exists as a third list ("always prompt"); evaluation order
   is deny → ask → allow; **native deny overrides hook decisions** — a
   PreToolUse `allow` or `ask` cannot rescue a call matching a native deny
   rule. Two mismatches in our stack: `pretool_hook.py:525-529` deliberately
   maps a deny-pattern match to `ask` ("let the user decide"), and
   `SettingsLoader` ignores `permissions.ask` entirely (`settings_loader.py`
   reads only allow/deny, both formats).

## 2. Decisions (locked, Anton 2026-08-26)

- **D1 — deny means deny.** Flip `pretool_hook.py`'s deny-match mapping from
  `ask` to a hard `deny` (with the matched pattern in the reason, which flows
  back to the agent). This aligns the sub-command widening with native
  semantics. The requests that used to reach Telegram tagged "Matches a denied
  pattern" — deny-listed sub-commands buried in compounds that native matching
  misses — stop being prompts and become denials.
- **D2 — honor `permissions.ask`.** `SettingsLoader` loads and merges the
  third list; the validator checks it after deny, before allow; an ask-match
  produces `ask` with its own reason even when every sub-command is also
  allowlisted.
- **D3 — the human-only tier is defined by `ask`.** With deny now hard-denying,
  the MCP decide tool refuses requests that match an ask pattern; "not in
  allowlist" requests are agent-decidable. Default (cheap to flip): the
  redirect-escape class ("writes outside the workspace / /tmp") is human-only
  too — it is the validator flagging potential damage, not mere unfamiliarity.
  Known and accepted: session **YOLO** (`permission_request_hook.py:1436`)
  auto-allows before the wait, so a YOLO'd session's ask-matched commands never
  reach a human either — YOLO is itself an explicit human grant, so the tier is
  not being bypassed by an agent.
- **D4 — the MCP is allowlisted globally.** `mcp__permissions__*` entries go
  into this repo's `.claude/settings.json` and reach the global file via the
  installer — the grant itself is git-versioned. The ask tier (D3) still caps
  what a decide call may touch.
- **D5 — parent-approves-child is the primary in-session use case.** A blocked
  main agent cannot call tools, but its background subagents keep running, and
  the commoner direction is the reverse: a parent deciding a child's request.
  An Agent-tool subagent shares the parent's `session_id` (rows distinguish it
  by `agent_id`), so the guard is: **a caller may not decide a row from its own
  session unless the row carries an `agent_id`.** Same-session subagent rows
  are decidable (parent→child); the main agent's own rows (`agent_id: None`)
  never are. Residual: one background subagent approving a sibling's request —
  accepted as parent-sanctioned. Cross-session collusion cannot be blocked
  structurally; it is the trust the global allowlist grants.
- **D6 — provenance: A + C, never B.** Only a workspace's **own** agent writes
  that workspace's settings (A); a foreign agent always **enqueues** a proposal
  (C) for the scheduled agent that lives in the target workspace. The rejected
  option (B: foreign agent does edit + commit + pull/push + install) needs
  worktree-clean checks, branch policy, push rights, and races live sessions —
  agent-driven git surgery in foreign checkouts is the category that has
  destroyed work before. Everything B buys, C delivers with ≤1 day latency,
  with the git work done in-context.
- **D7 — queue lives at `~/.claude/permission-queue/<project-key>/`, keyed by
  the main checkout.** `resolve_project_key(cwd)`: inside a git repo, normalize
  to the parent of `git rev-parse --git-common-dir` so **every worktree of a
  repo maps to one queue** — the one its main-checkout agent drains; non-git
  workspaces fall back to their own path. No foreign-checkout writes at all;
  the audit trail is the applied settings diff plus the daily agent's commits.
  A proposal filed from a feature-branch worktree is applied centrally (a
  commit to main), not on the branch — deliberate.
- **D8 — the history review runs daily**, as a scheduled local session in the
  claude-hooks workspace, and owns compaction of the stores it reads.

## 3. Scope

### 3.1 In scope

- Validator: D1 flip + D2 ask-list support, with tests (task 22-01).
- Wait loop: honor externally-written decisions on the relay path, finalize the
  Telegram message with agent attribution; `actor_agent` + `agent` resolution
  source in the state store (task 22-02).
- A `permissions` MCP server (context-mcp pattern: uv script, registered
  user-scoped by the installer): `list_permission_requests`,
  `get_permission_request`, `decide_permission_request`, `permission_history`,
  `allowlist_add`, `report_parser_issue` — with the D5 guard and the D3 tier
  (tasks 22-03, 22-04).
- Queue format + `resolve_project_key` + versioned-settings writers (task
  22-04).
- The daily reviewer: prompt, schedule, queue drain, allowlist application,
  parser-issue filing, store compaction/rotation (task 22-05).
- Live verification, human (task 22-06).

### 3.2 Out of scope — deliberately

- **Answering `AskUserQuestion` rows via the MCP.** Questions have reply
  semantics, not allow/deny; agent-answered questions are a different feature
  (see the hyppie-flow async-questions design for where that is headed).
  `list` may show them; `decide` refuses them.
- **A relay answer endpoint** (`POST /v1/messages/{id}/answer`). It would make
  agent decisions cross-machine and instant, reusing all server-side
  finalization — but every stated use case is same-machine, and the store +
  wait-loop path needs no deploy. Revisit if cross-machine orchestration
  arrives.
- **Auto-applying allowlist proposals.** The daily agent applies judgment and
  commits; nothing lands without a reviewing agent (and the commit is the
  reviewable artifact).
- **Editing another workspace's checkout, ever** (D6).
- **Retro-fitting the Whitelist button** onto versioned settings. The Telegram
  button keeps writing `settings.local.json`; promotion to versioned settings
  is the reviewer's job with better pattern judgment than
  `permission_suggestions[0]`.

## 4. Constraints & hazards

- **H1 — the flip removes a human-rescue path.** Today a false-positive deny
  match (constant expansion, wrapper peeling) still reaches a human who can
  approve it; after D1 it hard-blocks. Mitigations: the reason names the
  matched pattern and sub-command so the agent can reformulate;
  `log_manual_confirmation` already logs every non-allow decision, so the daily
  reviewer sees false positives in `bash_manual_confirm.log` and fixes the
  pattern or the parser. Watch the first weeks.
- **H2 — decide latency is bounded by the long-poll chunk.** The relay-path
  loop re-reads the store once per chunk (≤25 s). An agent decision therefore
  takes up to ~25 s to unblock the tool. Accepted for v1; shrink the chunk only
  if it hurts in practice.
- **H3 — a decision written for a dead waiter goes nowhere.** If the hook
  process is gone (session killed, machine slept), the store row flips but no
  tool runs and the Telegram message dangles until reaper expiry. The decide
  tool reports "decision recorded; takes effect when the waiting hook picks it
  up" rather than claiming execution.
- **H4 — the MCP server is long-lived; `SettingsLoader` caches for 60 s**
  (class-level TTL — irrelevant to per-invocation hooks, real for the MCP). A
  decide-time tier classification may lag a settings edit by up to a minute.
  Accepted; documented in the tool description.
- **H5 — one classifier, two callers.** The D3 tier must come from the same
  `BashPermissionValidator` the PreToolUse hook runs, re-run at decide time
  against the row's `cwd` settings — never from a copy of the matching logic.
  Divergence here silently converts human-only requests into agent-decidable
  ones.
- **H6 — a repo settings edit is not live everywhere** (finding 5). In the
  claude-hooks workspace it is live immediately (hooks re-read per invocation);
  every other workspace sees user-scope changes only after
  `install-claude-config.sh` re-runs its merge. The daily reviewer runs the
  merge as part of applying user-scope changes; anything else must say so.
  The merge itself has a D2 gap: Step 5's jq extracts only allow/deny and
  replaces `permissions` wholesale, so it would drop `ask` in both directions —
  22-01 §3b fixes it; until that lands, user-scope ask patterns do not exist.
- **H7 — store writes follow the existing flock protocol.** The state store
  rewrites the whole file under `LOCK_EX` on every update and scans it whole on
  every read; its own comments anticipate tens of thousands of rows. Compaction
  (22-05) must take the same lock, and every new writer (22-02's decide path)
  goes through `update_request_state`, never a bespoke writer.
- **H8 — the daily reviewer's spawn must pin model and effort** (epic 21: the
  non-TTY path warns on unpinned knobs; a scheduled session reading the
  floating harness default is exactly the defect 21 exists to report).

## 5. Acceptance

The epic is done when, on this machine:

1. A compound command with a deny-listed sub-command is **denied** with a
   reason naming the pattern — no Telegram message, no PermissionRequest, and a
   line in `bash_manual_confirm.log`.
2. A command matching a `permissions.ask` pattern prompts even when fully
   allowlisted; `decide_permission_request` refuses it as human-only; a
   Telegram answer still resolves it.
3. A manager session approves a spawned worker's pending Bash request via the
   MCP; the worker's tool runs within ~30 s; the Telegram message is finalized
   with agent attribution; the state store row carries `actor_agent` and
   `resolution_source: "agent"`; the audit log has the entry.
4. `decide_permission_request` refuses the caller's own main-agent row, and
   accepts a same-session row that carries an `agent_id`.
5. `allowlist_add` in the caller's own workspace writes `.claude/settings.json`
   (versioned); pointed at a foreign workspace it writes a queue entry instead
   — and from a worktree, the entry lands under the **main checkout's**
   project key.
6. One daily-reviewer run: drains its queue, applies/rejects proposals with a
   commit, runs the installer merge for user-scope changes, files at least the
   report structure for parser issues, and compacts terminal rows older than
   the retention window out of the hot store.
7. `python3 tests/run_all_tests.py` green with the new suites; the relay
   server's tests untouched and green.
