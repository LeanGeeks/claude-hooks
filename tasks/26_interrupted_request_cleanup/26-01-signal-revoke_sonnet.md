# Task 26-01 — The hook revokes its own cards when it is signalled

**Status:** todo · **Depends on:** none (shares two edit points with 26-02 — see
[state.md](./state.md) "Shared edit points")
**Read first:** [brd.md](./brd.md) §1 (findings 1–2, 4), §3 **H1/H2/H3**, §2.2 ·
[state.md](./state.md) invariants

## Goal

When the parked `PermissionRequest` hook is signalled — which is what an ESC in
the TUI does to it — it marks its own live rows terminal and strips their Telegram
buttons before exiting, instead of dying silently and leaving a live card behind.

**This is layer 1: the fast path, not the guarantee.** It is worth nothing if the
harness sends `SIGKILL`, and that is unmeasured today. Its first debug line is
the probe that settles it (§5). The guarantee is
[26-02](./26-02-orphan-sweep_sonnet.md), which must land regardless of what the
probe says.

**Do not** make this task conditional on the probe's outcome. A handler that is
never invoked costs one `signal.signal` call at hook start; a missing handler
costs a live card every time a catchable signal *is* what arrives.

## Scope — three files

### 1. `permission_state_store.py` — a bounded lock, and one constant

**a. `RESOLUTION_SOURCE_INTERRUPTED = "interrupted"`** next to the three existing
constants (`:71-73`). Nothing reads `resolution_source` — verified by
`grep -rn "resolution_source" --include=*.py .`, which finds only writers plus one
field-shape assertion in `tests/test_unit_state_store.py:522` — so a fourth value
is additive.

**b. `update_request_state(..., lock_timeout: Optional[float] = None)`.** This
is the H2 fix and the reason this edit lives here rather than in the hook.

When `lock_timeout` is `None` the function behaves **exactly as today**: blocking
`_acquire_lock`. When it is a float, acquire with `fcntl.LOCK_EX | fcntl.LOCK_NB`,
retrying every 25 ms until the budget is spent; on failure `debug_log` and return
`None` — the same "did not update" contract the function already has for a
terminal row, so no caller learns a new failure mode.

```python
def _acquire_lock_bounded(file_obj, timeout: float) -> bool:
    """Non-blocking acquire with a deadline. True if the lock is held."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.025)
```

**Why this is not optional.** `_acquire_lock` (`:148`) is a blocking `LOCK_EX`.
Python delivers signals on the main thread between bytecodes, so the handler can
start while that same thread sits inside a locked `with open(...)` block. `flock`
locks belong to the *open file description*, so re-opening the file from the
handler and blocking on it waits for a lock the process already holds and will
never release — a permanent hang, in a process the harness is about to `SIGKILL`
anyway. Bounded acquire converts that hang into a fast, logged give-up, and
26-02 catches what was given up on.

### 2. `telegram_permission_router.py` — one moved function

Move `revoke_telegram_message(request)` out of `posttool_hook.py:138` into the
router, unchanged, and leave `posttool_hook.py` importing it under its current
name so existing callers and tests keep working. It already resolves the row's
role token (`resolve_role_token`, `posttool_hook.py:107`), which is what makes a
role-routed card cancel against the installation that created it rather than
404ing against the default — do not reimplement that in a second place.

The only callers today are `posttool_hook.py:237` and
`tests/test_integration_permission_request.py:1399`, which reaches it as
`self.posttool_hook.revoke_telegram_message` — so the re-export must keep that
attribute resolving on the module, not merely the name importable. Re-grep
before moving.

*(26-02 needs this same helper. Whoever lands first performs the move; the other
task consumes it — see [state.md](./state.md).)*

### 3. `permission_request_hook.py` — the registry and the handler

**Imports this task adds**, and one name trap. `signal` is not imported in this
file today; `os` is (`:33`). `OrderedDict` needs
`from collections import OrderedDict`. `RESOLUTION_SOURCE_INTERRUPTED` goes into
the existing `from permission_state_store import (...)` block (`:46-57`), which
already provides `PermissionRequest`, `RequestState` and `update_request_state`.

**The trap:** this file imports the router **as `telegram_router`** (`:43`) —
`telegram_permission_router` is not a name in this module, while
`posttool_hook.py` imports it under the full name (its `:27`). Use
`telegram_router.` here, and do not copy a call between the two hooks.


**a. A module-level registry** of rows this process owns and has not yet seen
resolved:

```python
# request_id -> the row, for cleanup on signal (epic 26 layer 1).
_LIVE_ROWS: "OrderedDict[str, PermissionRequest]" = OrderedDict()
```

Register at **both** creation sites, and re-register after the message id is
known so the handler can revoke:

- `:1153` — the AskUserQuestion child rows (inside `_try_send_group`, after
  `send_question_message` returns a non-`None` id);
- `:1421` — the ordinary single-tool row, after `set_telegram_message_id`.

A row with no `telegram_message_id` is still worth registering: marking it
terminal is the half that matters.

**b. Arm once**, lazily, at first registration (never at import — the module is
imported by tests and by `posttool_hook`):

```python
for _sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    try:
        signal.signal(_sig, _on_interrupt)
    except (ValueError, OSError):   # not the main thread / not supported
        pass
```

`SIGINT` is included deliberately: today it raises `KeyboardInterrupt`, unwinds
without touching the rows, and leaves exactly the same orphan. There is no
existing `SIGINT` behaviour in this hook to preserve.

**c. The handler**, in this order — the order is the H3 budget:

```python
def _on_interrupt(signum, frame):
    debug_log(f"Interrupt: signal {signum}; revoking {len(_LIVE_ROWS)} live row(s)")
    rows = list(_LIVE_ROWS.values())
    _LIVE_ROWS.clear()
    for row in rows:
        try:
            update_request_state(
                row.request_id,
                RequestState.RESOLVED_TERMINAL,
                resolution_source=RESOLUTION_SOURCE_INTERRUPTED,
                lock_timeout=0.5,
            )
        except Exception as e:          # noqa: BLE001 — H1
            debug_log(f"Interrupt: row {row.request_id} not marked: {e}")
    def _revoke_all():
        for row in rows:
            if row.telegram_message_id:
                try:
                    telegram_router.revoke_telegram_message(row)
                except Exception as e:  # noqa: BLE001 — H1
                    debug_log(f"Interrupt: revoke of {row.telegram_message_id} failed: {e}")

    t = threading.Thread(target=_revoke_all, daemon=True)
    t.start()
    t.join(1.0)                 # bounded; see decision 5
    os._exit(0)
```

Five decisions in that block that are **not** free to change:

1. **All rows are marked before any is revoked.** The store write is local and
   cheap; the revoke is network and may be cut off mid-flight. Marking first
   means a `SIGKILL` that lands during the revoke still leaves the *state*
   correct, and 26-02 leaves it alone (it is no longer pending) while the relay
   reaper takes the buttons at TTL.
2. **`os._exit(0)`, not `sys.exit`.** `sys.exit` raises into whatever the main
   thread was doing — including back into the `finally: stop.set()` at `:1010`
   and the wait loop's poller — which can re-enter the store and re-block. This
   process has nothing left to do.
3. **Exit code 0.** The hook contract is "no output = no decision"; a non-zero
   exit from a `PermissionRequest` hook is a different signal to the harness
   entirely, and this path must not send it. (brd §3 H1)
4. **Never raise out of the handler.** Every step is individually wrapped. A
   traceback from a signal handler in a hook is noise the operator cannot act on.
5. **The revoke is bounded by a daemon thread, not trusted to return.** This is
   the one place the naive version is actively wrong: `revoke_telegram_message`
   → `cancel_message` → `RelayClient._request`, whose defaults are **connect 5 s
   / read 35 s** (`relay-server/relay_server/client.py:93-94`) with **up to three
   retries and exponential backoff** (`:158-182`). Against an unreachable relay a
   single card's revoke can therefore run **well over a minute** — inside a
   process the harness is waiting to reap. A daemon thread plus `join(1.0)` caps
   it: what lands, lands; what does not is covered by 26-02 and, failing that,
   the relay's own TTL reaper. `threading` is already imported (`:34`). Do **not**
   "fix" this by passing a shorter timeout down into the relay client — that is a
   cross-package change to a library this epic does not own (brd §2.2).

**d. Deregister on normal resolution** so a later signal cannot re-touch a settled
row: drop the id from `_LIVE_ROWS` wherever the hook already writes a terminal
state for it (`:417`, `:484`, `:812`, `:971`, `:1187`, `:1442`). This is belt to
`update_request_state`'s own idempotency (`:338`: terminal rows return `None`), so
if a call site is missed the behaviour is still correct — do not restructure those
paths to make it exhaustive.

## Testing

`tests/test_integration_permission_request.py` for the handler (it already has the
hook's harness), `tests/test_unit_state_store.py` for the lock parameter. Both run
under `python3 tests/run_all_tests.py`.

**Isolation — read this before you run anything.** `tests/run_all_tests.py:31`
and `tests/conftest.py:17` redirect `CLAUDE_PERMISSION_STATE_FILE` to a temp store
via `os.environ.setdefault` at import time. That runner-level redirect is the
*only* isolation there is: the `setUp` in `tests/test_unit_state_store.py:52-56`
looks like it isolates — it makes a temp dir and saves `STATE_FILE` — but it never
redirects anything, so a test file run **directly** writes to the real
`~/.claude/permission_requests.jsonl`. It has already happened: **324** rows in
the live store are old fixtures (`test-expired-get`, `test-dbl-allow`, ...),
newest 2026-06-20. Run only through `python3 tests/run_all_tests.py`.

| # | Case | Expect |
|---|---|---|
| 1 | `update_request_state(..., lock_timeout=None)` | today's behaviour, byte-identical result |
| 2 | lock held by a **separate process** (fork/subprocess holding `flock`), `lock_timeout=0.25` | returns `None` within ~0.3 s, row unchanged, no hang |
| 3 | `_on_interrupt` with two registered rows | both rows `resolved_terminal` / `interrupted`; `revoke_telegram_message` called once per row with a message id |
| 4 | registered row that has **no** `telegram_message_id` | marked terminal, no revoke attempted |
| 5 | row already resolved (`telegram`) then a signal | row keeps `telegram`/its decision — the handler's update no-ops |
| 6 | `revoke_telegram_message` raises | the other rows are still processed; no exception escapes |
| 7 | arming is idempotent | registering N rows installs the handler once |

Case 2 is the H2 regression guard and must use a **separate process** — an
in-process second `flock` on the same fd would not reproduce it. Case 5 guards
"the terminal/Telegram answer always wins".

Do **not** test by sending a real signal to the test runner. Call `_on_interrupt`
directly, with `os._exit` patched.

Baseline: re-measure `python3 tests/run_all_tests.py` before you start and report
before/after counts. (Epic 21 closed at 724 root hooks; treat that as stale.)

## Done criteria

1. `python3 -m py_compile` clean on all three files.
2. `python3 tests/run_all_tests.py` green, 7 cases added, before/after counts
   reported.
3. `grep -rn "revoke_telegram_message"` shows every pre-existing caller still
   resolving, after the move.
4. The `Interrupt: signal N` debug line is written **before** any other work in
   the handler — it is the probe (§5) and must survive even if everything after
   it fails.
5. **Not live until installed:** say so in your report; run
   `./install-claude-config.sh` only if the epic manager asks.
6. No edit outside the three files named above (plus the two test files).

## 5. The probe this task carries

Nobody has measured whether the harness sends a catchable signal or `SIGKILL`.
After this task is **installed**, the answer is one grep away following any ESC:

```
grep "Interrupt: signal" ~/.claude/permission_request_debug.log
```

- A line, with `15` (`SIGTERM`) or `1`/`2` — layer 1 works; note the number in
  [state.md](./state.md).
- **No line, and the row was closed by `orphaned` instead** — the harness
  `SIGKILL`s, layer 1 cannot help, and 26-02 is the whole fix. Record that too:
  it is a real finding, and it stops the next person re-proposing this handler.

Either outcome is a result. Do not treat "no line" as a bug in this task without
first checking whether the row was closed by 26-02.
