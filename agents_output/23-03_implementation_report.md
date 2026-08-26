# Implementation Report - 23-03 Queue-file engine (`questions_store.py`)

## Summary

Implemented `.claude/hooks/questions_store.py`: the only module in the system
that reads or writes a workspace question file. It loads `[questions]` from a
workspace's `roles.toml` through the existing `roles_config` loader, resolves
the anchor with a pure filesystem-only ladder, allocates ids and composes
entries under `flock`, and applies answers idempotently with escaping that
makes it impossible for human answer text to change how any later parse of the
file resolves. 87 new unit tests cover contract rules 1–6, id allocation under
8 concurrent processes, a killed writer, corrupt-file degradation, hostile
answer text, every `[questions.format]` override, anchor resolution across four
environments, the deliberately non-conforming fixture, and the greenfield flat
`questions.md` adoption test.

## Files Created

- `.claude/hooks/questions_store.py` — the queue-file engine (config, anchor
  resolution, parse contract, id allocation, composition, `mark_dispatched`,
  `apply_answer`, escaping, atomic write discipline).
- `tests/test_unit_questions_store.py` — 87 unit tests, no network, all
  fixtures written to the contract.

## Files Modified

- `install-claude-config.sh` — added `questions_store.py` to `REQUIRED_HOOKS`
  so 23-04 (MCP server) and 23-05 (listener) import the *same* module rather
  than reimplementing the format (invariant 5). **Not live** until
  `./install-claude-config.sh` re-runs; it was not re-run (see Blockers — it is
  not a blocker for this task, which needs no live hook).
- `tests/run_all_tests.py` — registered the new module as `unit_questions_store`.

Nothing else in the tree was touched. `tasks/23_async_questions/*.md` show as
modified in `git status` — those are the manager's documentation edits, not mine.

## Verification

- **Compile/import: PASS.** `python3 -m py_compile .claude/hooks/questions_store.py
  tests/test_unit_questions_store.py tests/run_all_tests.py` clean;
  `import questions_store` from a bare `sys.path` containing only
  `.claude/hooks` succeeds with no import-time side effects.
- **Tests: 1117 passed, 1 skipped, 0 failed** — `python3 tests/run_all_tests.py`
  (`Ran 1117 tests in 32.030s / OK (skipped=1)`). Baseline was 1030 passed / 1
  skipped; +87 are this task's. No pre-existing failure was observed.
- **Relay suite untouched: 315 passed** —
  `/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q`
  (`315 passed in 8.15s`), matching the stated baseline exactly. This task
  changed no relay file.
- **Installed (`install-claude-config.sh` re-run): no.** Not required — nothing
  in 23-03 needs a live hook, and re-running would rewrite the developer's live
  config while other sessions share this checkout.
- **Diff reviewed** against `0854326`: two one-line additions to tracked files,
  two new files. No duplicate function definitions, no dead code, no leftover
  `print`/debug statements, no `TODO`/stub.

## Decisions

### The six contract rules, one by one

**Rule 1 — an entry BEGINS with a well-formed id.** `parse_entries` matches
`^ {0,3}#{level}(?!#)[ \t]+` (exactly the configured level, not more), then
requires the *remaining text* to match the id regex anchored at `^`, followed by
`(?![A-Za-z0-9_-])`. There is no "contains the id" path anywhere in the module;
`_id_scan_re` — the unanchored variant — is used **only** for id allocation,
never for locating an entry. Tests assert that `## Q-388 — …after Q-253`,
`## Q-142 — … [SUPERSEDED by Q-269]` resolve to *their own* ids and that
answering `Q-253`/`Q-269` flips the mentioned entries, leaving the mentioning
headings byte-identical.

**Rule 2 — ids and `max + 1`.** The id shape is a template (`Q-{n:03d}` by
default, `{n}` or `{n:0Nd}` accepted) plus the optional short alpha suffix
`(-[A-Za-z]{1,4})`. An id's *identity* is `(number, variant)`, not its
spelling, so `Q-42` and `Q-042` are the same entry — which is what keeps a
caller-supplied qid and `max + 1` in agreement. `_max_number()` scans
`scan_files()` = the queue set (files named in `[questions.queue]`, or the flat
file, **union** the configured `glob`) **plus `answered/*.md`**, including
answered sidecar *filenames* so a 23-05 fallback file reserves its id.
Allocation is always `max + 1`; there is no first-free code path, and a test
asserts a queue of `Q-001, Q-005, Q-471` allocates `Q-472`.

Two deliberate choices inside the scan, both documented in the code:
- ids are collected from **heading lines at any level, anywhere in the heading**
  (so a `### Q-450 item 4` sub-heading or a `## Q-388 — …after Q-253`
  cross-reference still raises the mark). Allocating high is always safe;
  reissuing never is.
- **headings only, never body text.** This ties allocation safety to the
  escaping invariant: no escaped answer line can begin with `#`, so a hostile
  answer containing `## Q-9999` cannot inflate the counter. There is a test for
  exactly that.

**Rule 3 — status is the bracket whose FIRST WORD is in the set.**
`parse_status` walks *every* `[...]` on the heading (the status is not
necessarily the first bracket — `[D1]`, `[role="tablist"]` are real first
brackets in the reference data) and returns the first whose leading
`[A-Za-z0-9_]+` run is in the configured set, **case-insensitively** (so a
declared `superseded` matches `[SUPERSEDED by Q-269]`; this is required to hit
Phase 0's measured "33 edits avoided"). The token's *span* is recorded, and the
flip replaces only that span, so `[open 2026-08-13]` → `[resolved 2026-08-13]`
and `[resolved · design-addressed @v14, pin-pending]` parses as `resolved`.
`[questions.format].status` is an ordered list: element 0 is the token a new
entry is composed with, element 1 is the token `apply_answer` flips to, and
everything beyond is *recognised* vocabulary that is never written — declaring
beats rewriting.

**Rule 4 — boundary.** `_boundary_re(level)` is `^ {0,3}#{1,level}(?!#)(\s|$)`:
the next heading of the same-or-higher level, else EOF. `_insert_before_boundary`
inserts at the end of the entry, *before* the blank lines that separate it from
the next one, so the diff contains only the inserted lines.

**Rule 5 — ambiguity refuses.** `_locate` collects every entry whose
`(number, variant)` matches. Exactly one → operate; zero → `NOT_FOUND`; two or
more → `CONFLICT`, whether the duplicates are in one file or across the queue
set. Both are ordinary `ApplyResult` values, never exceptions — there is an
explicit test that a duplicate id does not raise, and every refusal test asserts
the file is **byte-identical** afterwards. The store never picks between
candidates. One documented ordering: an existing answer marker for this
`message_id` is checked *before* the ambiguity test, so a retry of an
already-applied answer reports success instead of getting permanently stuck on a
conflict that appeared afterwards (invariant 3 outranks rule 5 when the work is
provably already done).

**Rule 6 — non-conforming headings are ignored, never repaired.** A heading that
is not an entry is simply never in `parse_entries`' output; no code path
rewrites one. The required non-conforming fixture (`Q-NNN` schema example,
`## ✅ ANSWER … Q-265 is CLOSED.`, `### Q-450 item 4`, `Q-500-toolong`, a
duplicated `Q-291`, and a conforming-but-status-less `Q-600`) asserts: only
`Q-291 ×2, Q-600, Q-601` are entries; every malformed id returns `NOT_FOUND`
and modifies nothing; the duplicate returns `CONFLICT` and modifies nothing; the
non-entry headings survive an unrelated write untouched. Answering the
status-less `Q-600` inserts the answer and **does not invent a status bracket** —
the heading stays byte-identical, because rule 6 says never repair.

### How the escaping guarantees invariant 11

Three layers in `escape_answer_text`, all applied unconditionally, so the
guarantee does not depend on the answer's content:

1. **Indent.** Every content line is indented four spaces. Markdown allows at
   most three leading spaces before a heading, and every parser in this module
   is written as `^ {0,3}…` — so an indented line can never be a heading, a
   status-bearing line, an `**Answered by:**`-shaped field, or the
   `<!-- answer:N -->` marker.
2. **Per-line guard.** A line whose content would *still* start with a
   structural token after a full `lstrip` — `#`, `<!--`, `**`, a fence run,
   a `---`/`===`/`___` rule, or a word that is in the configured status set,
   with optional `[`/`*`/`_`/`-` decoration — is prefixed with a backslash. So
   even a future reader that strips indentation cannot see forged structure.
   (Consequence: an innocuous answer beginning with the word "open" renders as
   `\open`. It is inside a code fence where a backslash is literal, and literal
   compliance with "no emitted line may begin with a configured status token"
   was preferred over cosmetics.)
3. **Fence.** The block is wrapped in a backtick fence one longer than the
   longest backtick run in the answer, so the answer can neither close the fence
   nor open one of its own.

Two further seams that would otherwise let text through:
- **The idempotency marker is emitted by the store, not by the template**
  (`<!-- answer:N -->`), and is only ever recognised **anchored at line start**.
  A hostile answer containing `<!-- answer:999 -->` mid-line therefore cannot
  make a later, genuine answer with id 999 look already-applied — there is a
  test that does exactly this and asserts the real 999 still applies.
- **`answered_by`, `title`, `tags` and `options` are inline-sanitised**
  (newlines and control characters removed); heading fields additionally have
  `[`/`]` rendered as parentheses, because a bracket in a title is frame syntax
  that could change which bracket a later parse reads as the status. `body`
  stays **verbatim** as the task requires (invariant 6 / brd D3).

The property test does not just check the escaper: it applies a hostile answer
(containing a heading, a bracketed status token, a bare status word, two kinds of
fence, an `**Answered by:**`-shaped field and a forged answer marker) to a
two-entry file and asserts that re-parsing yields *the same entries with the same
ids in the same order*, that no new entry appeared, and that no produced line
begins with any forbidden token.

### Other implementation choices

- **Anchor resolution reads `.git` directly instead of running `git rev-parse`.**
  The task's Goal says "no subprocess"; architecture §3.1 describes the ladder in
  terms of `git rev-parse --git-common-dir`. Both are satisfied by reading the
  `gitdir:` pointer, the worktree's `commondir` file and `core.bare` from
  `config`. This makes the function provably pure — it creates nothing, spawns
  nothing and cannot touch git state — and testable without a git binary. A test
  snapshots the whole fixture tree before and after resolving all three anchor
  modes and asserts it is unchanged.
- **`resolve_anchor` returns a 3-field `NamedTuple`** `(root, workspace_id, mode)`
  rather than the bare `(root, workspace_id)` the task names, because 23-05's
  `--status` and 23-06's diagnostics need to report *which* rung produced the
  root (notably the bare fall-through, reported as `repo→worktree`). Callers use
  attributes.
- **Invariant 9 is structural, not a set of guards.** `open_store()` returns
  `None` when there is no `[questions]` section, and every operation is a method
  on the store — so with no config there is no entry point to call, nothing is
  read and nothing is created. A test asserts `docs/` is not created.
- **Read/write path preserves bytes exactly.** Files are read with
  `newline=""` (universal-newline translation would silently rewrite a CRLF
  file's every line ending) and split on `"\n"`, so `"\n".join(lines)` is the
  original text byte-for-byte. Newly inserted lines carry the file's dominant
  terminator. A CRLF round-trip test covers this; it caught a real bug during
  development.
- **`[questions.format].heading` must keep the status placeholder bracketed.**
  Rule 3 parses a *bracketed* token, so a workspace writing `<{status}>` would
  silently never flip. The loader records a clear config error for that rather
  than changing behaviour (surfaced to 23-06's diagnostics via
  `QuestionsConfig.errors`).
- **A corrupt/unreadable queue file raises `QuestionsStoreError`**, a typed,
  documented error carrying the path and the reason — never a raw `OSError` or
  `UnicodeDecodeError`. I read "degrades to a clear error, never to an exception
  that escapes the caller" as forbidding *unexpected* exception types, not as
  forbidding a documented one: `not_found` and `conflict` are reserved for the
  contract's ambiguity outcomes and widening `ApplyResult` with a fourth value
  would contradict architecture §3.5. This means `create_entry` also refuses
  while a queue file is unreadable, which is correct — allocating an id without
  being able to read the whole scan set risks reissuing one.
- **`answer_template` placement is normalised.** The rendered block is spliced
  so the fence always starts at column 0 regardless of where a workspace puts
  `{answer}` in its template.
- **`_parse_duration` adds a `d` suffix locally** rather than widening the shared
  `roles_config.parse_duration` (which this task does not own and which other
  hooks depend on).
- **The measured adoption numbers (290/290, 2 edits) were not re-measured.**
  Re-measuring means re-scanning the reference workspace, which the task
  explicitly forbids. The six rules are implemented as specified in
  architecture §3.2, including the case-insensitive status matching and the
  short-alpha-suffix id variant that those numbers depend on.

## Blockers

None. Every requirement in the task file is implemented and tested.

One thing to carry forward rather than a blocker: `questions_store.py` is now in
`REQUIRED_HOOKS`, but a repo hook is **not live until `install-claude-config.sh`
re-runs**. That script was deliberately not re-run here (it rewrites the
developer's live config, and this checkout is shared with other sessions). 23-06
owns installation; nothing in 23-03 requires a live hook.

## Questions for User

None.
