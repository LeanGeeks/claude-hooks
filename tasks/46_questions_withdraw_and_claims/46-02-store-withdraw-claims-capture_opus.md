# 46-02 — Store: withdraw, late answers, claims ledger, lock, capture

**Status:** done · **Depends on:** — (independent root)
**Read first:** [brd.md](./brd.md) D2, D4, D5, D11, D14 · [architecture.md](./architecture.md) §3 ·
`tasks/23_async_questions/architecture.md` §3.2–§3.5 · `tasks/23_async_questions/state.md`
invariants 5, 6, 11 · `.claude/hooks/questions_store.py`

## Goal

Give the queue-file engine the vocabulary the rest of the epic needs, without
breaking the property that makes it trustworthy: it reads and writes files and
does nothing else (no subprocess, no network).

## Scope

1. **Withdrawn word** — `[questions.format] withdrawn` (default `"withdrawn"`;
   one word, not the open word), always in `status_set` (architecture §3.1).
2. **`withdraw()`** — the outcome table of architecture §3.2, returning a
   `WithdrawResult` that carries the title, the file and the relay message id
   read from the entry's `**Dispatched:**` line. The reason goes through
   `escape_answer_text`. The `<!-- withdrawn -->` marker is recognised only at
   line start, only inside the entry's own range.
3. **`apply_answer` flips only from open** (architecture §3.3): any other state
   inserts the block with a `**Late answer**` line and returns
   `ApplyResult.LATE`. Idempotency on the answer marker is unchanged and still
   checked first.
4. **Claims ledger** — `claim()`, `claimed_ids()`, `_max_number` reads the
   ledger and `id_floor`; `create_entry(qid=…)` accepts only a claimed, unused
   id; the ledger is never part of the queue set (architecture §3.4).
5. **Lock** — in the git common dir when there is one; dual locking while a
   legacy `<dir>/.lock` exists; re-entrant per instance and thread
   (architecture §3.5).
6. **Capture** — `capture()` / `Capture.seed()` / `materialize()`; every read
   through `_read_lines`, every write through `_write` (architecture §3.6).
7. **Config** — `sync`, `claims`, `[questions.git]` parsed and validated here
   (the store owns `[questions]`), but nothing in this module acts on `sync`.

## Tests

`tests/test_unit_questions_store.py`: withdraw outcomes including CRLF and a
hostile reason; the race in both orders; a marker in a neighbouring entry;
claims (list items only, bounded count, explicit ids, wide glob); the lock
location in a repository, in a linked worktree and outside git; dual locking
proved with a competing `flock`; re-entrancy; capture (nothing on disk, seeded
content, every mutator, no nesting); config defaults and bad values.

**One existing test changes on purpose:**
`test_declared_vocabulary_flips_to_the_second_token` started from `[answered]`
and expected `[resolved]`. Under D5 that is no longer the behaviour; it now
starts from `[open]`, and a new test pins that `[answered]` and
`[SUPERSEDED by Q-269]` are left alone.

## Done when

- The module still imports nothing that can start a process.
- No answer text and no withdrawal reason can change how a file parses.
- A store built on a workspace with no new keys behaves as before, except for
  the D5 rule and the lock location.
