# 23-03 — Queue-file engine (`questions_store.py`)

**Status:** todo · **Depends on:** — (independent root; the critical path)
**Read first:** [brd.md](./brd.md) §2.6, §2.7, §5, D3, D8 ·
[architecture.md](./architecture.md) §3 · [state.md](./state.md) invariants 5, 6, 7 ·
`.claude/hooks/permission_state_store.py` (the lock + atomic-replace discipline
to copy) · `.claude/hooks/roles_config.py` (config loading) · the **real**
reference files at `/data/sync/work/natasha/hyppie-flow/docs/questions/` and the
schema they follow in that repo's `docs/workflow.md` §5.2

## Goal

The only module in the system that reads or writes a workspace question file.
Pure library: no relay, no MCP, no subprocess, no network. Both the MCP server
(23-04) and the listener (23-05) import it, so the format cannot drift between
the writer and the answerer.

## Phase 0 — settle the parse contract against reality (do this first)

Before writing the writer, run a **read-only** scan over every entry in the three
reference queue files and `answered/`, and report:

- how many entries the three anchors (architecture §3.2) locate cleanly;
- every entry they do not, with why;
- the distinct heading shapes, status tokens and tag forms actually in use;
- the id high-water mark and any gaps or duplicates.

If the anchors hold for all of them, say so and proceed. If a subset needs a
`heading` override, that is a finding for the config defaults, not a reason to
add heuristics. **Do not modify those files** — they are a live 300 KB artifact
with real history in another project.

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

- Phase 0 findings are recorded in this file or in `state.md`.
- Round-trip on a **copy** of the real reference files: allocate, append, answer,
  re-read — with the resulting diff containing only the intended lines.
- Concurrent writers (≥8 processes) allocate unique ids with no lost appends.
- Anchor resolution is correct in a worktree, in the primary checkout, in a bare
  repo's worktree, and outside git.
- `apply_answer` is idempotent under repeated calls with the same `message_id`.
- A workspace with no `[questions]` section makes every entry point a no-op.

## Tests

Unit, no network, fixtures **copied** from real entries (never the live files):
the three anchors against every fixture shape; id allocation under concurrency;
`answered/` participating in allocation; atomic replace under a killed writer;
apply idempotency / not-found / conflict; anchor ladder across all four cases;
hostile answer text (a reply containing a heading, a status token, a fenced block
and an answer-block field) leaving every anchor resolving as before;
config defaulting and every `[questions.format]` override; corrupt-file
degradation. Include one fixture that is a flat single `questions.md` with no
roles — the brd §5 adoption test.
