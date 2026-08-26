# Task 26-02 — A row whose owning process is gone gets closed

**Status:** todo · **Depends on:** none (shares two edit points with 26-01 — see
[state.md](./state.md) "Shared edit points")
**Read first:** [brd.md](./brd.md) §1 (findings 1, 4, 5, 6), §2.3, §3 **H4/H5/H7** ·
[state.md](./state.md) invariants

## Goal

Stamp every request with the identity of the process that created it, and let any
later hook invocation close the rows whose owner no longer exists.

**This is layer 2: the guarantee.** Unlike [26-01](./26-01-signal-revoke_sonnet.md)
it holds under `SIGKILL`, a crash, an OOM kill and a reboot, because it asks a
question that does not depend on the dying process cooperating: *is PID X, started
at tick T, still there?* If 26-01's probe comes back "no signal is ever
delivered", this task is the entire epic.

## Scope — three files

### 1. `permission_state_store.py` — owner identity

**a. Three optional fields** on `PermissionRequest` (`:77`), after `terminal_answers`:

```python
    # Who created this row (epic 26 layer 2). A row whose owner is gone is
    # closed by ``sweep_orphaned_requests``; a row with no owner (written before
    # epic 26) is never swept and keeps its TTL behaviour.
    owner_pid: Optional[int] = None
    owner_start_ticks: Optional[int] = None   # /proc/<pid>/stat field 22
    owner_host: Optional[str] = None
```

No migration. `from_dict` (`:110`) already filters to known fields and defaults
missing ones, so all 1634 existing rows load unchanged (brd §3 H7).

**b. `_proc_start_ticks(pid)`** — the H4 anti-PID-reuse token:

```python
def _proc_start_ticks(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat, or None if it cannot be read.

    ``comm`` (field 2) is parenthesised and may contain spaces *and* ')', so the
    only safe split is on the LAST ') ' — never ``split()`` on the whole line.
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            rest = f.read().rsplit(") ", 1)[1].split()
        return int(rest[19])          # field 22 = index 19 after state (field 3)
    except Exception:
        return None
```

`None` means **"cannot tell"**, and every caller must treat it as *alive*. Never
sweep on a guess (brd §3 H4).

**c. Stamp in `create_request`** (`:188`) — `os.getpid()`,
`_proc_start_ticks(os.getpid())`, `socket.gethostname()`. `socket` is the one
import this task adds to the store; `os`, `time`, `fcntl` and `List` are already
there (`:18-27`). All three inside one
`try/except` that leaves them `None` on any failure; a row that cannot be stamped
is a row that is never swept, which is exactly today's behaviour.

**d. `RESOLUTION_SOURCE_ORPHANED = "orphaned"`** next to the existing constants
(`:71-73`).

**e. `sweep_orphaned_requests() -> List[PermissionRequest]`** — one locked
read-modify-write pass, modelled on `expire_pending_requests` (`:537`), which is
the closest existing shape. For each `pending`, non-expired row:

| Condition | Action |
|---|---|
| `owner_pid` is `None` (pre-epic row) | skip |
| `owner_host != socket.gethostname()` | skip |
| `owner_pid == os.getpid()` | skip — **this process** |
| `os.kill(pid, 0)` raises `ProcessLookupError` | **sweep** |
| `os.kill(pid, 0)` raises `PermissionError` | skip — alive, another user |
| `_proc_start_ticks(pid)` is `None` | skip — cannot tell |
| stored `owner_start_ticks` is `None` | skip — cannot tell |
| ticks differ from the stored value | **sweep** — PID was reused |
| otherwise | skip — alive |

Sweeping means: state `RESOLVED_TERMINAL`, `resolution_source` `orphaned`,
`resolved_at` now, `updated_at` now. **Return the swept rows** so the caller can
revoke their Telegram messages — the store does no network I/O, and that
separation is why this function is unit-testable without mocking the relay.

Rewrite the file only when at least one row changed.

### 2. `telegram_permission_router.py` — the shared revoke

This task needs `revoke_telegram_message(request)` from `posttool_hook.py:138`
(role-token resolution included — see 26-01 §2 for why that matters). If 26-01
has already moved it to the router, consume it; if not, perform the move here
under the same terms. **Exactly one of the two tasks does the move.**

### 3. The two call sites

**a. `permission_request_hook.py:1371`**, in `main()`, immediately after the
existing `cleanup_expired_requests()` — the precedent this follows.
Add `sweep_orphaned_requests` to that file's `from permission_state_store import
(...)` block (`:46-57`), and mind the alias: this hook imports the router **as
`telegram_router`** (`:43`) while `posttool_hook.py` imports it under its full
name (`:27`). The two call sites below differ for exactly that reason:

```python
        cleanup_expired_requests()
        for _row in sweep_orphaned_requests():
            try:
                telegram_router.revoke_telegram_message(_row)
            except Exception as e:      # noqa: BLE001 — H1
                debug_log(f"Sweep: revoke of {_row.telegram_message_id} failed: {e}")
```

**b. `posttool_hook.py`**, in `main()`, after `load_telegram_config()` and
**before** `find_pending_request_by_tool_session` — this is the call site that
gives the epic its latency, because PostToolUse fires on every tool call in every
session on this machine.

```python
        for _row in sweep_orphaned_requests():
            try:
                telegram_permission_router.revoke_telegram_message(_row)
            except Exception as e:      # noqa: BLE001 — H1
                log_debug(f"Sweep: revoke of {_row.telegram_message_id} failed: {e}")
```

Note the two differences from (a) that are not stylistic: the module name, and
`log_debug` — `posttool_hook.py`'s logger is called that, not `debug_log`.

Both wrapped so no exception can escape (brd §3 H1).

## 4. Cost — measure it, then decide

`posttool_hook` already makes one full pass over a **3.7 MB / 1634-row** JSONL per
tool call (`find_pending_request_by_tool_session`, `:738`). This adds a second.

**Measure both and report the numbers** — from the repo root, against a **copy**,
never the live store, because this function rewrites rows:

```
cp ~/.claude/permission_requests.jsonl /tmp/store-probe.jsonl
CLAUDE_PERMISSION_STATE_FILE=/tmp/store-probe.jsonl python3 - <<'PY'
import sys, time; sys.path.insert(0, ".claude/hooks")
import permission_state_store as s
for label, fn in (("sweep", s.sweep_orphaned_requests),
                  ("existing full pass", s.get_all_pending_requests)):
    t = time.perf_counter(); fn()
    print(label, round((time.perf_counter() - t) * 1000, 1), "ms")
PY
```

The copy is also the honest input: it carries the real 1634-row shape,
including the 324 legacy fixture rows and every row with no owner at all.

- **≤ 50 ms:** call it unconditionally at both sites. Done.
- **> 50 ms:** gate the **PostToolUse** site behind a stamp file
  (`~/.claude/.last-orphan-sweep`, ISO timestamp, sweep at most once per 60 s;
  unreadable/missing stamp means sweep). Leave the **PermissionRequest** site
  ungated — it runs once per prompt, not once per tool call, and it is about to
  add a row of its own.

Say which branch you took and why, with the measurement. Do not gate on a guess in
either direction.

## Testing

`tests/test_unit_state_store.py`, following its existing case style.

**Isolation — read this before you run anything.** `tests/run_all_tests.py:31`
and `tests/conftest.py:17` redirect `CLAUDE_PERMISSION_STATE_FILE` to a temp store
via `os.environ.setdefault` at import time. That runner-level redirect is the
*only* isolation there is: the `setUp` in `tests/test_unit_state_store.py:52-56`
looks like it isolates — it makes a temp dir and saves `STATE_FILE` — but it never
redirects anything, so a test file run **directly** writes to the real
`~/.claude/permission_requests.jsonl`. It has already happened: **324** rows in
the live store are old fixtures (`test-expired-get`, `test-dbl-allow`, ...),
newest 2026-06-20. Run only through `python3 tests/run_all_tests.py`.

This task needs that warning more than most: unlike every existing case, which
only touches rows it created, `sweep_orphaned_requests()` **rewrites rows it did
not create**. Pointed at the live store it would close real pending requests.

| # | Row | Expect |
|---|---|---|
| 1 | owner pid = a **reaped child** (spawn `true`, `waitpid`, then sweep) | swept, `resolved_terminal` / `orphaned`, returned to caller |
| 2 | owner pid = `os.getpid()` with matching ticks | untouched |
| 3 | owner pid alive but `owner_start_ticks` deliberately wrong | swept (PID reuse) |
| 4 | `owner_pid = None` (legacy row) | untouched |
| 5 | `owner_host = "someone-else"` | untouched |
| 6 | row already `allow` / `resolved_terminal` | untouched, not returned |
| 7 | expired pending row | left to `expire_pending_requests`, not double-handled |
| 8 | `create_request` | writes all three owner fields; the row round-trips through `from_dict` |
| 9 | legacy row **without** the three keys | `from_dict` accepts it (H7 regression guard) |
| 10 | `_proc_start_ticks` on a comm with spaces/parens | parses (fake `/proc` file, or assert against `os.getpid()`) |
| 11 | nothing to sweep | file bytes unchanged (no rewrite) |

Case 1 must use a **genuinely dead** PID — a reaped child, not a random high
number, which could be alive. Case 3 is the H4 guard.

Baseline: re-measure `python3 tests/run_all_tests.py` before you start and report
before/after counts.

## Done criteria

1. `python3 -m py_compile` clean on all four touched files.
2. `python3 tests/run_all_tests.py` green, 11 cases added, before/after counts
   reported.
3. The cost measurement from §4, with the branch you took.
4. Demonstrated end-to-end against the **repo** copy: create a pending row owned
   by a process you then kill, run the sweep, show the row's before/after JSON.
5. **Not live until installed:** say so; run `./install-claude-config.sh` only if
   the epic manager asks.
6. No edit outside the four files named above (plus the test file).
