# Epic 20 — Codex background workers through amux

**Status:** planned · **Owner:** Anton · **Created:** 2026-08-21 · **Rev:** 1

> Tooling only. This epic does not change project workflows in Hyppie Flow or
> Flightclaim. See [state.md](./state.md) for ordering and
> [architecture.md](./architecture.md) for the cross-repository contract.

## 1. Problem

`amux-spawn` is the background-worker surface used by Claude orchestrators, but
its tracked-session contract is Claude-specific:

- launch flags and profiles target Claude Code;
- a Claude UUID is minted before launch;
- `Stop` hooks and Claude transcripts produce completion and status;
- `status`, `last`, and `wait` assume that producer and transcript shape.

The sibling amux fork can open Codex interactively, but that is not enough for a
Claude orchestrator to launch a bounded Codex worker, observe it, collect its
answer, and optionally resume it.

## 2. Thesis

Add Codex as a provider behind the existing `amux-spawn` worker contract while
keeping Claude behavior backward-compatible. Codex background workers run as
trusted, noninteractive `codex exec` processes inside amux and use Codex's JSONL
event stream plus final-message artifact as their lifecycle source.

Provider-specific process construction and persisted provider identity belong
to amux ([sibling epic](../../../amux/tasks/01_codex_cli_provider/)). This epic
owns the workflow-facing launcher, tracked handle, reads, waits, resume UX,
installation, and verification.

## 3. User-visible contract

The target surface is:

```bash
amux-spawn spawn review-123 \
  --provider codex \
  --yolo \
  --dir /path/to/project \
  --detach \
  -- "Read the project instructions and review task 123"

amux-spawn status review-123
amux-spawn last review-123
amux-spawn wait review-123
amux-spawn resume review-123 -- "Address the remaining issue"
amux-spawn rm review-123
```

Exact resume spelling may be adjusted by task 20-04, but the capability and
machine-readable outcome are required.

### Defaults

- `--provider` defaults to `claude`; existing calls remain unchanged.
- `--provider codex --yolo` means Codex
  `--dangerously-bypass-approvals-and-sandbox`: no approval prompts and no
  Codex sandbox. Host filesystem, network, and Docker access are intentional.
- Codex runs noninteractively for tracked background workers. Human interactive
  sessions remain outside this epic.
- No hardcoded Codex model. With no explicit model/profile, Codex's own config
  decides.

## 4. Required behavior

### 4.1 Launch

- Preserve multiline prompts and argument boundaries.
- Persist the provider so restart/resume cannot silently become Claude.
- Create separate Codex event and final-message artifacts per tracked session.
- Capture the Codex thread ID from authoritative output, not terminal scraping.
- Refuse unsupported provider/flag combinations with an actionable error.

### 4.2 Status and completion

The external state vocabulary remains `spawning`, `running`, `idle`, `stuck`,
and `terminated`:

- a completed `codex exec` with a valid result reads as `idle`, even though its
  tmux process has exited, and regardless of exit code;
- an active process reads as `running`, or `stuck` after the configured activity
  threshold;
- `turn.failed`, or disappearance without a valid completion, reads as
  `terminated` with reason context — a nonzero exit is reason context on that
  determination, never the determination itself;
- `last` returns the final assistant message without parsing the unstable Codex
  transcript format;
- `--wait` returns that message on success and retains the existing timeout/error
  exit-code contract.

### 4.3 Resume

- Resume uses the captured Codex thread ID.
- Resume starts a new bounded `codex exec resume` process and writes a new event
  segment without destroying prior evidence.
- A missing or malformed thread ID fails closed; it never starts a fresh,
  unrelated conversation under the old handle.

### 4.4 Compatibility

- Claude launch, hooks, handles, status derivation, and tests remain green.
- Old Claude handles without provider fields continue to mean `claude`.
- Existing `amux-spawn` callers need no new flags.

## 5. Explicit non-goals

- Porting Telegram permission requests to Codex.
- Codex allow/deny rules, sandbox profiles, network allowlists, or Docker
  mediation.
- Replacing foreground Claude orchestrators.
- Translating Claude native subagent definitions to Codex custom agents.
- Editing Hyppie Flow or Flightclaim workflow files.
- Depending on Codex's internal transcript format.

If a Codex worker needs human input, it returns a normal blocked/HALT result;
the foreground Claude workflow remains responsible for Telegram interaction.

## 6. Success criteria

- [ ] A Claude session can spawn a Codex worker with YOLO host access through
      `amux-spawn`.
- [ ] `status`, `last`, `wait`, `resume`, and `rm` work for that worker.
- [ ] A successful worker is distinguishable from failure and timeout without
      terminal-text heuristics.
- [ ] Codex can read/write a disposable workspace, access the network, and run a
      disposable Docker command during live verification.
- [ ] The provider and Codex thread ID survive restart/resume.
- [ ] Existing Claude tests and a real Claude spawn chain remain green.
- [ ] Manual verification succeeds against bounded tasks patterned after both
      example projects, without committing changes to those projects.

## 7. Primary dependency

The amux CLI work is tracked in
[`../amux/tasks/01_codex_cli_provider`](../../../amux/tasks/01_codex_cli_provider/).
Its lifecycle spike is the first task across both epics. Production work must not
invent a competing provider or artifact contract before that spike records its
decisions.
