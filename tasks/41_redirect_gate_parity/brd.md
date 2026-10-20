# Epic 41 — The write-redirect gate asks only about real escapes

**Status:** todo · **Owner:** Anton · **Created:** 2026-10-20 · **Rev:** 3
**Type:** this repo only (hooks, state store, permissions MCP, tests, docs; no
relay change, no `amux` change)

> Broken down into tasks — see [state.md](./state.md) for ordering, the shared
> edit points and the invariants.

**Provenance.** Filed 2026-10-20 from the operator's report: an auto-mode
session in `tradingtips` raised a Telegram card for
`echo '…' >> ~/.claude/projects/-data-sync-work-tradingtips/memory/MEMORY.md`
— a memory write the operator expected auto mode to let through. The operator
asked whether other auto-allowed calls are floored the same way. Everything in
§1 was read off this machine on that date from `~/.claude/bash_manual_confirm.log`
and by running the validator directly (repo and installed copies, nothing
executed; the quoting rows of §1.3 were also confirmed in bash 5.3 against
temp paths). Claims that were reasoned rather than measured are labelled
**unverified**.

## 1. Problem & thesis

### 1.1 The redirect gate is the only thing still flooring auto mode

Epic 40 made `pretool_hook.py` print nothing for an *unknown* command in
`auto` / `bypassPermissions` / `dontAsk` (`DEFERRING_MODES`,
`pretool_hook.py:449`), and kept two risk gates asking in every mode (epic 40
state.md invariant 4): a `permissions.ask` match and a write redirect whose
target escapes the workspace, `/tmp` or the `/dev` sinks
(`pretool_hook.py:703-735`). A hook `ask` outranks the mode.

The confirm log records `permission_mode` since 2026-09-21T17:51Z. From then to
2026-10-20T10:58Z it holds **279** entries in a deferring mode where the hook
emitted `ask`. **Every one is the redirect gate**; no `permissions.ask` match
fired in a deferring mode in that window. What each one cost differs by mode:

- **172 `auto`** — each a terminal prompt and a Telegram card, in a session
  that asked for neither. This is the operator's complaint.
- **107 `bypassPermissions`** — no prompt, no card: `permission_request_hook.py`
  auto-allows every PermissionRequest in a bypass session and records it
  (`:1653-1692`). In bypass mode the gate is an **audit trail, not a block**.

### 1.2 Why it fires — 365 refused targets in those 279 asks

| Targets | Cause | Fixed by |
|---|---|---|
| **181** | The target holds a shell variable or substitution the gate cannot resolve, with no literal anchor: loop variables (`for f …; > "$f.out"`), `x=$(mktemp); > $x`, `> "$S/.env"`. `_expand_constants` only resolves plain constant assignments. | 41-03 (D6: option (a)) |
| **87** | The target is outside the cwd's git repo. Mixed: real cross-repo writes, worktree → main-checkout writes, and writes inside a multi-repo launch directory (`acdn-workspace/resource-manager` → `acdn-workspace/agents_output/…`). The log does not record the launch directory, so the split is **not measurable**. | partly 41-02 (launch-dir share); the rest is the gate doing its job |
| **35** | Parser bug: a subshell's closing paren is glued to the target, so `(cmd 2>/dev/null)` refuses `/dev/null)`. | 41-01 |
| **28** | The cwd no longer exists (deleted worktrees), so the cause cannot be re-derived. The examples are relative writes from a subdirectory (`web/src` → `../index.html`), i.e. the cwd-narrowing below. | 41-02 |
| **27** | The project's auto-memory directory, `~/.claude/projects/<key>/memory/`. | 41-02 |
| **7** | Inside the cwd's repo but outside the cwd: the gate's "workspace" is the payload `cwd` (`resolve_workspace_dir`, `pretool_hook.py:380`), which follows the agent's `cd`. | 41-02 |

Whether the auto-mode classifier would have allowed each of these is **not
measurable** — it never got to vote.

### 1.3 Fail-opens found while diagnosing (security, every mode)

Measured 2026-10-20 with `permission_mode: default` and this repo's real
settings; group (i) also against the installed `~/.claude/hooks/` copy. Every
row below returns **`allow`** today. A command substitution's inner command is
validated on its own, so the payload has to be allowlisted — `echo`, `printf`,
`cat`, `tee` are. All predate epic 40.

**(i) The scanner reads the wrong target.** `_scan_write_targets`
(`bash_command_parser.py:2051`) takes **the next token** after a write redirect
as the whole target. A `CMD_SUBST` there yields its *inner text*; a word glued
from several tokens yields only its first piece.

| Command | Target recorded | Bash writes |
|---|---|---|
| `echo hi > $(echo ~/.bashrc)` | `echo ~/.bashrc`, read as a relative path | `~/.bashrc` |
| `ls >"$(echo /etc/x)"` | `echo /etc/x` | `/etc/x` |
| `echo hi > x$(echo y)/../../../../../home/anton/.bashrc` | `x` | `~/.bashrc` (if `xy/` exists) |

**(ii) Redirects inside a substitution that *is* the target are never
scanned.** The target branch `continue`s before the `CMD_SUBST` recursion
(`:2063-2065`): `echo hi > $(echo x > /etc/passwd)`, the backtick form, the
quoted `"$(…)"` form, and `> >(cat > /etc/x)` all pass, though the inner
redirect executes.

**(iii) Quoting is stripped once, not removed.** `_strip_quotes`
(`:2016`) removes one surrounding pair only, so the gate sees a different path
than bash: `> "/etc"/x`, `> ""/etc/x`, `> '/etc/x'y`, `> \/etc/x` (all write
`/etc/x`), `> "$HOME"/.bashrc`, `> ~/.bashrc"$V"` (write `~/.bashrc`). Unquoted
expansions the gate does not model either: `> {/etc/x,}` (brace expansion →
`/etc/x`), `> ~-/x` (`$OLDPWD/x`), `~+`, glob characters.

**(iv) The lenient fallback is weaker than its docstring.**
`_literal_prefix_inside_allowed_roots` (`pretool_hook.py:941`) vouches for an
unresolvable target whose literal prefix sits inside an allowed root. Its
docstring requires "no literal `..` path segment anywhere in the target"; the
code checks the prefix only. And the prefix is never tilde-expanded:
`> ~/.ssh/authorized_keys$V`, `> ~/.bashrc$V`,
`> .$V/../../../../../home/anton/.bashrc` all pass (`/etc/$V` correctly asks).

**(v) The hook's own settings are writable through the gate.**
`echo … > .claude/settings.local.json` is a write inside the workspace, so it is
`allow` in every mode. An agent can silently add `Bash(*)` to the allowlist the
hook reads — or, once 41-02 reads roots from settings, add a root. Same for
`.claude/settings.json` and `.claude/hooks/`.

### 1.4 Thesis

The gate exists to stop a known-escaping write (`> ~/.bashrc`,
`> ~/.claude/settings.json`) from going through on autopilot. It should
(a) see the target bash will actually write to, (b) use the same idea of
"in-bounds" Claude Code uses — minus anything that would let the agent widen
its own bounds, and (c) be honest about the targets it cannot resolve. Today it
fails on all three, in opposite directions: it lets escaping writes through and
stops memory writes.

## 2. Decisions

- **D1 — the scanner returns the target bash will use, or "unresolvable".**
  The target word is found by scanning the source from the redirect operand's
  offset to the end of the word (quotes, `$(…)`, backticks and `${…}` balanced),
  then **shell quote removal** is applied with quoted spans tracked. A word
  containing an unquoted command substitution, brace expansion, glob
  character, or `~+` / `~-` is reported in its source form and is unresolvable.
  Every `CMD_SUBST` inside a target is still recursed into for its own
  redirects, including process substitutions. (41-01)
- **D2 — a trailing `)` that closes an enclosing subshell is not part of the
  word**: strip `)` only when it is unbalanced within the word (`$(echo x)`
  keeps its paren). Quoted `")"` stays. (41-01)
- **D3 — the lenient fallback does what its docstring says**: for an
  *unresolvable* target, a literal `..` segment anywhere in it refuses it, and
  an unquoted leading `~` / `~/` is expanded before the containment check (an
  unexpandable `~user` refuses). Substitutions get the same lenient treatment
  as variables: an anchored `> /tmp/out_$(date +%s).txt` stays allowed, as
  today; the residual risk of a construct that expands to `../` is the one the
  code already accepts (`pretool_hook.py:924-929`). A target that fully
  resolves is judged on its realpath, `..` included (`> ../index.html` from a
  subdirectory is fine). (41-01)
- **D4 — protected paths inside the roots.** A write redirect into
  `<any root>/.claude/settings.json`, `<any root>/.claude/settings.local.json`
  or under `<any root>/.claude/hooks/`, and anything under `~/.claude/` other
  than the session's own memory dir, is a **risk** ask in every mode, even
  when the root contains it (a session launched in `$HOME`). (41-01)
- **D5 — allowed write roots mirror Claude Code's, from sources the agent
  cannot write**: the project root, the session's own auto-memory directory,
  and `additionalDirectories` — the last two read from **`~/.claude/settings.json`
  only** (project and local settings files are repo- or agent-writable; the
  CLI itself ignores `autoMemoryDirectory` in checked-in project settings).
  Not `~/.claude` as a whole and not other projects' memory directories (a
  session writing another project's memory is a cross-session prompt-injection
  path). Scope: the redirect gate and `rm`, not workspace-binary execution.
  (41-02)
- **D6 — operator answered 2026-10-20: option (a).** A target the gate cannot
  resolve *and* whose literal anchor is empty is an **unknown**: `ask_kind:
  'unknown'`, silent in `DEFERRING_MODES`, `ask` everywhere else. This includes
  command-substitution and brace-expansion targets (`> $(echo ~/.bashrc)` in
  auto mode goes to the classifier). In `bypassPermissions` the cost is the
  audit row only — the gate never blocked there (§1.1). A target that fully
  resolves outside the roots, whose literal anchor is outside, that is
  unresolvable and carries a literal `..`, or that hits a protected path (D4),
  stays a **risk** ask in every mode. "Resolves" uses `_resolve_target_path`
  (`expanduser` + `expandvars` with the hook's environment), so `$HOME/.bashrc`
  resolves and stays risk. Epic 40 invariant 4 is amended accordingly. (41-03)
- **D7 — one settings root for reads and writes**: `CLAUDE_WORKSPACE_DIR` →
  `CLAUDE_PROJECT_DIR` → `project_key.resolve_workspace_root(cwd)`, stored on
  the permission request row so the Whitelist button writes where the hook
  read. (41-05)
- **D8 — processes that re-run the validator use the row, not their own
  environment.** The router (card annotations, `telegram_permission_router.py:456`)
  and the permissions MCP (`permissions_mcp_lib.py:193`) re-derive the verdict
  later, in another process. They take the settings root (41-05) and the extra
  write roots (41-02) from the stored row, so the card describes the decision
  the hook actually made. (41-02, 41-05)

### 2.1 Rejected

- **Handing the whole redirect gate to the auto-mode classifier in deferring
  modes.** It would remove every prompt in §1.2, and the deterministic guard
  with it. The 87 outside-project row is mostly the gate working.
- **Renaming `.claude/` to fix H4.** `.claude/settings*.json` is where Claude
  Code reads project settings; the hook must read the same files. The defect
  is *which directory* is read, not the directory's name.
- **Allowlisting the memory write via `permissions.allow`.** Not possible: the
  parser strips redirect targets before rule matching, so no rule can vouch for
  a target.
- **41-03 options (b) and (c)** — (b) kept `$(…)` targets asking, (c) changed
  nothing. Recorded in 41-03 so they are not re-proposed without new evidence.
- **Honouring `additionalDirectories` from project / local settings** for
  parity with the CLI — those files are writable by the repo or the agent
  (§1.3 (v)), so a root named there could widen the gate silently.

## 3. Hazards

- **H1 — `CLAUDE_PROJECT_DIR` in the hook environment.** Documented by Claude
  Code as the project root for hooks; `roles_config.py:173` already reads it
  inside hooks and the permissions MCP relies on it
  (`permissions_mcp_lib.py:151-166`); **unverified** in the PreToolUse and
  PermissionRequest processes on the installed CLI. 41-05 logs what it sees and
  falls back when absent.
- **H2 — worktree memory key.** Observed: a session in
  `leads-platform--064-qa01` (transcript under
  `~/.claude/projects/-data-sync-work-tradingtips-leads-platform--064-qa01/`)
  wrote memory to `…-leads-platform/memory/`, the main checkout's key. So
  `dirname(transcript_path)/memory` alone misses worktrees. The CLI's key
  encoding maps more than `/` (observed: `dr_remi` → `dr-remi`,
  `/.worktrees/` → `--worktrees-`), so `project_key.encode_project_key`
  (`/` only) is not it. The main checkout derived from a git common dir is
  agent-influenceable (an agent can write a `.git` file inside the workspace),
  so it must be confirmed against the main checkout's own worktree list. 41-02
  specifies the derivation. CLI 2.1.304 also has an `autoMemoryDirectory`
  setting (custom location, ignored in checked-in project settings) and
  `autoMemoryEnabled`; 41-02 honours both from user settings.
- **H3 — `--add-dir`** directories are not in settings and, as far as known, not
  in the hook payload (**unverified**). Out of scope; recorded as a gap. Note
  `SettingsLoader`'s normaliser keeps only `permissions.allow/deny/ask`
  (`settings_loader.py:102-130`).
- **H4 — `SettingsLoader` reads `.claude/settings.json` from the `cwd`**
  (`settings_loader.py:36`). Measured 2026-10-20: after a `cd` into a
  subdirectory the project and local allow rules are not loaded, and the
  Telegram Whitelist button writes its rule into `<subdir>/.claude/`, where
  Claude Code never reads it. Fixed by 41-05 (D7).
- **H5 — memory writes in `default` mode.** The operator's premise is that
  Claude Code lets Write/Edit save into the auto-memory directory without a
  prompt; **unverified** here (the CLI exports an
  `isAutoMemPathSafeForCarveout`, which suggests it but proves nothing). Until
  41-04 verifies it, the memory root applies in `DEFERRING_MODES` only.
- **H6 — the redirect gate is not the only write path.** Allowlisted commands
  that write files (`tee`, `cp`, `sed -i`, interpreters) are governed by the
  allowlist, not by this gate; D4 closes the *redirect* route to the hook's
  settings only. Recorded, out of scope.
- **H7 — not live until `./install.sh --yes`.** At filing, the installed
  `pretool_hook.py` already differs from the repo (3585d41 not installed).
- **H8 — the repo's green-suite failure mode** (state.md invariant 6).

## 4. Acceptance

1. Every §1.3 command asks in `default` mode; none is ever `allow`. After
   41-03, those with an empty literal anchor (`> $(echo ~/.bashrc)`,
   `>"$(echo /etc/x)"`, `> {/etc/x,}`) are silent in deferring modes; the
   resolved-outside, anchored-outside, unresolvable-with-`..` and protected-path
   ones (`> "/etc"/x`, `> "$HOME"/.bashrc`, `~/.bashrc$V`, `.$V/../…`,
   `> .claude/settings.local.json`) ask in every mode. A redirect inside a
   target substitution (`> $(echo x > /etc/passwd)`) is scanned and asks.
   (41-01, 41-03)
2. `(ls 2>/dev/null) | grep a` raises no redirect ask (41-01).
3. In an auto-mode session: `echo x >> <own memory dir>/MEMORY.md` is silent;
   `echo x >> ~/.claude/settings.json` and `echo x >> <another project's memory
   dir>/MEMORY.md` still ask (41-02).
4. After a `cd` into a subdirectory, `> ../file` inside the project is silent
   (41-02).
5. After a `cd` into a subdirectory, a command allowed only by the project's
   `.claude/settings.json` is still allowed, and a Whitelist tap writes to the
   project root's `settings.local.json` (41-05).
6. A card raised by the harness in a deferring mode never claims the redirect
   gate asked (41-02, 41-03).
7. After install and a week of normal use, the deferring-mode redirect-ask
   tally is re-measured against §1.2 (41-04).
