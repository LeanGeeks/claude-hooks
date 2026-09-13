# Task 38 — `watch` digest silently drops a handle whose status derivation raises (bug)

**Status:** done · **Type:** bug · **Created:** 2026-09-13 · **Rev:** 2
**Priority:** high — the supervision stream manufactures silence; measured
2 h 09 m 38 s of lost detectability on a real fleet, against a <10 min target
**Suggested worker:** one implement → review → fix loop; small and
self-contained
**Read first:** §1 · §2
**Scope:** `.claude/bin/amux-spawn` (`_settled_snapshot` and the watch
helpers it calls), `tests/test_unit_amux_watch.py`,
`docs/amux-spawn-agent-reference.md`, `docs/amux-spawn-fleet-supervision.md`,
this file. Nothing else.
**Origin:** production post-mortem, 2026-09-13. Root cause isolated by the
manager; approved for fix by the repo owner.

## 1. The defect

`_settled_snapshot` (`.claude/bin/amux-spawn:2419`) partitions every
enumerated handle into settled or pending:

```python
for name, handle in handles.items():
    try:
        result = _derive_status(handle, stuck_after)
    except Exception:  # noqa: BLE001
        continue
```

A handle whose derivation raises is dropped from **both** partitions. It
vanishes from the digest's arithmetic (`settled` entries + `pending` count no
longer account for `watching`) with no trace in the emitted JSON that
anything was skipped. For a supervision stream whose entire contract is
"silence means nothing happened" (`docs/amux-spawn-fleet-supervision.md`,
level-triggered digests, successive emissions are supersets), this is a
silence-manufacturing construct: a subscriber cannot distinguish "nothing
settled" from "a handle blew up and was swallowed".

**What it cost, on a real fleet:** a judgment session committed its ruling
and settled `idle` at 21:57:35 UTC. The next digest fired 9 s later and did
not contain it. No digest mentioned it for 2 h 09 m 38 s, until a human
killed it by hand. The dependent work was blocked the whole time; the
detection target is under 10 minutes.

**Root cause is isolated — do not re-litigate or "fix" any of these:**

- The lifecycle reduction is NOT at fault. Replaying the truncated lifecycle
  through `claude_reducer.reduce_log()` over the exact silent window gives
  `state_hint='idle'`, `turn_open=False`, `has_stop_event=True`, and
  `_is_watch_reportable('idle', False) -> True`. A control run without the
  trailing `subagent_stop` is identical.
- `_derive_claude_status` is NOT at fault: it is deliberately written so a
  finished worker with a live pane derives `idle`, never `stuck`. Note the
  consequence — `--stuck-after` CANNOT backstop a lost `idle` settle, so
  there is no second line of defence today. That is why the swallow must be
  reported, not merely narrowed.
- Enumeration is NOT at fault: `amux-spawn watch --run-id <id> --block` over
  the same run-id returns every handle including the one that went missing.

That leaves the `except ... continue` in `_settled_snapshot` as the only code
path that can drop a reportable handle without a trace.

## 2. The fix

A handle whose derivation raises must be **REPORTED, not dropped**. The
stream already has the vocabulary: reportable outcomes are what `_settled_snapshot`
feeds both emission paths with. Concretely:

1. In `_settled_snapshot`, an exception from `_derive_status` produces a
   reportable entry for that handle instead of a `continue`. The entry must
   carry the handle name (as every digest entry does) and must identify the
   error — recommended shape `{"name": ..., "state": "error",
   "error": "<type name>: <message>"}`, but the exact field naming is the
   implementer's choice. It must identify the failure even when `str(exc)`
   is empty, so include the exception type. Keep the blanket `except
   Exception` (catch-broadly-and-report is the right shape here; do not
   narrow to specific exception types).
2. `_is_watch_reportable` must treat the error outcome as reportable, so the
   entry lands in `settled` and flows to **both** emission paths: streaming
   digests and `--block` lines. No separate handling should be needed for
   block mode — if it turns out to need any, that is a design smell to
   surface in the implementation report, not something to paper over.
3. The arithmetic invariant is restored and becomes a property of the stream:
   in every digest, `len(settled) + pending == watching`. Assert it in the
   tests (§3).
4. Change detection must keep working: the error state enters
   `_settled_fingerprint` like any other state, so a later poll that derives
   successfully (transient failure, e.g. a read race) changes the fingerprint
   and the corrected entry supersedes the error entry in the next digest.
   Verify no code is needed beyond the state being in the entry; if something
   blocks that, report it.
5. Update the digest documentation where it tells subscribers what states
   mean: `docs/amux-spawn-agent-reference.md` (the "Acting on a digest"
   state table, and the digest example/field table if needed) and
   `docs/amux-spawn-fleet-supervision.md` (equivalent sections). The new
   state means: *the worker's status could not be derived — something is
   wrong with this handle's evidence; investigate, do not treat as pending*.

**Decided by the manager — do not do these:**

- **No `enumerated` field in the digest.** The optional follow-up from the
  defect brief ("carry an `enumerated` count alongside `settled`/`pending`")
  is already satisfied: `watching` is emitted in every digest, is exactly
  `len(handles)` at every call site, and is documented as "Total handles in
  the subscription". Once the swallow is fixed, `watching != len(settled) +
  pending` is detectable by any reader — that is the whole of what the
  optional item wanted, at zero schema cost. Adding a second field equal to
  `watching` would be sprawl.
- **Out of scope, each exonerated above or a separate concern:** the
  reducer, `_derive_claude_status`'s precedence, the `stuck` invariant,
  enumeration/`run_id` filtering, `ls` scoping. Do not touch them, even
  where the new code sits next to them.

## 3. Tests

All in `tests/test_unit_amux_watch.py`, following the file's existing
conventions (throwaway `~/.amux` via `_redirect_amux_home`, `_seed_handle`,
`time.sleep` stubbed, `tmux_has_session` stubbed, `resolve_amux_session`
stubbed to `None`, stdout captured, advancing `time.time`). Watch each test
to **fail on the unfixed code first**, and quote the failure.

1. **The required regression test:** several handles in one run-id; force
   `_derive_status` to raise for exactly one of them (a side-effect function
   that raises for one name and delegates to the real implementation for the
   rest is the cleanest mechanism). The digest must:
   - contain the errored handle in `settled`, with its name and an
     identifying error value (not silently absent, not in `pending`);
   - still report the other handles correctly — one cleanly idle with a real
     `state`, at minimum; assert their actual state values, not just
     presence;
   - satisfy the arithmetic: `len(digest["settled"]) + digest["pending"] ==
     digest["watching"]`, and `watching` equals the number of seeded
     handles.
   This is "assert the arithmetic, not just the happy path": on the unfixed
   code the errored handle is missing from both partitions, so the arithmetic
   assertion must fail.
2. **Block mode:** same setup; `--block` must emit the errored handle as a
   settled line (with the error visible) rather than timing out on it, and
   the other handles still come out with their real states. On the unfixed
   code this test must also fail.

A direct `_settled_snapshot`-level unit test (partition returned, no digest
parsing) may be added if it fits cleanly; it does not replace either of the
above.

## 4. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`, with
  or without a pathspec. Mutation-test only in `/tmp` copies of the file.
  Never push.
- **A repo edit is not live.** Do not run `./install.sh`, and do not touch
  the installed copy at `~/.local/bin/amux-spawn`. Verification for this
  task is unit-level (the test module imports the repo copy directly); making
  the change live on a real machine is the owner's action, recorded as such
  in the implementation report — do not claim end-to-end verification.
- Run the suite and **read its output**, never just the exit code:
  - Module: `python3 tests/run_all_tests.py --module unit_amux_watch`
    — baseline before this task: **35 tests, OK**.
  - Full suite: `python3 tests/run_all_tests.py` — baseline measured at
    filing: **1921 ran, OK, skipped=1** (`test_headless_spawn`), exit 0.
    Note the runner's final `Ran N tests ... OK` summary is followed by
    trailing output from test atexit hooks — grep for `Ran .* tests`, do not
    trust the last lines of stdout. If you see failures, read them and
    determine whether they are yours before fixing or reporting.

## 5. Done criteria

1. §2's fix: an errored handle is reported as a reportable outcome in both
   emission modes, carrying name and error; `_is_watch_reportable` covers it;
   the arithmetic invariant holds; docs updated.
2. §3's tests, watched to fail first, with the failure output quoted in the
   implementation report.
3. Suite green with counts, output read and summarised (not just exit code).
4. Implementation log in `agents_output/task38_implementation_report.md`.

---

## 6. Implementation log (rev 2 — landed)

**Status:** done · **Landed:** 2026-09-13 · **Baseline commit:** `9cad056`
**Not installed.** `./install.sh` was not run; the copy tested is the repo
working tree. The installed copy at `~/.local/bin/amux-spawn` still carries
the swallow until someone reinstalls.

### 6.1 What changed

Landed as `1d797fd` (5 files, +499/−20): `.claude/bin/amux-spawn`,
`tests/test_unit_amux_watch.py`, `docs/amux-spawn-agent-reference.md`,
`docs/amux-spawn-fleet-supervision.md`, this file.

1. **`_settled_snapshot`** — the `except Exception` branch no longer
   `continue`s. It builds a reportable entry via the new `_watch_error_entry`:
   `{"name": ..., "state": "error", "error": "<Type>: <message>"}` (bare type
   name when the message is empty) and stores it in `settled`. The blanket
   `except Exception` is kept — catch-broadly-and-report, not narrowed.
2. **`_is_watch_reportable`** — `error` added to the reportable set, so the
   entry flows to both streaming digests and `--block` lines with zero
   block-mode-specific handling (an errored handle unblocks a `--block` wait
   with exit 0 instead of hanging it).
3. **Supersession** — no code needed: `error` enters `_settled_fingerprint`
   like any state, so a later poll that derives successfully re-emits the
   corrected entry. Test-proven.
4. **Arithmetic invariant** — `len(settled) + pending == watching` is now
   unconditional (every handle is stored or counted) and asserted by tests.
5. **Docs** — both digest docs document the `error` state ("status could not
   be derived — investigate, do not treat as pending") and the invariant; the
   fleet-supervision digest example's pre-existing arithmetic mismatch
   corrected (`pending: 1` → `2`, review pass 1 MEDIUM).
6. **`enumerated` field** — deliberately not added (§2 decision): `watching`
   already is the enumerated count at every emit site.

Out-of-scope surfaces (reducer, `_derive_claude_status`, stuck invariant,
enumeration/`run_id` filtering, `ls`) untouched — verified hunk-by-hunk in
review.

### 6.2 Verification

- All 5 new tests watched failing on the unfixed code first (`Ran 40 tests
  ... FAILED (failures=4, errors=1)` with the swallow restored via /tmp-copy
  mutation testing; repo tree untouched throughout).
- Module suite: 35 → **40 tests, OK**.
- Full suite: baseline 1921 ran OK skipped=1 → **1926 ran, OK, skipped=1**
  (`test_headless_spawn`), exit 0, read in full.
- Review pass 1: **PASS** (1 MEDIUM — fixed; 3 LOW — accepted, see
  `agents_output/task38_review_report.md`). Review pass 2: **PASS**, no new
  issues (`agents_output/task38_review_report_2.md`).
- Reports: `agents_output/task38_{implementation,fix,review,review_2,commit}_report.md`.
