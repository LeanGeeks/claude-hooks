# Review Report — 23-05 Answer listener runtime (`questions-listen`)

## Verdict: PASS

No BLOCKER or HIGH issues found.  One MEDIUM (a known 23-04 limitation that is
now load-bearing for this task), three LOW.

---

## Compile / import check

```
python3 -m py_compile \
  .claude/hooks/questions_listen_lib.py \
  .claude/bin/questions-listen \
  tests/test_unit_questions_listen.py
→ clean

python3 -c "import questions_listen_lib"  → clean
.claude/bin/questions-listen --status     → runs
```

---

## Multi-token finding — premise and fix

### Premise: TRUE

The relay's `GET /v1/answers` handler (`.relay-server/relay_server/app.py:1062`)
filters rows by `installation_id` in the SQL WHERE clause:

```sql
WHERE telegram_chat_id = ? AND state = 'answered'
  AND id > ? AND installation_id = ?
```

`installation_id` maps 1-to-1 to an `installation_token`.  A role bound to
its own token (e.g. `[roles] hpl = "rly_…"`) is a **separate installation**,
and its answered messages appear **only** in that installation's feed.  A
listener polling only the default token's feed would never see those answers.
The state.md log confirms this is real in production: chat 108296207 carries
two installations with 616 and 21 answered rows respectively.

The multi-token mechanism is necessary, not over-engineering.

### Fix soundness

**`watermarks` round-trip through `add_message_entry`.**
`add_message_entry` is byte-identical to 23-04's version.  It calls
`read_index` → mutates `messages` → `write_index`.  After 23-05 lands,
`Index.from_dict` parses `watermarks` from the JSON and `to_dict` writes it
back — the round-trip is lossless.

The only risk is during the deployment transition window (23-05 committed,
installer not yet re-run).  If an older installed `questions_listen_lib.py`
(23-04 version, whose `Index.to_dict` has no `watermarks` key) processes the
index after 23-05 has written `watermarks`, those keys are silently dropped.
The consequence is that the listener replays from `after=0` on next cycle.
Replaying is idempotent (invariant 3) and safe; no answer is lost or doubled.
This is a LOW severity transition issue resolved when 23-06 runs the installer.

**Top-level `watermark` mirror.**
`set_feed_watermark` advances `watermarks[fp]` and, when `fp == primary_fp`,
sets `index.watermark = watermarks[primary_fp]`.  Both fields are updated in
one `mutate_index` call (single atomic write).  They cannot diverge across a
crash.  `--status` reads from both the index (truth) and the status file
(snapshot); neither leaks token material — only fingerprints appear.

**`25 // N` soundness.**
`run_cycle` computes:
```python
wait = max(1, int(self.config.poll_seconds // max(1, len(due)))) if due else 0
```
- `if due else 0`: N=0 is handled before the division.
- `max(1, len(due))`: prevents ZeroDivisionError (redundant with the guard
  above, but harmless).
- `max(1, ...)` on the result: N=26 → `25//26 = 0` → floor to 1. No busy-loop.
- N=25: each feed gets 1 second. N=1 (normal): 25 seconds — exactly the
  architecture's `wait=25`.
- A `BaseException` crash in one feed's row propagates through `poll_feed` and
  stops the cycle (correct for crash-safety); regular exceptions in `get_answers`
  are caught per-feed by `_note_feed_error`, returning `[]` and letting the
  loop continue to the next feed.  One feed's failure does not starve the others.

**Unknown fingerprint starts at 0.**
`feed_watermark` returns `int(index.watermarks.get(fingerprint, 0))`.  A
full replay from 0 is the intended index-recovery path.  Safe.

---

## Six-step crash walk — independent verification

### Steps 1–5: confirmed write-nothing to the index

| Step | Writes anything to the index? | Evidence |
|------|-------------------------------|----------|
| 1 index lookup | No — `read_index` is a pure read | `process_answer` line ~1019 |
| 2 ack-finalize | No — `_finalize` calls `edit_message` only | `process_answer` line ~1032 |
| 3 re-resolution | No — `resolve_target` creates nothing | `questions_listen_lib.py:768` |
| 4a store write (before) | No — crash inside `QuestionsStore` before `os.replace` | `apply_answer` in `questions_store.py` |
| 4b store write (after) | Queue file written; index untouched | `apply_answer` returns, then `_apply` returns |
| 5 finalize PATCH | No — `_finalize` calls `edit_message` only | `_finalize` line ~1119 |

### Step 6: confirmed single atomic write

`_terminal` calls `mutate_index(_mutate)`.  The `_mutate` closure:
1. Upserts or removes the `pending` entry (one operation).
2. Calls `set_feed_watermark` to advance the watermark (one operation).

`mutate_index` wraps both in a single `_flock_ex` → `read_index` →
`mutate(idx)` → `write_index(idx)` → `_atomic_write_json` (tmp + `os.replace`)
sequence.  A crash either leaves the old file untouched or has the new file;
there is no intermediate state.  The pending record and the watermark advance
are **one write**.  Invariant 2 is upheld.

### Step 4b — idempotency check

`_locate` in `questions_store.py:1342`:
```python
if message_id is not None and _has_answer_marker(lines, message_id):
    return _Located(ApplyResult.APPLIED, path, lines, None)
```
This runs **before** the multi-match (ambiguity) loop.  Invariant 3 outranks
rule 5.  `apply_answer` then checks `if located.entry is None → return APPLIED`
without inserting a second block.  Confirmed: the idempotency check is keyed on
the relay message id and precedes the ambiguity rule.

### Step 5 — `_is_not_modified`

The implementer's description is slightly imprecise.  `_is_not_modified` is a
server-side helper (`relay_server/telegram_backend.py:52`).  The PATCH handler
(`app.py:863`) catches Telegram's "message is not modified" API error and
returns success to the client rather than propagating a 502.  So a re-PATCH
with identical text succeeds from the client's perspective — the behavior is
as claimed, but it is the relay *server* that makes it a no-op, not the relay
client.  Not a bug.

### TestCrashInjection verification

- `Crash` inherits `BaseException`.  The listener's `except Exception` handlers
  cannot swallow it.  Confirmed by inspection.
- Each of the seven tests (step 1, 2, 3, 4a, 4b, 5, 6) asserts on-disk state
  **at the crash point** and then runs `_replay()` (a fresh listener) asserting:
  - marker count = 1 (applied exactly once)
  - watermark advanced to the message_id
  - pending list empty
  - `client.edits` contains the finalization
- None of the crash tests merely assert "nothing raised".

---

## Three judgment calls

### 1. Missing index record → skipped, watermark advanced, nothing pended

**Agreed, with one caveat worth noting.**

The routing decision is correct for the high-volume case (permission approvals,
blocking `AskUserQuestion` answers) because those answers have no index entry
and cannot be applied.  Pending them would poison the backlog indefinitely.

The caveat: a genuinely-ours answer whose index record was lost due to
23-04's `index_routing_failed` failure mode takes exactly this path — skipped,
watermark advanced, no retry, no sidecar.  The answer text is in the Telegram
message and the queue entry exists, but the listener can never match them.
Recovery requires a human to look up the message id in the queue file.

This is a known 23-04 gap (documented in the state.md log and the 23-04
review report).  It is not a 23-05 code defect.  However, it means the task's
"Done when: an answer given while the machine was off is applied on the next
start" has a silent exception case that is not tested (because 23-04's
`index_routing_failed` path is not reproduced here).  BRD D10 ("never dropped")
is technically satisfied — the answer is in Telegram, not dropped — but the
automation contract is permanently broken for that answer.  **Severity: MEDIUM.**

### 2. Failed PATCH advances the watermark

**Agreed.**  The decision is durable in the queue file the moment `_apply`
succeeds.  A 404 on the Telegram message is a state the listener cannot repair
(the message is gone), and blocking the watermark on it would stall every
subsequent answer.  Epic 19's cleanup sweep is the documented backstop for
the unfinalized tag.

### 3. Unresolvable workspace pends forever

**Agreed with conditions.**  When `_sidecar` also fails (because the workspace
is completely gone), `_bump_pending` is called unconditionally and the entry
accumulates indefinitely.  The test confirms this (`test_sidecar_failure_keeps_the_answer_pending`).

"Visible forever beats lost once" is the right call.  However, `pending_count`
is surfaced on every `ask`, so an operator will see the stuck entry and can act.
The practical risk is bounded: a fully gone workspace means nobody is using that
checkout, so a stuck entry in `pending` is an alert, not a data loss.

---

## Completeness check

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Binary + library split | met | `.claude/bin/questions-listen` (lifecycle only), `questions_listen_lib.py` |
| Single instance via flock | met | `acquire_single_instance`; `TestSingleInstance` ×3 |
| `enabled = false` exits 0 immediately | met | `main()` lines ~139–146; `test_disabled_config_exits_zero` |
| Multi-feed watermark (multi-token) | met | `watermarks` dict; `TestFeedBehaviour.test_each_installation_feed_keeps_its_own_watermark` |
| Six-step apply pipeline | met | `process_answer`; `TestCrashInjection` ×7 |
| QuestionsStoreError → pending (23-03 obligation) | met | `_apply` two wrapped blocks; `test_questions_store_error_routes_to_pending` |
| conflict → pending | met | `_apply`; `test_conflict_routes_to_pending_and_never_guesses` |
| Pending retry with backoff | met | `retry_pending`; `test_backoff_holds_a_retry_until_it_is_due` |
| Sidecar fallback after max_attempts | met | `_sidecar`; `test_sidecar_after_max_attempts` |
| Sidecar idempotent on relay message id | met | `_sidecar` marker check; `test_sidecar_is_idempotent_on_message_id` |
| Invariant 7 — re-resolution at apply time | met | `resolve_target`; `TestAnchorReresolution` ×3 |
| Invariant 7 — deleted worktree lands in primary | met | `test_answer_for_a_deleted_worktree_lands_in_the_primary_checkout` |
| Invariant 8 — no subprocess/git/amux | met | grep finds only comments (see §Invariant 8) |
| Ack-notifications → finalize, no file | met | `process_answer`; `TestAckNotifications` |
| `--status` — no token material | met | `test_redacted_config_never_carries_token_material`; `test_status_file_carries_everything_status_prints` |
| 401 → slow retry, surfaced in status | met | `_note_feed_error`; `test_401_is_fatal_with_slow_retry_and_surfaces_in_status` |
| Backoff with jitter | met | `test_network_error_backs_off_with_jitter_and_caps` |
| Full replay from after=0 proves no double-apply | met | `test_full_replay_from_zero_applies_nothing_twice` |
| 25//N long-poll budget split | met | `test_the_long_poll_budget_is_split_across_feeds` |

---

## Issues Found

### Issue 1 — MEDIUM: `index_routing_failed` answers are permanently unserviceable

- **File:** `questions_listen_lib.py:1019–1028`
- **Problem:** A message that 23-04 sent to Telegram but failed to write to
  the index (`index_routing_failed: True` in the ask result) will appear in the
  feed as an answered message.  `process_answer` finds no index entry, logs at
  DEBUG, skips it, and advances the watermark.  No pending entry is created, no
  retry, no sidecar.  The answer is permanently stranded from the listener's
  perspective.  The human must locate the answer manually from the relay message
  id.  This is a 23-04 limitation, not a 23-05 code defect, but it is worth
  calling out because the task's primary goal is "never lose a human decision"
  and this is the failure mode that defeats it silently.
- **Fix:** None in 23-05 scope.  23-04 already documents the gap.  A future
  improvement could have 23-04 write a recovery hint somewhere the listener can
  find without the full index entry, but that requires protocol changes.

### Issue 2 — LOW: Transition window — `watermarks` dropped by installed 23-04 library

- **File:** `questions_listen_lib.py:248–254` (23-04's `Index.to_dict` has no `watermarks`)
- **Problem:** Between 23-05 being shipped (uncommitted → committed) and 23-06
  running the installer, the installed `questions_listen_lib.py` is still the
  23-04 version.  If the MCP server calls `add_message_entry` while the
  23-05 listener has already written a `watermarks` dict, 23-04's `to_dict`
  silently drops it.  The listener then replays all answers from `after=0` on
  the next cycle.
- **Impact:** Idempotent replay — no answer is lost or doubled, but the replay
  is slow for installations with many historical answers.
- **Fix:** Resolved when 23-06 runs the installer.  Could be mitigated by
  seeding `watermarks[primary_fp]` from the existing `watermark` value when the
  listener first reads an index with no `watermarks`, but current behavior is
  correct.

### Issue 3 — LOW: Transition window — `token_fp` dropped by installed 23-04 library

- **File:** `questions_listen_lib.py:197–219` (23-04's `PendingApply.to_dict` has 5 fields)
- **Problem:** Same transition window as Issue 2.  23-04's `PendingApply.to_dict`
  does not include `answered_at`, `last_attempt_at`, or `token_fp`.  If the MCP
  server reads a 23-05-written `PendingApply` and writes it back, those fields
  are dropped.  Consequence: `last_attempt_at = ""` causes the retry to fire
  immediately (faster, not slower); `token_fp = ""` causes `_finalize` to skip
  the Telegram PATCH (the log says "not configured on this machine").  The
  answer is still applied to the queue file; only Telegram finalization is lost.
- **Fix:** Resolved when 23-06 runs the installer.

### Issue 4 — LOW: First start after 23-05 installation replays from 0

- **File:** `questions_listen_lib.py:537–538`
- **Problem:** When 23-05 is installed on a machine that already has
  `watermark = N` (from 23-04's operation), `watermarks` is empty in the index.
  `feed_watermark` returns 0 for every feed.  The listener polls with `after=0`
  and replays all historical answers.
- **Impact:** Correct and idempotent, but potentially slow for installations
  with thousands of historical rows.  The 500-row page cap means large histories
  require multiple cycles to drain.
- **Fix:** Seed `watermarks[primary_fp]` from the existing `watermark` value
  on first start when `watermarks` is absent.  Not implemented; current behavior
  is safe.

---

## Invariant 8 — subprocess / git / amux grep

```
grep -n "subprocess\|os\.system\|amux\|git " \
  .claude/hooks/questions_listen_lib.py .claude/bin/questions-listen
```

All hits are in docstrings and comments.  No subprocess, no `os.system`,
no git, no amux in runtime code.  Invariant 8 is clean.

---

## Test quality

No `skipTest`, no bare `except`, no `try/except: pass`, no vacuous
"nothing raised" assertions in the new test file.

The four occurrences of `# must not raise` are inline comments above a
`run_cycle()` call; each is followed by meaningful state assertions
(pending count, watermark value, marker count, etc.).

The 7 `TestCrashInjection` tests each:
1. Inject `Crash(BaseException)` at the exact crash point.
2. Assert on-disk state at the crash (marker count and watermark, specific to
   the step).
3. Run a **fresh listener** over the same feed.
4. Assert the answer was applied exactly once, the watermark advanced, pending
   is empty, and the Telegram message was finalized.

`test_full_replay_from_zero_applies_nothing_twice` genuinely proves the
index-recovery property: it applies two answers, zeroes the watermarks, then
runs a second listener cycle and asserts the queue file is unchanged, both
markers appear exactly once, and the watermark ends at the last id.

---

## Test suite results

```
python3 tests/run_all_tests.py
→ Ran 1216 tests in 38.657s
   OK (skipped=1)

/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
→ 315 passed in 9.33s
```

Baseline was `Ran 1156, skipped=1`.  The +60 new tests are all this task's.
The single skip is the pre-existing `test_headless_spawn` (needs tmux + model
auth).  Relay suite is untouched.

---

## Code quality notes

- `_find_roles_toml` is local rather than delegating to
  `roles_config.find_roles_file` — the decision to avoid honouring
  `CLAUDE_PROJECT_DIR` in a resident daemon is correct and well-documented.
- `_finalize`'s return value is intentionally discarded by `process_answer` —
  a failed PATCH is logged and the pipeline continues; the return value would
  only be useful if the caller changed behaviour on failure, which it
  deliberately does not.
- `retry_pending` reads the index without a lock at the start, then makes
  per-entry `mutate_index` calls.  This is correct for a single-threaded loop
  — no concurrent writer exists while the listener holds the instance lock.
- The "persistent index-write failure ends the process" property (ENOSPC in
  step 6) is noted in the implementation report as deliberate and not covered
  by a test.  This is acceptable — the alternative (advancing a watermark we
  couldn't persist) is the failure we must prevent.

## Questions for User

None.
