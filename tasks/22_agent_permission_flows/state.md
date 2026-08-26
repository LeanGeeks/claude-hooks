# Epic 22 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, done criteria and tests. This file owns **cross-task invariants**,
**ordering**, and the **defaults that were chosen without a Phase 0**.

**No Phase 0.** The design was settled in discussion (brd §2, D1–D8). Three
defaults were chosen by recommendation rather than hard requirement and are
one-line changes if they prove wrong in practice:

1. redirect-escape requests are **human-only** (brd D3) — flipping them to
   agent-decidable is one constant in the tier check;
2. state-store retention is **30 days of terminal rows** in the hot file
   (22-05) — a number, not a design;
3. non-Bash tool requests are agent-decidable unless an ask pattern matches the
   tool name (22-03 §tier) — tightening means adding ask entries, not code.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 22-01 | [Validator: deny denies, ask asks](./22-01-validator-deny-and-ask.md) | todo | — | Independently shippable; changes live behavior on install. Watch H1 (no more human-rescue for deny false positives). |
| 22-02 | [External decisions reach the wait loop](./22-02-external-decisions-wait-loop.md) | todo | — | Store schema (`actor_agent`, `agent` source) + relay-path loop widening + Telegram finalization. No agent-facing surface yet. Concurrency + state-store races — the manager prompt's opus-implementer rule applies. |
| 22-03 | [Permissions MCP: read + decide](./22-03-permissions-mcp.md) | todo | 22-01, 22-02 | The server, registration, D5 guard, D3 tier. 22-01 defines the tier vocabulary; 22-02 makes decide effective. |
| 22-04 | [Allowlist writers + queue](./22-04-allowlist-writes-and-queue.md) | todo | 22-03 | `resolve_project_key`, queue format, versioned-settings writer, `allowlist_add` + `report_parser_issue` tools. |
| 22-05 | [Daily reviewer + compaction](./22-05-daily-reviewer-and-compaction.md) | todo | 22-01, 22-04 | Prompt, schedule, queue drain, installer merge, store compaction. |
| 22-06 | [Live verification](./22-06-live-verification_human.md) | todo | all | **human** — walks brd §5 end to end with real sessions and a real Telegram chat. |

## Dependency graph

```
22-01 ──┬────────────────► 22-03 ───► 22-04 ───► 22-05
22-02 ──┘                                          │
                              all ───────────────► 22-06 (human)
```

22-01 and 22-02 are independent roots and can run in parallel.

## Recommended order

1. **22-01 first, and let it soak.** It changes live gate behavior (deny
   hard-blocks; ask prompts). A few days of `bash_manual_confirm.log` under the
   new mapping tells you whether H1 bites before any agent is allowed to decide
   anything.
2. **22-02 alongside** — inert until something writes agent decisions.
3. **22-03**, then **22-04** — the MCP surface, read-only tools first if
   splitting the landing.
4. **22-05** once a queue exists to drain.
5. **22-06** last, against installed state, not the repo copy.

## Cross-task invariants

1. **Deny is final and prompts no one.** After 22-01, no path — hook, MCP, or
   reviewer — downgrades a deny match to a prompt. (brd D1)
2. **One classifier.** The MCP's tier decision re-runs `BashPermissionValidator`
   against the row's `cwd`; it never reimplements pattern matching. (brd H5)
3. **The guard is row-shaped, not session-shaped.** Refuse
   `row.session_id == caller ∧ row.agent_id is None`; allow same-session rows
   with an `agent_id`. (brd D5)
4. **Own workspace writes, foreign workspace enqueues.** No agent ever edits,
   commits, or pushes in a checkout it is not running in. (brd D6)
5. **Every agent decision is attributed.** `actor_agent` on the row,
   `resolution_source: "agent"`, an audit-log entry, and the attribution
   patched into the Telegram message before cancel. A decision the human cannot
   later see happened is a bug.
6. **All store writers go through `permission_state_store`'s flock protocol.**
   No bespoke JSONL writers anywhere in this epic, including compaction.
   (brd H7)
7. **`resolve_project_key` is the only project-identity function.** Enqueue and
   drain must import the same helper; two implementations that disagree on a
   worktree lose proposals silently. (brd D7)
8. **Repo-settings edits name their propagation.** Any claim that a user-scope
   pattern "is live" must say whether `install-claude-config.sh`'s merge ran.
   (brd H6, and the standing rule from the installed-hooks memory)
9. **Scheduled spawns pin model and effort.** (brd H8, epic 21)

## Log

- **2026-08-26 — epic created.** Filed from the AI-flows design discussion
  (Anton + agent, this repo, 2026-08-26). Review findings and the native-
  semantics verification (deny → ask → allow order; deny overrides hooks;
  `permissions.ask` exists) are recorded in brd §1; decisions D1–D8 in brd §2.
  Option B for foreign-workspace writes (edit+commit+push+install from outside)
  was **rejected** in favor of A+C — the reviewer-mutation incident is the
  precedent for why foreign-checkout git surgery is out. Specs only; no code
  written this sitting.
- **2026-08-26 — pre-handover review pass.** Two substantive finds, both fixed
  in the specs: (1) `install-claude-config.sh` Step 5's jq replaces
  `permissions` wholesale with `{allow, deny}` — repo `ask` lists would never
  propagate and a global `ask` key is erased on every install; now 22-01 §3b
  with its own done criterion. (2) The MCP guard had no behavior for a missing
  `CLAUDE_CODE_SESSION_ID`; now fail-closed (decide refuses) in 22-03 §1.
  Recorded as accepted, not fixed: session YOLO supersedes the ask tier (a
  human grant — brd D3 note); the Whitelist button on an ask-matched request
  is a persistent no-op (22-01 §4 note); `resolve_project_key` keys a
  submodule by its own path (falls back on non-`.git` basename — no submodules
  in current workflows). Also tightened: explicit-state listing goes through a
  new `get_requests` reader in the store module rather than server-side JSONL
  parsing (22-03 §2); the cron launcher must set `PATH`/`HOME` and log stderr
  (22-05 §3); the reviewer dry-run must eliminate the reviewer's own
  permission prompts (22-05 testing). Verified by reading, no spec change
  needed: PostToolUse's pending-only sweep skips agent-resolved rows
  (`find_pending_request_by_tool_session` matches `PENDING` only), and the
  decision dict `{"action": ...}` round-trips through `_ACTION_TO_STATE` and
  `build_output_decision` unchanged.
