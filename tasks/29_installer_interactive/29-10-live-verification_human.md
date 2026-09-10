# 29-10 — Live verification

**Status:** blocked · **Agent:** human · **Depends on:** 29-09
**Read first:** [brd.md](./brd.md) §7 · [architecture.md](./architecture.md)
§10.2 · [29-09 §6](./29-09-migration-and-docs.md) (the cutover you are checking) ·
`docs/installer.md` (written by 29-09)

## Why this is human

Four surfaces are gated by `CLAUDE_INSTALL_NO_EXTERNAL=1` in every automated
test, because exercising them for real means changing the machine the tests run
on (architecture §10.2):

1. **crontab** — per-user, in the system spool; not isolated by `HOME`.
2. **`systemctl --user enable` / `loginctl enable-linger`** — the real user
   manager, via `XDG_RUNTIME_DIR`.
3. **`tmux set -g`** — the running server, very likely the one the operator is
   sitting in.
4. **`install-amux.sh`** — sudo, `/usr/local/bin`, and up to a 900 s interactive
   wait.

No agent may perform these. They need a machine someone is willing to change and
a human who can see the result.

## Checklist

Run on a real machine, from a real checkout. Record the outcome of each line.

### Fresh install

- [ ] `--list` on this machine, before anything: does the detected state match
      what you know is installed?
- [ ] Interactive run, accepting defaults. Does the plan disclose every
      out-of-tree write before you confirm?
- [ ] After it: `claude` starts, the status line renders, a permission prompt
      still reaches Telegram.

### The four gated surfaces

- [ ] `enable amux-autowrap` → the marked line is in `~/.bashrc`; a new shell
      routes a profile function through amux-spawn; `disable` removes exactly
      that line and nothing around it.
- [ ] `enable questions-listen` → `systemctl --user is-enabled
      claude-questions-listen` is `enabled`, the daemon starts, and an answer
      given in Telegram reaches a queue file. `disable` stops and disables it.
- [ ] `enable daily-review-cron` → `crontab -l` shows the marked line with the
      correct checkout path. **Confirm your other crontab lines are intact.**
      `disable` removes only ours.
- [ ] tmux: `~/.tmux.conf` carries the marked block, and a running server has
      `focus-events on`. Confirm the documented behaviour that
      `ext_tmux_remove` does not revert the live server (29-03 §5).
- [ ] amux: on a machine without `amux`, does `amux` run `install-amux.sh`,
      and does its elevation ladder behave (passwordless sudo, and the
      print-the-root-command path)?

### The cutover (29-09 §6)

Check these before anything else — they are the ones that break *every* user, not
just this machine.

- [ ] The temporary real-`$HOME` guard is gone: a normal run on this machine is
      not refused, and `grep -rn 'CLAUDE_INSTALL_EPIC29_LIVE' .` finds nothing.
- [ ] Exactly one installer script is in the tree, and every reference in the
      repo names a file that exists.
- [ ] `docs/prompts/implementer.md` and `reviewer.md` no longer carry the
      epic-29 paragraphs, and their standing instruction points at the surviving
      script.

### The unattended path (29-08 §2.1)

The daily reviewer runs the installer at 06:15 with nobody watching. Verify by
hand what no test can prove about *this* machine:

- [ ] Deliberately skip one feature, then run the installer with `--yes` the way
      the cron job does. Confirm the skipped feature is **still not installed**
      and no other feature changed state.
- [ ] `docs/prompts/permission-review-daily.md` step 3 invokes the installer with
      `--yes`, and the launcher skips propagation when the installer script has
      uncommitted changes.

### Migration — the once-only path

- [ ] On a machine installed by the **old** script and never touched by the new
      one: does the first run show installed features at `Update`?
- [ ] Accept the defaults. Is the end state equivalent to before, with a manifest
      now present?
- [ ] If this machine had `[questions_listen] enabled = true`: was it migrated,
      and were you told the key is inert?

### Uninstall

- [ ] Uninstall `telegram` on a machine that also has `questions`. Does
      `questions` still work? (`roles_config.py` must survive — 29-05 §4.)
- [ ] Uninstall everything. Are `profiles.toml`, `history.jsonl` and the state
      stores untouched?
- [ ] Re-install everything. Does the machine work again?

### Rev 0's Fact 1

- [ ] Time enabling the questions listener from a cold start. It should be one
      command. That was the whole complaint.

## Done when

The user confirms the checklist, or records which lines failed. Per
`docs/prompts/implementation_manager.md` §37-49 this stays `blocked` until then;
it does not block the engineering tasks, which ship behind default-off controls
for both scheduled features (brd D14).
