# Epic 20 — Task 20-06 live verification sign-off evidence

**Date:** 2026-08-23 · **Operator:** Anton (authorized the agent-driven run)
**Executed by:** the implementation-manager Claude session, per the operator's
explicit instruction ("Run all 7 steps"), under the task file's disposable-root
safety rails.

## Installed chain

| Component | Location | Revision |
|---|---|---|
| amux-spawn | `~/.local/bin/amux-spawn` | repo `fb7e189` (main), deployed by `install-claude-config.sh` |
| amux | `/usr/local/bin/amux` | `db7e29d` on `feat/epic-10-amux-extensions` — 4 commits past pin `11a8426`, all docs + one 3-line test; **zero production-code delta** vs the pin (verified by `git diff --stat 11a8426..db7e29d`) |
| codex | `codex-cli 0.149.0` | `~/.nvm/.../bin/codex` |

`install-amux.sh` probes passwordless sudo with `sudo -n true` (line 404);
this host has NOPASSWD configured, hence no prompt.

## Procedure results

Disposal root: `mktemp -d /tmp/epic20-verify.XXXXXX` (removed after the run).

**1. Reviewer-style worker (patterned after Flightclaim's code-review tasks).**
Fixture project with `AGENTS.md` requirements and a seeded defect in
`src/pricing.js` (`total()` subtracts the 8% tax instead of adding it).
`spawn epic20v-rev --provider codex --wait` (no YOLO) reached idle in 75 s,
exit 0. `last` returned:

> `src/pricing.js:6` calculates `price - price * 0.08`, producing 92% of the
> price. … VERDICT: `total()` subtracts 8% tax instead of adding it.

The defect was identified precisely. Events ended `turn.completed`.

**2. Implementer-style worker (Hyppie Flow file/verdict contract).**
Fixture with `CLAUDE.md` requiring a report at
`agents_output/T1_01_implementer.md` ending in `## Verdict`, final message =
verdict line only. Spawned with `--yolo --dir <fixture>` (explicit `--dir`;
see finding F2). 43 s to idle. Verified: `utils/slugify.js` passes the spec
case — `slugify('  Hello_World!! 2026 ')` → `hello-world-2026`; the report
ends with `## Verdict` / `Verdict: done: …`; `last` returns only the verdict
line. Contract honored exactly.

**3. Read/resume surface.** `status` → `idle (idle/active …)`. `ls --json`
reports `provider: codex`, `state`, `attempt` (lookup is workspace-scoped —
F3). `resume … --wait` printed `attempt 2` on thread
`01a02fe4-9eb7-7ce0-a4a3-85c20145930a`; the events artifact contains **two
`thread.started` entries with the identical thread id** and two
`turn.completed`s; the result artifact holds the context-retaining answer
("I implemented `slugify(s)`, and … returns `'hello-world-2026'`").
Thread continuity and newest-successful result semantics confirmed. `rm`
removed the idle worker cleanly.

**4. YOLO host filesystem.** `--yolo` worker in `empty-ws/` created and read
back a marker at `<root>/host-root/marker-epic20.txt` — **outside its
workspace, inside the temp root**. Host-side check: file present, content
`epic20-verify-marker`. 22 s to idle.

**5. YOLO network + Docker.** Same pattern; result artifact:

> HTTP_STATUS=200
> DOCKER_OUT=epic20-docker-ok

(`curl https://example.com`; `docker run --rm --name epic20v-verify-c1
alpine:3 sh -c 'echo epic20-docker-ok'`.) No sandbox or approval block.
Post-run `docker ps -a --filter name=epic20v` → empty. The `alpine:3` image
cache entry (8.4 MB) was left in place (may pre-date the test; remove with
`docker rmi alpine:3` if desired).

**6. Deliberate failure and timeout.**
- Bogus model (`--model=epic20-no-such-model`): `status` → `terminated
  (nonzero_exit: exit 1)`; JSON `failure.reason = "nonzero_exit"`,
  `exit_code = 1`. §5-consistent.
- `--wait --timeout 20s` on a `sleep 90` worker: stdout `AMUX_WAIT_TIMEOUT`,
  **exit 3**; status honestly `running`; wall time ~30 s = 20 s timeout + the
  10 s orphan-grace artifact re-check (expected §5 behavior). `rm` refused to
  reap a live session without `--force`; `rm --force` killed (via tmux
  kill-session) and removed.

**7. Default-provider Claude worker.** `spawn epic20v-claude` with **no**
`--provider` (and no YOLO), `--wait` printed `EPIC20-CLAUDE-OK`, exit 0, 22 s.
Handle shows a Claude session UUID. Provider-default path unregressed.

## Cleanup

All workers `rm`'d (idle/terminated removed; the running one `--force`'d).
Residue counts after the run: tmux sessions matching `epic20v` — 0; spawn
handles — 0; Docker containers — 0. Temp root removed. The claude-hooks
working tree was left clean (one misplaced first attempt of step 2 wrote
`utils/slugify.js` + `agents_output/T1_01_implementer.md` into the repo
before `--dir` was used; both were untracked worker output and were deleted
immediately — see F2).

## Findings recorded as follow-ups (none block sign-off)

- **F1 — `spawn --wait` prints a stale final message for Codex workers.**
  Steps 1–3: it printed nothing (attempt 1) or the *previous* attempt's
  message (resume), while `last` and the result artifact were always correct.
  The Claude path prints correctly (step 7), which localizes the defect to
  the Codex read-back (handle `last_message` vs result artifact). Exit codes
  are unaffected.
- **F2 — without `--dir`, the Codex worker's cwd is the amux workspace root**
  (git toplevel), not the invoking shell's cwd. The step-2 first attempt
  located the fixture spec by searching and then wrote its outputs relative
  to its own cwd — into the claude-hooks repo. `--dir` is the correct usage;
  the operator docs should state this more loudly.
- **F3 — `status`/`last`/`ls` lookups are workspace-scoped by cwd.** Run from
  a different directory they report "no tracked handle". Working as designed;
  worth one line in the docs.

## Success-criteria checklist (BRD §6)

- [x] A Claude session spawned a Codex worker with YOLO host access through
      `amux-spawn` (steps 4–5; this very session is the Claude orchestrator).
- [x] `status`, `last`, `wait`, `resume`, and `rm` all exercised (steps 1–6).
- [x] Success distinguishable from failure and timeout without terminal-text
      heuristics (idle vs `nonzero_exit` vs `AMUX_WAIT_TIMEOUT`/exit 3).
- [x] Codex read/wrote a disposable workspace, accessed the network, and ran
      a disposable Docker command (steps 2, 4, 5).
- [x] Provider and Codex thread ID survive restart/resume (step 3: same
      thread id across segments, context retained).
- [x] Existing Claude tests green (908/0/1) and a real Claude spawn works
      (step 7).
- [x] Manual verification bounded-task patterns after both example projects,
      with no workflow files edited (fixtures patterned after Flightclaim's
      review tasks and Hyppie Flow's `docs/workflow.md` verdict contract;
      neither repo touched).

## Postscript — F1 root cause corrected and fixed (2026-08-23, post-rebase)

The F1 note above attributed the stale print to the handle `last_message`
field. Investigation during the fix disproved that hypothesis: the wait path
already read the result artifact. The confirmed root cause was two defects:

1. the reducer resolved turn outcome last-wins across appended resume
   segments, so a mid-resume worker falsely read idle via attempt-1's
   `turn.completed` (the stale print after resume), and
2. codex writes the result file after flushing `turn.completed`, so an
   immediate read printed empty content (the empty first print) — gated now
   by a `.rc`-finality conditional re-check (not a fixed delay).

Both surfaces route through one `_final_message` helper reusing the `last`
derivation; Claude output byte-pinned; exit codes and `AMUX_WAIT_TIMEOUT`
untouched. Reviewed PASS with zero findings; 7 new tests; suite 923 / 0 / 1.
F1 is CLOSED. (F2/F3 remain documentation follow-ups.)
