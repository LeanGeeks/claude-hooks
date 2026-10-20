# Epic 41 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, tests and done criteria. This file owns **ordering**, the **shared
edit points** and the **cross-task invariants**.

**No Phase 0.** The one cross-cutting decision — how unresolvable write targets
are classified — was answered by the operator on 2026-10-20 (option (a), brd
D6) and is locked in 41-03.

**Numbering.** Highest existing epic is 40; this is 41. It amends epic 40's
state.md invariant 4 (41-03 does the edit).

**No epic `architecture.md`.** This epic changes which roots one gate accepts
and where settings are read from — no new structure. The repo-root
`architecture.md` is the relevant document; 41-02, 41-03 and 41-05 update it.

## Tasks

Run in table order, one at a time. The implementer model is the filename
suffix.

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 41-01 | [The gate reads the real target](./41-01-the-gate-reads-the-real-target_opus.md) | todo | — | **Security fix first.** Five fail-open groups in every mode (brd §1.3): substitution and glued targets, unscanned inner redirects, quoting, the lenient fallback's `~` / `..`, and redirects into the hook's own settings. Also the `/dev/null)` paren bug. Its report carries the testing baseline. |
| 41-05 | [Settings follow the project root](./41-05-settings-follow-the-project-root_sonnet.md) | todo | — | One settings-root resolver (`CLAUDE_PROJECT_DIR` → git top level) for the hook's reads and the Whitelist button's writes; `settings_root` stored on the request row. Added after filing, hence the number. |
| 41-02 | [Allowed write roots mirror Claude Code](./41-02-allowed-roots-mirror-claude-code_opus.md) | todo | 41-01, 41-05 | Project root, own memory dir (registered-worktree key with an encoding self-check, user-level `autoMemoryDirectory`; deferring modes only until H5 is verified), user-level `additionalDirectories`; `write_roots` stored on the row for the router and MCP. Redirect gate + `rm` only. |
| 41-03 | [Unresolvable targets are unknowns](./41-03-unresolvable-targets-are-unknowns_opus.md) | todo | 41-01, 41-02 | Operator answered (a). 181 of 365 refused targets. Card wording, MCP human-only tier. Amends epic 40 invariant 4. |
| — | `./install.sh --yes` | todo | 41-03 committed | The orchestrator runs it once all four are committed (never `--all`). Not an agent task. |
| 41-04 | [Re-measure and live checks](./41-04-remeasure-and-live-checks_human.md) | todo | install | **human** — live checks H1/H2/H5 and the Whitelist-from-subdir check right after install; the tally after a week. Leave `blocked — awaiting human` once install has run. |

## Dependency graph

```
41-01 ──┬──► 41-02 ──► 41-03 ──► install ──► 41-04 (human)
41-05 ──┘
```

- 41-01 first: 41-02 and 41-03 reason about "the target", and until 41-01 lands
  that is sometimes the wrong string.
- 41-05 is independent of 41-01; 41-02 needs it because its project root is
  41-05's resolver and its `write_roots` row field sits next to 41-05's
  `settings_root`.
- 41-03 after 41-02: its bucket rule asks "is the literal anchor inside the
  allowed roots", and 41-02 changes the roots.

## Shared edit points

| File | 41-01 | 41-05 | 41-02 | 41-03 |
|---|---|---|---|---|
| `bash_command_parser.py` `_scan_write_targets` (`:2051`) | target reassembly, `)` | — | — | — |
| `pretool_hook.py` `_literal_prefix_inside_allowed_roots` (`:941`), `_is_redirect_target_allowed` (`:912`) | `..`, `~`, protected paths | — | memory carve-out | outcome function beside it |
| `pretool_hook.py` `_allowed_roots` (`:884`), validator constructor (`:592`) | — | — | extra roots | — |
| `pretool_hook.py` `main()` (`:2163-2167`), `replay_from_log` (`:528`) | — | settings root | extra roots from payload | — |
| `pretool_hook.py` `validate_bash_command` decision block (`:709-757`) | — | — | — | precedence, `unresolved_redirect_targets` |
| `project_key.py` | — | `resolve_settings_root` | — | — |
| `settings_loader.py` normaliser (`:102`), or a direct read of `~/.claude/settings.json` | — | — | user-level `additionalDirectories`, `autoMemory*` | — |
| `permission_state_store.py`, `permission_request_hook.py` (`:1636`) | — | `settings_root` | `write_roots` | — |
| `telegram_permission_router.py` validator (`:456`), Whitelist (`:1172`) | — | row's `settings_root` | row's `write_roots` | — |
| `telegram_permission_router.py` `BashAnnotations` / card (`:379-670`) | — | — | — | unresolved heading |
| `permissions-mcp/permissions_mcp_lib.py` `_build_validator` (`:193`; second caller `find_deny_collision` `:953`), tiering (`:326-340`) | — | row's `settings_root` | row's `write_roots` | unresolved → human-only |
| `architecture.md` (`:112` and settings mentions) | — | ✓ | ✓ | ✓ |
| epic 40 `state.md` invariant 4 + Log | — | — | — | ✓ |
| `tests/test_integration_pretool.py` `TestRedirectTargets` (`:1450`) | ✓ | — | ✓ | ✓ |

Line numbers are as of 3585d41; each task shifts the next one's. Re-locate by
name.

## Testing baseline

At 3585d41 (2026-10-20): `python3 tests/run_all_tests.py` → **2010 tests,
`OK (skipped=2)`** (from that commit's message, not re-run at filing). 41-01's
implementer re-measures on a clean tree before the first edit and puts the
result in its report; **the orchestrator copies it here**. Later tasks report
against that number.

## Cross-task invariants

1. **Deny is unconditional** (epic 40 invariant 1, unchanged).
2. **Never vouch for what the gate cannot read.** No change in this epic may
   turn a target the gate does not fully understand into an `allow`. An
   unresolvable target with no literal anchor is at most *silent in a
   deferring mode* (41-03).
3. **Known-escaping writes ask in every mode.** A target that resolves outside
   the allowed roots, hits a protected path (brd D4), or is unresolvable with a
   literal anchor outside the roots or a literal `..`, emits `ask` in every
   mode, `bypassPermissions` included (where it is an audit row: the
   PermissionRequest hook auto-allows it, brd §1.1). A *resolved* `..` is
   judged on its realpath.
4. **Allowed roots are exact, and the agent cannot widen them.** Own project
   root, own memory dir (or `autoMemoryDirectory`), `additionalDirectories`,
   `/tmp` — the last three read from `~/.claude/settings.json` only, never
   from project or local settings files (repo- or agent-writable). Never
   `~/.claude` (except the own memory dir), never `~/.claude/projects`, never a
   sibling project's memory dir, never `<root>/.claude/settings*.json` or
   `<root>/.claude/hooks/`, even when a root contains them.
5. **Telegram mirrors the terminal** (epic 40 invariant 5). Processes that
   re-run the validator use the row's stored roots, never their own
   environment (brd D8).
6. **A green suite is not evidence on its own.** Mutation-prove the rows each
   task names: copy the file aside with `cp`, break the behaviour, confirm the
   test fails, restore from the copy. **Never restore with `git checkout`** —
   it reverts to HEAD and destroys uncommitted work. Reviewers run
   `git status` before and after any mutation and confirm the tree matches.
   If a requirement is awkward to test, say so in the report rather than
   weakening it.
7. **Shared checkout.** Other epics may be in flight: no repo-wide git
   operations (`git stash`, `git checkout .`, `git add -A`, `git commit -a`);
   committers stage only the files the task touched. Task reports under
   `agents_output/` are gitignored but tracked — commit them with
   `git add -f`.
8. **A repo edit is not live** until `./install.sh --yes` (never `--all`). Say
   which copy you tested. Live checks that need the installed copy are listed
   as owed in the report; 41-04 closes them.

## Log

- **2026-10-20 — fresh-context review; rev 3.** An independent reviewer
  reproduced brd §1.3 and the §1.2 tally, and found more fail-opens, all
  confirmed: quoting only stripped once (`> "/etc"/x`, `> "$HOME"/.bashrc`,
  `> {/etc/x,}`), redirects inside a target substitution never scanned
  (`> $(echo x > /etc/passwd)`), and the hook's own settings writable through
  the gate (`> .claude/settings.local.json` → `allow`). All now in 41-01.
  Also: the `..` rule now applies to unresolvable targets only;
  `additionalDirectories` / `autoMemoryDirectory` come from user settings only
  (an agent could otherwise widen its own roots); the worktree memory root
  needs a registered worktree and a key-encoding self-check; the memory root
  ships for deferring modes only until H5 is verified, computed in `main()` so
  the validator stays mode-unaware; 41-03 gains the MCP human-only tier and the
  card branch for an allowlisted command; 41-04's tally read `emitted` as a
  dict (it is a string). The reviewer's request to re-confirm (a) for
  `bypassPermissions` was resolved without the operator: the PermissionRequest
  hook already auto-allows every request in bypass mode, so the gate is an
  audit trail there and (a) costs only the row; `$HOME/.bashrc` resolves
  through `expandvars` and stays risk. 41-02 and 41-03 moved to opus.

- **2026-10-20 — review pass before handover; rev 2.** Operator chose 41-03
  option (a). The review found: two more fail-opens in the lenient fallback
  (`~` never expanded, `..` checked in the prefix only — brd §1.3 (ii)), now in
  41-01; 41-01's `x$(echo y)` row contradicted the documented lenient design
  (now `allow`, anchored inside); the router and permissions MCP re-run the
  validator without the payload, so 41-02 stores `write_roots` on the row
  (D7); `SettingsLoader` drops `additionalDirectories`; the CLI's
  `autoMemoryDirectory` / `autoMemoryEnabled` settings; (a) needed a precise
  bucket rule (anchored-outside and `..` stay risk) and card wording that does
  not claim the gate asked; 41-04's tally filtered on the reason prefix, which
  (a) changes; model suffixes, the install step and 41-03 → after 41-02 were
  missing. Files renamed for the suffixes.
- **2026-10-20 — 41-05 added.** The operator asked how to fix brd H4 (settings
  read from the cwd); renaming `.claude/` was considered and rejected (brd
  §2.1). The fix reuses `project_key.resolve_workspace_root` as the fallback so
  the hook's reads agree with the permissions MCP's existing writes
  (`permissions_mcp_lib.py:1038`), and stores the root on the request row for
  the Whitelist button, which runs later in another process.
- **2026-10-20 — filed, spec only.** From the operator's memory-write card in
  an auto-mode `tradingtips` session. Diagnosis in brd §1; the `$(…)` fail-open
  (brd §1.3) was found while checking whether the paren fix was safe. No code
  was changed: the parser fix is security-sensitive and the roots change
  touches `rm` and settings resolution, so both go through the normal
  implement → review → mutation-proof loop.
