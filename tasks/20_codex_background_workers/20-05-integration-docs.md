# Task 20-05 — Installer, integration tests, and operator docs

**Status:** todo · **Depends on:** 20-03, 20-04, amux 01-05

## Goal

Ship the Codex worker path through the same installation and verification
surfaces as the existing launcher.

## Work

1. Add every new module/executable to the installer allowlists and diagnostics.
2. Pin an amux revision containing sibling epic 01 and make drift actionable.
3. Add integration tests with fake `amux` and fake `codex` executables for
   launch, JSONL, result capture, failure, timeout, resume, and cleanup.
4. Add a private-tmux integration test where practical.
5. Document examples for detached, wait, status/last, resume, model/profile, and
   YOLO behavior, including the absence of Telegram permission prompts.
6. Run the full Claude regression suite and sibling amux suite.

## Done when

- A fresh install exposes the Codex-capable launcher and all imports resolve.
- Documentation identifies the trusted-host implications of YOLO without
  presenting permission integration as a prerequisite.
- Automated tests require no OpenAI credentials and leave no live tmux sessions.
- Full suites are green at the pinned revisions.
