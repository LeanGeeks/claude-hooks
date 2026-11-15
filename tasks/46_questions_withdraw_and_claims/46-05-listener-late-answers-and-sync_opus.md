# 46-05 — Listener: late answers, and committing what it writes

**Status:** done · **Depends on:** 46-02, 46-03
**Read first:** [brd.md](./brd.md) D5, D12 · [architecture.md](./architecture.md) §6, §4.5 ·
`tasks/23_async_questions/state.md` invariants 2, 3 · `.claude/hooks/questions_listen_lib.py`

## Scope

- `_apply` calls `apply_answer` through `questions_git.run`
  (`summary="answer <id> (relay #N)"`) and returns `(error, late)`.
  `ApplyResult.LATE` is success: the watermark advances, nothing pends, and the
  finalize text tells the human their answer was kept without reopening the
  question.
- A degraded sync is **not** an apply failure. The answer is in the file; the
  debt is in the repository's sync state. It must never route to `pending`
  (invariant 2 is about answers, not commits).
- `retry_sync_pending()` — after the poll in each cycle; for each repository
  the index knows, only if its sync state says pending, at most once per
  `SYNC_RETRY_INTERVAL_S`; a debt created in this cycle is reported at once and
  first retried one interval later. Nothing it does can raise into the loop.
- `--status` lists owing repositories with the reason.

## Tests

`tests/test_unit_questions_listen.py` (late answers; the retry pass with the
engine patched) and `tests/test_unit_questions_git.py::TestListenerAndOpsWithGit`
(a real repository: an answer with no session running is committed and pushed;
an answer during a rebase is applied at once and synced after the rebase ends).

## Done when

- An answer waits for git at most a fixed, short time (32 s in the worst case,
  nothing while a repository is known to be failing), and git always catches up.
- The listener never syncs a repository that no tool marked as owing.

(Rev 2, after review: the first wording — "an answer never waits for git", "the
listener never commits a hand edit" — claimed more than the code did. An answer
did wait on the full network timeout, and a sync of an owing repository does
carry whatever is in its queue files.)
