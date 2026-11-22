# Implementation Report - 50-09 Role prompts

## Summary

Wrote the two role prompts that replace hook fencing (OD-2): `epic-driver-driver.md` (149 lines) and the worker wrapper `epic-driver-worker.md` (91 lines). Added `TestRolePrompts` to `tests/test_unit_epic_driver.py`, which extracts every fenced verdict example and checks it against 50-05's `parse_verdict_line`.

## Files Created

- `docs/prompts/epic-driver-driver.md` - 149 lines. Event protocol, verdict grammar with one example per verb plus a merged example, forbidden tools, D8 production-write rule, questions and approvals (`mark --grant`), worker review loop (`epic-driver queue`), closing sequence, push rules, epic records, handoff format.
- `docs/prompts/epic-driver-worker.md` - 91 lines. Wrapper around the epic's own prompt (m10): never commit, never ask the operator (draft in report, `HALT:`), report to the event's path, end `DONE: <step>`, forbidden tools, handoff format.

## Files Modified

- `tests/test_unit_epic_driver.py` - appended the `TestRolePrompts` section (7 tests). Nothing else changed; no code changed.

## Example-extraction convention

Documented in each prompt's leading HTML comment:
- A fenced block with info string exactly `verdict` holds valid verdict lines, one per line; each must parse with `epic_driver_lib.parse_verdict_line`.
- A fenced block with info string exactly `verdict-invalid` holds counter-examples; each must fail to parse.
- The prompt body sits in a four-backtick fence so three-backtick blocks nest inside it. Handoff format blocks use a plain fence and are not extracted.
- The test also requires: the driver shows every verb except `DONE`, a merged line, and a `name=sha` form; the worker's valid examples are only `DONE`/`HALT`.
- Counter-examples are syntax-level only (the parser is syntax-only); semantic invalids (`DONE` from the driver, `ADVANCED` from a worker, an id not in the event, fenced verdict) are stated in prose.

## Verification

- Compile/import: PASS (`py_compile tests/test_unit_epic_driver.py`; the module imports and runs).
- Tests: `python3 tests/run_all_tests.py --module unit_epic_driver`: 492 tests, all OK (7 new). Full suite `python3 tests/run_all_tests.py`: 3037 tests, OK, 2 skipped (baseline 3030 + 7 new).
- Installed (install.sh --yes re-run): not applicable (installing prompts is 50-10; no install run).

## Architecture-vs-code discrepancies

1. `epic-driver queue <step> --to <role>`: architecture §14.3 writes `--to implementer`, but `queue_request` requires `--to` to be the ledger's `worker.implementer` or `worker.reviewer` role name, and only for worker steps. `--file` is made absolute by the CLI. The prompt tells the driver to use the role name shown in the item.
2. `mark --grant` takes the approval as an option value, with `--question` and `--note` optional; matches §18. `mark` positional `step`/`action` are separate. No conflict, noted for exactness.
3. The `rotate` event text (code) lists the handoff sections as "Where things stand, Decisions made here, Open threads, First thing to check, Files"; matches §11.3. The prompts copy §11.3's header block.
4. The `approval_needed` item text in code says "end with `ASKED: Q-<n> <approval>`", consistent with §15.1.
5. The worker's `DONE` is accepted by the code for any step id (`STEP_ID_RE`); the code allows workers only `DONE`/`HALT` (§14.3), stricter than §10's "workers only" for DONE alone. The worker prompt follows the code.
6. The driver's `ADVANCED` for worker steps requires one sha per repo (bare sha acceptable for one repo, `name=sha` for several); driver steps need none. Prompt follows the code.
7. The event file's `role` item (code) says "Read your role prompt at <path> first." plus "It wraps the epic's own prompt at <path>; read that too." for workers; the worker prompt matches.

## Decisions

- Four-backtick outer fence so the examples nest; house style (a `## System Prompt` fence) otherwise kept.
- The driver prompt does not say where the integration worktree path is shown beyond `<worktree_root>/integration/<repo name>` (architecture §14.1); the worker_settled item carries no worktree path in the code.
- `HANDOFF:` example in the driver prompt uses a plausible absolute path; it is syntax-checked only.
- Forbidden list in both prompts also names `sleep` loops and, for the worker, production changes unless the epic prompt assigns them.

## Blockers

None.

## Questions for User

None.
