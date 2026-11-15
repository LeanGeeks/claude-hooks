# Epic 46 — State & orchestration

Read [brd.md](./brd.md) and [architecture.md](./architecture.md) first.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 46-01 | [Relay: cancel cascade + ladder cap](./46-01-relay-cancel-cascade-and-ladder-cap_sonnet.md) | done (not deployed) | — | `relay-server/` only. No schema change. **Takes effect only when the relay is redeployed.** |
| 46-02 | [Store: withdraw, late answers, claims, lock, capture](./46-02-store-withdraw-claims-capture_opus.md) | done | — | The store stays pure. |
| 46-03 | [Git engine](./46-03-git-sync-engine_opus.md) | done | 46-02 | `questions_git.py`; real-repository tests. |
| 46-04 | [Operations, MCP tools, CLI flags](./46-04-ops-mcp-cli_sonnet.md) | done | 46-01, 46-02, 46-03 | `questions_ops.py`; `withdraw`, `claim`, `--sync`. |
| 46-05 | [Listener: late answers + sync](./46-05-listener-late-answers-and-sync_opus.md) | done | 46-02, 46-03 | |
| 46-06 | [Docs and installation](./46-06-docs-install_sonnet.md) | done | 46-04, 46-05 | |
| 46-07 | [Live verification](./46-07-live-verification_human.md) | todo | 46-06, relay deploy | **human** |

## Cross-task invariants

Epic 23's twelve bind here. These are added or amended:

1. **`questions_store.py` starts no process.** Git lives in `questions_git.py`
   and nowhere else.
2. **`sync = "off"` starts no process either**, and its tool results have the
   keys they always had. Amends epic 23 brd §8 ("no git refs"), for workspaces
   that opt in.
3. **Only queue files are pushed; the local branch never is.**
4. **The checkout is touched only through a pathspec.** No checkout, stash,
   reset, rebase, merge or pull — the same rule epic 23's state.md gives agents
   working in a shared checkout.
5. **Nothing local is written until the remote has accepted** (when a push is
   being attempted). That is what makes re-allocation free.
6. **A git failure is never an apply failure.** The file is the durable act;
   what git owes is recorded and retried, and never loses or delays an answer.
7. **An answer flips the status only from open**, and a withdrawal applies only
   to an open entry. Whichever reaches the file first decides; the other is
   recorded or refused, never dropped.
8. **Two generations of the code exclude each other** while a legacy lock file
   exists.

## How this epic was executed

One session, 2026-11-15, on a relayed request from the operator
(`agents_output/questions-setup-and-features-report.md`). Not the
implementer → reviewer → fixer loop of `docs/prompts/implementation_manager.md`
run task by task: the design, 46-02…46-06 and their tests were written by the
session that designed them, with 46-01 delegated to a sub-agent because it
touches only `relay-server/` and could run alongside. Independent review was
then done per the repo's reviewer prompt; reports are in `agents_output/46-*`.

## Log

- **2026-11-15 — design.** Two measurements decided the shape (brd §2.2, §2.3):
  the reference workspace's `main` was 26 commits ahead of its upstream, which
  rules out pushing the branch; and a scratch-repository experiment showed that
  publishing a *snapshot* of the queue file reconciles cleanly where replaying
  the tool's own edit conflicts on both `rebase` and `merge`.
- **2026-11-15 — 46-01 done** (sub-agent). Relay suite 315 → 331. Not deployed.
- **2026-11-15 — 46-02…46-06 done.** See the report for suite counts.
  - One existing store test encoded the behaviour D5 removes and was changed on
    purpose (46-02's task file names it).
  - The first run of the git tests found a real defect, not a test bug: after
    losing the push race on every attempt, the local fallback allocated above a
    *stale* remote-tracking ref and handed out the winner's id. One more fetch
    on exhaustion fixes it; the test that caught it stays.
  - The hook-suppression test was mutation-checked (removing
    `core.hooksPath=/dev/null` and `--no-verify` fails it).
- **2026-11-15 — first installation and live run** (`./install.sh --yes`; a
  throwaway workspace with its own bare remote, never a real workspace's queue).
  - Found live: `ask` to the default role sent `escalate_after_sec` with no
    `escalate_to_token`, which the relay rejects (422). Both are now sent or
    neither. The entry had been written and pushed before the refusal, exactly
    as designed, and was withdrawn from the CLI.
  - A full round trip ran with no session attached: asked, answered from
    Telegram fifteen minutes later, flipped by the systemd listener, which
    committed and pushed `chore(questions): answer Q-002 (relay #8821)`.
  - A stray test process was found writing the real listener status file: a
    listener test, started by the relay sub-agent's suite run while
    `questions_listen_lib.py` was half edited, fell into the real poll loop
    with a test config. Killed; the real index was verified untouched. The
    lesson for an orchestrator: do not let a sub-agent run the top-level suite
    while the session is mid-edit in the files that suite imports.
- **2026-11-15 — review round 1** (`agents_output/46-01_review_report.md` PASS;
  `46-02-03_review_report.md` FAIL, 2 HIGH + 7 MEDIUM; `46-04-06_review_report.md`
  FAIL, 1 HIGH + 5 MEDIUM). Fix reports beside them. What changed shape:
  - **The line merge is gone.** `git merge-file --union` produced one unclosed
    fence out of two machines' answers to the same entry, and the result parsed.
    The merge is now entry by entry and refuses what it cannot decide
    (architecture §4.3).
  - **The engine checks what it publishes**: regular files in the workspace's
    queue set only — no symlinks, no git-ignored paths, no stray glob matches.
  - **Time is bounded**, not unbounded-but-rare: per-call timeouts, a total
    budget, a cool-down after a network failure, and a tighter budget for the
    listener. The docs' "never waits for git" was wrong and is gone.
  - **Found while fixing, not by review:** recording the sync base when the
    local write had failed made the next sync push the stale local file over
    the entry just published. The base is recorded only when the local file was
    written.
- **2026-11-15 — review round 2** on the rewritten engine
  (`agents_output/46-round2_review_report.md`, FAIL: 2 HIGH, 3 MEDIUM;
  fixes in `46-round2_fix_report.md`). Both HIGHs were in the case the feature
  exists for, and neither was reachable by a test that forces *one* race:
  - **A simultaneous push is not a "non-fast-forward".** The loser is told
    `[remote rejected] (failed to update ref)`. Classified as a refusal, it went
    down the local path with the winner's id: 8 duplicated ids in 12 concurrent
    asks. The classifier now knows the wording, and — since no list of wordings
    is complete — every unsuccessful push is settled by fetching and looking.
    The test that would have caught it runs two clones from threads.
  - **The operator's pull conflicted after two machines asked in turn**, which
    brd §2.3 said could not happen (it had been measured on one machine). D15:
    a merge driver, registered in the clone's local git configuration.
  - Also: a glob match that is not a queue was being integrated; the
    `in_flight` marker was written and never read; a refused merge now names
    the ids, and the way out of a collision is documented and tested.
- **2026-11-15 — review round 3** (confirmation of the round-2 fixes;
  `agents_output/46-round3_review_report.md`, FAIL: 2 HIGH, 2 MEDIUM; fixes in
  `46-round3_fix_report.md`). The reviewer ran three machines at random and
  25 seeds in 60 failed — all for one reason, which was also behind the second
  HIGH: **the merge had no notion of an entry's later state.** A twin carries
  another machine's question as it was; when the upstream answers it, both
  sides have "a different version of an entry the base never had". The merge
  now takes the later state (architecture §4.3), bases are recorded for files
  that were only brought down, a failing merge driver falls back to git's own
  merge instead of leaving an unmarked file, and retries are jittered.
  - The reviewer's randomized run is now a test
    (`TestThreeMachinesAtRandom`: ask, answer, withdraw, claim, sync, pull by
    merge and by rebase, in a seeded random order; every pull clean, no id used
    twice, nothing lost, every checkout ends reading as the upstream does).
    8 seeds in the suite; 60 seeds run by hand, all green; all 8 fail without
    the later-state rule.
- **2026-11-15 — review round 4** (`46-round4_review_report.md`, FAIL: 1 HIGH,
  1 MEDIUM, 2 LOW; `46-round4_fix_report.md`). The engine's integration and the
  base recording held (nothing found in 80 seeds without hand edits, no
  sequence that pushes stale content). The driver did not: it took git's line
  merge first, and git returned a "clean" file with one id heading two entries
  once a hand-written entry shifted the alignment (about one seed in eight with
  hand edits). The driver now merges by entry first and takes a line merge
  only if it is sound; its exit statuses are its own (10/11), so an interpreter
  that dies with 1 falls back to git's merge. The simulation gained a `hand`
  action; 80 seeds green.
- **2026-11-15 — review round 5** (the driver only;
  `agents_output/46-round5_review_report.md`): **PASS**, 1 MEDIUM, 2 LOW. The
  round-4 HIGH is closed; the exit contract and the whole-file conflict were
  exercised and hold. The MEDIUM was a consequence of putting the entry merge
  first: a hand-deleted line made the shorter version look like an earlier
  state of the answered one, and the line came back. The later-state rule now
  also requires the earlier version to contain the base. Fixed with a unit test
  and an end-to-end merge test; the LOW temp-file leak is fixed; the residual
  trust in statuses 10/11 is accepted (the real CLI returns them only after
  `os.replace` succeeds). Not reviewed again after this fix: it narrows one
  rule, is covered by tests in both directions, and a 40-seed sweep is green.
  Not exercised by any reviewer: a renamed queue file, and the ledger through
  the driver (the ledger merge itself has unit tests).
  - Worth keeping from this epic: **every round's HIGH was in the multi-machine
    case, and none was reachable by a test with one race or one machine.** The
    first tests of an engine like this should have been the random ones.
