# 23-03 — Queue-file engine (`questions_store.py`)

**Status:** todo · **Depends on:** — (independent root; the critical path)
**Read first:** [brd.md](./brd.md) §2.6, §2.7, §5, D3, D8 ·
[architecture.md](./architecture.md) §3 · [state.md](./state.md) invariants 5, 6, 7 ·
`.claude/hooks/permission_state_store.py` (the lock + atomic-replace discipline
to copy) · `.claude/hooks/roles_config.py` (config loading) ·
`agents_output/23-03_phase0_anchor_scan.md` (the settled Phase 0 findings —
read this **instead of** opening the reference workspace)

## Goal

The only module in the system that reads or writes a workspace question file.
Pure library: no relay, no MCP, no subprocess, no network. Both the MCP server
(23-04) and the listener (23-05) import it, so the format cannot drift between
the writer and the answerer.

## Phase 0 — DONE (do not redo)

The read-only scan over the reference workspace has been run by the manager and
the contract is settled. **Do not re-scan, and do not open
`/data/sync/work/natasha/hyppie-flow/` for anything but curiosity — it is
another project's live repo and is not this epic's to touch.**

- Findings: `agents_output/23-03_phase0_anchor_scan.md`
- The contract you implement: **architecture.md §3.2, rules 1–6** (rewritten).

Two framing points that changed since this task was first written:

1. **This is a contract workspaces adopt, not a parser that retrofits them.**
   Adoption is out of scope for this epic and happens per-workspace. The bar is
   *migratable* compatibility — a queue must be editable into conformance
   **without information loss**, not conformant as it stands.
2. The old anchor 1 ("first heading **containing** the id as a word") is
   **replaced** by "heading at the configured level whose text **begins** with a
   well-formed id". The old rule mis-resolved 5 real ids to other questions'
   entries and would have flipped the wrong entry's status. Do not implement the
   old rule.

Measured against the reference queues, the contract recognises 290/290 entries
and needs 2 lossless edits to adopt. If your implementation changes that number,
you have changed the contract — stop and say so.

## Scope

### 1. Config

Load `[questions]` from the workspace's `.claude/roles.toml` through the existing
loader: `dir`, `anchor`, `nudge`, `escalate_after`, `[questions.queue]` role→file,
`[questions.format]` overrides (`id`, `status`, `heading`, `answer_template`,
`glob`). Defaults are the reference schema (brd §5). Absent section → the store
is inert and every entry point is a clean no-op (invariant 9).

A role absent from `[questions.queue]` has no queue and is chat-only. A workspace
with no roles gets one destination and one file.

### 2. Anchor resolution

The ladder in architecture §3.1, including the bare-repo fall-through and the
non-git case. Returns `(root, workspace_id)`. Must be **pure and side-effect
free** — it may not create directories or touch git state.

### 3. Id allocation and composition

Under `flock` on `<dir>/.lock`, in one critical section: scan the configured
`glob` **plus `answered/`** for existing ids, allocate the next per `id` format,
compose the entry, append, fsync, release. Scanning `answered/` is what prevents
reissuing an archived id.

Composition is **frame-only** (invariant 6): heading, id, status token, tags,
`body` verbatim, rendered options, `**Routed to:** @alias`. Nothing else. A
future field belongs in `body`.

### 4. `mark_dispatched(qid, message_id)` and `apply_answer(...)`

`apply_answer(qid, answer_text, answered_by, message_id) → applied | not_found |
conflict`, idempotent on `message_id` (invariant 3): an answer block already
citing that id is a no-op success. Flips the status token on the heading line,
inserts the rendered `answer_template` before the boundary, moves and deletes
nothing.

`not_found` is a normal, expected outcome (the entry lived on a branch that is
gone) and must be returned cleanly, not raised.

### 5. Answer escaping (invariant 11)

Answer text is human free-form from Telegram and is inserted into a file whose
structure is parsed by anchors. Escape it so that **no possible answer text
changes how any later parse of that file resolves**: no produced line may begin
with `#`, with a configured status token, or with an `**Answered by:**`-shaped
field. This is simultaneously the corruption fix and the injection bound
(brd §8) — treat a hostile answer as a first-class test input, not an edge case.

### 6. Write discipline

Every mutation: `flock` → read → modify in memory → tmp + `os.replace` in the same
directory → release. Never a partial write, never a truncate-then-write. Assume a
human may have the file open in an editor and a `git checkout` may land mid-flight;
a corrupt or unreadable file degrades to a clear error, never to an exception that
escapes the caller.

## Done when

- Round-trip on a contract-conforming fixture: allocate, append, answer, re-read
  — with the resulting diff containing only the intended lines.
- Concurrent writers (≥8 processes) allocate unique ids with no lost appends.
- Anchor resolution is correct in a worktree, in the primary checkout, in a bare
  repo's worktree, and outside git.
- `apply_answer` is idempotent under repeated calls with the same `message_id`.
- A workspace with no `[questions]` section makes every entry point a no-op.

## Tests

Unit, no network. Fixtures are **written to the contract** (architecture §3.2) —
not copied from any live workspace file. Cover:

- each contract rule 1–6, including a heading that *mentions* another entry's id
  (`## Q-388 — …after Q-253`) resolving to the mentioning entry and never the
  mentioned one;
- a status token carrying a free-text suffix (`[resolved 2026-08-13]`) parsing as
  `resolved` with the suffix preserved on rewrite;
- a declared non-default status set via `[questions.format].status`;
- id allocation under ≥8 concurrent writers; `answered/` participating;
  `max + 1` never back-filling a gap;
- atomic replace under a killed writer; corrupt-file degradation to a clean error;
- apply idempotency on `message_id`; `not_found`; **`conflict` on two entry
  headings for one id** — the store must refuse, never pick;
- anchor resolution across worktree / primary checkout / bare-repo worktree /
  non-git;
- hostile answer text (a reply containing a heading, a status token, a fenced
  block and an `**Answered by:**`-shaped field) leaving every later parse
  resolving identically — invariant 11;
- config defaulting and every `[questions.format]` override;
- **one deliberately non-conforming fixture** — a malformed id, a duplicate id
  and a missing status token — asserting the store ignores what it must ignore,
  returns `conflict`/`not_found` where it must, and modifies nothing;
- one flat single `questions.md` with no roles — the brd §5 adoption test.
