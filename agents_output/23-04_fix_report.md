# Fix Report — 23-04 Questions MCP Server

Addressed all four issues from the review report. No rework of existing logic;
all changes are strictly scoped to the raised findings.

---

## Issue 1 (MEDIUM) — Broken fixture in `test_min_interval_allows_after_cooldown`

**Files changed:**
- `tests/test_unit_questions_mcp.py:694–704`

**What changed:**

Two calls inside the test constructed fixtures while `CLAUDE_PROJECT_DIR` still
pointed at `_ISOLATION_DIR` (the module-level isolation sentinel):

1. `qs.load_questions_config(self.ws.workspace_dir)` — `find_roles_file` saw
   `CLAUDE_PROJECT_DIR` set, looked in `_ISOLATION_DIR/.claude/roles.toml`
   (which doesn't exist), and returned `None`.
2. `roles_config.load_catalog(self.ws.workspace_dir)` — same path through
   `find_roles_file`, returned `None`, leaving the mock cache without a catalog.

Both calls now pass `roles_path=self.ws.roles_path` / `path=self.ws.roles_path`
directly so `find_roles_file` is bypassed entirely and the env-var isolation
doesn't interfere.

The `skipTest` escape was removed and replaced with:

```python
self.assertIsNotNone(
    patched_config,
    "load_questions_config returned None — fixture is broken, not a skip",
)
```

A broken fixture now fails loudly. The test runs and asserts both dispatches
succeed.

**Audit of the rest of the test file for silently-passing patterns:**

Searched for `skipTest`, `bare except`, `try/except: pass`, and assertions that
only check "no exception raised":

- `skipTest`: one occurrence — the one just fixed. No others.
- `try/except: pass`: none.
- Bare `except`: none.
- "No exception" assertions (assertions containing only `assertIsNotNone` on an
  opaque return value with no further structural check): none among the new tests.
  The 35 previously passing tests all assert on specific fields (`dispatched`,
  `error`, `id`, queue-file contents, index round-trips). No test has a pattern
  that can silently pass when its fixture breaks.

---

## Issue 2 (MEDIUM) — Index write failure loses the human's answer

**Files changed:**
- `questions-mcp/questions_mcp_lib.py:576–640` (ask path)
- `questions-mcp/questions_mcp_lib.py:42–44` (`import logging` added)

**What changed:**

The `add_message_entry` call now retries up to 3 times before giving up:

```
attempt 1 → failure → sleep 0.1 s
attempt 2 → failure → sleep 0.2 s
attempt 3 → failure → give up
```

Most transient causes (lock contention, a brief permissions blip, a filesystem
flush) clear within 300 ms. If all three fail:

- `logging.WARNING` is emitted with `message_id`, `qid`, and the queue-file
  path so the operator can locate the entry.
- `index_routing_failed: True` is added to the result dict (a boolean field
  the calling agent can test without string-matching the error text).
- The `error` string is extended with an explicit note that the answer for
  this `message_id` cannot be routed automatically and the queue file must be
  checked by hand.
- The queue-file write and the Telegram message are **not** unwound. The entry
  is preserved (invariant 1 / brd D4 intact).

**What an operator sees when this fires:**

A `WARNING` log line (wherever the MCP server's stderr goes) like:

```
WARNING questions_mcp_lib: index write failed after 3 attempts — message_id=54321
  qid=Q-2026-08-26-001 will not be routed automatically; check the queue file at
  docs/questions/for-product-lead.md. Error: [Errno 28] No space left on device
```

The agent session receives a result with `index_routing_failed: True` and an
`error` field that includes the message id and instructions to check the queue
file.

**Recovery:** The human's Telegram answer will still arrive at the relay. Because
no index entry exists for that `message_id`, the listener (23-05) cannot route it
automatically. The operator looks up the queue file named in the log, finds the
open entry bearing the `qid`, and applies the answer manually (edit the entry to
mark it resolved). Alternatively, if disk space is restored before the human
replies, the operator can re-run the index write from the relay history.

---

## Issue 3 (LOW) — `test_cache_is_per_workspace` does not prove isolation

**Files changed:**
- `tests/test_unit_questions_mcp.py:773–796`

**What changed and why:**

The test previously cleared `lib._ws_cache` before each of the two calls, so a
single-key global cache would have passed it too (the cache was always empty when
each call was made). The second `lib._ws_cache.clear()` before the ws2 call was
removed.

The test now calls ws1 (caching it), then calls ws2 WITHOUT clearing the cache.
If the cache were keyed globally, ws2 would receive ws1's config
(`workspace_id="test-ws"`) and the assertion `assertIn("test-ws", ws_ids)` would
pass but `assertIn("second-ws", ws_ids)` would fail — which is exactly the
failure the test must produce when isolation is broken.

Both assertions are retained:

```python
self.assertIn("test-ws", ws_ids, "ws1 entry missing from index")
self.assertIn("second-ws", ws_ids, "ws2 entry missing — cache isolation broken")
```

Strengthening was chosen over deleting because the test verifies a distinct
property (cache key correctness) from `test_each_workspace_gets_its_own_config`
(end-to-end workspace isolation). Deleting would leave a gap in the cache
contract.

---

## Issue 4 (LOW) — Unguarded `None` from `qs.open_store` in notify ack path

**Files changed:**
- `questions-mcp/questions_mcp_lib.py:733–737`

**What changed:**

The inline expression:

```python
root=(str(qs.open_store(workspace_dir).root) if wc.qs_config is not None else ""),
```

was replaced with:

```python
_notify_store = qs.open_store(workspace_dir) if wc.qs_config is not None else None
root=(str(_notify_store.root) if _notify_store is not None else ""),
```

`open_store` can return `None` if `roles.toml` is removed between the cache load
and this line (e.g. a race). The guard degrades cleanly to an empty string for
`root`, which the index shape allows (`rel_path` is also empty for ack-notification
entries). No `AttributeError` is raised; the notification proceeds.

---

## Verification

### Compile check

```
python3 -m py_compile questions-mcp/questions_mcp_lib.py   → PASS
python3 -m py_compile tests/test_unit_questions_mcp.py     → PASS
```

### Full suite

```
python3 tests/run_all_tests.py
Ran 1156 tests in 32.2s
OK (skipped=1)
```

Baseline was 1156 passed / 2 skipped. After the fix:
- pass count: **1156** (unchanged — the previously skipped cooldown test now runs
  and passes, replacing the skip)
- skip count: **1** (was 2)
- Remaining skip: `test_headless_spawn`
  (`test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`) — the pre-existing
  live-spawn test requiring tmux + model auth; unrelated to this task.

### Relay suite

```
/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
315 passed in 7.83s
```

Relay suite untouched.

---

## Blockers

None.
