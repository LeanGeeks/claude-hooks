# Task 20-06 — Live cross-project verification

**Status:** todo · **Type:** human/live · **Depends on:** 20-05

## Goal

Verify the installed chain with real Claude, Codex, tmux/amux, network, and
Docker. Do not modify either example project's workflow definitions.

## Procedure

1. From a Claude session, spawn a bounded Codex reviewer-style task patterned
   after Flightclaim and wait for its result.
2. Spawn an implementer-style task in a disposable worktree or temporary
   fixture patterned after Hyppie Flow's file/verdict contract.
3. Verify `status`, `last`, `wait`, `ls --json`, resume, and `rm` for both.
4. In a disposable directory, have Codex create/read a marker file outside the
   project but inside an explicit temporary test root.
5. Perform a harmless network request and run a disposable Docker container;
   record evidence that Codex was not sandbox- or approval-blocked.
6. Exercise a deliberate nonzero failure and a short stuck/timeout case.
7. Run one real Claude worker afterward to catch provider-default regressions.

## Safety of the live check

YOLO intentionally grants broad host access, but the verification commands must
still use a `mktemp -d` root, a uniquely named disposable container, and no real
credentials or production services. Record exact cleanup and verify it.

## Sign-off evidence

- Installed revisions of both repositories and `codex --version`.
- Commands, handle/status JSON, final results, and resume thread continuity.
- Network/Docker/file evidence and cleanup confirmation.
- Any project-instruction gaps discovered, recorded as follow-up knowledge work
  rather than silently changing the example workflows.
