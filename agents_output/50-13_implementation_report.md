# Implementation Report - 50-13 Fixes from the 50-12 live run (F-1..F-4)

## Summary

F-1 (spurious git deferral), F-2 (`due_not_started` between "answered" and "granted"), F-3 (folder-trust `enable`
gate), F-4 (login-expiry gate and alarms) and the optional `ssh_without_connect_timeout` false positive are
implemented, with tests, architecture and doc amendments. The F-3 research turned up something the lead should see
before telling the operator the trust setup is done: **trusting `~/.local/state/epic-driver/` does not cover a git
worktree.** Claude Code takes a worktree's trust from its main repository. The gate therefore also checks every
worker repo (see F-3).

Nothing live was touched. No `epic-driver` verb was run for real, the run's state dir and the rig files were not
read, the driver tmux server and the units were not touched, `./install.sh` was not run, `~/.claude.json` was not
written, there were no git operations and nothing was committed. Every probe ran under a throwaway HOME on a private
tmux socket (details under F-3).

## Files Modified

- `.claude/hooks/epic_driver_lib.py`: all fixes (functions listed per item below).
- `.claude/bin/epic-driver`: `enable --login-days N` (`_non_negative_int`), usage line.
- `tests/test_unit_epic_driver.py`:
  - `TickWorld` gains fakes for `claude_config` and `login_expiry`.
  - Gate-number assertions are renumbered (4..11 → 6..13).
  - The catalogue count is now 31.
  - A new 50-13 section at the end of the file holds the tests.
- `tasks/50_epic_driver/architecture.md`: §6.2, §8, §12.11, §16.2, §18, §18.1, §23.
- `docs/epic-driver.md`:
  - §1: the trust prerequisite and the new gates.
  - §2: `--login-days`.
  - §4: a new subsection, "Folder trust: a one-time setup".
  - §9: troubleshooting rows.
- `tasks/50_epic_driver/50-12-signoff-evidence.md` (the 50-12 runbook): notes under §1 `enable` and under check 9.

I created no files except this report. `tasks/50_epic_driver/state.md` was already modified before I started, and
I did not touch it.

## Per scope item

### F-1: `BoundedWorld.git` marked every ledger load expired

- **Change:** `BoundedWorld.git` now runs a clamped call (left < its timeout) and sets `expired` only through the new
  `clamped_call_timed_out(res, left_after)`. That means `rc is None` plus either "timed out" in the error or less
  than 1 s left after the call. A refused call (< 1 s left) still expires. A clamped call that returns in time is a
  normal result, including a real git failure such as a missing base ref, which now surfaces as `ledger_invalid`
  instead of being deferred.
- **Audit:** `BoundedWorld` has three users.
  - The ledger stage of `tick_pass`: this was the bug.
  - `prepare_worker_steps`: it had the same spurious deferral. Any step whose git calls came within 30 s of
    `PREPARE_END_S` was deferred even when the calls were fast. The `BoundedWorld` fix covers it, and its docstring
    is updated.
  - The `assume_git=True` re-load: it never clamps.
  - `run_sha_checks` and the other phases use `fits=` worst-case gates, not `BoundedWorld`, so they are unaffected.
- **Tests:** `TestBoundedWorldExpiry50_13` and `TestTickLedgerGit50_13`.
  - A fast clamped call is not expired, and its timeout is still clamped.
  - A clamped call that fails in time is not expired.
  - A clamped timeout expires; so does a clamped `rc=None` at the deadline.
  - A refused call expires; an unclamped timeout does not.
  - `prepare_worker_steps` prepares all steps when its calls were clamped but fast.
  - Tick: a normal pass with fast clamped calls verifies the base refs (`rev-parse --verify` is called) and its
    tick.log carries no "repos and base refs not checked" note.
  - Tick: a missing base ref found by a clamped call raises `ledger_invalid`, not a deferral.
  - Tick: a clamped call that times out defers, with the note.
  - Checked in process by monkeypatching back the old rule: 2 of these tests fail under it.

### F-2: `due_not_started` between "answered" and "granted"

- **Change:**
  - A new hold, `HOLD_APPROVAL_ANSWERED = "approval_answered"`, joins `QUIET_HOLDS`.
  - The new `approval_answer_unsettled(state, approval_id)` gives the reason, or None. It covers two windows:
    - Same pass: `_reconcile_questions` sets `questions[Q].answer_pending = True` when it sees the question resolved.
      `_commit_event` pops it and sets `answer_event` (the §8 field that was never written before) when the event
      carrying the `answer` item commits. This window matters because `select` runs at step 5, before the dispatch
      commit at step 6.
    - Later passes: an outbox entry with kind `answer` carrying the approval that is not `_entry_complete` (not
      confirmed, or no complete verdict yet).
  - `_individual_holds` uses it when the approval is ungranted and no approval question is open.
  - Once settled without a grant, the hold reverts to plain `approval` and the alarm can fire. With no question at
    all, nothing changes.
- **Side effect, deliberate:** during that window `next_approval_to_ask` no longer proposes the same approval again.
  Before, in the same pass as the answer, `approval_needed_items` could add a second `approval_needed` for it: the
  outbox guard didn't apply yet, and the raise cycle was "closed". The test checks that no `approval_needed` is
  planned.
- **Why the flag:** `answer_pending` is set only by the new code, so old resolved records without it (the live run's
  Q-001) are unaffected. Items are always committed in the pass that creates them (`plan_dispatch` docstring), so the
  flag cannot outlive its pass.
- **Tests:** `TestApprovalAnswerQuiet50_13`.
  - The full sequence: open (quiet) → resolved in the reconcile (quiet, `answer_pending`, not re-asked) → committed
    (`answer_event` set, quiet through the outbox) → confirmed with no verdict (quiet) → settled without a grant
    (`due_not_started` fires) → granted (dispatchable).
  - With no question, the alarm fires.
  - An answer for another approval does not quiet this one.

### F-3: folder-trust gate

- **Research (Claude Code 2.1.330 bundle, read-only, plus probe sessions):**
  - Trust lives in `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json` when set):
    `projects[<abs>].hasTrustDialogAccepted`.
  - The lookup is not "the path or any parent". Claude Code first checks the folder's canonical git root: for a
    linked worktree, that is the main repository. It then walks from the folder up to its git root and stops there.
    Only outside git does the walk reach any parent.
  - Verified with probe sessions under a throwaway `HOME`/`CLAUDE_CONFIG_DIR` (synthetic `.claude.json`, a dummy API
    key, a private tmux socket; nothing real read or written). A parent folder was trusted:
    - a plain subfolder: no dialog;
    - a non-git wave dir: no dialog;
    - a git repo below it: dialog;
    - a git worktree below it: dialog;
    - a worktree inside a wave dir: dialog;
    - an untrusted control folder: dialog.
  - With the main repo trusted as well, the dialog was gone from every worktree. The control still showed it.
  - The live evidence fits this. `~/.claude.json` has `/home/anton/epic50-rig/ws` and `/home/anton/epic50-rig/repo`
    trusted and no key below the state root other than the root itself. The rig worker most likely went through
    because the repo was trusted (when the operator answered a dialog), not because of the state-root trust.
- **Change:**
  - New functions: `claude_config_path`, `git_toplevel`, `git_canonical_root` (pure file reads of `.git` / `commondir`,
    no git run), `trusted_by` (Claude Code's rule), `trust_folders(ledger)` and `trust_refusal`.
  - New World methods: `World.claude_config` / `RealWorld.claude_config`, read only.
  - `cmd_enable` gate 4 checks the folders below and refuses with exit 3, naming each untrusted folder. The fix text
    is: open `claude` there once and answer "Yes, I trust this folder", or set `projects["<folder>"].
    hasTrustDialogAccepted = true` yourself; epic-driver never writes the file. The folders:
    - the workspace;
    - the state root;
    - a `worktree_root` outside the state root;
    - **each worker repo**.
  - An absent, unreadable or non-object `~/.claude.json` refuses with the fix, and the text lists the folders.
- **Decisions:**
  - **Worker repos are added to the brief's "workspace + state root".** Without them the gate passes while every
    single-repo worker still stalls on its first spawn, which is exactly the 50-12 check-9 failure.
  - **I mirrored Claude Code's git-bounded rule rather than the literal "any parent".** The literal rule would let
    `enable` pass for a git workspace under a trusted parent that Claude Code still prompts for.
  - The lead may want to confirm both: they extend the operator's 11-23 decision, they don't change it. `enable`
    still writes nothing.
- **Gate placement:** new gates 4 (trust) and 5 (login) go right after the ledger, before the listener and the test
  notify (no side effect before them). The old gates 4..11 are renumbered 6..13 in code, comments, tests,
  architecture §18.1 and the runbook note.
- **Tests:** `TestFolderTrust50_13` (real temp dirs, real `git init` / `git worktree add`) and
  `TestEnableTrustAndLogin50_13` (the CLI with the fakes).
  - Trusted directly, and through a parent outside git.
  - Not trusted, the key missing, `false`, a non-boolean, no `projects`.
  - Trust above a git root does not reach in.
  - A worktree takes its main repo's trust; the state root alone is not enough.
  - `trust_folders` for each ledger shape.
  - The config path resolution.
  - `RealWorld.claude_config` raises on absent, broken or non-object files and never writes (mtime and content
    unchanged).
  - Gate 4 names each untrusted folder plus the fix, and refuses before any notify.
  - Gate 4 passes directly and through a parent.
  - Gate 4 refuses on an absent, unreadable or unparsable config with the fix.
  - Gate 4 checks worker repos.

### F-4: login expiry

- **Source found:** `~/.claude/.credentials.json` → `claudeAiOauth.refreshTokenExpiresAt` (epoch milliseconds).
- **How I verified it:**
  - In the installed 2.1.330 bundle, the banner component (`oauth-expiry`, "Your login expires in N days" with the
    hint "run /login to renew") calls a function that does this:
    - It reads the stored OAuth tokens.
    - It returns null unless the provider is first-party, a claude.ai login is in use, and a refresh token exists with
      a plausible `refreshTokenExpiresAt`.
    - It ignores the expiry when `expiresAt > refreshTokenExpiresAt + 3 d`.
    - It shows the banner while `0 < refreshTokenExpiresAt − now ≤ 3 d`, with `daysLeft = ceil(ms / day)`.
  - The token merge on a refresh sets `refreshTokenExpiresAt = min(old, new)`, and only `sign_in` replaces it. So a
    token refresh never extends it, and only `/login` does. It is the login's hard expiry, not the short-lived
    `expiresAt` (about 8 h).
  - Consistency on this machine (expiry fields only, no token values printed):
    - `refreshTokenExpiresAt` = 2026-12-21T07:20Z, which is 28 days after a sign-in at about 07:20Z today. That fits
      the operator's `/login` after the "3 days" banner.
    - `expiresAt` = 15:53Z, the file's mtime (07:53Z) + 8 h: a later access-token refresh that left the refresh
      expiry alone.
  - I could not observe the pre-login value. The logic link is from the bundle.
  - Because a reliable source exists, the pane-banner fallback was not built.
- **Change:**
  - New function `login_expiry_from_credentials(data)`. It mirrors Claude Code's rule, looks at the refresh token's
    presence only, and returns a datetime or None.
  - New functions `login_days_left` (rounds up, like the banner) and `login_conditions(expiry, now)`.
  - New World methods `World.login_expiry` / `RealWorld.login_expiry`. They never raise, and return None when the
    file is absent or unreadable.
  - Tick, step 1: `run_conds += login_conditions(world.login_expiry(), now)`.
    - `login_expiring` (subject: run) at ≤ 3 days, detail "expires in N day(s) (ISO); run /login in claude to renew".
    - `login_expired` (run) once the expiry has passed.
    - Both clear through the normal engine when a `/login` moves the expiry out.
  - `enable` gate 5:
    - An expired login refuses.
    - A login expiring within `--login-days N` (default 7) refuses, with the fix: `/login`, or `--login-days 0` to
      enable anyway.
    - No login known gives a note, not a refusal. Otherwise it prints the expiry.
  - Constants: `LOGIN_ENABLE_DAYS = 7`, `LOGIN_ALARM_DAYS = 3`. `ALARM_IDS` gains both ids after `inhibitor_down`, and
    §16.2 gains both rows.
- **Decision: refuse, not warn.** A multi-week run loses every session at expiry. The override is explicit
  (`--login-days 0`), and the tick alarm still covers the run.
- **Known limitation:** the check is account-wide. A run whose roles all use non-Anthropic profiles would still get
  `login_expiring` if a claude.ai login exists.
- **Tests:** `TestLoginExpiry50_13`, `TestLoginAlarm50_13` and `TestEnableTrustAndLogin50_13`.
  - The parser on synthetic JSON:
    - the good case;
    - none, a list, an empty dict, no `claudeAiOauth`, an empty refresh token;
    - an expiry that is a string, a bool, too small or NaN;
    - an access token outliving the refresh expiry by more or less than 3 days.
  - Days-left rounding.
  - The thresholds: just over 3 d gives nothing, exactly 3 d and 1 h give `login_expiring`, 0 and past give
    `login_expired`, plus the detail text.
  - `RealWorld.login_expiry` on an absent, broken and synthetic file.
  - The tick raises `login_expiring` (with a notify), then `login_expired` (and `login_expiring` clears), then both
    clear after a re-login; an unknown login raises nothing.
  - Gate 5: expired, within 7, within `--login-days 30`, and expired with 0 all refuse; 8 days, `--login-days 0` and
    no login pass. `--login-days -1`, `x` and `1.5` are usage errors (exit 2).

### Optional: `ssh_without_connect_timeout` false positive (fixed, it was trivial)

- **Cause:** in 037's `sa3_h01_close.sh` and `wd_fresh.sh` (read only), the bare word "ssh" appears in error strings
  ("ssh root@h01 failed"), while the actual ssh runs through `acdn037_ssh` in a sourced `lib.sh` that applies
  `$EPIC_DRIVER_SSH_OPTS`.
- **Change:** `_warn_ssh` skips a script's own text when it sources a lib that mentions `EPIC_DRIVER_SSH_OPTS` or
  `ConnectTimeout`. New helpers: `_SOURCE_RE`, `_SCRIPT_DIR_FORMS`, `_read_head` and `_sources_ssh_opts`. They
  resolve `. "$(dirname "$0")/x"`, `$(dirname $0)`, `${0%/*}` and the `BASH_SOURCE` forms, paths relative to the
  script or the plan dir, and absolute paths. Any other `$`-path is not followed. Checked against both 037 scripts:
  silenced.
- **Tests:** `TestSshWarningSourcedLib50_13`. Four source forms are silenced. It still warns for:
  - plain ssh;
  - a sourced lib without the options;
  - a missing lib;
  - an unresolvable `$LIBDIR` path.

### Scope 6: does a mid-run stamp change need more than `./install.sh --yes` + one tick?

**No, nothing more is required by the design.**

- **Why install + one tick is enough:**
  - §17 and §23 step 0, and the code (`.claude/bin/epic-driver` `main` → `check_stamps` → `tick_stamp_mismatch`):
    every tick is a fresh oneshot process (the §22 unit is `Type=oneshot`, `ExecStart=… epic-driver tick`). It hashes
    the stamp set against `stamps.json` before importing anything. Nothing long-lived holds driver code: the
    inhibitor runs `systemd-inhibit … sleep infinity`, and sessions are Claude processes. So the first tick after
    install runs the new code.
  - `install.sh` `feature_epic_driver_install` copies the modules, the lib and the prompts, rewrites the unit files,
    runs `daemon-reload`, and writes `stamps.json` last. It enables and disables nothing, so the timer stays on.
  - A stale `stamp_mismatch:run` from an earlier mismatch clears on the first tick whose stamps agree (per
    `tick_stamp_mismatch`'s contract).
- **What this change does not touch:**
  - It changes no unit text, so the running inhibitor needs no restart.
  - It changes no prompt.
  - It adds no stamped file, so `STAMPED_FILES` and the installer list are unchanged.
  - It adds no state-schema bump: `answer_pending` and `answer_event` are optional, and the live run's records lack
    them harmlessly.
  - The new trust and login gates apply only to `enable`, which the live run does not re-run. The new tick login
    read finds 27 days left today, so no alarm.
- **Caveats (operational, not extra steps):**
  - A tick that starts while install is copying files (between the first copy and the final stamps write) sees a
    mismatch. It records `stamp_mismatch`, tries one notify and exits 1 without touching sessions; the next agreeing
    tick clears it. To avoid that notify, run install just after a pass has finished. The timer fires every 10 min
    (`OnCalendar=*:0/10`, `AccuracySec=30s`), and `pause` would not help because the stamp check precedes it.
  - A tick already running keeps the old modules it imported. Its state writes stay compatible.
  - Sessions already running keep the role prompt text they read at spawn. New spawns read the new prompts, which
    are unchanged here.

## Architecture / doc amendments

- **§6.2:** an answered approval question counts as "open" until its answer event settles; settled without a grant,
  it is an ordinary hold.
- **§8:** the `questions[Q].answer_event` and `answer_pending` rules.
- **§12.11:** gate 4 refuses up front for untrusted folders.
- **§16.2:** `login_expiring` and `login_expired` rows (run subject, source, clear on `/login`).
- **§18:** `enable [--login-days N]`.
- **§18.1:**
  - Gate 4 (folder trust) is new: the folders, Claude Code's git rule, the refusal and fix, and the rule that
    `enable` never writes the config.
  - Gate 5 (login) is new.
  - The old gates 4..11 are renumbered 6..13.
- **§23 step 1:** read the login expiry.
- **`docs/epic-driver.md`:**
  - §1 states the trust prerequisite and the new gates.
  - §2 adds `--login-days`.
  - §4 adds "Folder trust: a one-time setup": the state root, the workspace and every worker repo, and why the state
    root does not cover worktrees.
  - §9 adds troubleshooting rows.
- **50-12 runbook (`50-12-signoff-evidence.md`):** a note under §1 (the new gates, the renumbering, the trust setup
  incl. worker repos) and under check 9.

## Verification

- Compile/import: PASS (`py_compile` on the lib, the CLI and the tests; the lib imports).
- Tests: `python3 tests/run_all_tests.py` (foreground, full log captured): `Ran 3101 tests in 683.516s`, then
  `OK (skipped=2)`, exit 0. The 50-13 section alone: 45 tests plus 52 subtests, all passing.
- Installed (install.sh --yes re-run): no. The manager does it after the commit; this task forbids it.

## Blockers

None. For the lead's attention (decisions, not blockers):
1. F-3 extends the brief: worker repos are also gated, and the lookup is git-bounded. Both come from verified Claude
   Code behaviour. The operator's "trust the state root once" does not cover worker worktrees, so the docs now say to
   trust each worker repo too. Their lookup is git-bounded in the same way. For 037, trust each repo its worker steps
   name (and the acdn workspace) before `enable`.
2. F-4 refuses rather than warns at `enable`, with `--login-days 0` to override.

## Questions for User

None.
