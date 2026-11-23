# Review 50-13 (F-1..F-4)

## Verdict: CLEAN (no HIGH or MEDIUM findings; two LOW notes)

Compile/import OK; diff read line by line against the brief; no mutation testing done (the implementer's in-process
mutation check was not repeated; `git diff --stat` unchanged by me).

## Verification by item

- F-1: `BoundedWorld.git` (epic_driver_lib.py ~7594-7610) sets `expired` only for a refused call (<1 s left) or a
  clamped call with rc None that says "timed out" (RealWorld text "git timed out after ...", line 709) or ends with
  <1 s left. A fast clamped call, including a real git failure, is a normal result. Users audited: ledger stage in
  `tick_pass` (the bug), `prepare_worker_steps` (same bug, fixed by the same change), and the `assume_git=True` reload
  (never clamps). Tests cover fast clamped, clamped timeout, refused, base refs verified in a normal pass, and no
  "repos and base refs not checked" note.
- F-2: `approval_answer_unsettled` (~3101) covers both windows. Window 1: `answer_pending` set in
  `_reconcile_questions`, cleared in `_commit_event`; reconcile is skipped on a partial pass, and no save happens
  between reconcile and the dispatch commit, so the flag cannot outlive its pass. Window 2: an `answer` outbox entry
  carrying the approval that is not `_entry_complete`. It is not an indefinite mute: an entry that never settles is
  covered by `delivery_unconfirmed`/`verdict_missing`/`handoff_timeout`; a withdrawn question sets no flag; once settled
  without a grant the plain `approval` hold returns and the alarm fires. No question: unchanged.
- F-3: I read Claude Code 2.1.330's own trust code in the binary (read only: `Iy`/`jpn`/`My`). Confirmed: for a linked
  worktree whose main repo qualifies (`rootOnly`) only the main repo root's key counts; otherwise the walk goes from the
  folder up to its git root and stops; outside git it walks all parents. The implementer's `trusted_by` mirrors this.
  Absent/unreadable/non-object config refuses with the fix; `enable` only reads (`RealWorld.claude_config`);
  refusal is `Refused` -> exit 3.
- F-4: the bundle's banner code (`HBr`/`Ebn`) matches: first-party login, refresh token present, plausible
  `refreshTokenExpiresAt` (ms), ignored when `expiresAt > refresh+3d`, banner at 0 < left <= 3 d with ceil days.
  `login_expiry_from_credentials` mirrors it (bool, NaN, <1e12, strings, overflow all return None; seconds-valued
  input is treated as unknown rather than misread). Credential key names checked (`claudeAiOauth.refreshTokenExpiresAt`
  exists); no secret values in code, fixtures ("synthetic-*"), docs or report. Thresholds: enable refuses <= N days
  (default 7, `--login-days`, 0 off, expired always refuses); tick `login_expiring` at <= 3 d, then `login_expired`;
  both clear on re-login through the normal engine (test present). Rows added to architecture §16.2 and docs §9.
- Invariants: state saved before effects unchanged; alarm ids go through the existing engine (ack/repeat); the tick adds
  only a file read (no LLM); no runtime file in a tracked tree. Docs §1/§2/§4/§9, the 50-12 runbook notes and the
  architecture amendments are present and consistent with the code (gate renumbering 4..11 -> 6..13 consistent).

## Findings

1. LOW - epic_driver_lib.py `trust_folders` (~9150): the `worktree_root` outside the state root is always required when
   any worker step exists. Claude Code needs it only for multi-repo wave directories (non-git); for single-repo workers
   the repo's trust suffices. Scenario: single-repo ledger with a custom `worktree_root` is refused for an
   unnecessary folder. Fix (optional): require it only when a worker has more than one repo; the fix text already
   lets the operator trust it, so it is a one-line extra step, not a blocker.
2. LOW - `trusted_by` tries both `abspath` and `realpath` and passes if either is trusted. Claude Code resolves one
   canonical form; if a trust key exists only for a symlinked spelling, `enable` could pass while Claude still asks.
   Also the worktree's own key is accepted by the walk where Claude (rootOnly case) checks only the main repo root.
   Both are over-lenient in rare setups; the failure mode is a startup_dialog alarm at the first spawn, not data loss.
   Fix: prefer realpath only.

## Extra worker-repo trust gate: sound

The evidence is the 2.1.330 binary's own lookup (worktree takes its main repo's trust; parent trust does not reach into
a git repo) plus the implementer's probe sessions under a throwaway HOME. The brief's "path or any parent" rule would
let `enable` pass for exactly the case 50-12 check 9 failed on. It cannot refuse a legitimate setup: a repo that is not
trusted does stall its worker's first spawn, and the fix is a one-time trust per repo. Residual over-refusal is
finding 1 only. It also does not conflict with the operator's decision (enable still writes nothing); the lead should
tell the operator that trusting the state root alone is not enough for worker repos.

## Suite

`python3 tests/run_all_tests.py`: Ran 3101 tests in 698.410s, OK (skipped=2).
