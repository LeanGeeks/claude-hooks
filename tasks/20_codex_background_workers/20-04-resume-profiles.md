# Task 20-04 — Resume and Codex model/profile ergonomics

**Status:** todo · **Depends on:** 20-02, 20-03, amux 01-04

## Goal

Support a second bounded turn on the same Codex thread and make model selection
predictable without imposing Claude's profile semantics on Codex.

## Work

1. Add the decided resume surface and require a captured Codex thread ID.
2. Serialize resume against spawn/remove and reject concurrent live attempts.
3. Preserve prior event/result evidence; record an attempt number and start a new
   artifact segment.
4. On failed resume launch, restore the prior readable result/state.
5. Pass explicit Codex model/profile choices through the amux contract while
   leaving omission to Codex's own config.
6. Keep existing Claude `~/.claude/profiles.toml` behavior unchanged; document
   any provider-specific spelling needed to avoid ambiguous `--profile` meaning.

## Done when

- Stub and real-fixture tests prove the exact thread ID is resumed.
- Missing/malformed IDs never fall back to a fresh thread.
- No Codex model is injected when the user did not request one.
- Attempt history remains inspectable and `last` returns the newest successful
  result.
