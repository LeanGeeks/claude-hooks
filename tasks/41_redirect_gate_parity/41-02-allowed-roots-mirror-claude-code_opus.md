# Task 41-02 — Allowed write roots mirror Claude Code's

**Status:** todo · **Depends on:** 41-01, 41-05 · **Implementer:** opus
(cross-cutting security change: gate, state store, router, permissions MCP)
**Read first:** [brd.md](./brd.md) §1.2, §1.3 (v), D4, D5, D8, H2, H3, H5 ·
[state.md](./state.md) invariants 2–6 · 41-05's report (the resolver and the
`settings_root` row field it added) · 41-01's report (protected paths)

## Goal

The redirect gate and the `rm` check call `_allowed_roots`
(`pretool_hook.py:884`): today the payload `cwd` and `/tmp`. Widen it to what
Claude Code itself treats as in-bounds — from sources the agent and the repo
cannot write — and nothing more.

### 1. Project root

41-05's `project_key.resolve_settings_root`, in addition to the `cwd`. Do not
resolve it a second way. A session that `cd`'d into `web/src` can then write
`../index.html`.

### 2. The session's own auto-memory directory

- **Base case:** `dirname(transcript_path)/memory`, when `transcript_path` has
  the `~/.claude/projects/<key>/<id>.jsonl` shape. Anything else (missing,
  malformed, a deeper subagent transcript) → no memory root. `transcript_path`
  is in real PermissionRequest payloads (`permission_request_debug.log`); there
  is no local evidence yet for PreToolUse — log what pretool receives.
- **Worktree case (brd H2):** a worktree session's memory lives under the
  *main checkout's* key. Derive it only when all of these hold, else add no
  extra root:
  1. the main checkout is `project_key.resolve_project_root(<project root>)`
     and differs from the project root;
  2. the project root appears in `git -C <main> worktree list --porcelain` —
     the registration lives in the main checkout's `.git`, which an agent in
     the worktree cannot write silently, unlike the worktree's own `.git` file;
  3. the key encoding is **self-checked**: encode the project root with the
     encoding you implement and require it to equal
     `basename(dirname(transcript_path))`. Observed CLI behaviour maps at least
     `/`, `.` and `_` to `-` (`dr_remi` → `dr-remi`, `/.worktrees/` →
     `--worktrees-`); `project_key.encode_project_key` maps `/` only and must
     not be changed (it is the queue key). If the self-check fails, the
     encoding is wrong for this path — add no worktree root.
- **`autoMemoryDirectory` / `autoMemoryEnabled`:** CLI 2.1.304's settings
  schema has both ("Custom directory path for auto-memory storage. Supports ~/
  prefix … Ignored if set in projectSettings"). Read them from
  **`~/.claude/settings.json` only** (D5; project and local files are
  repo- or agent-writable). An override replaces the default dir;
  `autoMemoryEnabled: false` → no memory root.
- **Mode (H5):** until 41-04 verifies that Claude Code writes memory without a
  prompt in `default` mode, add the memory root in `DEFERRING_MODES` only.
  Compute the roots in `main()` (or the helper below) from the payload's mode
  and pass the resulting list to the validator — the validator itself stays
  mode-unaware (epic 40 invariant 2: mode translation lives in `main()`).
- **Carve-out from 41-01's protected paths:** the memory dir is the one place
  under `~/.claude/` the gate may allow. Nothing else under `~/.claude/` and
  nothing under a *sibling* key.

### 3. `additionalDirectories`

From `~/.claude/settings.json` `permissions.additionalDirectories` only (D5,
brd §2.1). Absolute entries and `~/` entries only; ignore relative ones.
`SettingsLoader._normalize_to_modern_format` (`settings_loader.py:102-130`)
drops the key today — either read the global file directly in the helper or
extend the loader so the three existing lists are unchanged and the new value
carries its source. `--add-dir` is out of scope (H3).

## One computation, stored on the row (D8)

The router re-runs the validator to annotate the card (`_bash_annotations`,
`telegram_permission_router.py:424-495`) and the permissions MCP re-runs it to
classify (`permissions_mcp_lib._build_validator`, `:193-203`). Both run later,
in other processes, without the payload. If they derive roots differently from
pretool, a card lists a memory-dir write as "outside the workspace" for a call
pretool allowed (the misattribution epic 40 brd H4 removed).

- One helper computes the extra roots from `(payload, env, mode)`.
  `pretool_hook.main()` uses it; `permission_request_hook.py` calls the same
  helper on its own payload (same `transcript_path` / `cwd` / env / mode) and
  stores the result as an optional `write_roots` list on the row next to
  41-05's `settings_root`, following the same store precedent.
- `BashPermissionValidator` takes the extra roots as a constructor argument
  (default: none — today's behaviour). Router and MCP pass the row's
  `write_roots`; rows without it get none.
- `_build_validator` has a second caller with no row —
  `find_deny_collision` (`permissions_mcp_lib.py:953`). It must keep working
  with no extra roots.
- `replay_from_log` (`pretool_hook.py:528`): log entries carry no
  `transcript_path`, so replay gets the project root (41-05) and no memory
  root. Say so in its help text.

## Out of scope

- **Workspace-binary execution** (`_is_workspace_binary`, `:769`, which has its
  own root list at `:814`) keeps workspace + `/tmp` only. If you refactor it to
  share `_allowed_roots`, give it the narrow set explicitly.
- **Where settings are read from** — 41-05.
- **Unresolvable-target classification** — 41-03.

## Tests

Temp dirs for project root, cwd, a fake `HOME` with
`~/.claude/projects/<key>/` (transcript file inside) and a sibling
`<key2>/memory/`, and a real temp git repo + worktree for the worktree rows.

| Case | Mode | Expected |
|---|---|---|
| `echo x >> <own memory>/MEMORY.md` | auto | no redirect ask |
| same | default | ask (H5: deferring modes only until verified) |
| `echo x >> <sibling key>/memory/MEMORY.md` | auto | ask / risk |
| `echo x >> ~/.claude/settings.json` | auto, bypassPermissions | ask / risk |
| `echo x >> <own key>/other.jsonl` (outside `memory/`) | auto | ask / risk |
| registered worktree: `echo x >> <main key>/memory/a.md` | auto | no redirect ask |
| worktree whose `.git` file points at a repo that does not list it | auto | ask (no extra root) |
| encoding self-check fails | auto | ask (no extra root) |
| cwd = `<root>/web/src`, `ls > ../index.html`, `CLAUDE_PROJECT_DIR=<root>` | auto | no redirect ask |
| same, `CLAUDE_PROJECT_DIR` unset, root is a git repo | auto | no redirect ask (41-05's fallback) |
| same, no env, not a repo | auto | ask (today's behaviour) |
| target inside a global `additionalDirectories` entry | auto | no redirect ask |
| same entry only in project or local settings | auto | ask (ignored) |
| `autoMemoryDirectory` in `~/.claude/settings.json` | auto | that dir is the memory root; default dir is not |
| `autoMemoryDirectory` only in project / local settings | auto | ignored |
| `autoMemoryEnabled: false` | auto | no memory root |
| `rm <own memory>/old.md` | auto | allowed by the rm check |
| `<own memory>/tool` as a binary | any | not a workspace binary |
| malformed / missing `transcript_path` | auto | no memory root, no crash |
| router card for a row with `write_roots` = own memory, command also has an unknown sub-command | auto | 📝 section does not list the memory target |
| permissions MCP classification of the same row; `find_deny_collision` with no row | — | memory target not refused; collision check unchanged |

Mutation-prove the sibling-memory, `~/.claude/settings.json`, unregistered-worktree
and project-settings-`additionalDirectories` rows.

## Docs

`architecture.md:112` (the PreToolUse row names the allowed write roots) and
any other place that says "workspace, `/tmp`" for the redirect gate or `rm`.

## Done when

- Table passes; suite green against the baseline, skips by name.
- `agents_output/41-02_implementation_report.md` records the `transcript_path`
  seen in a PreToolUse payload (or that the live check is owed), the observed
  key-encoding pairs used for the self-check, and the H5 status.
