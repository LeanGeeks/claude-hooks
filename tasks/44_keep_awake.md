# Task 44 — Keep the machine awake while agents work

**Status:** done (one human check open) · **Type:** feature · **Created:** 2026-11-08 · **Rev:** 1
**Scope:** `.claude/hooks/keep_awake_hook.py` (new), `install.sh` (feature
`keep-awake`; per-script hook ownership), `tests/test_unit_keep_awake.py`
(new), `tests/test_unit_installer.py` (`TestSharedEventOwnership`),
`docs/keep-awake.md` (new), `docs/installer.md`, `architecture.md`.

## Goal

The machine must not suspend while a Claude Code session is working,
**including while it is parked** on a question. A crashed session must never
keep the machine awake indefinitely.

## Decisions

- **D1 — The lock is a process.** One logind inhibitor per working session:
  `systemd-inhibit --what=sleep:idle --mode=block … keep_awake_hook.py --watch <sid>`.
  logind drops the lock when the watcher exits, so state on disk can never
  hold the machine awake by itself.
- **D2 — Owner-bound.** The hook walks up to the owning `claude` process and
  records PID + start ticks (epic 26's identity rule). The watcher waits on a
  pidfd for it: crash, `kill -9`, OOM or a closed pane release the lock at once.
  No owner found → no lock.
- **D3 — Parked is busy.** A parked session is a hook blocking *inside* a
  turn: `permission_request_hook.py` waiting on the relay (a descendant of the
  owner), or a terminal permission dialog (`Notification[permission_prompt]`,
  valid until the transcript changes). Epic 23 async questions need nothing:
  the asking session exits, and the listener catches up on wake.
- **D4 — Esc is the idle case.** Verified live on 2026-11-08 (Claude Code in
  `ai-playground-2`): Esc on a running Bash tool kills the tool shell, writes a
  `tool_result` + `[Request interrupted by user for tool use]` to the
  transcript, and fires **no `Stop`**. So the watcher releases an open turn
  that has no running tool, nothing parked and no transcript (or subagent
  transcript) change for `IDLE` minutes (default 30).
- **D5 — "Running" is a tool shell under the owner.** Claude Code runs Bash
  (foreground and background) as `bash -c source …/shell-snapshots/…`. MCP
  servers are children too and must not count, hence the marker rather than
  "any child". Also verified live: the transcript gets the `tool_use` entry
  only when the call starts and nothing while it runs, so transcript recency
  alone would drop a long foreground command.
- **D6 — After `Stop`.** Release at once unless `background_tasks` is non-empty
  or a tool shell is still running; then keep while that holds, capped at
  `BACKGROUND_HOURS` (default 2) after the turn ended, so a dev server left
  running doesn't keep the machine awake for good.
- **D7 — Backstop.** No hook event for `MAX_HOURS` (default 24) → release.
  Covers a live but wedged session. The relay parks a prompt for at most 12h.
- **D8 — Per-script hook ownership in the installer.** amux and keep-awake
  both own `UserPromptSubmit`/`Stop`/`SessionEnd`/`Notification[permission_prompt]`.
  The old per-event merge would let `--only keep-awake` wipe amux's `Stop`
  entry, and `--uninstall amux` wipe keep-awake's. Ownership is now
  (event, script): a re-wire drops only our entries for the scripts it
  re-wires (plus entries whose script is no longer in `REQUIRED_HOOKS`);
  uninstall drops only the uninstalled feature's scripts
  (`UNINSTALL_HOOK_SCRIPTS` replaces `UNINSTALL_HOOK_EVENTS` /
  `UNINSTALL_NOTIFICATION_MATCHERS`). The amux probe now checks for
  `spawn_producer_hook.py` rather than "any of ours on `Stop`".
- **D9 — Linux only.** macOS (`caffeinate -i -w <pid>`) is a possible follow-up;
  nothing here is tested there.
- **D10 — Lid close still suspends** (`LidSwitchIgnoreInhibited=yes` default).
  Intended.
- **D11 — "Answered" is a user entry.** A parked hook or a terminal prompt
  holds until the *main* transcript gets a `user` entry stamped after it (the
  tool result, a denial, an Esc, or a new prompt). Assistant entries and
  subagent transcripts do not count, and a blocking hook that lingers after
  its prompt was answered stops counting. Its start time comes from btime
  (1 s resolution), so a 1.5 s slack keeps the preceding tool result from
  reading as its answer.
- **D12 — Open-turn tool shells are capped too.** After Esc, a background
  shell (a dev server) would otherwise hold until the 24h backstop. Foreground
  Bash is capped at 10 min, so with no transcript activity a tool shell holds
  for at most `BACKGROUND_HOURS` since the last activity.
- **D13 — Guarded hook commands.** `[ -f <hook> ] || exit 0; python3 <hook> …`:
  after an uninstall, a session still on the old configuration would
  otherwise get exit 2 from `python3` and fail its prompts.

## Known limitation

If *another* `Stop` hook blocks the stop (`decision: block`), Claude keeps
working without a lock until its next prompt or permission prompt. This only
fails toward sleep, and no hook in this repo blocks `Stop`.

## Found on the way (fixed here)

- `install.sh`'s permissions merges (union and overwrite) rebuilt the whole
  `permissions` object, dropping `defaultMode`, `additionalDirectories` and any
  other key on every install. Seen live: the task-44 install removed
  `"defaultMode": "default"` from `~/.claude/settings.json` (restored). Both
  merges now replace only `allow`/`deny`/`ask`; test
  `test_other_permissions_keys_survive_both_modes`.

## Acceptance

- [x] Unit: acquire, idempotent re-acquire, `Stop` release, `SessionEnd`
      release, owner `SIGKILL` release without any hook (and faster than the
      poll interval), killed holder swept and re-acquired, dead owner swept by
      another session's event, Esc/idle release, max-hold backstop, tool and
      relay-parked holds, terminal permission prompt hold until answered,
      background hold and both background releases, disabled, missing
      `systemd-inhibit`, unknown owner, malformed payload, silent stdout,
      owner discovery through `sh`.
- [x] Installer: both features wired on shared events; either installed alone
      keeps the other; no duplicates on re-install; either uninstalled leaves
      the other; amux probe not fooled; retired script dropped; foreign entry
      survives.
- [x] Full suite green: `python3 tests/run_all_tests.py` → 2199 tests, OK
      (skipped=2), 2026-11-08, after the review and permissions fixes.
- [x] Mutations caught (cp-backup protocol): no pidfd wake (also showed the
      owner-exit test was passing via the watcher's startup check, now fixed),
      any entry type counted as an answer, parked ignoring answers, no
      open-turn tool cap, no parked detection, and permissions-key preservation.
- [x] Independent review: no orphan path found. Findings 1, 2, 4, 5, 6, 7, 8, 9
      fixed (D11–D13, name-based hook match, flock'd lock sweep, jq guards,
      tests); 3 documented above.
- [x] Live, real `systemd-inhibit`, 2026-11-08:
  0. Stand-in owner + `kill -9` → released in 86 ms; logind `BlockInhibited`
     showed `sleep:idle` while held and dropped it after.
  1. Real session in `ai-playground-2`, 40 s foreground command → lock listed
     as `Claude session 02688a52 working in …/ai-playground-2`.
  2. Turn ended → `released: turn ended` the same second as the `stop`
     lifecycle event.
  3. `kill -9` of the real `claude` mid-tool → released in 104 ms
     (`session process exited`).
  4. Esc mid-tool with `IDLE_MINUTES=1` → tool killed, no `Stop`, lock kept,
     released 62 s after Esc (`idle for 1 min`).
  5. Real parked session: after the install, an already-running session
     (`ring-central`) picked up the hooks without a restart and was reported
     `parked on a relay prompt` while its `permission_request_hook.py` waited
     on Telegram.
- [ ] Human: with a lock held, `systemctl suspend` is refused (or GNOME's
      automatic suspend does not fire); with none held, it suspends.
