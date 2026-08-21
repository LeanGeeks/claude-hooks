# Epic 20 — Architecture: provider-neutral tracked workers

Companion to [brd.md](./brd.md). This document owns the consumer side of the
contract; the process-launch side is owned by the
[amux epic](../../../amux/tasks/01_codex_cli_provider/architecture.md).

## 1. Ownership boundary

```text
Claude orchestrator / human
          |
          v
amux-spawn                         (this repository)
  provider selection, naming, cap, tracked handle, wait/read/resume UX
          |
          v
amux CLI                           (../amux)
  persist provider/mode, construct provider argv, tmux process, thread identity
          |
          v
claude TUI + hooks   OR   codex exec + JSONL events/final-message artifact
```

`amux-spawn` must not reconstruct Codex's shell command behind amux's back.
amux must not absorb workflow concepts such as run IDs, stuck thresholds, or
orchestrator output formatting.

## 2. Launch modes

| Provider | Tracked background mode | Lifecycle source |
|---|---|---|
| Claude | existing interactive Claude process seeded with a prompt | Claude hooks + transcript mtime |
| Codex | bounded `codex exec`, seeded with a prompt | JSONL event artifact + process exit + final-message artifact |

The sibling spike decides exact amux option names and redirection mechanics.
Required semantics are stable: provider and run mode are persisted, argv
boundaries are preserved, and Codex starts in YOLO mode only when explicitly
requested.

## 3. Handle migration

The current handle is a strict schema. Task 20-01 extends it rather than creating
a parallel Codex registry. New conceptual fields are:

```jsonc
{
  "provider": "claude|codex",       // absent in legacy handles => claude
  "session_id": "...",             // Claude UUID or captured Codex thread ID
  "activity_path": "/abs/path",     // transcript for Claude, event JSONL for Codex
  "result_path": "/abs/path|null",  // Codex final-message artifact; optional for Claude
  "process_pid": 1234,               // best effort; nullable
  "exit_code": 0,                    // nullable until process completion
  "failure": null                    // normalized summary, not raw terminal scrape
}
```

Existing fields such as `transcript_path` remain during migration so installed
Claude hooks and old readers do not break. The task must update
`HANDLE_FIELDS`, constructors, readers, installer fixtures, and all strict-schema
tests together.

Artifact paths are under the existing `~/.amux/spawn/` registry, use the session
name as the stable key, are mode `0600`, and are written atomically where
applicable. JSONL itself is append-only. Resume creates a numbered segment or
appends an explicit attempt boundary; it never truncates the previous run.

## 4. Codex event reducer

The reducer consumes documented `codex exec --json` events. The spike records
the exact installed-version fixtures, but the minimum recognized facts are:

- thread/session started and its ID;
- turn started;
- turn completed;
- turn failed;
- final assistant message artifact;
- process exit code and event-file mtime.

Unknown event types are ignored and retained in the raw artifact. Malformed or
partially written final lines are tolerated while the process is live. No reader
imports or parses Codex's internal transcript file.

## 5. State derivation

State remains derived at read time.

```text
turn.completed present, no turn.failed       -> idle  (completion evidence is authoritative;
                                                        exit code is not required and not consulted)
turn.failed present                          -> terminated  (regardless of exit code)
process/tmux alive, activity too old          -> stuck
process/tmux alive                            -> running
launch begun, no authoritative event yet      -> spawning/running
process gone, no turn.completed, no turn.failed -> terminated  (exit code carried as reason context)
```

For Codex, a dead tmux session is not automatically failure: bounded execution
is expected to exit. Completion evidence takes precedence over liveness.

**Priority rule (01-01 evidence, codex-cli 0.149.0):** Completion evidence is
authoritative. A valid `turn.completed` event maps to `idle` regardless of exit
code — including the SIGKILL-orphan edge case where exit 137 and `turn.completed`
are both present (the orphan child completed the turn after the parent was killed).
An explicit `turn.failed` event maps to `terminated` regardless of exit code.
Exit code is consulted only when there is NO completion evidence and no explicit
failure event: process gone with neither maps to `terminated`, with the exit code
carried as reason context. No implementer should treat exit 0 as a necessary
condition for `idle`.

Evidence notes from the amux 01-01 lifecycle spike (codex-cli 0.149.0; fixtures
in `../../../amux/tests/fixtures/codex/`):

- Exit code `0` alone is **not** success. A SIGTERMed `codex exec` exits `0`
  having emitted only `thread.started` and `turn.started`. Likewise the
  `--output-last-message` file can exist with no `turn.completed`. The reducer
  must require a `turn.completed` line; the exit code is not consulted for this
  determination.
- The exit code arrives as a small sibling `.rc` file written by amux's launch
  wrapper. An **absent** `.rc` file means the wrapper never ran to completion
  (the session was killed) and must reduce to `terminated`, not to "still
  running".
- `codex exec` forks a child process that survives a bare `kill -9` on the
  parent: events and the result file can still appear up to ~10 s **after** the
  tmux session is gone. So "process gone, no completion evidence" must be
  re-checked against the artifact rather than latched on first observation, and
  worker termination must go through `tmux kill-session`, never `kill -9`.
- Event lines are flushed as they occur even when stdout is a regular file, so
  mtime is a sound activity signal — but a trivial turn is legitimately silent
  for several seconds and a tool-using turn for far longer, so the `stuck`
  threshold belongs in minutes.
- `item.completed` ids restart at `item_0` in every process, so they are unique
  per resume segment only, never across segments.
- A non-UUID thread id makes Codex start a brand-new thread and exit `0`. §4.3's
  fail-closed rule must be enforced by validating the stored id as a UUID *and*
  by comparing the new segment's `thread.started.thread_id` with the requested
  one.

Activity is the event artifact's mtime, falling back to handle `created_at`.
`stuck` is never persisted. A successful `idle` handle can later be resumed;
the next attempt temporarily returns to `spawning`/`running`.

## 6. Result and wait contract

- `last` reads the final-message artifact, bounded by a documented size limit.
- `wait` uses the same reducer as `status`; it does not tail terminal output.
- Existing exit codes stay: `0` success, `1` launch/runtime failure, `3` caller
  timeout.
- JSON output includes at least provider, state, session/thread ID, attempt,
  exit code, result path, and normalized failure reason.

## 7. Resume

Resume is a coordinated operation:

1. acquire the normal spawn/session lock;
2. require `provider=codex`, an idle/terminated process, and a valid thread ID;
3. rotate or start the next event artifact segment;
4. ask amux to launch `codex exec resume <thread-id> <prompt>`;
5. update the handle attempt atomically only after launch succeeds.

Concurrent resumes of the same handle are rejected. A failed resume preserves
the previous result and evidence.

## 8. YOLO and environment

`--provider codex --yolo` delegates to amux's provider-specific mapping and must
result in `--dangerously-bypass-approvals-and-sandbox`. `amux-spawn` must never
forward Claude's `--dangerously-skip-permissions` to Codex.

No additional network, filesystem, or Docker policy is introduced. Live
verification uses only disposable files/containers and a harmless network
request, despite the worker having full host access.

## 9. Compatibility gates

- Legacy handle fixture with no `provider` behaves exactly as Claude.
- Claude producer hooks ignore Codex handles.
- Codex reducers do not import Telegram or permission modules.
- Removing a handle reaps its provider artifacts but never broad-globs outside
  the resolved session prefix.
- Installers deploy every new module and executable used by the launcher.
