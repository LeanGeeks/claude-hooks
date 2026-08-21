# Epic 20 — State & orchestration

Read [brd.md](./brd.md), then [architecture.md](./architecture.md). The first
cross-repository task is the amux lifecycle spike; it closes the remaining CLI
and event-shape uncertainties before production implementation.

## Phase 0 — locked decisions (2026-08-21)

1. **Cross-repository ownership.** The claude-hooks implementation manager also
   drives the sibling amux epic
   [`01_codex_cli_provider`](../../../amux/tasks/01_codex_cli_provider/state.md).
   amux tasks are executed in the amux repository under the same
   implementer/reviewer/fixer/committer sequence; their reports live in that
   repository's `agents_output/`.
2. **amux branch base.** amux Epic 01 stays on the existing
   `feat/epic-10-amux-extensions` branch (tip `9b05d10` at decision time). No new
   epic branch is cut, and this epic pins that branch rather than `main`.
3. **Codex toolchain.** Verification uses the locally installed `codex-cli`
   `0.149.0` with the existing `~/.codex/auth.json` credentials.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|---|---|---|---|
| 20-01 | [Provider-neutral handle and event reducer](./20-01-handle-event-reducer.md) | todo | amux 01-01 | Schema migration, fixtures, pure Codex JSONL reducer; no launch changes |
| 20-02 | [Provider-aware launcher](./20-02-provider-aware-launcher.md) | todo | 20-01, amux 01-03 | `--provider codex`, YOLO delegation, artifacts, multiline prompt |
| 20-03 | [Codex reads and supervision](./20-03-codex-reads-supervision.md) | todo | 20-01, 20-02, amux 01-04 | `status`, `last`, `wait`, failure and stuck behavior |
| 20-04 | [Resume and Codex model/profile ergonomics](./20-04-resume-profiles.md) | todo | 20-02, 20-03, amux 01-04 | Resume lock/attempts; no hardcoded model |
| 20-05 | [Installer, integration tests, and docs](./20-05-integration-docs.md) | todo | 20-03, 20-04, amux 01-05 | Packaging, regression suite, operator docs |
| 20-06 | [Live cross-project verification](./20-06-live-verification_human.md) | todo | 20-05 | Real Claude→Codex workers; disposable YOLO/network/Docker checks |

`amux NN-NN` refers to tasks in
[`../../../amux/tasks/01_codex_cli_provider/state.md`](../../../amux/tasks/01_codex_cli_provider/state.md).

## Dependency graph

```text
amux 01-01 ─────► 20-01
     │              │
     └─► 01-02 ─► 01-03 ─► 01-04 ─► 01-05
                         │      │        │
20-01 ───────────────────┴─► 20-02       │
20-01 + 20-02 + 01-04 ─────► 20-03       │
20-02 + 20-03 + 01-04 ─────► 20-04       │
20-03 + 20-04 + 01-05 ─────► 20-05 ─► 20-06
```

## Recommended execution

1. Run amux 01-01 first and commit its fixtures/decisions without production
   changes.
2. Implement amux 01-02 and this epic's 20-01 in parallel.
3. Finish amux launch/lifecycle tasks, then 20-02 through 20-04.
4. Package and regression-test in 20-05.
5. Perform 20-06 manually. It may use prompts patterned after the two reviewed
   example projects, but it must not edit their workflow definitions.

## Cross-task invariants

1. `--provider` defaults to `claude` everywhere.
2. Legacy handles without `provider` are Claude handles.
3. amux is the only component that constructs provider executable argv.
4. This repository is the only component that derives tracked-worker status and
   orchestrator-facing output.
5. Codex lifecycle comes from `codex exec --json`, process exit, and the explicit
   final-message artifact—not terminal scraping or internal transcripts.
6. A completed bounded Codex process maps to `idle`; process death without valid
   completion maps to `terminated`.
7. YOLO Codex workers use the official bypass flag and receive full host access.
8. No task adds Telegram permission handling or edits example-project workflows.

## Verification baseline

Every engineering task runs its focused tests plus the existing amux-spawn,
producer, reads, supervise, and remove suites. The final engineering task runs
the full repository suite and the sibling amux suite at the pinned revision.
