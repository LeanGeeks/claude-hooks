# Epic 29 — Architecture

Read [brd.md](./brd.md) first. This file specifies the internal structure of the
rewritten `install-claude-config.sh`: the feature registry, the manifest, the
probe/execute engine, the external-surface seam, and how any of it gets tested.

**Language and dependencies are unchanged.** Bash 4+, `jq`, `python3`. No
`dialog`, no `whiptail`, no new runtime dependency (brd §9). The script keeps its
current shape as a single file — splitting it into a `lib/` tree is a bigger
refactor than this epic needs and would break the "curl one script" property.

## 1. Component map

```
install-claude-config.sh
├── preamble            deps check, colors, logging, HOUSEKEEPING (always)
├── registry            §2  one verb-set per feature + the module-owner map
├── manifest            §3  ~/.claude/install-manifest.json read/write
├── probe               §4  detected state per feature (disk, never manifest)
├── plan                §5  selector (TTY) or flags (non-TTY) → the plan
├── execute             §6  install / update / uninstall per feature
│   ├── modules         §6.3 dependency closure + refcounted removal
│   ├── settings        §7  ownership-scoped jq merges
│   └── external        §8  bashrc / crontab / tmux / systemd — the SEAM
└── report              summary, failures, backup location
```

Order is load-bearing and matches brd D6: **probe → plan → confirm → execute →
report.** Nothing prompts during `execute`.

### 1.1 Two files, for the duration of the epic only

29-02 §0 copies `install-claude-config.sh` to `install.sh` and freezes the
original. Everything this document describes is built in `install.sh`; the frozen
copy is untouched pre-epic code, kept working so that the project's standing
instruction — *"re-run `install-claude-config.sh` to make a hook edit live"* —
stays safe while the replacement is under construction, and so that 29-09's
migration test has a real pre-epic fixture rather than an invented one. It also
keeps every task's line-number citation into the old script valid for the whole
epic.

`install.sh` carries a temporary guard refusing to run against the real `$HOME`
without `CLAUDE_INSTALL_EPIC29_LIVE=1`. 29-09 §6 removes the guard, collapses the
two files back into one, and settles the final filename. This subsection goes
with them.

## 2. The feature registry

Each feature is a set of shell functions sharing an id prefix. This is the
registry — there is no separate table to drift from it.

```bash
# Presentation order. Also the execution order (dependencies come first).
FEATURES=(
    statusline
    permission-hooks
    telegram
    profiles
    amux
    permissions-allowlist
    context-mcp
    claude-history
    questions
    daily-review
)

feature_<id>_title()      # one line, shown in the checklist
feature_<id>_writes()     # one line: what it writes outside ~/.claude, or ""
feature_<id>_default()    # install | skip     (brd D14)
feature_<id>_requires()   # space-separated feature ids, or ""
feature_<id>_modules()    # hook .py basenames this feature needs
feature_<id>_probe()      # exit 0 if installed on this machine
feature_<id>_install()    # idempotent apply
feature_<id>_uninstall()  # remove what this feature owns
feature_<id>_suboptions() # space-separated sub-toggle ids, or ""
```

`profiles` precedes `amux` in `FEATURES` because `amux-spawn.bash` sources
`claude-profiles.bash` (brd §4). `permission-hooks` precedes `telegram` for the
same reason. The registry order must satisfy every `_requires()` — 29-02 asserts
this at startup rather than trusting the author.

**Sub-toggles** are not features. They are options on a feature, carried in the
manifest under that feature's `options`, and they are what `enable`/`disable`
address (brd D15):

| Sub-toggle id | Feature | Effect |
|---|---|---|
| `amux-autowrap` | `amux` | `.bashrc` line sourcing `amux-spawn.bash` |
| `profiles-autosource` | `profiles` | `.bashrc` line sourcing `claude-profiles.bash` |
| `questions-listen` | `questions` | `systemctl --user enable --now` + linger |
| `daily-review-cron` | `daily-review` | the crontab line |

`amux-autowrap` and `profiles-autosource` are **mutually exclusive** —
`amux-spawn.bash` sources the profiles snippet internally, and sourcing both
double-loads it (`install-claude-config.sh:491`: *"Do NOT source both"*).
Enabling one disables the other, and says so.

### 2.1 Shared modules and the owner map

Per brd constraint 2.1, hook `.py` files are installed as the **dependency
closure** of the selected features, not as a per-feature copy. The map is
explicit so `_uninstall` can refcount (brd D4):

```bash
declare -A MODULE_OWNERS=(
    [bash_command_parser.py]="permission-hooks telegram"
    [settings_loader.py]="permission-hooks telegram"
    [pretool_hook.py]="permission-hooks"
    [permission_request_hook.py]="permission-hooks"
    [session_yolo_store.py]="permission-hooks"
    [permission_state_store.py]="permission-hooks telegram amux"
    [project_key.py]="permission-hooks telegram"
    [telegram_permission_router.py]="telegram"
    [posttool_hook.py]="telegram"
    [notification_hook.py]="telegram"
    [reply_injector.py]="telegram"
    [settings_writer.py]="telegram"
    [roles_config.py]="telegram questions"
    [amux_spawn_lib.py]="amux profiles"
    [lifecycle_events.py]="amux"
    [claude_event_reducer.py]="amux"
    [codex_event_reducer.py]="amux"
    [spawn_producer_hook.py]="amux"
    [questions_store.py]="questions"
    [questions_listen_lib.py]="questions"
)
```

A module is copied if **any** selected feature owns it, and removed only when
**no** still-installed feature owns it. 29-02 asserts this map covers exactly
`REQUIRED_HOOKS` — a module in one and not the other is a startup error, which is
how a future hook file avoids being silently dropped.

> `amux_spawn_lib.py` is owned by `profiles` as well as `amux` because
> `claude-profiles.bash` reads it to generate the profile functions
> (`amux_spawn_lib.py:1382`). Uninstalling `amux` while keeping `profiles` must
> leave it in place.

## 3. The manifest — `~/.claude/install-manifest.json`

Records **intent**, never truth (brd D2). Written after a successful run,
rewritten in full each time.

```json
{
  "schema": 1,
  "repo": "/data/sync/work/leangeeks-ai/claude-hooks",
  "revision": "da66886",
  "updated_at": "2026-09-10T12:00:00Z",
  "features": {
    "statusline": {
      "state": "installed",
      "at": "2026-09-10T12:00:00Z",
      "artifacts": [
        "~/.claude/statusline/statusline.py",
        "~/.claude/statusline/subagent.py",
        "~/.claude/statusline/pricing.default.json"
      ],
      "settings_keys": ["statusLine", "subagentStatusLine"],
      "options": {}
    },
    "permissions-allowlist": {
      "state": "installed",
      "options": { "mode": "union" },
      "added_allow": ["Bash(git status:*)", "..."],
      "added_deny": []
    },
    "amux": { "state": "installed", "options": { "amux-autowrap": true } },
    "questions": { "state": "skipped", "options": { "questions-listen": false } }
  }
}
```

`state` is one of `installed` | `skipped` | `failed`. `artifacts` is what makes
uninstall exact — it is written by `_install`, not guessed by `_uninstall`.
`added_allow` / `added_deny` record what a **union** merge contributed, so
uninstall subtracts exactly those and leaves hand-added patterns alone (§7.2).

A manifest whose `schema` is unknown is ignored with a warning, not parsed
optimistically. A manifest whose `repo` differs from `SCRIPT_DIR` is honoured but
warned about — the checkout moved, and `daily-review` and the MCP
registrations embed absolute paths that are now stale.

## 4. Probing — detected state

`feature_<id>_probe` answers "is this installed **on this machine**", reading only
the disk. Never the manifest (brd D2, constraint 2.3). Probes are cheap,
side-effect-free, and must not require the manifest, the repo, or network.

Representative probes:

| Feature | Probe |
|---|---|
| `statusline` | `~/.claude/statusline/statusline.py` exists **and** `settings.json` has `.statusLine.command` pointing into it |
| `permission-hooks` | `settings.json` has a `hooks.PreToolUse` entry whose command references `$GLOBAL_HOOKS_DIR` |
| `telegram` | `~/.claude/hooks/telegram_permission_router.py` exists **and** `hooks.PostToolUse` references it |
| `amux` | `~/.local/bin/amux-spawn` exists **and** `hooks.Stop` references `spawn_producer_hook.py` |
| `profiles` | `~/.claude/shell/claude-profiles.bash` exists |
| `permissions-allowlist` | `settings.json` `permissions.allow` is non-empty |
| `questions` | `~/.claude/bin/questions-listen` exists |
| `daily-review` | a crontab line carrying the marker exists |

**Files alone are not enough.** A machine can carry `statusline.py` from an old
run with the `statusLine` key since removed by hand; that is *not* installed, and
`Update` must re-wire it. Every probe checks the artifact **and** its wiring —
this is what makes success criterion 6 (old-script machines re-run to an
identical end state) reachable.

Sub-toggle probes are separate and equally concrete: `amux-autowrap` is "the
marked line is present in `~/.bashrc`"; `questions-listen` is
`systemctl --user is-enabled` (§8.4); `daily-review-cron` is the marked crontab
line.

## 5. The plan

### 5.1 Interactive (TTY)

A numbered checklist, redrawn on each toggle. Not a full-screen TUI — printed,
scrollable, SSH-safe.

```
Claude Code configuration — what should this machine run?

  1 [Install ] Statusline (+ per-subagent rows)
  2 [Install ] Permission hooks (bash guard, approvals, /yolo)
  3 [Update  ] Telegram integration
                 writes: python user-site .pth, ~/.local/bin/claude-roles
  4 [Install ] Model profiles
  5 [Update  ] amux integration
                 writes: /usr/local/bin/amux (sudo), ~/.tmux.conf, ~/.local/bin
       5a [ ] auto-wrap sessions in amux        (edits ~/.bashrc)
  6 [Install ] Global permissions allowlist  → union
  7 [Install ] MCP: context-usage
  8 [Install ] claude-history
  9 [Skip    ] Async questions
       9a [ ] enable listener daemon           (systemd + linger)
 10 [Skip    ] Daily permission review
      10a [ ] write crontab line               (crontab)

  <n> cycle state · <n>a toggle sub-option · a all · s skip all
  p preview plan · Enter accept · q quit
```

Cycling order depends on detected state (brd D1):

- not installed → `Install` ⇄ `Skip`
- installed → `Update` → `Keep` → `Uninstall` → `Update`

A feature whose `_requires()` is unmet cannot be set to `Install`; selecting it
selects its prerequisites and says so on one line. Setting a prerequisite to
`Uninstall` while a dependent is installed is refused with the dependent named.

`p` prints the same plan `--dry-run` prints (§5.3). `Enter` shows the plan and
asks once for confirmation. That confirmation is the only gate — after it, the
run is unattended (brd D6), which matters because `amux` can block for 15
minutes (constraint 2.6).

### 5.2 Non-interactive

`--yes` with a manifest replays it. `--yes` with `--only`/`--with`/`--without`
uses those. `--yes` with neither a manifest nor a selection is an **error** — brd
§5 forbids "install everything" from being reachable by accident. No TTY and no
`--yes` is likewise an error, not an implicit `--all`.

"Replays it" is exact and load-bearing: each feature's action comes from its
manifest `state`, and `state: "skipped"` stays skipped. The D14 defaults are
consulted only for a feature id the manifest does not mention at all — a feature
added to the registry since that manifest was written — and those are reported as
newly-offered rather than folded in. A `--yes` run must never be able to install
something the user declined.

That guarantee has a non-human consumer. Once `daily-review-cron` is enabled, the
scheduled reviewer runs the installer unattended at 06:15 to propagate user-scope
permission changes (`docs/prompts/permission-review-daily.md` step 3). A defaults
fallback there would silently install every declined feature on a machine nobody
is watching; the error above is what makes that case fail loudly instead.

### 5.3 The plan object

Whatever produced it, the plan is the same structure: an ordered list of
`(feature, action, options)` with `action ∈ {install, update, keep, uninstall}`,
plus the derived module closure and the derived external-surface writes.
`--dry-run` renders it and exits 0 without touching anything — including without
writing the manifest.

## 6. Execution

### 6.1 Per feature

```
for feature in plan order:
    case action:
      install|update: feature_<id>_install   → record artifacts in manifest
      keep:           no-op except shared modules (§6.3, brd D3)
      uninstall:      feature_<id>_uninstall → clear manifest entry
```

`_install` is written to be idempotent: `install` and `update` are the same code
path. The distinction exists for the *user's* mental model and for the summary,
not for the executor.

### 6.2 Failure isolation

Per brd D17.1, a failing feature is caught, recorded `failed` in the manifest,
and the loop continues. This requires `set -e` to be **scoped**, not global:
run each `feature_<id>_install` inside a guarded call so a non-zero return is
data, not a script abort. The `settings.json` and `~/.claude.json` writes stay
all-or-nothing per run (§7.3).

### 6.3 Modules and `Keep`

The module closure is computed from every feature whose action is `install`,
`update` **or** `keep`. Modules in that closure are always copied fresh — brd D3,
constraint 2.4. `Keep` therefore means "do not change my wiring or my external
surfaces"; it never means "hold these `.py` files at an old version".

On `uninstall`, a module is deleted only if no feature remaining `installed` in
the manifest owns it (§2.1). The relay `.pth` follows the same rule, owned by
`telegram` alone.

## 7. Settings writes

### 7.1 Hook entries — identified by their command, not by a marker

The current script replaces the whole `hooks` object
(`install-claude-config.sh:770`), destroying any entry the user added. The
replacement rule: **an entry is ours if its `command` references
`$GLOBAL_HOOKS_DIR`.** Nothing foreign points there, so no marker key is needed
and no settings pollution is introduced.

- Install/update: remove our entries for the events this feature owns, then
  append the current ones. Foreign entries in the same event array survive, in
  order.
- Uninstall: remove our entries for those events; if an event array becomes
  empty, delete the key.

Event ownership:

| Event | Owner |
|---|---|
| `PreToolUse` | `permission-hooks` |
| `PermissionRequest` | `permission-hooks` |
| `PostToolUse` | `telegram` |
| `Notification[idle_prompt]` | `telegram` |
| `Notification[permission_prompt]` | `amux` |
| `UserPromptSubmit`, `Stop`, `SubagentStop`, `SessionEnd` | `amux` |

`Notification` is shared between `telegram` and `amux` by matcher — a merge must
key on `matcher`, never replace the `Notification` array wholesale.

### 7.2 Permissions

`mode: union` (default, brd D7) — set-union with the existing arrays, order
preserved, duplicates dropped; the added subset is recorded in the manifest so
uninstall can subtract exactly it. `mode: overwrite` — today's replace semantics,
with the count of discarded entries reported before the write.

Legacy `allowedTools` / `disallowedTools` continue to be folded in and deleted,
as today (`install-claude-config.sh:654-655, 665-669`).

### 7.3 Atomicity

Unchanged from today and worth keeping: build the merged document in memory,
validate with `jq empty`, write, re-validate, restore from the timestamped backup
on failure (`install-claude-config.sh:1004-1021`). `~/.claude.json` gets the same
treatment (it already has it at `:809-816`).

## 8. External surfaces — the seam

Everything that writes outside `~/.claude` goes through exactly one function per
surface. This is what makes brd D5 auditable and what makes §10 testable.

```bash
ext_bashrc_add <marker> <line>      ext_bashrc_remove <marker>
ext_cron_add   <marker> <line>      ext_cron_remove   <marker>
ext_tmux_apply                      ext_tmux_remove
ext_systemd_enable <unit>           ext_systemd_disable <unit>
ext_symlink_add <target> <link>     ext_symlink_remove <link>
ext_run_installer  <script> [args]
```

**Every one of them honours `CLAUDE_INSTALL_NO_EXTERNAL=1` by logging what it
would do and returning success without doing it.** That single env var is the
difference between a test suite that can exercise the installer and one that
edits the developer's crontab (§10).

### 8.1 Marked, idempotent, reversible

The pattern to copy is the existing tmux block
(`install-claude-config.sh:536-576`): an exact marker line, `grep -Fxq`
idempotency, a timestamped backup into `$BACKUP_DIR` before any edit, and removal
that deletes the marker and its managed lines and nothing else.

```
# claude-hooks:amux-autowrap  (install-claude-config.sh — remove with: disable amux-autowrap)
source "$HOME/.claude/shell/amux-spawn.bash"
```

The marker carries its own removal instruction, so a user who finds it in
`~/.bashrc` a year later knows what wrote it and how to undo it.

### 8.2 `~/.bashrc`

Target is `~/.bashrc` specifically, not "the shell rc": the snippets are bash
(`shell/*.bash`) and the repo has no zsh support. If `~/.bashrc` is absent, warn
and skip rather than creating one — an absent bashrc usually means a different
shell, and creating it would be a silent no-op for that user.

### 8.3 crontab

`crontab -l` → filter → `crontab -` , with the pre-edit copy saved into
`$BACKUP_DIR`. No `crontab -r` under any circumstance. The line is exactly the one
`docs/permission-review-daily.md:24` documents, naming the checkout path
(constraint 2.5):

```
15 6 * * * /data/sync/work/leangeeks-ai/claude-hooks/shell/permission-review-daily.sh
```

with the marker on the preceding comment line. An existing unmarked line with the
same command is adopted (marker added, not duplicated).

### 8.4 systemd

Unchanged in mechanism from `install-claude-config.sh:950-1001` — write the unit,
`daemon-reload` — but the **enable** step moves behind the `questions-listen`
sub-toggle and out of `config.toml`. Reading
`[questions_listen] enabled` from `~/.config/claude-tg-relay/config.toml` is
**dropped**: that is rev 0's Fact 1, and keeping both would leave two sources of
truth. 29-09 migrates an existing `enabled = true` into the manifest on first run
and tells the user the key is now inert.

## 9. CLI

Argument parsing precedes everything, including the dependency checks, so
`--help` and `--list` work on a machine missing `jq`.

`--yes` is the unattended entry point and means "replay the recorded selection" —
§5.2 states the guarantee and why an automated caller depends on it.

`enable <sub-toggle>` / `disable <sub-toggle>` (brd D15) load the manifest, verify
the parent feature is installed, apply **only** that surface via §8, and rewrite
the manifest. They never touch files, settings, or any other feature — that
narrow blast radius is the entire point of the subcommand existing.

Unknown sub-toggle ids exit non-zero listing the valid ones. `enable` on a
sub-toggle whose parent is not installed is an error naming the parent, not a
silent cascade into installing it.

## 10. Testing

**Today `install-claude-config.sh` has no automated coverage at all.** The suite
(`tests/run_all_tests.py`, 36 modules) does not execute it, and the only
shell-script precedent is `tests/test_amux_pin.py`, which greps
`install-amux.sh` for a pinned sha rather than running it. This epic adds the
first real coverage, and the seam in §8 is what makes it possible.

### 10.1 Harness

New module `tests/test_unit_installer.py`, registered in `TEST_MODULES` as
`unit_installer` (29-02 adds it; later tasks extend it). Python `unittest`
driving the bash script through `subprocess`, matching the runner conventions.

Each test runs the installer with:

```python
env = {
    **os.environ,
    "HOME": str(tmp_home),              # isolates ~/.claude, ~/.local, ~/.bashrc, ~/.tmux.conf
    "CLAUDE_INSTALL_NO_EXTERNAL": "1",  # neutralizes the four HOME-escaping surfaces
    "CLAUDE_INSTALL_ASSUME_TTY": "0",
}
```

### 10.2 What `HOME` does and does not isolate

This distinction is the reason §8 exists. **Isolated by a `HOME` override:**
`~/.claude/**`, `~/.claude.json`, `~/.local/bin`, `~/.local/share/bash-completion`,
`~/.config/systemd/user`, `~/.bashrc`, `~/.tmux.conf`, and
`python3 -m site --user-site` (it is `HOME`-derived).

**NOT isolated — these escape and must be gated by
`CLAUDE_INSTALL_NO_EXTERNAL`:**

1. `crontab` — per-user, stored in the system spool. A test that forgets this
   edits the developer's real crontab.
2. `systemctl --user enable` / `loginctl enable-linger` — talk to the real user
   manager via `XDG_RUNTIME_DIR`, not `HOME`.
3. `tmux set -g` — mutates the running server, which is very likely the one the
   developer is sitting in.
4. `install-amux.sh` — sudo, `/usr/local/bin`, and a 900 s interactive wait.

A test that needs to assert *what would have happened* asserts on the log line
each `ext_*` function emits in no-external mode. There is no test that performs
any of the four for real; verifying them is task 29-10, by a human, on a machine
they are willing to change.

### 10.3 Coverage expected by the end of the epic

- Registry integrity: `MODULE_OWNERS` covers exactly `REQUIRED_HOOKS`; every
  `_requires()` is satisfiable and ordered correctly in `FEATURES`.
- Probe correctness: for each feature, clean home → not installed; after
  `--only <id> --yes` → installed; artifact present but wiring removed → not
  installed.
- Closure and refcounting: install `telegram`+`questions`, uninstall `telegram`,
  assert `roles_config.py` survives and `questions` still probes installed.
- `Keep` semantics: shared modules refresh, wiring untouched.
- Settings ownership: a foreign `hooks.PreToolUse` entry and a foreign
  `permissions.allow` pattern both survive install, update and uninstall.
- Union vs overwrite, and that uninstall subtracts only `added_allow`.
- Failure isolation: a feature forced to fail leaves the others installed and the
  exit status non-zero.
- `--dry-run` writes nothing at all — no manifest, no backup, no settings change.
- `enable`/`disable` touch only their own surface.
- Idempotency: two consecutive `--yes` runs produce byte-identical
  `settings.json`, manifest and `~/.bashrc`.
