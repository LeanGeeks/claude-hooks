# 16-04 — Workspace seen-store

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §2.5, §4.2, §5.6 · [architecture.md](./architecture.md) §4

## Goal

Record which workspaces on this machine have actually talked to Telegram, so the
listener can offer them as a pick list — and refuse everything else.

The evidence exists nowhere durable today (brd §2.5): `permission_requests.jsonl`
carries `cwd` (`permission_state_store.py:81`) but its update paths rewrite the
file and it holds a working set — a live sample had **3 rows, 2 workspaces, one
of them `/test`**. The idle hook computes a workspace name at
`notification_hook.py:640` and throws it away.

This store is also the **allowlist** (brd §5.6): v1 accepts no arbitrary paths
from Telegram, so a workspace that is not in here cannot be spawned into.

Independent of the relay and of every other task in this epic — buildable and
testable on its own.

## Scope

### New module — `.claude/hooks/telegram_workspaces.py`

```python
STORE = Path.home() / ".claude" / "telegram_workspaces.json"

def record(cwd: str, name: str | None = None) -> None
def list_recent(limit: int = 20) -> list[Workspace]     # newest first
def find(name: str) -> list[Workspace]                  # case-insensitive basename match
def resolve_root(cwd: str) -> str                       # see below
```

```json
{ "version": 1,
  "workspaces": {
    "/data/sync/work/leangeeks-ai/claude-hooks": {
      "name": "claude-hooks", "last_seen": "2026-08-02T15:28:11Z", "count": 37 } } }
```

`Workspace` is a frozen dataclass `(path, name, last_seen, count)`.

### Root normalisation

`record` stores `resolve_root(cwd)`, not `cwd`: walk up at most 5 levels looking
for a directory containing `.git` or `.claude`, and fall back to `cwd` when
neither is found. Without this, a session whose cwd is a subdirectory registers a
"workspace" called `hooks` that would later spawn a session inside
`.claude/hooks`. Never walk above `$HOME` or into `/`.

`name` defaults to `basename(resolve_root(cwd))`; callers that already have a
display name pass it.

### Writers — three call sites, one line each

| Where | Source of `cwd` |
|---|---|
| `telegram_permission_router.send_permission_message` | `request.cwd` |
| `telegram_permission_router.send_question_message` | `request.cwd` |
| `notification_hook.py` idle path | the hook payload's `cwd`, next to the existing `get_workspace_name(cwd)` call |

Record **after** a successful send (a `message_id` came back), not before: the
store's claim is "this workspace has reached Telegram from this machine", and an
unreachable relay must not populate an allowlist.

Every call is wrapped so a store failure cannot affect the send. These are the
most latency- and reliability-sensitive paths in the product; a write that
raises, blocks, or takes a lock it cannot get must degrade to a no-op.

### Concurrency and pruning

- Read–modify–write under an `flock` on the store file, atomic `tmp + rename`,
  following `permission_state_store.py`'s discipline. Several sessions on one
  machine hit this concurrently.
- Prune entries whose `last_seen` is older than 180 days on write, and cap the
  file at 200 entries (drop the oldest). This is a pick list, not an audit log —
  `permission_actions.jsonl` is the audit log.
- A missing, empty, truncated or unparseable file reads as **empty**, and the
  next write replaces it. Never raise at a call site.

### Installer

Add `telegram_workspaces.py` to `REQUIRED_HOOKS` in
`install-claude-config.sh:163` **in this task**, not in 16-07. From here on
`telegram_permission_router` imports it, and the installer copies only what that
list names — a mid-epic install would otherwise deploy a router that cannot
import its own dependency and silently disable Telegram (the same trap 15-01
documents).

## Implementation notes

- `list_recent` sorts by `last_seen` descending, then `count` descending; ties
  broken by name for stable test assertions.
- `find` returns *all* matches so the caller can disambiguate two workspaces
  sharing a basename (16-05 relies on this and 16-06 renders the choice).
- No caching: hooks are short-lived processes.
- Do not import the relay client, `httpx`, or anything from `amux_spawn_lib`
  here. This module is pure filesystem so the listener can import it without
  dragging hook dependencies into a long-lived process.

## Testing

New `tests/test_unit_telegram_workspaces.py`, registered in
`tests/run_all_tests.py` alongside the existing unit suites. Point the store at a
`tmp_path` for every test — never touch the real `~/.claude`.

- `record` creates, upserts (`count` increments, `last_seen` advances), and keeps
  other entries untouched.
- `resolve_root`: subdirectory with `.git` above → repo root; subdirectory with
  `.claude` above → that dir; nothing found within 5 levels → unchanged cwd;
  never escapes above `$HOME`.
- `list_recent` ordering and limit; `find` is case-insensitive and returns both
  entries when two paths share a basename.
- Corrupt / truncated / empty / missing file → empty list, and the next `record`
  produces a valid store.
- Concurrent writers (threads or processes) → no lost update, no partial file.
- Pruning by age and by cap.
- Router integration: a successful `send_permission_message` records once; a send
  that returns `None` records nothing; a store that raises does not change the
  send's return value (patch the store to raise and assert the send still
  succeeds).
- Idle integration: an idle notification records the workspace; the existing
  notification-hook tests stay green.

## Done criteria

- [ ] `telegram_workspaces.py` with `record` / `list_recent` / `find` /
      `resolve_root`, atomic and locked.
- [ ] Root normalisation prevents subdirectory entries.
- [ ] Three writers wired, recording only after a successful send, all fail-open.
- [ ] Corrupt or missing store degrades to empty everywhere.
- [ ] Age and size pruning.
- [ ] `telegram_workspaces.py` listed in `REQUIRED_HOOKS`.
- [ ] New unit suite registered in `tests/run_all_tests.py`; existing router and
      notification suites unchanged and green.
