# Task 20-02 — Provider-aware launcher

**Status:** todo · **Depends on:** 20-01, amux 01-03

## Goal

Allow `amux-spawn spawn --provider codex` to create a tracked, bounded Codex
worker through amux while preserving every existing Claude call.

## Read first

- [Epic BRD](./brd.md) §§3–4
- [Epic architecture](./architecture.md) §§1–3 and §8
- amux 01-03 acceptance criteria and installed/pinned CLI revision
- `.claude/bin/amux-spawn` launch, argument parsing, lock, naming, and cap code

## Work

1. Add validated `--provider claude|codex`, defaulting to Claude.
2. Pass provider and bounded-exec mode through amux's public CLI contract.
3. Allocate collision-safe, mode-0600 event/result artifact paths.
4. Create the provider-aware handle under the existing spawn lock.
5. Delegate `--yolo` translation to amux; do not synthesize provider flags here.
6. Preserve multiline prompts and explicit model/extra argv boundaries.
7. Roll back handle/artifacts/session cleanly on partial launch failure.

## Done when

- Unit tests assert exact amux argv for both providers.
- Existing no-`--provider` fixtures are byte-for-byte equivalent in behavior.
- Codex launch creates no Claude transcript guess and mints no fake Codex UUID.
- A stub Codex JSONL stream can populate the thread ID after launch.
- Concurrent naming/cap tests remain green.
