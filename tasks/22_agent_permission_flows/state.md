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
| 22-01 | [Validator: deny denies, ask asks](./22-01-validator-deny-and-ask.md) | done | — | Independently shippable; changes live behavior on install. Watch H1 (no more human-rescue for deny false positives). |
| 22-02 | [External decisions reach the wait loop](./22-02-external-decisions-wait-loop.md) | done | — | Store schema (`actor_agent`, `agent` source) + relay-path loop widening + Telegram finalization. No agent-facing surface yet. Concurrency + state-store races — the manager prompt's opus-implementer rule applies. |
| 22-03 | [Permissions MCP: read + decide](./22-03-permissions-mcp.md) | done | 22-01, 22-02 | The server, registration, D5 guard, D3 tier. 22-01 defines the tier vocabulary; 22-02 makes decide effective. |
| 22-04 | [Allowlist writers + queue](./22-04-allowlist-writes-and-queue.md) | done | 22-03 | `resolve_project_key`, queue format, versioned-settings writer, `allowlist_add` + `report_parser_issue` tools. |
| 22-05 | [Daily reviewer + compaction](./22-05-daily-reviewer-and-compaction.md) | in_progress | 22-01, 22-04 | Prompt, schedule, queue drain, installer merge, store compaction. |
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
- **2026-08-26 — 22-01 done.** Deny hard-denies (D1), `permissions.ask` loaded,
  merged and checked between deny and allow (D2), deny arm emitted in `main()`
  and the replay path, installer Step 5 jq carries `ask` (§3b), downstream
  helper now names ask-matched parts. Tests 923 → 931, all green; review PASS
  with no BLOCKER/HIGH/MEDIUM findings. **Not live in other workspaces:**
  `install-claude-config.sh` has not been re-run, so `~/.claude/hooks/` still
  carries the old mapping and user-scope `ask` patterns do not yet exist
  (invariant 8, H6). Run the installer before 22-06.
- **2026-08-26 — 22-02 done.** Store carries `actor_agent` and
  `RESOLUTION_SOURCE_AGENT`; the relay-path wait loop adopts an external
  decision only when the row is agent-sourced *and* in `{ALLOW, DENY, STOP}`
  (both halves of the gate now isolated by tests); `_finalize_agent_decision`
  patches the Telegram message with 🤖 attribution then cancels, best-effort.
  Tests 931 → 942. Review PASS; one MEDIUM (source gate untested in isolation)
  and two LOW findings fixed and re-reviewed PASS. Inert until 22-03 writes
  decisions. Decision-dict contract for 22-03: `{"action": "allow"|"deny"|"stop"}`.
- **2026-08-26 — 22-03 done, and the installer has run.** `permissions-mcp/`
  ships the server plus `permissions_mcp_lib.py`; `get_requests(states, since)`
  added to the store module (readers follow invariant 6 too);
  `append_agent_decision_reason` exposed as the sanctioned public path for the
  caller's free-text reason. Guard is row-shaped (D5), tier re-runs the real
  `BashPermissionValidator` against the row's `cwd` (invariant 2), decide fails
  closed with no `CLAUDE_CODE_SESSION_ID`. Tests 942 → 984. Review PASS, one
  LOW fixed and one LOW (stray untracked `tasks/23_async_questions/`) left
  alone as out-of-epic.
  **`./install-claude-config.sh` was re-run (user-approved, 2026-08-26 17:10).**
  So from now on invariant 8 reads differently: the installed hooks under
  `~/.claude/hooks/` now carry 22-01's deny flip and 22-02's agent path,
  `permissions` is registered in `~/.claude.json` with `CLAUDE_HOOKS_REPO`, the
  four `mcp__permissions__*` grants are in the global allowlist, and
  `permissions.ask` now exists as a key in the global settings instead of being
  erased (§3b proven end to end). Backup:
  `~/.claude/backups/settings.json.20260826_171034.bak`.
  Live criteria verified with a real `claude -p` session driving the MCP
  cross-session against a scratch store: list → decide → row carries
  `actor_agent` + `resolution_source: "agent"` + the reason.
  **H1 watch is now open** — the deny flip is live with no soak period, so
  `bash_manual_confirm.log` is the place to catch false-positive hard blocks.
- **2026-08-26 — 22-04 done.** `.claude/hooks/project_key.py` is now the only
  project-identity function (invariant 7); `.claude/hooks/settings_writer.py`
  generalizes the router's atomic writer to any of allow/ask/deny, with the
  router delegating byte-compatibly and still targeting `settings.local.json`;
  `permissions-mcp/permission_queue.py` owns queue IO (a third module the task
  did not name — reviewer judged it sensible factoring, and 22-05's drain can
  import it without the MCP lib). `allowlist_add` writes the caller's own
  `.claude/settings.json` and enqueues for every other project key;
  `report_parser_issue` always enqueues to the claude-hooks key. Refusals
  (malformed pattern, deny collision) happen before any disk effect. Tests
  984 → 1020, review PASS with two LOW notes both marked "no change needed".
  **Installer re-run (2026-08-26 17:38):** `project_key.py` and
  `settings_writer.py` are installed under `~/.claude/hooks/` and import
  cleanly there; the global allowlist now carries all six
  `mcp__permissions__*` grants. Queue root override for tests:
  `CLAUDE_PERMISSION_QUEUE_DIR`.
  **For 22-05:** import `resolve_project_key` from `.claude/hooks/project_key.py`
  and the drain helpers from `permissions-mcp/permission_queue.py` — do not
  reimplement either. This repo's key is `-data-sync-work-leangeeks-ai-claude-hooks`.
