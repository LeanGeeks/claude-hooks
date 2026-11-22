# 50-09 — Role prompts

**Status:** todo · **Depends on:** 50-06, 50-08
**Read first:** [brd.md](./brd.md) §4 (OD-2, OD-8, OD-9) · [architecture.md](./architecture.md) §9.1, §9.2, §10,
§11.3 (the handoff format), §14.3–§14.5, §15, §19 · [state.md](./state.md) P0-7, P0-13, OQ-4, invariants 3, 4, 6 ·
review m10 · `docs/prompts/implementer.md`, `docs/prompts/reviewer.md` (house style) · the activecdn implementer
prompt (`/data/sync/work/activecdn/acdn-workspace/docs/prompts/implementer.md`, rule 7) as an example of an epic's own
prompt

## Goal

The text rules that replace hook fencing (OD-2): what a driven session reads first, how it reports, what it must never
do, and how the driver closes a wave and asks the operator.

## Scope

1. **`docs/prompts/epic-driver-driver.md`**, per §19: the event protocol (read the event file, the `role` item, merged
   items); the verdict grammar with one example per verb, a merged-event example, the ping; forbidden tools (`amux`,
   `amux-spawn`, Monitor, `watch`, `AskUserQuestion`, sleep loops); D8's production-write rule (only through the epic's
   reviewed scripts, under its standing approvals; long actions detached on the server with their own record);
   questions only through `mcp__questions__ask`, with a recommendation; approvals one at a time and the `mark --grant`
   step (§15.1); the closing sequence (re-run tests, explicit staging that never includes the question-queue
   directory, commit on the step branch, `--no-ff` merge into `epic/<epic>`, the `ADVANCED` sha forms) and the push
   rules (driver's decision; never any force form; repos without a remote stay on the epic branch); the review loop
   and `epic-driver queue` (OQ-4); never edit the ledger; epic records only in the workspace repo's integration
   worktree, never in the operator's checkout; a dirty worktree is discarded only after asking; the handoff format,
   only when a rotate event asks.
2. **`docs/prompts/epic-driver-worker.md`**, the wrapper (review m10): read the epic's own prompt at the path the event
   names and follow it, except: never commit (OD-9); never ask the operator (draft the question in the report, end
   `HALT: <one line>`); write the implementation or review report to the path the event names; end `DONE: <step>`; the
   same forbidden-tools list; the handoff format, only when asked.
3. Both prompts short enough to read in one turn (target under 250 lines each), in the house style of
   `docs/prompts/implementer.md`.

## Tests

- A prompt test in `tests/test_unit_epic_driver.py` that every verdict example in both prompts parses with 50-05's
  parser, and that the forbidden-tool names and the `--force` ban appear.

## Done when

- A reader of either prompt alone can produce a valid verdict for every event kind of §9.1 it can receive.
- Nothing in the prompts contradicts an operator decision (brd §4) or a cross-task invariant.
