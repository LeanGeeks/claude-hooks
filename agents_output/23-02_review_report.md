# Review Report — 23-02: Relay per-message nudges + escalation

## Verdict: FAIL

One HIGH issue (Finding A) and two MEDIUM issues block acceptance.  Tests compile and
all 303 relay-suite / 1030 top-level tests pass, but the passing tests do **not**
demonstrate the task's documented Done-when clause, because the implementer substituted
workaround durations instead of fixing the parser.

---

## Compile / Import

```
python3 -m py_compile availability.py db.py models.py reaper.py app.py  → all OK
```

All five modified modules import cleanly.

---

## Test counts

| Suite | Claimed | Actual |
|---|---|---|
| `pytest relay-server/tests/ -q` | 303 passed | **303 passed** (6.99 s) |
| `python3 tests/run_all_tests.py` | 1030 passed, 1 skipped | **1030 passed, 1 skipped** |

Counts match.

---

## Completeness Check

| Requirement | Status | Evidence |
|---|---|---|
| Repeating-tail `*` syntax parsed | met | `availability.py:492–545`; 9 unit tests |
| Active-time arithmetic for repeating rung | met | `_compute_next_nudge_due`; test class 2 |
| `nudge_enabled=0` override precedence | met | `reaper.py:750–772`; 2 passing tests |
| Three nullable columns on `messages` | **partially met** — four added (see Issue 2) | `db.py:48–55, 151–167` |
| Migration slot 5, no table rebuild | met | `db.py:151–167`; ALTER TABLE × 4 + partial index; no rebuild |
| Escalation fires exactly once | met (but see Issue 3) | `reaper.py:972–993` |
| Escalation to right installation | met | `reaper.py:1003–1015`; lookup by `token_hash` |
| Unknown-token 422 at send time | met | `app.py:653–671`; test `test_unknown_escalation_token_at_send_time` |
| Answer from escalated copy → original | met | `app.py:2748–2794, 2881–2931`; test confirmed |
| Child not surfacing in other installation's feed | met | child carries original `installation_id`; feed filters on it |
| Original answered → child cancelled | met | `_cancel_escalated_children`; test confirmed |
| Feed wake on escalation answer | met | answer routes through `_record_answer` which calls `answer_waiters.notify` |
| `reaper_tick` `answer_waiters` threaded to `reaper_loop` | met | `reaper.py:1183` |
| Done-when: `4h,1d,3d,7d*` nudges in week three | **not met** (see Issue 1) | parser rejects `1d`; tests use `24h`/`72h`/`168h` |
| Invariant 9 (existing rows byte-identical) | met | all four columns nullable, no NOT NULL, no defaults |
| Invariant 4 (no role concept server-side) | met | reaper resolves token hash → installation only |
| v4→v5 migration forward test | met | `test_v4_migrates_to_v5_cleanly`, `test_v4_v5_schema_columns_match` |

---

## Issues Found

### Issue 1 — HIGH: `parse_duration` does not accept `d` (days), so the epic's documented default is unparseable

- **File:** `relay-server/relay_server/availability.py:449` (`_DURATION_RE`) and `:535`
- **Problem:**
  `_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")` matches only `h` and `m`.
  Calling `parse_duration('1d')` returns `None`; calling
  `parse_nudge_schedule_with_repeat('4h,1d,3d,7d*', 10)` raises:
  ```
  ValueError: bad duration '1d'; accepted forms: 15m, 3h, 2h30m
  ```
  The string `4h,1d,3d,7d*` is the default stated in **five** places:
  brd §5 config example, architecture §7 defaults table, state.md chosen-defaults
  list, task §1, and the task's own Done-when clause ("A message with `4h,1d,3d,7d*`
  still nudges in week three").

  The implementer recorded this as a *Decision* ("test schedules use `24h`, `72h`,
  `168h` in place of `1d`, `3d`, `7d`") rather than a defect.  This rewrites the
  requirement to fit the parser: the Done-when clause is therefore **not demonstrated**,
  and when 23-04 reads a human-authored `roles.toml` with `nudge = "4h,1d,3d,7d*"`,
  the POST /v1/messages will return 422 on the first send.

  Widening `parse_duration` to accept `d` is safe: the regex change only adds
  acceptance (`(\d+d)?` before the `h` group); all strings valid today remain valid.
  The epic-19 nudge path uses the same `parse_duration` via `parse_nudge_schedule`
  — it will only gain tolerance for day-format strings, never lose any.

- **Fix:**
  1. Change `_DURATION_RE` to `re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")`,
     adding a `d` capture group and extracting `days = int(m.group(1) or 0)`.
  2. Return `timedelta(days=days, hours=hours, minutes=minutes)`.
  3. Update the error message to `"accepted forms: 15m, 3h, 2h30m, 1d, 2d12h"`.
  4. Replace the substituted test values (`24h`, `72h`, `168h`) with the spec literals
     (`1d`, `3d`, `7d`) so the Done-when clause is actually demonstrated.

---

### Issue 2 — Architecture deviation: fourth column `parent_message_id` (ACCEPTED)

- **File:** `relay-server/relay_server/db.py:55, 164`
- **Problem:**
  The task specifies three nullable columns (`nudge_schedule_override`, `escalate_at`,
  `escalate_to_token_hash`); the implementer added a fourth: `parent_message_id INTEGER`.
- **Ruling: the addition is justified and should be accepted.**

  Without `parent_message_id`, the webhook handler has no way to identify which
  open message is the parent of the row being tapped.  The three specified columns
  all live on the *original* row; none carry information about child↔parent linkage
  in the other direction.  Alternatives (a separate `message_escalations` table, or
  encoding the parent id in `payload_json`) would be more invasive or would couple
  the payload schema to server internals.  The fourth column is the minimal
  mechanism that supports answer attribution and sibling cancellation.

  Column is `INTEGER` with no `NOT NULL` and no default → SQLite assigns NULL for
  every existing row (verified via `PRAGMA table_info`); invariant 9 is satisfied.
  The migration adds it via `ALTER TABLE ADD COLUMN` with no table rebuild.

  Record this deviation as accepted.

---

### Issue 3 — MEDIUM: Escalation loss on transient send failure; dangling child never cleaned up on parent expiry

- **File:** `relay-server/relay_server/reaper.py:972–993` (clear-before-send);
            `relay-server/relay_server/reaper.py:363–468` (expiry pass, no child cancel)
- **Problem A — silent loss:**
  `escalate_at` is cleared in a transaction *before* the Telegram send and the
  child-row insert.  If `backend.send_message` raises (transient outage, rate limit,
  wrong chat id), the escalation is gone: `escalate_at` is NULL, no re-fire will
  occur, and no observable error reaches the sender.  The child row is inserted
  *before* the send (to get its relay id for keyboard encoding), so a failed send
  leaves a child row with `telegram_message_id = 0` in the `open` state.

  This violates the spirit of brd D10 ("a failed thing is kept, retried and surfaced,
  never dropped").  The implementer's rationale ("a missed escalation is better than a
  repeated one") is a legitimate trade-off, but: (a) it is not recorded in the task as
  a deliberate choice, and (b) the user is given no signal that their escalation was
  silently dropped.  A retryable failure should at minimum log a WARNING with enough
  detail to surface in alerting.

  The log *does* say "best-effort, continuing; child row %s left open", but that
  entry is DEBUG-adjacent and the child row with `tg_msg_id=0` is inert — it will
  never be answered, never be nudged (no `next_nudge_at`), never be expired (its
  `expires_at` is `NEVER_EXPIRES`).

- **Problem B — expiry does not cancel escalated children:**
  The reaper's expiry pass (`reaper.py:386–468`) transitions the parent to `expired`
  but does **not** call `_cancel_escalated_children`.  After the parent expires:
  - Open escalated copies remain visible in the escalation-target's Telegram chat
    with an active keyboard.
  - A user who taps the keyboard will trigger `_handle_callback_query`, which detects
    `parent["state"] != 'open'` and cancels the child — so the child is *eventually*
    resolved on tap, but the Telegram message still shows active buttons until then.
  - A dangling child from a failed send (Problem A, `tg_msg_id=0`) will never be
    tapped and will remain `open` forever once the parent expires.

- **Fix:**
  1. Add a `_cancel_escalated_children` call inside the expiry loop in `reaper.py`,
     after `_mark_expired` succeeds, so the escalated copy loses its keyboard at the
     same time the parent is expired.
  2. For Problem A: consider clearing `escalate_at` *after* a successful send+insert
     (accepting a theoretical duplicate on a crash between send and clear), or at
     minimum escalate the log level to WARNING and record a `escalation_failed_at`
     sentinel so operators can detect the failure.

---

### Issue 4 — LOW: `answer_waiters` accepted by `_escalation_pass` but never used

- **File:** `relay-server/relay_server/reaper.py:938–941, 951–953`
- **Problem:**
  `_escalation_pass` accepts `answer_waiters: ConditionWaiterRegistry | None` and its
  docstring says it "cannot immediately wake any parked answer feed long-pollers" when
  the parameter is absent.  However, `answer_waiters` is never referenced in the
  function body.  Waking answer waiters is already handled by `_record_answer` in
  `app.py`'s webhook path; the escalation pass only *fires* the escalation, it does
  not record answers.  The parameter is dead code and the docstring is misleading.
- **Fix:** Remove `answer_waiters` from `_escalation_pass`'s signature and the call
  site in `reaper_tick:522`.  Update the docstring.

---

## Code Quality Notes

- **`_row_is_capped` called twice per row:** The comprehension `[r for r in chat_rows if _row_is_capped(r)]` + `[r for r in chat_rows if not _row_is_capped(r)]` calls `_row_nudge_schedule` (and therefore `parse_nudge_schedule_with_repeat`) twice per row.  Not a correctness issue at current message volumes, but worth noting.

- **Test substitution obscures the requirement gap:**  Using `24h`/`72h`/`168h` in place of `1d`/`3d`/`7d` means the tests pass but do not exercise the documented input.  When Issue 1 is fixed the tests should be updated to use the spec literals.

- **`test_duplicate_not_in_target_installation_feed` is shallow:**  It asserts `installation_id != inst_id_b` on the child row but does not actually invoke the feed endpoint for installation B to confirm no row surfaces.  Not a blocker but would be stronger with a feed call.

- **No test for expiry → child cancellation:**  No test asserts that a child is cancelled when the parent expires.  This gap aligns with Issue 3B being unimplemented.

---

## Rulings on Pre-Identified Findings

**A — CONFIRMED, HIGH.**  `parse_duration('1d')` returns `None`; `parse_nudge_schedule_with_repeat('4h,1d,3d,7d*', 10)` raises `ValueError: bad duration '1d'` (verified by running the code).  The implementer substituted longer hour equivalents in tests rather than fixing the parser.  The Done-when clause is not demonstrated.  Fix: add `d` support to `_DURATION_RE`; the change is backward compatible.

**B — JUSTIFIED, accepted.**  `parent_message_id` is the minimal addition required.  No three-column design can support answer attribution without it.  Column is nullable, NULL for all existing rows, added via `ALTER TABLE` with no rebuild.  Record as an accepted architecture deviation.

**C — MEDIUM.**  Clear-before-send loses escalations on transient failures and leaves dangling child rows that are never cleaned up when the parent expires (the expiry pass does not call `_cancel_escalated_children`).  The "fire-once" rationale is plausible, but the silent loss + leaked rows together are a MEDIUM defect, not an acceptable trade-off without explicit sign-off.  Recommend (at minimum): add `_cancel_escalated_children` to the expiry pass, and raise the log level on send failure to WARNING.

---

## Questions for User

None — all rulings are deterministic from the code and spec.
