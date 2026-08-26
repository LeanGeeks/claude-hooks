# Implementation Report - 23-05 Answer listener runtime (`questions-listen`)

## Summary

Extended `.claude/hooks/questions_listen_lib.py` (created by 23-04, which owns
the index shape) with the resident half of the epic: config, the long-poll loop
over one or more installation-scoped answer feeds, the six-step apply pipeline,
the pending-retry pass with its `answered/<qid>.md` sidecar fallback, the
single-instance lock and the status file. Added `.claude/bin/questions-listen`
as process lifecycle only (run / `--once` / `--status`), and a 60-test module
that asserts the crash-safety properties rather than the implementation —
including crash injection at each of the six steps with a replay assertion, and
a full `after=0` replay proving index recovery cannot double an answer.

## Files Created

- `.claude/bin/questions-listen` — the binary. Resolves the lib, takes the
  `flock`, runs or reports. Exits 0 for "disabled", "another copy is running"
  and a clean shutdown alike; exits 1 only when enabled with no token/server_url.
- `tests/test_unit_questions_listen.py` — 60 tests: apply pipeline, watermark
  discipline, crash injection ×7 (six steps, step 4 in two variants), failure
  routing (`not_found` / `conflict` / `QuestionsStoreError` / unresolvable /
  workspace-id mismatch / unexpected exception), missing-index-entry handling,
  full replay from `after=0`, ack-notifications, anchor re-resolution ×3,
  pending retries + sidecar ×5, feed backoff / `401` / multi-feed, single
  instance, config, `--status`, and the CLI.

## Files Modified

- `.claude/hooks/questions_listen_lib.py` — extended, not replaced. See
  "What I extended vs. left alone" below.
- `tests/run_all_tests.py` — registered `unit_questions_listen`.

## Verification

- **Compile/import:** PASS. `python3 -m py_compile` on
  `.claude/hooks/questions_listen_lib.py`, `.claude/bin/questions-listen`,
  `tests/test_unit_questions_listen.py`, `tests/run_all_tests.py` — all clean.
  `import questions_listen_lib` clean; `questions-listen --status` runs.
  No shell file was touched, so no `bash -n` was needed (ran it on
  `install-claude-config.sh` anyway: clean).
- **Tests:** `python3 tests/run_all_tests.py` →
  `Ran 1216 tests in 37.680s` / `OK (skipped=1)`.
  Baseline was `Ran 1156, skipped=1`; +60 are this task's. The single skip is
  still the pre-existing `test_unit_amux_spawn.TestLiveSpawn.test_headless_spawn`
  (`live spawn test (needs tmux + model auth); set AMUX_SPAWN_LIVE_TEST=1`).
  **No `skipTest` in any new test.**
- **Relay suite untouched:** `/tmp/relay-test-venv/bin/pytest relay-server/tests/
  --tb=short -q` → `315 passed in 8.65s`.
- **Installed (`install-claude-config.sh` re-run): NO — deliberately.**
  `questions_listen_lib.py` is already in `REQUIRED_HOOKS` (23-04 added it), so
  nothing needed adding there. The `questions-listen` symlink into
  `~/.local/bin` and the `claude-questions-listen.service` unit are **23-06's**,
  per its task file §1, and I did not write the unit file. Re-running the
  installer in this shared checkout would clobber another session's live config.
  **Consequence: this task is verified by unit tests, not end-to-end against a
  live relay** — that is 23-07's gate.

## Decisions

### The six apply steps, and what a crash at each one does

The pipeline is `process_answer()`. Steps 1–5 write nothing to the index; step 6
is a single `flock`-ed read-modify-write that records the terminal outcome
(pending upsert/removal) **and** advances the watermark together. That single
write is where invariant 2 lives: there is no window in which the watermark has
moved past an answer that nothing is tracking.

| Crash at | On-disk state after the crash | What the next start does | Why nothing is lost or doubled |
|---|---|---|---|
| **1. index lookup** | nothing written | feed re-delivers (watermark unmoved), lookup succeeds, applies | the lookup is a pure read |
| **2. ack finalize** (`qid is None`) | nothing written, message not finalized | re-delivered, PATCHed, watermark advances | an ack touches no file, so there is nothing to double; the relay row is already `answered` |
| **3. re-resolution** | nothing written | re-delivered, re-resolved from scratch | resolution is pure — it creates nothing and spawns nothing |
| **4a. before the store write** | nothing written | re-delivered, applies | `apply_answer` had not reached `os.replace` |
| **4b. after the store write** | queue file has exactly one `<!-- answer:N -->`; watermark unmoved | re-delivered; `apply_answer` sees its own marker and returns `applied` without inserting | invariant 3 — idempotency is keyed on the relay message id, checked before the ambiguity rule |
| **5. the finalizing PATCH** | answer in the file, watermark unmoved, message unfinalized | re-delivered; apply is an idempotent no-op; PATCH retried | a re-PATCH with identical text is a relay no-op (`_is_not_modified`), and epic 19's cleanup sweep is the backstop |
| **6. the watermark advance** | answer in the file, message finalized, watermark unmoved | re-delivered; apply no-op; PATCH no-op; watermark advances | the whole of step 6 is one atomic write, so it either happened or it did not |

Each row has a test (`TestCrashInjection`) that injects a `BaseException` — so
the listener's own `except Exception` handlers cannot swallow it — asserts the
on-disk state at the crash, then runs a **fresh listener** over the same feed and
asserts the answer is applied exactly once, the message is finalized, the
watermark is at the message id, and `pending` is empty.

### How each failure routes

- **`QuestionsStoreError`** (corrupt/unreadable queue file — the 23-03
  obligation) → caught around *both* store calls (`resolve_target` and
  `apply_answer`) → `pending`, exactly like `not_found`. The loop continues to
  the next answer; a second answer for the same broken file also pends rather
  than aborting the cycle (tested with a non-UTF-8 queue file and two answers).
- **`conflict`** → `pending`, same as `not_found`. The store refuses to guess
  between two entries claiming an id and so does this; nothing is written.
- **A missing index record** (not ours, or 23-04's `index_routing_failed`, or a
  lost index) → **logged at DEBUG, skipped, watermark advanced, nothing pended**.
  This is the high-volume case, not the edge case: the feed carries *every*
  answered message of the installation, so every permission approval and every
  blocking `AskUserQuestion` answer arrives here. Pending them would fill the
  backlog with rows that can never be applied and would make `pending_answers`
  meaningless. Nothing is deleted, nothing is marked applied, no file is touched.
- **A failed PATCH** (as opposed to a crash) → logged, watermark **advances**.
  The decision is already durable in the queue file; a message that cannot be
  edited (deleted, 404) must not stall every later answer.
- **An unforeseen exception** from the store → `pending` with
  `unexpected error: <Type>: <msg>`, logged with `logger.exception`. Loud, not
  masked. This is the only broad catch and it exists because the loop must
  outlive one bad workspace.
- **An unresolvable workspace** (root gone, no `roles.toml`, no `[questions]`,
  or a `workspace_id` mismatch) → `pending` and it **stays** pending: the
  sidecar needs a resolved `<dir>` to write into, so when even that fails the
  entry is kept, counted and surfaced rather than dropped. Visible forever beats
  lost once.

### What I extended in `questions_listen_lib.py` vs. left alone for 23-04

**Left byte-identical:** `read_index`, `write_index`, `add_message_entry`,
`count_pending`, `_atomic_write_json`, `IndexEntry`, `INDEX_PATH`, `_LOCK_PATH`.
`git diff` shows no line changed inside any of them. `questions-mcp` imports
those and is committed; their semantics are untouched.

**Extended, additively:**
- `PendingApply` gained `answered_at`, `last_attempt_at`, `token_fp` — all
  optional with tolerant `from_dict`, so an entry written without them parses.
  `token_fp` is a **fingerprint**, never token material; it tells a retry which
  relay client to finalize with. 23-04 never writes `pending`.
- `Index` gained `watermarks: dict[fingerprint, int]`. 23-04's
  `add_message_entry` round-trips it through the same class, so nothing is lost.
- `_flock_ex` — hoisted its function-local `import contextlib` to module scope.
  Behaviour identical.
- Module docstring and the shape block now document both halves.

### Why there is more than one watermark

The answer feed is **installation-scoped**, and a machine can hold several
installation tokens: `[roles] hpl = "rly_…"` binds a role to its own
installation. An `ask` routed to that role is sent with *that* token, so its
answer appears only in *that* installation's feed. Polling the default token
alone would silently strand every answer given to a role-bound human — the one
outcome this component may not produce.

So: `watermarks[fingerprint]` is the truth for every feed, and `watermark`
mirrors the primary (top-level `installation_token`) feed's value, which keeps
the architecture §5 shape and `--status` honest in the ordinary single-token
case. The loop stays single-threaded and, with one token, is *exactly* the
spec'd `GET /v1/answers?after=<watermark>&wait=25`; with N tokens the 25 s budget
is split (`25 // N`) so a cycle still completes in ~25 s and no feed starves.
A fingerprint with no recorded position starts at 0 — a full replay, which is
safe because applies are idempotent.

### Other choices

- **Re-resolution treats the stored `root` as a probe, never an address**
  (invariant 7). `resolve_target` walks to the nearest *existing* directory at or
  above it, finds `.claude/roles.toml`, loads the config, **forces the index
  entry's recorded anchor mode back on** (so a `worktree` entry is not suddenly
  looked for in the primary checkout, or the reverse), and runs
  `qs.resolve_anchor` afresh. A stored worktree root under `anchor = "repo"`
  therefore redirects to the primary checkout — tested by writing a same-named
  queue file into both and asserting which one changed.
- **`_find_roles_toml` is local, not `roles_config.find_roles_file`.** The
  shared helper honours `CLAUDE_PROJECT_DIR`, a test-isolation hook for hooks
  that run *inside* a session. A resident daemon serving many workspaces must
  key on the index entry alone; honouring an inherited env var would resolve
  every workspace to whatever directory systemd happened to export.
- **A `workspace_id` mismatch refuses rather than writes.** If the path now
  holds a different workspace, writing there would be worse than retrying in
  plain sight.
- **The PATCH text is `✅ <answer>` plus a `<i>Q-NNN · workspace</i>` provenance
  line.** The relay's PATCH replaces the message body and the index does not
  carry the original question text (adding it would change 23-04's shape), so
  the provenance line is what keeps the finalized chat message traceable back to
  the queue entry. Answer text is HTML-escaped and capped at 3500 chars.
- **`answered_by` is `@<role>`** from the index entry (`telegram` when there is
  no role); the timestamp and `relay #<id>` come from the store's own
  `answer_template`, so the provenance in brd §8 is complete.
- **Nothing is spawned** (invariant 8). No subprocess of any kind: anchor
  resolution reads `.git` metadata directly (23-03's design), and there is no
  `amux`, no git, no shell anywhere in the module.
- **The sidecar** (`<dir>/answered/<qid>.md`) is written under the store's own
  directory lock, with the answer rendered through `qs.render_answer_block` so
  invariant 11's escaping applies, and is idempotent on the relay message id.
  It is not a queue file (`answered/` is scanned for id allocation only), and no
  queue entry is moved or deleted.
- **Backoff:** jittered ×2 from 2 s to 300 s on network error, reset on the
  first success; `401` is a separate `unauthorized` state with a flat 300 s
  retry, surfaced in `--status`. Disconnects log at DEBUG — a laptop that slept
  drops its connection every time and that is not news.
- **Pending backoff:** 60 s doubling, capped at 3600 s, sidecar at
  `max_attempts` (default 10, configurable via `[questions_listen].max_attempts`).
- **`--status` never prints token material** — only fingerprints. Asserted by a
  test that serialises the whole status blob and the whole rendered output and
  greps for the token.

### Requirements that were awkward to test, stated plainly

- **"Anchor re-resolution after moving a checkout."** A checkout whose *whole
  path* moved cannot be found again: the index is machine-local and there is no
  workspace registry on this machine (epic 16's seen-store is still `todo`), so
  `workspace_id` cannot be mapped back to a path. That case is tested for what
  it actually does — `pending` with `workspace not resolvable: …`, watermark
  advanced, nothing lost — rather than being weakened into something that passes.
  The re-resolution that *is* possible (a stored worktree root redirecting to
  the primary checkout, and the reverse being honoured under `anchor =
  "worktree"`) is tested directly against a synthetic linked-worktree layout.
- **A persistent index-write failure** (e.g. ENOSPC in step 6) propagates and
  ends the process rather than being swallowed, so systemd restarts it and the
  answer replays. That is deliberate — advancing a watermark we could not
  persist would be the losing outcome — but it means a full disk produces a
  restart loop rather than a quiet degradation. Not covered by a test.

## Blockers

None for the engineering scope of 23-05.

Two things are **out of scope by assignment**, recorded so 23-06/23-07 do not
assume they happened:

- The task's Done-when clauses are demonstrated by unit tests with a fake relay
  client, not against a live relay + real bot. `install-claude-config.sh` was
  **not** re-run (23-06 owns installation; re-running it in a shared checkout
  would clobber another session's live config), so the listener is not installed
  or running on this machine and no end-to-end verification is claimed.
- The systemd unit `claude-questions-listen.service` (architecture §5.2) and the
  `~/.local/bin/questions-listen` symlink are **23-06's** per its own task file
  §1; I created neither. Nothing was added to `REQUIRED_HOOKS` (the module was
  already registered by 23-04) and `install-claude-config.sh` is unmodified.

## Questions for User

None.
