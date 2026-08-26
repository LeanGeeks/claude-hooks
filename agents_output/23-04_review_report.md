# Review Report — 23-04 Questions MCP Server

## Verdict: PASS

No BLOCKER or HIGH issues found. Two MEDIUM issues and two LOW issues. The
implementation is correct and can proceed; the MEDIUM issues should be addressed
before 23-07 live verification.

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| `questions-mcp/server.py` thin shell, uv script | met | `questions-mcp/server.py:1–5,36` |
| `questions-mcp/questions_mcp_lib.py` all logic | met | `questions_mcp_lib.py:1–758` |
| `.claude/hooks/questions_listen_lib.py` index module | met | `questions_listen_lib.py:1–311` |
| Write-before-send ordering (invariant 1) | met | `questions_mcp_lib.py:505–618`; crash-simulation test (`TestWriteBeforeSend`) proves entry exists when relay is called |
| Unresolvable role refuses before write | met | `questions_mcp_lib.py:422–432` |
| Relay failure: `dispatched:false` with id, entry intact | met | `questions_mcp_lib.py:553–574`; `test_relay_failure_leaves_entry_returns_dispatched_false` |
| `max_open` from durable queue file | met | `questions_mcp_lib.py:481`; `store.count_open()` reads queue files |
| `min_interval_s` refusal | met | `questions_mcp_lib.py:493–503` |
| No `[questions]` section → clear error, byte-identical elsewhere (invariant 9) | met | `questions_mcp_lib.py:403–410`; `TestMissingQuestionsSection` |
| Role, path, workspace never reach relay (invariant 4) | met | only `dest.token` is passed to relay; `workspace_id` appears in message text (display-only, not routing) — consistent with existing hooks |
| Index module: `watermark` + `pending` round-trips, flock + tmp + os.replace | met | `questions_listen_lib.py:168–308`; `test_pending_and_watermark_preserved` |
| `notify(ack=True)` → `kind=question`, index entry with no `qid` | met | `questions_mcp_lib.py:683–725`; `test_notify_ack_sends_question_kind` |
| `notify(ack=False)` → `kind=notification`, nothing recorded (invariant 10) | met | `questions_mcp_lib.py:732–757` |
| `questions_listen_lib` in `REQUIRED_HOOKS` | met | `install-claude-config.sh` diff |
| Per-call workspace resolution, 5 s TTL cache | met | `questions_mcp_lib.py:154–197` |
| Invariant 5 — no hand-parsing of queue or index JSON | met | server imports `questions_store` and `questions_listen_lib`, uses their APIs throughout |
| `QuestionsStoreError` from `create_entry` surfaces cleanly | met | `questions_mcp_lib.py:518–521` |
| Escalation token resolved locally, passed to relay | met | `questions_mcp_lib.py:531–538` |
| `mark_dispatched` called after index write | met | `questions_mcp_lib.py:597–605` |
| relay client gains `never_expires`, `nudge_schedule`, `escalate_after_sec`, `escalate_to_token` | met | `relay_server/client.py` diff; fields are conditional so existing callers produce byte-identical bodies |
| Agent-facing docs in tool descriptions | met | `server.py:51–106,121–139` |
| MCP registration left to 23-06 | met | stated in report, confirmed by absence of installer block for questions |
| Tests: 36 new, full suite 1156 passed / 2 skipped | confirmed | suite output matches claim |
| Relay suite untouched at 315 passed | confirmed | `/tmp/relay-test-venv/bin/pytest` output |

---

## Issues Found

### Issue 1: MEDIUM — Skipped test `test_min_interval_allows_after_cooldown` has a broken fixture

**File:** `tests/test_unit_questions_mcp.py:692–735`

**Problem:** At module load time (line 48) the test module sets
`CLAUDE_PROJECT_DIR = _ISOLATION_DIR` (an empty temp dir) to prevent
`roles_config.find_roles_file` from walking the developer's real tree.
`test_min_interval_allows_after_cooldown` then calls
`qs.load_questions_config(self.ws.workspace_dir)` on line 694 without patching
`CLAUDE_PROJECT_DIR` to `self.ws.workspace_dir`.  `find_roles_file` sees
`CLAUDE_PROJECT_DIR` set, looks exclusively in `_ISOLATION_DIR/.claude/roles.toml`
(which doesn't exist), and returns `None`.  `load_questions_config` therefore returns
`None`, and the test self-skips with "no [questions] section".

The cooldown logic itself is **correct** — a scratch script
(`/tmp/.../scratchpad/test_cooldown.py`) drove `lib.ask` twice across an elapsed
`min_interval_s` of 0.05 s and confirmed both the refusal and the recovery.
The timestamp update on line 525 runs only after a successful write; the
comparison on line 496 (`elapsed < min_interval`) is strict, so expiry is exact.
No bug in the rate-limit logic.

**Fix:** In `test_min_interval_allows_after_cooldown`, wrap the
`load_questions_config` call in `patch.dict(os.environ, {"CLAUDE_PROJECT_DIR":
self.ws.workspace_dir})`, or pass `roles_path=self.ws.roles_path` to bypass the
env-var lookup:

```python
patched_config = qs.load_questions_config(
    self.ws.workspace_dir, roles_path=self.ws.roles_path
)
```

The 35 tests that pass use `_call_ask` / `_call_notify` which patch
`CLAUDE_PROJECT_DIR` inside the `with` block; this test calls `load_questions_config`
outside that block.

---

### Issue 2: MEDIUM — Index write failure leaves an answer permanently unapplied

**File:** `questions-mcp/questions_mcp_lib.py:587–594`

**Problem:** When `add_message_entry` raises (disk full, permissions error, etc.),
the code appends to `result["error"]` and returns.  The entry exists in the queue
file and the Telegram message was sent.  The listener (23-05) feeds on `GET
/v1/answers?after=<watermark>` and for each answer does `index lookup: message_id
→ workspace_id, qid`.  A message_id with no index entry is invisible to the
listener: it has no way to know which workspace to write back to.  The human's
answer is permanently unapplied.

Brd D10 says answers are "kept, retried, and surfaced; never dropped."  D10 is
specifically about the apply path, but the index write failure produces the same
outcome as a dropped answer from the listener's perspective.

The error string in the MCP return value notifies the calling agent session, but
once that session exits the information is lost.

**Severity context:** This requires a disk-level failure at the moment of the index
write.  It will not be triggered by normal relay or store failures.  But because
the human does answer and the answer is silently discarded, it is not a graceful
degradation.

**Fix:** Before returning, retry `add_message_entry` once (or twice) with a brief
sleep.  If all retries fail, log prominently and consider writing a fallback
`~/.claude/async_questions_failed.jsonl` with the message_id, workspace_id and
qid so the user can recover manually.  At minimum, document in the agent-facing
error that the returned `message_id` will not be matched by the listener.

---

### Issue 3: LOW — `test_cache_is_per_workspace` clears the cache before each call and does not prove isolation

**File:** `tests/test_unit_questions_mcp.py:773–793`

**Problem:** The test clears `lib._ws_cache` before calling ws1, then clears it
again before calling ws2.  A cache with a single global key would pass this test
too, because the cache is always empty when each call is made.  Isolation is not
proven by this test.

`test_each_workspace_gets_its_own_config` (lines 748–771) is stronger: it calls
ws1 and ws2 without clearing the cache between them and verifies that each
workspace's queue file contains only its own entry.  That test provides real
isolation evidence.

**Fix:** In `test_cache_is_per_workspace`, call ws1 first (caching it), then call
ws2 WITHOUT clearing the cache, and verify ws2 received its own
`workspace_id="second-ws"` config (e.g., by checking the index entry or the queue
file created).

---

### Issue 4: LOW — `notify` ack path calls `qs.open_store` inline without a `None` guard

**File:** `questions-mcp/questions_mcp_lib.py:713`

**Problem:**

```python
root=(str(qs.open_store(workspace_dir).root) if wc.qs_config is not None else ""),
```

If `wc.qs_config is not None` but `qs.open_store(workspace_dir)` returns `None`
(roles.toml deleted between the cache load and this line), the code raises
`AttributeError: 'NoneType' object has no attribute 'root'`.  In practice this
cannot happen within a single function invocation, but the call is also
unnecessary — the resolved anchor root could be cached in `_WorkspaceCache` just
as `qs_config` is.

**Fix:** Either add a guard:

```python
_store = qs.open_store(workspace_dir)
root = str(_store.root) if _store is not None else ""
```

Or extend `_WorkspaceCache` to carry the resolved root alongside `qs_config` and
reuse it here.

---

## Judgment Call Rulings

### 1. `min_interval_s` state is in-memory: acceptable as implemented

`stdio`-type MCP servers are one process per Claude Code session
(confirmed from `~/.claude.json` and the `permissions-mcp` precedent).
`_last_ask_time` is therefore per-session, not cross-session.

Cross-session bypass (start a new session every 30 s to reset the in-memory
counter) requires an agent that deliberately spawns sessions, which is outside
the threat model for an in-loop `ask` call.  The brd explicitly scopes
durability to `max_open` only ("the counter is the queue file's open entries,
so it is durable").  D12 says `min_interval_s` is a "short per-process guard" —
the 30 s default matches that framing.  **Ruling: PASS.**

### 2. Escalation target when destination is the default role: correct

When `dest.is_default` is True (`questions_mcp_lib.py:535`), `escalate_to_token`
stays `None` and is omitted from the relay request.  Brd D6 says the relay is
told "where to escalate, never what a role is."  When the target IS the default
role there is no higher destination to escalate to; sending a duplicate to
the same installation is operationally wrong.  The relay treats an absent
`escalate_to_token` as "no escalation" (architecture §2.2).  **Ruling: PASS.**

### 3. Index write failure → answer permanently unapplied: insufficient surfacing

See Issue 2 above.  The error string returned to the agent is the only signal.
Once the session exits, the mapping from message_id to workspace is gone, and the
listener will silently skip the answer when it arrives.  The D10 guarantee ("never
dropped") is violated in spirit.  **Ruling: MEDIUM issue; fix before live gate.**

### 4. `QuestionsStoreError` during `mark_dispatched`: entry still findable, correct

`mark_dispatched` stamps `**Dispatched:**` on the entry — an audit marker.
It is not part of the parse contract that `apply_answer` uses; `apply_answer`
locates an entry by its qid heading.  The index already records the message_id →
qid mapping (step 4 succeeded before step 5).  When the human answers, the
listener resolves the qid from the index and calls `apply_answer(qid, ...)`, which
finds the entry regardless of whether the dispatch marker is present.  **Ruling:
correct; PASS.**

---

## Skipped Test Ruling

`test_min_interval_allows_after_cooldown` (Issue 1) — the skip is **not masking a
real defect**.  The rate-limit logic is correct.  The skip is caused by a broken
fixture: `load_questions_config` is called without patching `CLAUDE_PROJECT_DIR`
to the test workspace, so `find_roles_file` looks in the wrong directory.  Fix is
a one-liner.

---

## Load-bearing Property Checks

**Invariant 1 ordering:** Confirmed by reading `questions_mcp_lib.py:505–618`.
`create_entry` (lock → allocate → compose → fsync) runs at line 509 before
`_send_async_question` at line 559.  Index write (line 588) and `mark_dispatched`
(line 599) follow the relay call.  The crash-simulation test
`test_entry_exists_before_relay_is_called` verifies the queue file is populated
at the moment the relay mock is invoked — it tests ordering behavior, not just
return shape.

**Invariant 4 — relay never learns role/path/workspace:** Relay receives
`dest.token` (an opaque string), `escalate_to_token` (another opaque string), and
the rendered HTML body (`body_html`).  The body includes `workspace_id` (the
user's friendly name, not a path) for human readability — consistent with existing
hooks.  No filesystem path, no role alias, no `roles.toml` content reaches the
relay.

**Invariant 5 — no hand-parsing:** `questions_mcp_lib.py` uses `qs.open_store`,
`qs.create_entry`, `qs.mark_dispatched`, `qll.add_message_entry`, `qll.count_pending`.
No `open().read()` / `json.loads()` on a queue file or index file anywhere in
the MCP server.

**Index module shape:** `questions_listen_lib.py:79–191` defines `IndexEntry`,
`PendingApply`, `Index`.  All architecture §5 fields are present: `watermark`,
`messages` (with `workspace_id`, `anchor`, `root`, `rel_path`, `qid`, `role`,
`created_at`), `pending` (with `message_id`, `answer`, `attempts`,
`first_failed_at`, `last_error`).  `test_pending_and_watermark_preserved`
confirms both fields survive a write/read round-trip.  An ack-notification entry
with `qid=None` reads back with `qid=None` (`test_ack_entry_has_no_qid`).
`flock` + tmp + `os.replace` + dir fsync are all present (`questions_listen_lib.py:197–250`).

**Invariant 10 / brd D7:** `notify(ack=True)` sends `kind="question"` with
`_ACK_BUTTON` (one `[ Acknowledge ]` button).  `notify(ack=False)` sends
`kind="notification"` with no keyboard and records nothing.  Both paths confirmed
in `questions_mcp_lib.py:683–757`.

**Invariant 9:** No `[questions]` section returns a clear, actionable error with
`add an entry with at least dir = "docs/questions"`.  No write is attempted.
No relay call is made.  `TestMissingQuestionsSection` confirms both.

**Per-call workspace resolution:** `_resolve_workspace_dir` reads
`CLAUDE_PROJECT_DIR` → `PWD` → `os.getcwd()` per call.  Cache keyed by the
resolved string at `questions_mcp_lib.py:186`.  `test_each_workspace_gets_its_own_config`
proves two concurrent workspace dirs produce isolated queue files.

**`max_open` from durable queue:** `store.count_open()` reads the queue file(s)
on each call (`questions_store.py:1044–1057`).  Not an in-memory counter.
`test_max_open_refuses_when_full` writes 20 open entries to the real file and
confirms refusal.

**Single-select rejection:** The task says "reject a multi-select request" but
the actual implementation accepts any options list and treats it as single-select.
There is no `multi_select` parameter on `ask` (the tool schema has `options:
Optional[List[str]]`).  The task text means "do not send `multi_select=true` to
the relay", not "parse a `multi_select` flag from the caller".  The implementation
is correct — it never sets `multi_select` on the relay call.

---

## Code Quality Notes

- The `_WorkspaceCache` dataclass annotates `qs_config` as `"qs.QuestionsConfig |
  None"` (forward reference string) — safe because the field is only ever accessed
  as a known-type object after a None-check.
- `_send_notification` accepts a `kind` parameter and applies `never_expires` only
  when `kind == "question" and nudge_schedule is not None`.  A `notify(ack=True)`
  from a workspace without `[questions]` gets `nudge_schedule=None` and therefore
  no `never_expires=True`.  The Telegram message expires after the 24 h `ttl_sec`
  default.  This is a reasonable limitation but not explicitly documented.
- The idempotency key for `notify` uses `time.time_ns()`, which is correct (each
  notification is a distinct message; idempotency key uniqueness is desired).

---

## Questions for User

None.
