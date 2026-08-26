# 22-04 — Allowlist writers + the proposal queue

**Status:** todo · **Depends on:** 22-03 (extends the same server)
**Read first:** [brd.md](./brd.md) §2 D6/D7, §4 H6 · [state.md](./state.md)
invariants 4, 7, 8 · `telegram_permission_router.py:860-902` (the existing
atomic settings writer)

## Goal

Vision items 4–6: agents promote patterns into **versioned** settings — their
own workspace directly, any other workspace via a queue — and file parser
issues the same way. One mechanism, two entry types.

## Scope

### 1. `resolve_project_key(cwd)` — the only project-identity function

New module `.claude/hooks/project_key.py` (hooks dir so both the MCP and any
future hook can import it):

- Inside a git repo: `git -C <cwd> rev-parse --path-format=absolute
  --git-common-dir`; the main checkout is its parent when the basename is
  `.git`, else (bare/odd layouts) fall back to `cwd`. **Every worktree of a
  repo maps to the main checkout** (brd D7).
- Not a git repo / git missing: `os.path.realpath(cwd)`.
- Key encoding: the resolved path with `/` → `-` (the `~/.claude/projects`
  convention), used as the queue directory name.
- Subprocess use is confined to this module, result cached per-path for the
  process lifetime.

Enqueue and drain must both import this function — two implementations that
disagree on a worktree lose proposals silently (invariant 7).

### 2. Queue format — `~/.claude/permission-queue/<project-key>/`

One JSON file per entry, `<utc-ts>_<8hex>.json`, written atomically
(tmp + rename, the settings-writer pattern):

```json
{
  "type": "allowlist_proposal" | "parser_issue",
  "created_at": "<iso8601>",
  "source": {"session_id": "...", "actor_agent": "...", "cwd": "..."},
  // allowlist_proposal:
  "pattern": "Bash(git log:*)",
  "scope": "workspace" | "user",
  "rationale": "...",
  "evidence_request_ids": ["..."],
  // parser_issue:
  "command": "<the raw command>",
  "observed": "<decision + reason produced>",
  "expected": "<what the reporter believes is right>",
  "notes": "..."
}
```

Drained entries are **moved** to `processed/` inside the same directory (never
deleted by the writer side; the daily reviewer owns retention there). No locks
needed — one file per entry, rename is atomic, the consumer moves before
acting.

### 3. Versioned-settings writer

A sibling of `update_settings_local_json`
(`telegram_permission_router.py:860`) targeting `.claude/settings.json` —
same read-tolerate-corrupt / dedupe / tmp-rename discipline. Extend it to
accept the target list (`allow` | `ask` | `deny`), defaulting to `allow`.
Place it where both the router and the MCP can import it without a cycle
(likely a new `settings_writer.py` in hooks; the router's function may
delegate to it, byte-compatible behavior).

**The Telegram Whitelist button keeps writing `settings.local.json`** —
explicitly out of scope to change (brd §3.2).

### 4. MCP tools (extend `permissions-mcp/server.py`)

- `allowlist_add(pattern, scope="workspace", target_workspace=None, rationale)`:
  - Validate the pattern shape first (`Bash(...)` / `Tool` forms; refuse
    free text) and refuse a pattern that collides with an existing deny entry
    in the target's merged settings — a pattern that deny would override
    anyway is a confusing no-op.
  - `scope="workspace"`: resolve target = `resolve_project_key(target_workspace
    or caller cwd)`. **If target == caller's own project key** → write
    `.claude/settings.json` directly (invariant 4's "own workspace writes").
    **Else** → enqueue an `allowlist_proposal` into the target's queue and say
    so ("proposal queued for <key>; the workspace's scheduled reviewer applies
    it").
  - `scope="user"`: target is the claude-hooks checkout
    (`CLAUDE_HOOKS_REPO` env from registration, 22-03 §5). Same own-vs-foreign
    rule — an agent *in* the claude-hooks workspace writes the repo's
    `.claude/settings.json`; everyone else enqueues. Every user-scope result
    carries the H6 note: not live in other workspaces until the installer
    merge runs.
- `report_parser_issue(command, observed, expected, notes)`: always an enqueue
  into the claude-hooks project key's queue — there is no "own workspace"
  shortcut; parser issues are for the reviewer even when filed from the repo
  itself (a uniform path is worth more than saving one hop).
- Register the two tool names in the repo `.claude/settings.json` allow list
  beside 22-03's.

## Testing

| # | Case | Expect |
|---|---|---|
| 1 | `resolve_project_key` from a real temp repo + `git worktree add` | worktree and main checkout yield the same key |
| 2 | non-git dir | realpath key, no crash without git |
| 3 | own-workspace `allowlist_add` | pattern appears once in `.claude/settings.json`; second call dedupes |
| 4 | foreign-workspace `allowlist_add` | queue entry in the **target's** key dir; caller's checkout untouched |
| 5 | worktree caller, foreign main-checkout target | entry lands under main-checkout key (the D7 point) |
| 6 | pattern colliding with deny | refused, deny entry named |
| 7 | malformed pattern | refused |
| 8 | `scope="user"` from a non-claude-hooks workspace | enqueued to claude-hooks key with the H6 note |
| 9 | `report_parser_issue` | entry of type `parser_issue`, atomic file, parseable |

## Done criteria

1. Suites green; counts reported.
2. Manual walkthrough from a real worktree of a scratch repo, literal outputs:
   cases 4 and 5 demonstrated on disk.
3. Reinstall note for the settings/tooling that ships via the installer.
4. No edits outside `permissions-mcp/`, the two new hooks modules, the router
   delegation, `.claude/settings.json`, `install-claude-config.sh` (only if
   registration needs the new env), and tests.
