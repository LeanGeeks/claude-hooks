# Implementation Report — 22-04 Allowlist writers + the proposal queue

## Summary

Agents can now promote permission patterns into **git-versioned** settings and
file parser issues, through one mechanism with two entry types. The caller's own
checkout is written directly (`.claude/settings.json`); every other project gets
an atomic queue entry under `~/.claude/permission-queue/<project-key>/` for the
scheduled reviewer that lives there — no agent ever touches a checkout it is not
running in. Project identity comes from a single new function,
`project_key.resolve_project_key`, under which **every worktree of a repo maps
to the main checkout** (brd D7), and the settings-file writer that used to live
inline in the Telegram router is now a shared module both callers use.

## Files Created

- `.claude/hooks/project_key.py` — the epic's **only** project-identity function
  (invariant 7). `resolve_project_root` (parent of `git rev-parse
  --path-format=absolute --git-common-dir` when its basename is `.git`, else
  `realpath(cwd)`), `encode_project_key` (`/` → `-`), `resolve_project_key`, and
  `resolve_workspace_root` (`git rev-parse --show-toplevel` — where *we* may
  write, deliberately not the same thing as the key). All subprocess use in this
  feature is confined here; results are memoized per input path for the process
  lifetime (the long-lived MCP server runs one `git` per distinct cwd).
- `.claude/hooks/settings_writer.py` — the generalized atomic writer:
  read-tolerate-corrupt → dedupe → tmp + rename, parameterized by file
  (`settings.json` / `settings.local.json`) and list (`allow` / `ask` / `deny`).
  Returns a `SettingsWriteResult` (`ok`, `added`, `path`, `error`) so a caller
  can tell "written" from "already there" while `ok` stays the router's boolean.
- `permissions-mcp/permission_queue.py` — the queue: root (env-overridable),
  per-project directories, the two entry builders (`allowlist_proposal`,
  `parser_issue` — the §2 schema), atomic `enqueue`, `list_entries`,
  `read_entry`, and `mark_processed` (move into `processed/`, never delete).
  It never computes a project key; callers hand it one from
  `resolve_project_key`.
- `tests/test_unit_allowlist_queue.py` — 36 tests covering all nine table cases
  plus the writer and queue contracts. Real temp git repos with real
  `git worktree add` for the worktree cases.

## Files Modified

- `permissions-mcp/permissions_mcp_lib.py` — new §"Allowlist writing + the
  proposal queue": pattern parsing/validation (`parse_pattern`), deny-collision
  detection (`find_deny_collision` / `_deny_subsumes`), `claude_hooks_repo()`,
  `allowlist_add()` and `report_parser_issue()`.
- `permissions-mcp/server.py` — registers the two new tools (`allowlist_add`,
  `report_parser_issue`) with agent-facing docstrings; module docstring now says
  six tools.
- `.claude/hooks/telegram_permission_router.py` — `update_settings_local_json`
  now delegates to `settings_writer.add_permission_pattern` (same file, same
  list, same dedupe, same output); the inline body is deleted, not left behind.
- `.claude/settings.json` — `mcp__permissions__allowlist_add` and
  `mcp__permissions__report_parser_issue` added beside 22-03's four.
- `install-claude-config.sh` — `settings_writer.py` and `project_key.py` added
  to `REQUIRED_HOOKS`. **This is the one edit outside the task's scope list**,
  and it is load-bearing: the installed router imports `settings_writer`, so
  without it the next install would ship a router that cannot import its writer.
  No new env var was needed — 22-03 already exports `CLAUDE_HOOKS_REPO` in the
  MCP registration.
- `tests/run_all_tests.py` — registers the new module as `unit_allowlist_queue`.

## Verification

- Compile/import: **PASS** — `python3 -m py_compile` on all six changed/created
  Python files; `import project_key, settings_writer, permission_queue,
  permissions_mcp_lib, telegram_permission_router` clean.
- Tests: **1020 passed, 0 failed** (1 skipped), `python3 tests/run_all_tests.py`.
  Baseline before this task was 984 → +36 new tests. No pre-existing unit
  failures.
  - `python3 tests/run_all_tests.py --quick` (scenario_check) reports one
    failure, `git push --force should return 'deny'`. **Pre-existing**: verified
    by `git stash -u` → same failure on untouched HEAD. It reads the developer's
    real merged settings, not a scratch fixture.
- Installed (`install-claude-config.sh` re-run): **no** — deliberately not run
  (see Reinstall note).

## Reinstall note (done criterion 3)

Nothing in this task is live outside this checkout yet. The manager needs to run
`./install-claude-config.sh` to:

1. copy `settings_writer.py` + `project_key.py` into `~/.claude/hooks/` — until
   then the **installed** `telegram_permission_router.py` is the old inline
   version (still working; it is simply stale, not broken);
2. merge the two new `mcp__permissions__*` grants from this repo's
   `.claude/settings.json` into the global one, so the tools can be called from
   other workspaces without a prompt.

The MCP server itself runs from this checkout (`uv run --script
permissions-mcp/server.py`), so the two new tools appear as soon as a session
restarts the server — the registration in `~/.claude.json` is unchanged.

## Manual walkthrough (done criterion 2) — cases 4 and 5, literal output

Script: two scratch git repos (`alpha`, `beta`), each with a real linked
worktree; the "agent" runs **from `alpha-feat`, a worktree of alpha**, with
`HOME`, `CLAUDE_PERMISSION_QUEUE_DIR` and the state store pointed at scratch
paths, and calls the same `lib.allowlist_add` the tool body calls.

```
=== layout ===
caller (a worktree of alpha): /tmp/22-04-walkthrough-LIv8/alpha-feat  [branch: feat-22-04]
case 4 target (beta main):    /tmp/22-04-walkthrough-LIv8/beta
case 5 target (beta worktree):/tmp/22-04-walkthrough-LIv8/beta-feat   [branch: beta-feat]

caller identity: CallerIdentity(session_id='walkthrough-session-2204', project_dir='/tmp/22-04-walkthrough-LIv8/alpha-feat', actor_agent='walkthrough- @ alpha-feat') 

=== case 4: foreign workspace (beta's MAIN checkout) ===
{
  "ok": true,
  "action": "queued",
  "pattern": "Bash(pytest:*)",
  "scope": "workspace",
  "target_project_key": "-tmp-22-04-walkthrough-LIv8-beta",
  "target_workspace": "/tmp/22-04-walkthrough-LIv8/beta",
  "path": "/tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta/20260826T132904Z_5445d751.json",
  "entry": {
    "type": "allowlist_proposal",
    "created_at": "2026-08-26T13:29:04.539127+00:00",
    "source": {
      "session_id": "walkthrough-session-2204",
      "actor_agent": "walkthrough- @ alpha-feat",
      "cwd": "/tmp/22-04-walkthrough-LIv8/alpha-feat"
    },
    "pattern": "Bash(pytest:*)",
    "scope": "workspace",
    "rationale": "beta's suite runs on every task; the prompt fires each time",
    "evidence_request_ids": [
      "req-abc123"
    ]
  },
  "status": "proposal queued for -tmp-22-04-walkthrough-LIv8-beta; the workspace's scheduled reviewer applies it (nothing is written in that checkout by this call \u2014 brd D6)",
  "notes": []
}

=== case 5: foreign WORKTREE of beta (D7: keyed by the main checkout) ===
{
  "ok": true,
  "action": "queued",
  "pattern": "Bash(npm run build:*)",
  "scope": "workspace",
  "target_project_key": "-tmp-22-04-walkthrough-LIv8-beta",
  "target_workspace": "/tmp/22-04-walkthrough-LIv8/beta-feat",
  "path": "/tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta/20260826T132904Z_016a7555.json",
  "entry": {
    "type": "allowlist_proposal",
    "created_at": "2026-08-26T13:29:04.541521+00:00",
    "source": {
      "session_id": "walkthrough-session-2204",
      "actor_agent": "walkthrough- @ alpha-feat",
      "cwd": "/tmp/22-04-walkthrough-LIv8/alpha-feat"
    },
    "pattern": "Bash(npm run build:*)",
    "scope": "workspace",
    "rationale": "every branch of beta builds the same way",
    "evidence_request_ids": []
  },
  "status": "proposal queued for -tmp-22-04-walkthrough-LIv8-beta; the workspace's scheduled reviewer applies it (nothing is written in that checkout by this call \u2014 brd D6)",
  "notes": []
}

=== queue on disk ===
/tmp/22-04-walkthrough-LIv8/queue
/tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta
/tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta/20260826T132904Z_016a7555.json
/tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta/20260826T132904Z_5445d751.json

--- /tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta/20260826T132904Z_016a7555.json
{
  "type": "allowlist_proposal",
  "created_at": "2026-08-26T13:29:04.541521+00:00",
  "source": {
    "session_id": "walkthrough-session-2204",
    "actor_agent": "walkthrough- @ alpha-feat",
    "cwd": "/tmp/22-04-walkthrough-LIv8/alpha-feat"
  },
  "pattern": "Bash(npm run build:*)",
  "scope": "workspace",
  "rationale": "every branch of beta builds the same way",
  "evidence_request_ids": []
}
--- /tmp/22-04-walkthrough-LIv8/queue/-tmp-22-04-walkthrough-LIv8-beta/20260826T132904Z_5445d751.json
{
  "type": "allowlist_proposal",
  "created_at": "2026-08-26T13:29:04.539127+00:00",
  "source": {
    "session_id": "walkthrough-session-2204",
    "actor_agent": "walkthrough- @ alpha-feat",
    "cwd": "/tmp/22-04-walkthrough-LIv8/alpha-feat"
  },
  "pattern": "Bash(pytest:*)",
  "scope": "workspace",
  "rationale": "beta's suite runs on every task; the prompt fires each time",
  "evidence_request_ids": [
    "req-abc123"
  ]
}
=== both foreign checkouts untouched (no .claude/, clean tree) ===
/tmp/22-04-walkthrough-LIv8/beta: .claude present=no | git status --porcelain: <empty means clean>
/tmp/22-04-walkthrough-LIv8/beta-feat: .claude present=no | git status --porcelain: <empty means clean>
/tmp/22-04-walkthrough-LIv8/alpha-feat: .claude present=no | git status --porcelain: <empty means clean>
```

Case 4 reads: a proposal aimed at `beta`'s main checkout is queued under
`-tmp-…-beta`, and `beta` is untouched. Case 5 is the D7 point: the proposal was
aimed at `/tmp/…/beta-feat`, a **worktree**, and the entry still landed in
`-tmp-…-beta` — the main checkout's key — not `-tmp-…-beta-feat`. Both foreign
checkouts (and the caller's own worktree) end with no `.claude/` directory and a
clean `git status`.

## Decisions

- **`resolve_workspace_root` is a second function, not a second identity.**
  The queue key is the main checkout; the place an agent may *write* is the
  checkout it is running in. For a worktree agent those differ, and invariant 4
  says the write goes to the worktree. Both live in `project_key.py` so all git
  invocation stays in one module, and only `resolve_project_key` is ever used as
  an identity.
- **Own-key targets always write the caller's own checkout.** If a worktree
  agent names its own repo's main checkout as `target_workspace`, the keys match,
  so per the task it is the direct-write path — but the write goes to *its own*
  worktree and the result says so in a note ("an agent never edits a checkout it
  is not running in"). Refusing invariant 4 was not an option; silently writing
  the named foreign directory would have violated it.
- **The deny check reads the target's *main checkout*.** A queued proposal is
  applied centrally (D7), so the main checkout's merged deny list is the one
  that decides whether the entry would ever fire. For own-workspace writes it
  reads our own checkout, since that is the file being written.
- **Deny collision means "deny fully covers the proposal".** Exact match, a
  bare-tool deny (`WebFetch` vs `WebFetch(domain:…)`), or — for `Bash` forms —
  the pattern's representative command (`Bash(git log:*)` → `git log`) matching
  the deny entry through `BashPermissionValidator._matches_pattern`, the
  validator's own matcher. A *narrower* deny (`Bash(git push:*)`) does not block
  a broader proposal (`Bash(git:*)`): that is normal layering, not a no-op.
  The matcher is used rather than `validate_bash_command` because the full
  validation path short-circuits on shapes like a workspace-local `rm` before it
  ever consults the deny list, and a collision missed that way would be written
  as a silent no-op allow entry.
- **`rationale` is required** (empty → refusal), mirroring 22-03's required
  `reason`. In `server.py` it is the second positional parameter because Python
  cannot put a required parameter after defaults; the lib keeps the task's
  argument order with an empty default it refuses.
- **Two malformed-input corners of the writer changed on purpose** (documented
  in the module docstring): a `permissions` key that is not an object, or a list
  that is not an array, now returns `False` **without writing** instead of
  raising `TypeError` at the caller — a hand-broken file is reported, not
  overwritten. Well-formed files are byte-identical to the old path, which the
  existing whitelist suites (`test_unit_whitelist.py`, `scenario_check.py`) still
  prove, plus a new test asserting the router still writes
  `settings.local.json` and never `settings.json`.
- **Queue root is `CLAUDE_PERMISSION_QUEUE_DIR`-overridable**, read at call time
  (not import time), following `permission_state_store`'s env-override
  convention. The tests set it to a scratch dir — required, since the default is
  the developer's real `~/.claude/permission-queue/`.
- **Temp entry files are dot-prefixed** (`.<name>.json.tmp`) so a concurrent
  drain globbing `*.json` cannot see a half-written entry even before the rename.
- **`mark_processed` ships here** rather than in 22-05 because the
  move-never-delete contract is part of this task's §2 queue format; 22-05
  imports it (with `resolve_project_key`) instead of re-deriving either.

## Blockers

None.

## Questions for User

None.
