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
4. **Pinned amux revision.** This epic builds against amux
   `11a8426a014e8b9ca30134758e66e3912628b647` on branch `feat/epic-10-amux-extensions` (sibling epic 01
   complete, suite 398 passed / 0 failed). Tasks 20-02 onward must verify
   against this revision; task 20-05 records it in the installer/docs.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|---|---|---|---|
| 20-01 | [Provider-neutral handle and event reducer](./20-01-handle-event-reducer.md) | done | amux 01-01 | Schema migration, fixtures, pure Codex JSONL reducer; no launch changes |
| 20-02 | [Provider-aware launcher](./20-02-provider-aware-launcher.md) | done | 20-01, amux 01-03 | `--provider codex`, YOLO delegation, artifacts, multiline prompt |
| 20-03 | [Codex reads and supervision](./20-03-codex-reads-supervision.md) | done | 20-01, 20-02, amux 01-04 | `status`, `last`, `wait`, failure and stuck behavior |
| 20-04 | [Resume and Codex model/profile ergonomics](./20-04-resume-profiles.md) | done | 20-02, 20-03, amux 01-04 | Resume lock/attempts; no hardcoded model |
| 20-05 | [Installer, integration tests, and docs](./20-05-integration-docs.md) | done | 20-03, 20-04, amux 01-05 | Packaging, regression suite, operator docs |
| 20-06 | [Live cross-project verification](./20-06-live-verification_human.md) | blocked | 20-05 | Real Claude→Codex workers; disposable YOLO/network/Docker checks |

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

## Log

- **2026-08-21 — amux 01-01 done** (amux `819807e`). Lifecycle contract locked
  against codex-cli `0.149.0`; 17 redacted fixtures committed. Three findings
  changed this epic's contract and are recorded in `architecture.md` §5: exit
  code is not a success signal, `codex exec resume` rejects `-C/--cd`,
  `-p/--profile` and `-s/--sandbox`, and a non-UUID resume id silently starts a
  new thread and exits `0` (so 20-04 must validate the thread ID *and* compare
  `thread.started.thread_id` against the requested one — Codex will not fail
  closed for us).
- **2026-08-21 — 20-01 done.** Handle schema 14 -> 20 fields with
  `transcript_path` retained; legacy no-provider handles resolve to `claude`.
  New pure-stdlib `.claude/hooks/codex_event_reducer.py` implements the
  completion-evidence-beats-exit-code rule. Codex fixtures vendored into
  `tests/fixtures/codex/` from amux `819807e` (see `SOURCE.txt`); refresh them
  from that path if the amux fixtures change.
- **Test baseline:** this repo's suite is now **762 passed / 0 failed / 1
  pre-existing skip** (was 716 before 20-01). The sibling amux suite baseline is
  **184 passed / 1 failed**, that failure pre-existing and unrelated.
- **Not yet live:** 20-01 changed no runtime behavior, so
  `install-claude-config.sh` has NOT been re-run. It must be re-run before 20-06
  live verification.
- **2026-08-23 — 20-02 done.** `amux-spawn spawn --provider codex` launches a
  bounded Codex worker through amux's public CLI at the pinned revision; no
  Codex argv is constructed in this repo and YOLO translation is delegated to
  amux (grep-audited by review). Collision-safe mode-0600 artifacts, handle
  created under the existing spawn lock, forced-failure rollback verified clean.
  Reviewed PASS (0 blocker/high; 2 LOW report-number corrections applied
  in place). Suite **805 passed / 0 failed / 1 pre-existing skip**.
  - Note for 20-04: the space form `--model X` is consumed by the CLI's
    optional `suffix` positional — pre-existing epic-10 behavior on the Claude
    path, preserved for byte-for-byte compatibility. `--model=X` round-trips
    verbatim for both providers. Task 20-04 (model/profile ergonomics) owns the
    decision whether to document or fix the space form.
  - The 20-02 implementer run was interrupted by a process restart and resumed;
    review included an explicit restart-seam audit (no duplicated/orphaned code
    found).
- **2026-08-23 — 20-03 done.** `status`/`last`/`ls`/`wait`/`rm` are
  provider-dispatched; Codex derivation follows architecture §5 exactly
  (completion evidence authoritative regardless of exit code; absent `.rc` ->
  terminated; bounded artifact-based orphan grace window, no sleep on the read
  path). Suite **847 ran / 0 failed / 1 pre-existing skip** — which now includes
  `test_unit_amux_codex_reads` (41 tests) and the previously unregistered
  `test_logging_verification` (9 tests), both added to `run_all_tests.py`.
  - **Incident (recorded honestly):** the first review's mutation testing
    reverted its edits with `git checkout`, destroying the entire uncommitted
    CLI implementation. Detection: the fixer's post-edit runner count DROPPED
    (838 -> 806) — an unexplained count decrease is a data-loss alarm, not
    flakiness. The implementer restored the code from its own context; a fresh
    full review then PASSED (0 blocker/high) with mutation spot-checks re-run
    under a cp-backup protocol and a final git-status gate. See memory
    `reviewer-mutation-git-checkout-destroys-work`.
  - Final review's 2 LOW + 1 INFO findings were all dispositioned by direct
    inspection without code changes: the `except OSError` widening was rejected
    (dead breadth on a correct fail-open idiom), the "duplicated" constant is
    already documented by its adjacent comment, and the "missing" `_age_s`
    docstring exists.
  - `test_unit_amux_spawn.py`'s codex refusal test updated to reflect that
    `--wait`/`--notify` are now supported for codex (`--profile` refusal
    retained) — tracks the real behavior added here, confirmed by review.
- **2026-08-23 — 20-04 done.** `amux-spawn resume <name> -- "prompt"` launches
  a second bounded turn on the same Codex thread: lock-serialized §7
  coordination (gates -> segment -> amux resume argv -> atomic attempt update
  only after launch success), thread-ID authority from amux meta.json with
  fail-closed missing/malformed/mismatch errors, amux's rc-66 quarantine
  surfaced as `thread_mismatch` without clobbering prior result or evidence.
  Attempt counter gates in-flight reads (no stale idle while attempt 2 runs);
  `last` returns the newest successful result. Reviewed PASS with zero findings
  (first clean-sheet review in the epic). Suite **889 ran / 0 failed / 1
  pre-existing skip**; 42 resume tests in `test_unit_amux_resume.py`,
  registered in the runner.
  - **`--model` space-form decision (closed):** `--model=X` / `--profile=X` are
    documented as the unambiguous spawn forms in the CLI epilogs; the parser is
    unchanged and the Claude path byte-identical. Resume accepts both forms
    naturally (no suffix positional). `--profile` on resume errors with
    guidance because `codex exec resume` rejects `-p`.
  - No Codex model is injected when the caller omits one; explicit choices pass
    through to amux's contract on both spawn and resume.
- **2026-08-23 — 20-05 done.** Packaging, regression, and docs shipped:
  - **Installer:** the epic's new modules were already allowlisted
    (`codex_event_reducer.py` joined REQUIRED_HOOKS in 20-01); a full audit
    confirmed the CLI's every local import (`amux_spawn_lib`,
    `codex_event_reducer`, lazy `permission_state_store`) ships, and the only
    unshipped hooks file is the legacy stray `test_task03.py` (imported by
    nothing). Diagnostics now import the reducer from the deployed location
    (Step-3 sanity + the final hook test) and probe the PATH-resolved `amux`
    for the Codex provider surface (warn-only: Claude spawning must not fail
    on it). `install-claude-config.sh` was RUN for real and verified: the
    deployed `~/.local/bin/amux-spawn` completed a full fake-amux worker
    cycle (spawn → idle → last → rm) importing both modules from
    `~/.claude/hooks/`. **Finding:** the installed `/usr/local/bin/amux`
    predates the pin (no `__codex-run`); the installer's probe now reports
    this as STALE. Run `./install-amux.sh` (pin updated to `11a8426…`, Codex
    fork markers added) before 20-06 live verification.
  - **Drift check:** `tests/test_amux_pin.py` verifies pin containment
    (`git merge-base --is-ancestor`) against the sibling checkout when it
    exists, skips cleanly when absent, fails with checkout/merge-or-move-the-
    pin instructions on drift, and pins the revision consistently across
    tests, `install-amux.sh`, the operator doc, and Phase 0 above.
  - **Integration:** `tests/test_integration_codex_cli.py` (13 tests) runs
    the REAL CLI end to end through PATH-shimmed fake `amux` + fake `codex`
    (fixture-shaped JSONL, wrapper artifacts) and a state-file fake tmux:
    launch, JSONL flow, result capture, failure, timeout, resume (incl. the
    rc-66 thread-mismatch quarantine), cleanup, argv boundaries, --yolo
    translation. `tests/test_integration_codex_tmux.py` runs one worker in a
    REAL tmux pane on a private `-L` socket with a stubbed provider,
    proving the pane/argv path and evidence-over-liveness end to end.
    No credentials anywhere; teardown asserts zero live sessions/pids.
  - **Docs:** `docs/amux-spawn-codex-workers.md` — operator examples for
    detached spawn, wait, status/last, resume, model (`--model=X` form), rm;
    YOLO trusted-host implications stated plainly; Codex workers have NO
    Telegram permission prompts by design (blocked/HALT result; the
    foreground Claude workflow owns human interaction) — never presented as a
    prerequisite.
  - Suite **908 ran / 0 failed / 1 pre-existing skip** (was 889); amux suite
    at HEAD `db7e29d` (contains the pin): **398 passed / 0 failed**.
- **2026-08-23 — 20-05 done. All engineering tasks complete.** Installer
  allowlists and diagnostics ship `codex_event_reducer.py` and the
  Codex-capable launcher; `install-claude-config.sh` was run for real and the
  DEPLOYED copies verified importable, with a full fake-environment worker
  cycle (spawn -> idle -> last -> rm) executed from the installed paths.
  Drift check: ancestor-containment against the `11a8426` pin with
  skip-when-absent and an actionable fix-or-move-the-pin message; the pin is
  recorded in the tests, `install-amux.sh`, and
  `docs/amux-spawn-codex-workers.md`. 19 integration tests (fake amux + fake
  codex through PATH shims driving the real CLI, plus one private-tmux test).
  YOLO docs state the full host filesystem/network/Docker access plainly and
  present the absence of Telegram prompts as an explicit design decision.
  Reviewed PASS (3 LOW doc/comment fixes applied in place). Final verification:
  this repo **908 ran / 0 failed / 1 pre-existing skip**; sibling amux at the
  pinned lineage **398 passed / 0 failed**.
  - **OPERATOR ACTION REQUIRED before 20-06:** the installed
    `/usr/local/bin/amux` is STALE (dated 2026-06-22, pre-Codex, lacks the
    `__codex-run` marker). Live verification must first refresh it:
    `./install-amux.sh` (pin `11a8426…`, branch `feat/epic-10-amux-extensions`;
    requires sudo, writes to `/usr/local/bin`). The installer's stale-detection
    diagnostic now emits this guidance itself.
- **2026-08-23 — 20-06 blocked, awaiting human evidence.** All engineering
  tasks (amux 01-01..01-05, claude-hooks 20-01..20-05) are complete, reviewed,
  and committed. The live gate requires the operator:
  1. refresh the stale pre-Codex `/usr/local/bin/amux` via `./install-amux.sh`
     (pin `11a8426…`; requires sudo, writes to `/usr/local/bin`), and
  2. run the seven-step live procedure in
     [20-06-live-verification_human.md](./20-06-live-verification_human.md),
     recording the sign-off evidence listed there.
  Mark `done` only when that evidence is recorded.
