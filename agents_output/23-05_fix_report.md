# Fix Report — 23-05 Issue 4: First-start watermark seeding

## What changed

### `.claude/hooks/questions_listen_lib.py`

**New method** `Listener._seed_primary_watermark()` (inserted after `feed_by_fingerprint`, before the `# ── the loop ──` section).

The method reads the index under the existing `mutate_index` flock and, if the
primary fingerprint is absent from `watermarks`, seeds it from the top-level
`watermark` field.  Three guard conditions inside the `_seed` closure:

1. `if fp in idx.watermarks: return` — never overwrites a recorded position.
2. `legacy = idx.watermark` — `Index.from_dict` already coerces every value to
   `int` defaulting to 0, so a missing or malformed field is safe.
3. `if legacy > 0:` — a zero or negative legacy watermark means "never polled";
   seeding from it would be a no-op or incorrect, so it is skipped.

**Call site** in `run_cycle`, immediately after `self.cycles += 1`:

```python
if self.cycles == 1:
    self._seed_primary_watermark()
```

This fires on the very first cycle of any `Listener` instance (whether called
from `run_forever` or the `--once` CLI path) and is never repeated.

## Why seeding only the primary fingerprint is the correct scope

The top-level `watermark` field is, by definition, a mirror of the **primary**
feed's position — `set_feed_watermark` writes it only when
`fingerprint == primary_fingerprint`.  It therefore only knows where the primary
feed stood when 23-04 was operating.

A role-bound token (e.g. `[roles] hpl = "rly_…"`) has its own installation
feed.  That feed was **never polled** by 23-04 because 23-04 did not support
multi-token listening.  Starting it at 0 (a full replay) is therefore the
correct and safe starting point: any answers given to the role during the 23-04
era need to be delivered.  Seeding the role feed from the primary's legacy
position would skip those answers permanently and silently — exactly the
outcome this component must not produce.

## Tests added

Six tests in the new class `TestLegacyWatermarkSeed`
(`tests/test_unit_questions_listen.py`):

| Test | Property verified |
|------|-------------------|
| `test_legacy_watermark_seeds_primary_fingerprint` | `watermarks[primary_fp]` is set to `index.watermark` when the key is absent; top-level mirror stays consistent |
| `test_legacy_watermark_does_not_seed_role_bound_fingerprint` | A second (role-bound) fingerprint remains absent from `watermarks` after seeding |
| `test_existing_primary_entry_is_never_overwritten` | When `watermarks[primary_fp]` already exists, the seed is a no-op |
| `test_existing_entry_is_not_moved_backwards` | A recorded position higher than the legacy field survives untouched |
| `test_fresh_index_with_no_legacy_watermark_starts_at_zero` | A brand-new installation (no index file → `watermark=0`) starts at 0, not incorrectly seeded |
| `test_seed_runs_only_on_the_first_cycle` | `self.cycles == 1` guard fires exactly once; the second cycle never re-seeds |

None of the tests use `skipTest`, bare `except`, or "nothing raised" assertions.
Each test checks a concrete observable state (watermark values and presence in
the map) after running a cycle.

## Verification

```
python3 -m py_compile .claude/hooks/questions_listen_lib.py
python3 -m py_compile tests/test_unit_questions_listen.py
→ both clean
```

```
python3 tests/run_all_tests.py
→ Ran 1222 tests in 32.398s
   OK (skipped=1)
```

Baseline was `Ran 1216, skipped=1`.  The +6 tests are all this fix's.
The single skip is the pre-existing
`test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`
(`live spawn test (needs tmux + model auth); set AMUX_SPAWN_LIVE_TEST=1`).

```
/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
→ 315 passed in 7.01s
```

Relay suite is untouched.

## Blockers

None.
