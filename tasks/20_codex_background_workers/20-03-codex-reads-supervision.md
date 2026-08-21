# Task 20-03 — Codex reads and supervision

**Status:** todo · **Depends on:** 20-01, 20-02, amux 01-04

## Goal

Make `status`, `last`, `ls`, `wait`, and `rm` honest for bounded Codex workers.

## Work

1. Dispatch state derivation by handle provider.
2. For Codex, combine reducer facts, event mtime, process/tmux liveness, and exit
   metadata using the precedence in architecture §5.
3. Treat a bounded run carrying completion evidence (a `turn.completed` line
   plus a readable final-message artifact) as `idle`, not `terminated` —
   regardless of exit code, per architecture §5.
4. Read `last` from the explicit final-message artifact with a size bound.
5. Preserve `wait` exit codes `0`, `1`, and `3` and its stdout-only result
   contract.
6. Add Codex-specific reason context for turn failure, nonzero exit, malformed
   completion, and stale activity.
7. Make `rm` remove only the exact handle's event/result artifacts.

## Done when

- Tests cover running→idle, running→failed, running→stuck, timeout, missing
  result, tmux exit after success, and removal.
- `ls --json` identifies the provider and normalized state.
- Claude status/read/supervision fixtures remain unchanged.
- No terminal-pane scraping is used as an authoritative signal.
