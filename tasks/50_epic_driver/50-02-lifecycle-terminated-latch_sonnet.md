# 50-02 — Lifecycle: clear the `terminated` latch

**Status:** todo · **Depends on:** Phase 0
**Read first:** [architecture.md](./architecture.md) §13.4, §13.3 · [state.md](./state.md) P0-4 · review M3 ·
`.claude/hooks/claude_event_reducer.py` (`reduce_events`, `empty_reduction`) · `.claude/hooks/spawn_producer_hook.py`
(`handle_user_prompt_submit`, `handle_session_end`) · `.claude/hooks/lifecycle_events.py` · `.claude/bin/amux-spawn`
(`_derive_claude_status`) · `tests/test_unit_claude_reducer.py`, `tests/test_unit_spawn_producer.py`

## Goal

A session that ended and was resumed reads `running`/`idle` again instead of `terminated` forever. P0-4 chose the
cheap fix: a `turn_start` after a `session_end` clears the latch. No new event type, no new hook, no installer change.

## Scope

1. `claude_event_reducer.reduce_events`: on `turn_start`, if `terminated` is set, clear it. The state hint then
   follows the normal rules (open turn ⇒ `running`; a later `stop` ⇒ `idle`). A `session_end` after that sets it again.
2. The stored handle: `handle_user_prompt_submit` already writes `state = "running"` whatever the previous state.
   Keep it so and pin it with a test; 50-01's resume writes `spawning` before the first turn. Confirm that
   `_derive_claude_status` then reports a resumed session from the reducer, not from a stale stored `terminated`.
3. Update the reducer's module docstring (the latch is no longer permanent) and `docs/amux-spawn-fleet-supervision.md`
   wherever it says a terminated session stays terminated.

## Tests

- Reducer: `session_end` alone ⇒ terminated; `session_end, turn_start` ⇒ running, not terminated;
  `session_end, turn_start, stop` ⇒ idle; `session_end, turn_start, stop, session_end` ⇒ terminated; a log whose
  last line is a partial write after `session_end` does not clear it.
- Producer: `UserPromptSubmit` on a handle whose stored state is `terminated` writes `running` and appends
  `turn_start`.
- amux-spawn derived status: a handle stored `spawning` with a live pane and a log `session_end, turn_start` reads
  `running`.

## Done when

- The reducer stays pure stdlib with no project imports.
- No other reducer behaviour changes (the existing suite passes untouched).
