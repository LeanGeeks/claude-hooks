# Task 20-01 — Provider-neutral handle and Codex event reducer

**Status:** todo · **Depends on:** amux 01-01 lifecycle spike

## Goal

Make tracked-worker state capable of representing Claude and Codex without
changing launch behavior yet.

## Read first

- [Epic architecture](./architecture.md), especially §§3–5
- [amux spike task](../../../amux/tasks/01_codex_cli_provider/01-01-lifecycle-spike.md)
- `.claude/hooks/amux_spawn_lib.py`
- `.claude/bin/amux-spawn` state derivation
- `tests/test_unit_amux_spawn.py`, `tests/test_unit_amux_reads.py`, and
  `tests/test_unit_amux_supervise.py`

## Work

1. Extend the strict handle schema with provider/lifecycle fields while keeping
   old handles readable as Claude.
2. Add a pure, bounded reducer for captured Codex JSONL fixtures from amux 01-01.
3. Normalize thread ID, turn completion/failure, activity time, exit status, and
   final-message availability.
4. Ignore unknown events and tolerate a partial last line during a live write.
5. Ensure Claude producer hooks either preserve the new fields or explicitly
   ignore Codex handles.

No subprocess launch, amux invocation, or Telegram code changes in this task.

## Done when

- Legacy and new Claude handle fixtures derive the same results.
- Codex fixtures cover started, completed, failed, unknown-event, truncated-line,
  and process-disappeared cases.
- The reducer never reads Codex's internal transcript directory.
- Strict-schema and atomic-write tests pass.
