# 29-03 — External-surface managers and the no-external seam

**Status:** todo · **Depends on:** 29-02 (registry, manifest, harness)
**Read first:** [brd.md](./brd.md) D5, D13, D16, §8 ·
[architecture.md](./architecture.md) §8, §10.2 · [state.md](./state.md)
invariants 5, 7 · `install-claude-config.sh:536-602` (the tmux block — the
pattern to copy) · `install-claude-config.sh:940-1001` (the systemd block) ·
`docs/permission-review-daily.md` (the crontab line and its reasoning)

> **Which file you edit:** `install.sh`. 29-02 §0 froze
> `install-claude-config.sh` as the pre-epic reference copy — every
> `install-claude-config.sh:NNN` citation below points into that frozen file and
> describes code as it was before the epic. Do not edit it. Run `install.sh` only
> against a temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`
> ([state.md](./state.md), bootstrapping hazard).

## Goal

One function per external surface, all of them marked, idempotent, reversible and
backed up — and a single environment variable that neutralizes every one of them
so the installer can be tested at all.

**This task is the safety boundary of the epic.** Everything that can damage a
developer's machine outside `~/.claude` lives here and nowhere else.

## Scope

### 1. The seam

Implement the functions listed in architecture §8:

```
ext_bashrc_add / ext_bashrc_remove
ext_cron_add   / ext_cron_remove
ext_tmux_apply / ext_tmux_remove
ext_systemd_enable / ext_systemd_disable
ext_symlink_add / ext_symlink_remove
ext_run_installer
```

**Every one honours `CLAUDE_INSTALL_NO_EXTERNAL=1`** by logging the action it
would take — in a stable, parseable form, because tests assert on those lines —
and returning success without performing it. This is not a debug convenience; it
is the only thing standing between the test suite and the developer's real
crontab (architecture §10.2).

No code outside these functions may invoke `crontab`, `systemctl`, `loginctl`,
`tmux set`, `ln -s`, or another installer script, or append to a file in `$HOME`
that is not under `~/.claude`. 29-05 and 29-08 call these; they do not reimplement
them.

### 2. Marker discipline

Copy the existing tmux pattern (`install-claude-config.sh:536-576`): an exact
marker line, `grep -Fxq` idempotency, and a timestamped backup into `$BACKUP_DIR`
before any edit to an existing file.

Markers carry their own undo instruction, per architecture §8.1:

```
# claude-hooks:amux-autowrap  (install-claude-config.sh — remove with: disable amux-autowrap)
```

`ext_*_remove` deletes the marker and the lines it manages, and **nothing else**.
A removal that cannot find its marker is a no-op that says so, never a
best-effort guess at which line was ours.

### 3. `~/.bashrc`

Per architecture §8.2: target `~/.bashrc` specifically. If it does not exist,
warn and skip — do not create it. The repo ships bash snippets only
(`shell/*.bash`) and an absent `~/.bashrc` usually means a different shell, where
creating one is a silent no-op for that user.

Enforce the mutual exclusion (architecture §2): `amux-autowrap` and
`profiles-autosource` cannot both be present. Adding one removes the other and
logs that it did. `install-claude-config.sh:491` is the existing statement of why
(*"Do NOT source both"* — `amux-spawn.bash` sources the profiles snippet
internally).

### 4. crontab

Per architecture §8.3: `crontab -l` → filter → `crontab -`, with the pre-edit
listing saved into `$BACKUP_DIR` first.

- **Never `crontab -r`** under any circumstance, including "the list is now
  empty". Write the empty list explicitly instead.
- `crontab -l` exits non-zero when no crontab exists; that is a normal empty
  state, not an error.
- An existing **unmarked** line whose command matches ours is adopted — add the
  marker above it, do not duplicate the line.
- The line is exactly the one `docs/permission-review-daily.md:24` documents,
  naming the checkout path directly (brd constraint 2.5 — the script cannot be
  run from a copy).

### 5. tmux

Keep both halves of today's behaviour (`install-claude-config.sh:536-602`): the
persistent `~/.tmux.conf` lines and the live `tmux set -g` on a running server.
Add the removal side, which does not exist today. Note in the code that the live
`set -g` is **not** reverted by `ext_tmux_remove` — a running server's options
belong to that server, and silently changing them back could disrupt a session
the user is sitting in. Say so in the uninstall summary instead.

### 6. systemd

Move the **enable** decision out of `~/.config/claude-tg-relay/config.toml` and
behind the `questions-listen` sub-toggle (architecture §8.4, brd D15). The unit
file is still written and `daemon-reload` still runs whenever `questions` is
installed; only `enable --now` and `loginctl enable-linger` move.

Do not delete the `config.toml` key handling in this task — 29-09 owns the
migration and the user-facing message about it.

### 7. `ext_run_installer`

Wraps `install-amux.sh` (brd D12). It delegates elevation entirely
(`install-amux.sh:397-418` already implements the full ladder) and reports the
sub-installer's exit status. It must **not** re-prompt, wrap the output, or add a
timeout — the sub-installer can legitimately wait up to 900 s for a manual root
install, and that is why brd D6 collects all decisions before execution.

## Done when

- With `CLAUDE_INSTALL_NO_EXTERNAL=1`, no `ext_*` function touches anything
  outside `$HOME`, and each logs a stable line describing what it skipped.
- Every `ext_*_add` is idempotent: running it twice leaves one marker and one
  line.
- Every `ext_*_remove` reverts its own `_add` exactly, leaving surrounding
  content byte-identical.
- Adding `amux-autowrap` when `profiles-autosource` is present removes the
  latter and says so.
- `crontab` handling survives: no crontab at all, a crontab with unrelated lines,
  and a crontab already carrying an unmarked copy of our line.
- `grep -nE 'crontab|systemctl|loginctl|tmux set|ln -s' install-claude-config.sh`
  shows hits only inside the `ext_*` functions.

## Tests

Extend `tests/test_unit_installer.py`.

- Each `ext_*_add` / `_remove` pair against a `tmp_home`: idempotency,
  exact-revert, foreign-content preservation.
- bashrc: absent file → warn and skip, nothing created; mutual exclusion both
  directions.
- crontab: all three starting states from "Done when", asserted against the
  no-external log lines rather than a real crontab. **No test may call `crontab`
  for real** — assert that by checking the harness sets
  `CLAUDE_INSTALL_NO_EXTERNAL=1` (29-02 put that assertion in `setUp`).
- A source-level test, in the spirit of `tests/test_amux_pin.py`: grep the
  installer for the dangerous commands and assert every occurrence is inside an
  `ext_*` function body. This is the invariant most likely to be broken by a
  later, well-meaning edit, and a grep test is the only thing that catches it.
