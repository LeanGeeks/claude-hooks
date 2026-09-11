# Daily permission reviewer — schedule and operator notes

The scheduled consumer that closes epic 22's loop. Once a day it drains this
workspace's permission-proposal queue, reviews the previous day's permission
traffic, applies or rejects proposals with a commit, files or fixes parser
issues, and compacts the state store.

| Piece | Path |
|---|---|
| Seed prompt (the reviewer's instructions) | `docs/prompts/permission-review-daily.md` |
| Cron launcher | `shell/permission-review-daily.sh` |
| Compaction | `python3 .claude/hooks/permission_state_store.py compact` |
| Run log | `temp/permission-review/<UTC-date>.log` (gitignored) |
| Queue drained | `~/.claude/permission-queue/<project-key>/` |

## Installing the schedule

The installer writes the crontab line for you, behind the `daily-review-cron`
sub-toggle (epic 29, brd D13). Enable it once the `daily-review` feature is
installed:

```bash
./install.sh enable daily-review-cron
```

The installer writes this line, marked and reversible:

```cron
15 6 * * * /data/sync/work/leangeeks-ai/claude-hooks/shell/permission-review-daily.sh
```

using the actual checkout path it was run from. Verify it landed:

```bash
crontab -l | grep permission-review
```

Disable with `./install.sh disable daily-review-cron`, which removes the marked
line and leaves all other crontab entries untouched.

**Manual alternative.** If you prefer to write the crontab yourself:

```bash
crontab -e
```

```cron
15 6 * * * /path/to/claude-hooks/shell/permission-review-daily.sh
```

To run the launcher from a checkout at a non-standard path, either write the
path directly in the cron line or set `CLAUDE_HOOKS_REPO=<path>` in the line:

```cron
15 6 * * * CLAUDE_HOOKS_REPO=/alt/path /alt/path/shell/permission-review-daily.sh
```

### Why the crontab line names the repo path directly

The installer ships `shell/` entries two ways: `.bash` snippets are copied to
`~/.claude/shell/` for the user to `source`, and `claude-history` / `claude-roles`
are copied there and symlinked into `~/.local/bin`. Neither fits this launcher.
It is repo-bound by construction — it reads its prompt out of `docs/prompts/`
and spawns a session whose working directory *is* this checkout — so a copy on
`PATH` would still have to find the repo, and the only reliable way for it to do
that from cron's empty environment is a hard-coded path. This is why the
`daily-review` feature installs no files: it only verifies the launcher and
schedules it. Naming the repo path in the crontab line puts that one path in one
place, where the person editing the schedule can see it. The checkout is stable
on this machine; if it ever moves, the crontab line moves with it.

The `CLAUDE_HOOKS_REPO` override in the cron line above handles a checkout at a
different path without touching the launcher itself.

## What the launcher guarantees

**Model and effort are pinned.** `--model=sonnet --effort=high` are always
passed (override with `PERMISSION_REVIEW_MODEL` / `PERMISSION_REVIEW_EFFORT`;
the defaults are literals, so an empty environment still pins). This is epic 22
invariant 9 / brd H8: a spawn that pins neither serves whatever the operator's
last interactive `/model` wrote into the harness settings, and that value floats
between sessions. `amux-spawn` warns on stderr about exactly this on the non-TTY
path — a warning that appearing in the run log means the pinning broke.

**Cron's environment is nearly empty.** No profile is sourced. The launcher sets
`HOME` (from `getent passwd` if cron did not export it) and a full `PATH`
covering `~/.local/bin` (amux-spawn, uv, claude), `/usr/local/bin` (amux) and
`/usr/bin` (tmux, git, jq, python3).

**A silent 06:15 failure is diagnosable.** Everything after the environment
block — both streams, including amux-spawn's own warnings — is appended to
`temp/permission-review/<UTC-date>.log`, along with the resolved repo, model,
effort, PATH and the amux-spawn exit code. Appended, not truncated, so a manual
run does not erase the scheduled one.

## Watching a run

```bash
tail -f temp/permission-review/$(date -u +%Y-%m-%d).log   # the launcher's own log
amux ls                                                   # the session it started
amux peek permission-review-<n>                           # what the session is doing
```

The reviewer's final message is forwarded to Telegram by the idle-notification
hook — that is the delivery mechanism for the daily summary, and it costs
nothing extra.

## Running it by hand

```bash
./shell/permission-review-daily.sh
tail -n 40 temp/permission-review/$(date -u +%Y-%m-%d).log
```

For a rehearsal that must not touch live global settings, seed the session with
`PERMISSION_REVIEW_DRY_RUN=1` (skips the installer merge and the push) and
`CLAUDE_PERMISSION_QUEUE_DIR=<scratch>` (drains a fixture queue instead of the
real one).

## Compaction

The reviewer runs it as step 4, but it stands alone:

```bash
python3 .claude/hooks/permission_state_store.py compact --json
python3 .claude/hooks/permission_state_store.py compact --max-age-days 7
python3 .claude/hooks/permission_state_store.py compact --no-rotate-log
```

Terminal rows older than the retention window (30 days by default) are appended
to `~/.claude/permission_requests.archive.jsonl` and dropped from the hot file.
**Pending rows never move, regardless of age** — expiry retires those first, and
a pending row in cold storage would strand a hook still waiting on it. Rows with
missing or unparseable timestamps are kept: they are evidence. The whole
read-modify-write happens under the state store's existing exclusive `flock`
(brd H7), and the archive is appended *before* the hot file is rewritten, so an
interrupted run duplicates a row into cold storage rather than losing it.

The same run rotates `~/.claude/bash_manual_confirm.log` aside to
`bash_manual_confirm.log.<UTC-timestamp>` once it exceeds 5 MB. The PreToolUse
hook appends with `open(path, 'a')`, so the live log reappears on the next
command that is not auto-approved — no coordination needed.

## Adopting the reviewer in another repo

Steps 1–2 of the prompt (drain the queue, review the traffic) are
workspace-generic. Another repo adopts them by copying
`docs/prompts/permission-review-daily.md` into its own checkout and adding a
cron line for a launcher that exports `CLAUDE_HOOKS_REPO` pointing here. That
repo's proposals carry scope `workspace`, so it skips the installer merge, and
it must not run compaction — the state store is one user-scoped file for the
whole machine, and one scheduler owning it is the point.
