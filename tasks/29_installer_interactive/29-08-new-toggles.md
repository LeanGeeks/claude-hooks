# 29-08 — New toggles: `claude-history` and daily permission review

**Status:** todo · **Depends on:** 29-02 (registry), 29-03 (crontab seam), 29-05
(executors)
**Read first:** [brd.md](./brd.md) D11, D13, D14, constraints 2.5, 2.8 ·
[architecture.md](./architecture.md) §8.3 · [state.md](./state.md) invariants 5,
7 · `shell/claude-history` · `shell/permission-review-daily.sh` ·
`docs/permission-review-daily.md` · `docs/prompts/permission-review-daily.md:240-258`
(step 3 — the seed prompt tells the scheduled agent to run the installer)

> **Which file you edit:** `install.sh`. 29-02 §0 froze
> `install-claude-config.sh` as the pre-epic reference copy — every
> `install-claude-config.sh:NNN` citation below points into that frozen file and
> describes code as it was before the epic. Do not edit it. Run `install.sh` only
> against a temporary `HOME` with `CLAUDE_INSTALL_NO_EXTERNAL=1`
> ([state.md](./state.md), bootstrapping hazard).

## Goal

Two artifacts the repo ships but has never installed become the `claude-history` and `daily-review` features.

## Scope

### 1. `claude-history`

`shell/claude-history` is referenced today only by a comment in the installer
(`:451`) and is otherwise dead weight (brd constraint 2.8).

It is genuinely standalone: it reads `~/.claude/history.jsonl` and
`~/.claude/projects/` directly and imports nothing from the repo — as
dependency-free as the status line. So the feature is small:

- copy to `~/.claude/shell/claude-history`, `chmod +x`;
- symlink into `~/.local/bin` via `ext_symlink_add`, on the same terms as
  `claude-roles` and `claude-questions` today (`:466-473`): only when the
  directory exists **and** is on `PATH`, to avoid a dangling symlink the user
  will never find;
- probe: the symlink resolves to our copy.

It needs `fzf` at runtime (it shells out for the picker; `--list` works without
it). Probe for `fzf` at install time and **warn without failing** — `--list`
remains useful, and a missing optional tool is not an install failure.

Default: `Install` (brd D14).

### 2. `daily-review` — daily permission review

**This feature installs no files.** `shell/permission-review-daily.sh:52`
resolves the checkout from its own location:

```bash
REPO_DIR="${CLAUDE_HOOKS_REPO:-$(dirname "$(dirname "$SCRIPT_PATH")")}"
```

and reads `docs/prompts/permission-review-daily.md`, logging into the repo's
`temp/`. Copied to `~/.claude/shell/` it resolves `REPO_DIR` to `~/.claude` and
breaks. `docs/permission-review-daily.md:34-45` already argues for naming the
checkout path directly in the cron line, for this exact reason.

So `daily-review` is: **verify and schedule.**

- Verify `shell/permission-review-daily.sh` exists in the checkout and is
  executable; fix the mode if not.
- Verify `docs/prompts/permission-review-daily.md` exists — the launcher seeds
  the session with it, and a missing prompt file makes the 06:15 run fail
  silently, which is the failure mode the launcher's own header (§3) is written
  to avoid.
- Require `amux`: the launcher starts an `amux-spawn` session. Refuse
  to install without it, naming the dependency.
- The `daily-review-cron` sub-toggle writes the line via `ext_cron_add`
  (29-03 §4).

Default: `Skip`, and the sub-toggle defaults off (brd D14). Both this and
`questions` schedule ongoing autonomous activity — here, a job that edits
`settings.json` and commits — and rev 0's *"never enable it silently"* applies to
the whole feature, not only to the scheduling step.

### 2.1 The job runs the installer — pin it to the recorded selection

This is the sharpest edge in the task and it is easy to miss, because it lives in
a file the feature does not install.

`docs/prompts/permission-review-daily.md:251` instructs the scheduled agent, in
step 3 ("Propagate user-scope changes"), to run:

```bash
./install-claude-config.sh
```

so that a permission pattern it just applied to the repo's `settings.json`
reaches every other workspace on the machine. So the moment `daily-review-cron`
is enabled, this repo owns an unattended 06:15 job that executes the installer
out of the working tree. Two consequences, both this task's to handle:

**a. It must not reset the user's feature selection.** After this epic the
installer is a selector, and a bare run in a cron environment is wrong twice
over: there is no TTY, and without a manifest-driven selection the D14 defaults
would apply — which would *install features the user deliberately skipped*. That
is the old "install everything" behaviour leaking back in through the one path
nobody is watching.

The mode that is already correct is brd §6.3: **`--yes` with a manifest present
replays exactly the recorded selection**, `state: "skipped"` included, and 29-07
§1 already makes `--yes` with neither a manifest nor an explicit selection a hard
error. That combination is precisely the unattended contract this job needs — no
new flag. Your job here is to *pin* it:

- Update `docs/prompts/permission-review-daily.md` step 3 to run
  `./install.sh --yes` (the epic's final name — 29-09 owns the rename of the
  frozen copy, so coordinate the exact string with that task if you land first).
- State in that step **why** the flag is not optional: it means "re-apply what
  this machine already chose", and a bare run in cron will fail the no-TTY check
  rather than silently reinstall the world. Keep the existing invariant-8
  instruction to name the run in the summary either way.
- Extend the step so the agent reports the replay, not just the merge: if the
  installer's summary shows a feature changing state, that is a bug in the
  manifest, and the agent says so rather than accepting it.

**b. It executes whatever the working tree contains.** A checkout mid-way through
an installer change gets run at 06:15 against the real `$HOME`. Withhold the
propagation step in that case — but only that step; draining the queue and
reviewing the day's traffic are still worth doing.

The launcher is where the check belongs, because it runs before the agent has any
say. `shell/permission-review-daily.sh` already does pre-flight checks in this
shape (`:82` missing prompt, `:87` missing `amux-spawn`), and already pins the
session's knobs through the environment:

- In the launcher, if `git -C "$REPO_DIR" status --porcelain -- <installer>` is
  non-empty, export `PERMISSION_REVIEW_NO_PROPAGATE=1` and log a line saying the
  installer is dirty and propagation is withheld. Do **not** make it a `FATAL` —
  the review still runs.
- In the seed prompt's step 3, instruct the agent: if
  `PERMISSION_REVIEW_NO_PROPAGATE=1` is set, skip the installer run and report
  "propagation withheld — installer has uncommitted changes" instead of the
  usual two outcomes. This pairs with the existing invariant-8 rule that the
  summary must always say whether the merge ran.

Note also `shell/permission-review-daily.sh:89`, whose failure message tells the
operator to *"run ./install-claude-config.sh"*. That is another reference 29-09
§6 has to repoint at the surviving filename; flag it there rather than fixing it
here, so the cutover has one owner.

### 3. The documented stance reverses

`docs/permission-review-daily.md:18` says *"The repo never writes your crontab.
Add the line yourself"*, and `:16-26` walks through `crontab -e`. Per brd D13
that is no longer true.

Update the doc:

- the installer now writes the line, behind `daily-review-cron`, marked and
  reversible;
- the manual `crontab -e` path stays documented as the alternative;
- `:34-45`'s reasoning about naming the repo path directly is **kept**, and
  reframed as the reason the feature installs nothing (§2 above);
- `:47` (running from a checkout at a different path) still applies — say that
  `CLAUDE_HOOKS_REPO` in the cron line is how, and that the installer writes the
  path of the checkout it was run from.

Do not delete the operator notes. This is an edit, not a rewrite.

## Done when

- `claude-history` installs, symlinks, probes, and uninstalls cleanly.
- Missing `fzf` warns and still installs.
- `~/.local/bin` absent or not on `PATH` → no symlink, warning, install still
  succeeds.
- `daily-review` refuses to install without `amux`, naming it.
- `daily-review` copies nothing into `~/.claude/`.
- A missing `docs/prompts/permission-review-daily.md` fails the feature with a
  message naming the file, rather than scheduling a job that will fail at 06:15.
- `enable daily-review-cron` writes exactly the documented line with the checkout
  path; `disable` removes it and leaves other crontab lines untouched.
- `docs/permission-review-daily.md` no longer claims the repo never writes the
  crontab, and still carries the path-naming rationale.
- `docs/prompts/permission-review-daily.md` step 3 runs the installer with
  `--yes` and explains that the flag means "replay the recorded selection".
- A `--yes` run against a manifest in which a feature is `"skipped"` leaves that
  feature not installed. This is the regression that proves an unattended run
  cannot resurrect the old install-everything behaviour.
- The launcher sets `PERMISSION_REVIEW_NO_PROPAGATE=1` and logs it, without
  failing the run, when the installer has uncommitted changes; the seed prompt
  tells the agent what to do with it.

## Tests

Extend `tests/test_unit_installer.py`.

- `claude-history`: install/probe/uninstall; `fzf` absent; `~/.local/bin` absent
  and present-but-not-on-`PATH`.
- `daily-review`: dependency refusal; nothing copied into `~/.claude`; missing
  prompt file fails with the right message.
- Cron line content asserted against the `CLAUDE_INSTALL_NO_EXTERNAL` log line —
  exact command, exact schedule, checkout path — never against a real crontab
  (architecture §10.2).
- A docs test in the spirit of `tests/test_amux_pin.py`: assert
  `docs/permission-review-daily.md` and the installer agree on the cron line, so
  the two cannot drift. Extend it to assert that
  `docs/prompts/permission-review-daily.md` invokes the installer with `--yes` —
  a bare invocation there is the defect described in §2.1a, and a grep is the
  only thing that catches it coming back.
- Selection replay: write a manifest with `claude-history` `"skipped"` and
  `statusline` `"installed"`, run `--yes`, assert the end state matches the
  manifest and not the D14 defaults. Repeat with a sub-toggle recorded off.
- Launcher dirty-tree detection: a temp git repo with a modified installer;
  assert `PERMISSION_REVIEW_NO_PROPAGATE=1` is exported and logged, and that the
  launcher still proceeds to the spawn (stub `amux-spawn` on `PATH`). A clean
  tree must not set it.
