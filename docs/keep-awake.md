# Keep the machine awake while agents work

The `keep-awake` feature stops the machine from suspending (and from idle
suspend) while a Claude Code session is working, including while it is parked
on a question. When nothing is working, the machine sleeps as normal.

Linux only: it uses logind inhibitor locks (`systemd-inhibit`). Without
`systemd-inhibit` on PATH the hook does nothing.

## What counts as working

A session holds one lock from the moment a prompt is submitted until its turn
ends. While the turn is open the lock stays while any of these is true:

- a tool is running (Claude Code's Bash tool, foreground or background);
- the session is **parked**: `permission_request_hook.py` is waiting on the
  relay for a Telegram answer, or a terminal permission dialog is unanswered;
- the transcript or a subagent transcript changed in the last 30 minutes.

When the turn ends (`Stop`), the lock is released at once, unless background
work is still outstanding: then it is kept while that work runs, for at most
2 hours after the turn ended.

Closing the laptop lid still suspends: logind ignores inhibitor locks for the
lid switch by default (`LidSwitchIgnoreInhibited=yes`).

## Why a crashed session cannot keep the machine awake

The lock is held by a process, not written to a file:

```
systemd-inhibit --what=sleep:idle --mode=block … python3 keep_awake_hook.py --watch <session>
```

logind drops the lock when that process exits, whatever the reason. The
watcher exits by itself when:

| Situation | When the lock goes |
|-----------|--------------------|
| Claude exits or crashes (`kill -9`, OOM, closed tmux pane) | Immediately (the watcher waits on a pidfd for the claude process) |
| The turn ends normally | Immediately (`Stop` hook) |
| The session ends (`/exit`, `/clear`) | Immediately (`SessionEnd` hook) |
| You press Esc mid-turn (Claude Code fires no `Stop` then) | After 30 minutes of no activity |
| A background task or dev server is left running after the turn | At most 2 hours after the turn ended |
| The session is alive but wedged and fires no hook events | After 24 hours (backstop; a relay prompt parks for at most 12h) |

If the watcher itself is killed, logind releases the lock immediately. If the
hook cannot find its owning `claude` process, it takes no lock at all. Every
failure ends with the machine free to sleep, never with a lock stuck on.

One gap: if another tool's `Stop` hook blocks the stop (`decision: block`),
Claude keeps working without a lock until its next prompt. That errs toward
sleep, never toward a stuck lock.

## Seeing what holds the machine awake

```bash
python3 ~/.claude/hooks/keep_awake_hook.py --status   # our locks, with the reason each is held
systemd-inhibit --list                                # every lock on the machine (ours: WHO="claude-hooks keep-awake")
tail ~/.claude/keep_awake.log                         # acquire / release events, with reasons
python3 ~/.claude/hooks/keep_awake_hook.py --release-all   # drop all of ours now
```

## Configuration

Environment variables, read by the hook (set them in your shell or in the
`env` block of `~/.claude/settings.json`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `CLAUDE_KEEP_AWAKE` | `1` | `0` disables; a held lock is released on the session's next hook event |
| `CLAUDE_KEEP_AWAKE_IDLE_MINUTES` | `30` | Open turn with nothing running and no transcript change → release |
| `CLAUDE_KEEP_AWAKE_BACKGROUND_HOURS` | `2` | Longest hold after a turn ends with background work |
| `CLAUDE_KEEP_AWAKE_MAX_HOURS` | `24` | Release if the session fires no hook event for this long (`0` = never) |

Install or remove it with `./install.sh --with keep-awake` /
`./install.sh --uninstall keep-awake`. Running sessions pick up the change on
their next hook event (Claude Code reloads `settings.json` hooks live; seen on
2026-11-08). The hook commands are guarded, so a session that still runs the
old configuration after an uninstall does not fail its prompts.
