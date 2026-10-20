# Task 41-05 — Settings are read and written at the project root, not the cwd

**Status:** todo · **Depends on:** none (41-02 depends on this) ·
**Implementer:** sonnet
**Read first:** [brd.md](./brd.md) H1, H4, D7, D8 · [state.md](./state.md)
invariants 6, 7 · `project_key.py` module docstring and
`resolve_workspace_root` (`:119`) · `permissions_mcp_lib.resolve_caller_identity`
(`permissions-mcp/permissions_mcp_lib.py:151`)

## Problem

Every hook-side caller builds `SettingsLoader` from the payload `cwd`, and the
payload `cwd` follows the agent's `cd`. Reproduced 2026-10-20 with a temp
project whose `.claude/settings.json` allows `Bash(zzprobe:*)`:

| cwd | `zzprobe run` |
|---|---|
| `proj/` | allow |
| `proj/sub/` | **ask** — project and local rules not loaded |

Claude Code keeps applying the project's rules after a `cd`; the hook does not.
In `default` mode that is extra prompts; in deferring modes it is mostly hidden
(unknown commands are silent since epic 40).

The write side is worse. The Telegram Whitelist button calls
`update_settings_local_json(request.cwd, …)`
(`telegram_permission_router.py:1172`), so a rule approved while the agent sat
in a subdirectory lands in `<subdir>/.claude/settings.local.json` — a file
Claude Code never reads and the hook reads only while the agent is in that exact
directory. One candidate on this machine:
`dr_remi/hipaa_form/frontend-react/.claude/settings.local.json` (a sub-directory
of the `hipaa_form` repo); whether the button created it or a session was
launched there is **not known**.

The permissions MCP already does this right: `allowlist_add` resolves
`CLAUDE_PROJECT_DIR` (falling back to `PWD`) and writes at
`resolve_workspace_root` of it (`permissions_mcp_lib.py:151-166`, `:1038`).

## Scope

1. **One resolver, in `project_key.py`** (the module that owns project identity):
   `resolve_settings_root(cwd, env=None) -> str`, in this order:
   1. `CLAUDE_WORKSPACE_DIR` — the existing override (tests, replay).
   2. `CLAUDE_PROJECT_DIR` — Claude Code's project root for hooks: the launch
      directory, which is where Claude Code itself reads project settings.
      `roles_config.py:173` already reads it inside hooks (precedent).
      **Verify it is set in the installed CLI's hook environment** (brd H1) —
      a debug-log line from a real PreToolUse and PermissionRequest
      invocation, main session and worktree session.
   3. `resolve_workspace_root(cwd)` — the git top level, else `realpath(cwd)`.
      Same fallback the MCP write path uses, so reads and writes agree. Log at
      debug level whenever this branch is taken.

   Do **not** use `resolve_project_root` (main checkout): a worktree's settings
   are its own (epic 22 invariant 4).
2. **Callers:**
   - `pretool_hook.py` `main()` (`:2163-2167`): the `SettingsLoader` gets the
     settings root. **Keep the validator's `workspace_dir` = `cwd`** — widening
     the redirect / `rm` roots is 41-02's, and keeping them apart lets each be
     reviewed alone.
   - `pretool_hook.py` `replay_from_log` (`:528-529`): today it builds one
     validator from `CLAUDE_WORKSPACE_DIR` / `getcwd()`. Keep that override
     first (it is D7's step 1); when it is unset, resolve per entry from the
     entry's logged `workspace` with no other env (replay has no session
     environment). A validator per distinct root, not one per entry.
   - Store the resolved root on the permission request row: a new optional
     `settings_root` field in `permission_state_store.py` next to `cwd`
     (`:114`, `create_request` `:286`), passed from `permission_request_hook.py`
     (the permission-row `create_request` call at `:1635`; the `:1344` call
     creates AskUserQuestion child rows, which never reach a settings write). Rows without it (older rows) fall back to
     `resolve_workspace_root(row.cwd)`. Check the store's serialisation and any
     schema/version handling for how an optional field is added elsewhere in
     this file — follow that precedent.
   - `telegram_permission_router.py` card validator (`:456`) and Whitelist write
     (`:1172`): use the row's `settings_root` (D7). These run later in another
     process, without the session's `CLAUDE_PROJECT_DIR` — which is why the
     root is stored, not re-derived.
   - `permissions_mcp_lib._build_validator` (`:193-203`): classify with the row's
     `settings_root` when present. Its second caller, `find_deny_collision`
     (`:953`), has no row and must keep working unchanged.
3. **No migration** of stray `settings.local.json` files. List what you find
   (`.claude/settings.local.json` below a git top level, under
   `/data/sync/work`) in the report and leave the decision to the operator.

## Tests

| Case | Expected |
|---|---|
| cwd = `proj/sub`, `CLAUDE_PROJECT_DIR=proj`, rule in `proj/.claude/settings.json` | allow |
| same, rule in `proj/.claude/settings.local.json` | allow |
| cwd = `proj/sub`, no `CLAUDE_PROJECT_DIR`, `proj` is a git repo | allow (git top-level fallback) |
| no env, not a repo | today's behaviour (`cwd`) |
| `CLAUDE_WORKSPACE_DIR` set | wins over both |
| worktree cwd, `CLAUDE_PROJECT_DIR` = the worktree | worktree's settings, not the main checkout's |
| Whitelist tap on a row with `cwd=proj/sub`, `settings_root=proj` | rule written to `proj/.claude/settings.local.json`; `proj/sub/.claude/` not created |
| Whitelist tap on an old row without `settings_root` | written at `resolve_workspace_root(cwd)` |
| permissions MCP classification of a row with `settings_root` | uses that root's rules |
| redirect gate after this task, cwd = `proj/sub`, `> ../x` | unchanged (still refused — 41-02 widens it) |

Mutation-prove the Whitelist write-location row and the first row.

## Docs

`architecture.md` wherever it says the hook reads settings from the workspace /
cwd, and the Whitelist-button description, if any.

## Done when

- Table passes; suite green against the baseline, skips by name.
- `agents_output/41-05_implementation_report.md` lists the observed
  `CLAUDE_PROJECT_DIR` values (or says the live check is owed after install)
  and the stray `settings.local.json` files found.
