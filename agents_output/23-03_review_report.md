# Review Report — 23-03 Queue-file engine (`questions_store.py`)

## Verdict: PASS

No BLOCKER or HIGH issues found. All six parse-contract rules are implemented
correctly, tests are genuine and pass (1117/1 skipped), relay suite untouched
(315 passed). Four explicit rulings on the implementer's judgment calls below.

---

## Compile and import

```
python3 -m py_compile .claude/hooks/questions_store.py tests/test_unit_questions_store.py
→ clean (no output)
```

Import from a bare `sys.path` containing only `.claude/hooks` succeeds with no
import-time side effects (confirmed by module-level guard: no I/O at import).

---

## Test runs

```
python3 tests/run_all_tests.py
→ Ran 1117 tests in 32.1s   OK (skipped=1)
  (baseline 1030/1; +87 are this task's)

/tmp/relay-test-venv/bin/pytest relay-server/tests/ --tb=short -q
→ 315 passed in 7.1s
  (baseline 315; 0 change)
```

---

## Completeness Check

### Rule 1 — entry BEGINS with well-formed id

**Met.** `parse_entries` (`.claude/hooks/questions_store.py:513`) uses
`_entry_heading_re(level)` to match headings at *exactly* the configured level
(not deeper), then matches `_id_start_re(fmt)` anchored at the start of the
remaining text. There is no "contains" path for locating. `_id_scan_re` (the
unanchored variant) appears only in `_max_number` (line 1040) for id allocation,
never for entry location. Tests `test_mentioned_id_resolves_to_its_own_entry` and
`test_bracket_mention_does_not_capture_the_id` confirm that `## Q-388 — …after
Q-253` resolves to Q-388 and that `[SUPERSEDED by Q-269]` does not capture Q-269.

### Rule 2 — max+1, never first-free; answered/ participates

**Met.** `_max_number` (line 1040) scans all heading lines at any level using the
unanchored `_id_scan_re`. It iterates `scan_files()` = queue_files + answered/*.md
and also checks `answered/` sidecar filenames via `scan_re.match(path.stem)` —
so a sidecar written by the 23-05 fallback reserves its id even if it contains no
heading. Allocation is always `_max_number() + 1`; there is no first-free path.

Q-42 / Q-042 identity: `parse_id` returns `(int(m.group("num")), variant)`, so
both tokenise to `(42, "")` — same key, no collision, no mis-allocation. This
is also what keeps a caller-supplied qid and the next-id in agreement across
zero-padded formats.

Tests: `test_allocation_is_max_plus_one_and_never_backfills_a_gap` (Q-001,
Q-005, Q-471 → Q-472), `test_answered_directory_participates_in_allocation`,
`test_answered_sidecar_filename_alone_participates`, and the concurrent-writer
test (8 processes, 8 unique ids, 9 entries asserted).

### Rule 3 — status is the bracket whose first word is in the configured set

**Met.** `parse_status` (line 493) iterates every `[…]` on the heading using
`re.finditer(r"\[([^\]\n]*)\]", line)` and extracts the leading
`[A-Za-z0-9_]+` run with a match inside the bracket. It compares lower-cased
against `fmt.status_set` (which is already lower-cased at construction time).
The first matching bracket wins — so `[D1]` and `[role="tablist"]` are skipped.

The `StatusToken` records `start` and `end` as character offsets of the *word*
within the heading string (not the whole bracket). The flip in `apply_answer`
(line 1295) is `heading[:token.start] + resolved_status + heading[token.end:]`,
which replaces only the word and leaves the bracket's surrounding text
(including free-text suffix and square brackets) in place.

Concretely for `[open 2026-08-13]`: token.start = position of `o`, token.end =
position after `n`; result is `[resolved 2026-08-13]`. For `[SUPERSEDED by
Q-269]`: result is `[resolved by Q-269]`. Both are correct per the spec.

For `[D1]  [open]  [blocks: iter-7]`: `parse_status` skips `[D1]` (not in
set), matches `[open]`, flips only that word → `[D1]  [resolved]  [blocks:
iter-7]`. Tested by `test_status_is_not_necessarily_the_first_bracket`.

The configured open status (element 0) is written to new entries;
`resolved_status` (element 1, or element 0 if only one) is what flip writes.
Extra declared tokens beyond index 1 are recognised but never written.

### Rule 4 — boundary

**Met.** `_boundary_re(level)` (line 560) is `^ {0,3}#{1,{level}}(?!#)(?:\s|$)`,
matching any heading of levels 1 through `level`. Sub-headings (level+1 or
deeper) are not boundaries. `_insert_before_boundary` (line 1397) walks
backward from the boundary line to skip trailing blank lines before inserting,
so the diff contains only the inserted lines and not spurious blank changes.

### Rule 5 — ambiguity refuses

**Met.** `_locate` (line 1332) collects all entries whose `(number, variant)`
matches across the target files. Exactly one → APPLIED; zero → NOT_FOUND; two
or more → CONFLICT. Both NOT_FOUND and CONFLICT are `ApplyResult` enum values,
never raised. The file is provably unmodified on conflict or not-found (asserted
by byte-comparison in `test_two_entry_headings_return_conflict` and
`test_zero_entries_return_not_found`).

Idempotency ordering: the answer-marker search (`_has_answer_marker`, line 1378)
runs *before* the `parse_entries` pass. If the marker is found, `_locate` returns
`_Located(APPLIED, path, lines, entry=None)`. `apply_answer` sees `entry is None`
and returns APPLIED without attempting a write. Invariant 3 is preserved: a retry
of an already-applied answer reports success even if a conflict later appeared in
the same file (tested by `test_idempotency_outranks_a_later_conflict`).

The marker search is file-wide (not entry-specific): see Issue 1 below.

### Rule 6 — non-conforming headings are ignored, never repaired

**Met.** A heading that does not match `_entry_heading_re` + `_id_start_re` never
appears in `parse_entries`' output. No code path writes to a non-entry heading.

For a status-less conforming entry (Q-600 in the non-conforming fixture): the
code at line 1291 checks `if entry.status_token is not None` before flipping.
When the token is absent, nothing is written to the heading; it stays
byte-identical. The answer block is still inserted. Tested by
`test_missing_status_token_is_never_repaired`, which asserts `## Q-600 —
conforming but status-less\n` verbatim in the rewritten file.

### Scope — all done-when criteria

| Criterion | Status |
|---|---|
| Round-trip: allocate → dispatch → answer → re-read | Met — `test_round_trip_allocate_append_answer_reread` |
| ≥8 concurrent writers, unique ids, no lost appends | Met — 8 real subprocesses, asserted |
| Anchor resolution in 4 environments | Met — `TestAnchorResolution` covers all |
| `apply_answer` idempotent on message_id | Met — `test_apply_is_idempotent_on_message_id` |
| No-op with no `[questions]` section | Met — `test_absent_section_makes_the_store_inert` |

### Invariant 9 — inert without config

**Met.** `open_store` (line 1418) returns `None` when `load_questions_config`
returns `None`. Every mutation is a method on `QuestionsStore`. With no store
object there is no entry point to call. `test_absent_section_makes_the_store_inert`
asserts `docs/` is never created.

### Write discipline

**Met.** `_atomic_write` (line 873):
- `tempfile.mkstemp(dir=str(path.parent), …)` — tmp in same directory, preventing
  cross-device `os.replace` failure.
- `os.fsync` before `os.replace`.
- `os.unlink(tmp)` in the `except BaseException` clause — tmp cleaned on any
  failure before the replace.
- Directory-level `os.fsync` after the rename for durability.
- The killed-writer test (`test_a_killed_writer_leaves_the_file_untouched`)
  patches `os.replace` to SIGKILL the process at replace time; the original
  file is asserted byte-identical afterwards.
- The failed-replace test (`test_a_failed_replace_cleans_up_and_changes_nothing`)
  asserts no `.tmp` files remain.

### Concurrency

**Genuine.** The concurrent test (`test_concurrent_writers_allocate_unique_ids`)
launches 8 subprocess.Popen processes, each running a self-contained Python
script that imports `questions_store` and calls `create_entry`. It waits on all 8
with `.communicate(timeout=120)`, asserts 8 unique qids, and asserts 9 total
entries (seed + 8 appended). This is real parallelism, not simulation.

### Purity

**Met.** `test_resolution_is_side_effect_free` snapshots
`sorted(self.tmp.rglob("*"))` before resolving all three anchor modes and asserts
the sorted list is identical afterwards. No directory or file was created.

### REQUIRED_HOOKS registration

**Correct.** `install-claude-config.sh` line 163 adds `"questions_store.py"` to
the REQUIRED_HOOKS array. Not re-run (correct — 23-06 owns installation; this
task needs no live hook). The REQUIRED_HOOKS mechanism copies the file to the
install dir so both 23-04 and 23-05 import the same module.

---

## Issues Found

### Issue 1: `_has_answer_marker` is file-wide, not entry-specific [LOW]

- **File:** `.claude/hooks/questions_store.py:1356–1366` (`_locate`)
- **Problem:** The idempotency marker search scans all lines of the target file.
  If `apply_answer("Q-002", …, message_id=65)` is called after
  `apply_answer("Q-001", …, 65)` already applied to Q-001 in the same file,
  `_locate` finds marker 65 and returns `APPLIED` without applying to Q-002.
  In practice this requires the relay to assign the same `message_id` to two
  different qids, which the architecture forbids (one relay message → one qid).
  23-05 mitigates it further by passing `rel_path` from the index, but even with
  `rel_path` the marker is file-wide.
- **Fix:** Document the precondition clearly in `_has_answer_marker`'s docstring:
  "correct only when message_id is unique per qid within the file, which the
  relay guarantees." No code change required; this is a known trade-off.

### Issue 2: per-line status-word guard fires inside an already-indented fence [LOW]

- **File:** `.claude/hooks/questions_store.py:576–593` (`_needs_guard`,
  `escape_answer_text`)
- **Problem:** `_needs_guard` returns True for any line whose stripped content
  begins with a configured status word (e.g. "open", "resolved"). Since answer
  lines are already indented four spaces, the spec's parse contract (which only
  inspects heading lines beginning with `#`) can never interpret them as status
  tokens regardless of their content. The guard is therefore over-broad for status
  vocabulary: an answer like "Open the file by clicking…" becomes `\Open the
  file…` in the rendered document — a visible backslash in a code fence.
- **Fix:** Exempt status-vocabulary matching from the guard when the line is
  already inside a fenced, indented block. Alternatively, restrict the guard to
  the prefix patterns that are structurally dangerous outside any fencing context:
  `#`, `<!--`, `**`, fence runs, setext rules. Status tokens cannot be forged as
  status tokens without being on a heading line, which the 4-space indent
  prevents. Ruling: see Judgment Call 3 below.

### Issue 3: `_is_bare` git-config parser does not validate section context [LOW]

- **File:** `.claude/hooks/questions_store.py:779–794` (`_is_bare`)
- **Problem:** The parser matches `^bare\s*=\s*(\S+)` against every non-comment
  line of the git config, regardless of which `[section]` it falls under. A git
  config that has `bare = true` in a non-`[core]` section (e.g. a
  `[branch "main"]` key with that name) would cause a false bare-detection.
  In practice, `bare` is only ever written by git in `[core]`, and the last
  matching line wins (so `[core] bare = false` followed by `[X] bare = true`
  would wrongly return bare=True).
- **Fix:** Track the current section and only match when inside `[core]`. This
  is a theoretical concern — no real git repository has `bare` outside `[core]`
  — but the fix is five lines and removes a latent surprise.

---

## Judgment Call Rulings

### JC-1: `QuestionsStoreError` on a corrupt/unreadable file

**Ruling: Agree, with a documented obligation on 23-05.**

The task says "degrades to a clear error, never to an exception that escapes
the caller". The phrase "escapes the caller" most naturally means "escapes the
function call" as a raw, undocumented exception type — which `OSError` and
`UnicodeDecodeError` would be. `QuestionsStoreError` is a typed, documented
exception with a clear meaning, carrying the path and reason. Raising it is the
idiomatic "clear error" path; it is not an exception that "escapes" in the
problematic sense.

The consequence for 23-05 is real: if the listener loop calls
`store.apply_answer(…)` without catching `QuestionsStoreError`, one unreadable
queue file can kill the listener — the component whose "whole job is never
losing an answer". This is an obligation, not a current bug, because 23-05 has
not been written yet. The 23-05 task file must explicitly require the listener
to catch `QuestionsStoreError` and move the answer to `pending` (the same path
as a NOT_FOUND). This should be noted in 23-05's "read first" section.

### JC-2: `resolve_anchor` returns `(root, workspace_id, mode)` instead of `(root, workspace_id)`

**Ruling: Agree.**

This is a new module with no existing callers. Adding a third field to a
`NamedTuple` is backward-compatible as long as callers use attributes (not
positional unpacking), which is the natural idiom for a `NamedTuple`. The `mode`
field has concrete utility: 23-05 needs to log which rung produced the root
(especially `"repo→worktree"`, the bare fall-through), and 23-06's
`claude-questions` diagnostic explicitly needs this for its output. The
alternative — tacking the mode on as a separate return value in 23-05/23-06 —
would be duplication. Accepted.

### JC-3: Escaping renders "open" as `\open` inside the answer fence

**Ruling: Disagree — the guard is over-broad for status vocabulary.**

The 4-space indent already makes these lines unreachable by `parse_status`:
that function only inspects heading lines beginning with `#`, and a line with 4
leading spaces can never be a heading (markdown allows at most 3). The per-line
status guard fires on content that is already structurally neutralised by the
indent, at the cost of corrupting innocent answers. The word "resolved" is
common English; "open" is common English. A 23-05 answer of "Resolved: proceed
with JWT" would reach the git diff as `\Resolved: proceed with JWT`.

That said, the security guarantee (invariant 11) is fully upheld — the guard's
over-breadth does not create a hole, and the backslash is literal inside a code
fence, so the content is still readable. This is a LOW cosmetic issue, not a
correctness defect.

**Recommended fix:** in `_needs_guard`, remove status vocabulary from the
per-line check. Keep `#`, `<!--`, `**`, fence runs, and setext rules (these are
dangerous regardless of indentation in a lenient reader). Status words inside
an indented fence cannot be misread by any parser that respects the contract in
this file.

### JC-4: Anchor resolution reads `.git` files directly

**Ruling: Agree.**

The four environment cases were verified by tracing the code and the tests:

1. **Normal primary checkout** — `_find_git` finds `.git` directory; `_git_common_dir`
   reads `commondir` (absent → returns `git_dir`); `_is_bare` reads `config`
   (`bare = false`); `primary = common.parent = repo_root`. ✅
2. **Linked worktree** — `.git` is a *file* containing `gitdir: …/worktrees/name`;
   `_read_gitdir_file` resolves to the worktree git dir; `_git_common_dir` reads
   `commondir` with a relative path (e.g. `../..`) and resolves to the primary
   `.git`; `primary = common.parent`. ✅
3. **Bare repo's worktree** — worktree `.git` file → worktree git dir →
   `commondir` → bare git dir (not named `.git`); `_is_bare` returns True
   immediately on `common.name != ".git"` (or on `bare = true` in config for a
   `.git`-named bare dir); falls through to worktree root. ✅
4. **Non-git directory** — `_find_git` returns None; `_nearest_marker_root`
   walks up for `.claude`, else returns cwd. ✅

One low-severity caveat: `_is_bare` parses `bare = value` from every config line
without tracking the current `[section]`. In a pathological git config, a
`bare = true` in a non-`[core]` section would produce a false positive. Real git
never writes `bare` outside `[core]`, so this is theoretical (see Issue 3).

The side-effect-free property is verified by a snapshot test
(`test_resolution_is_side_effect_free`). The no-subprocess purity requirement is
met: no `subprocess.run`, `os.system` or `shlex.split` anywhere in the module.

---

## Escaping Attempts — Invariant 11

Eighteen adversarial inputs were tested in a scratch script
(`scratchpad/break_escaping.py`, `break_escaping2.py`). The falsifiable property
is: *re-parsing the file after insertion yields the same entries, same ids, same
order, and no new entry*. All 18 passed.

Scenarios attempted:

| Input | Result |
|---|---|
| ```` ``` ```` followed by `## Q-999 — injected  [open]` | Fence grows to 4 ticks; heading is indented + backslash-guarded |
| `<!-- answer:999 -->` and `<!-- answer:0 -->` in body | Lines indented 4 spaces + `\<!--` guard; ANSWER_MARKER_RE does not match |
| `[open]` and `[resolved 2026-08-13]` at line start | Indented + backslash-guarded; not at heading position |
| CRLF line endings (`\r\n`) | Normalised to `\n` inside escape; CRLF round-trip preserved by file read/write |
| 1000-backtick run | Fence grows to 1001 ticks |
| Full-width `＃` (U+FF03) before `Q-999 — …  [open]` | Not `#` (U+0023); `parse_entries` does not match; no effect |
| Zero-width space before `##` | Indent normalised; zero-width chars are not in `_CONTROL_RE` range but the line starts with 4 spaces so the heading regex cannot fire |
| Complete well-formed entry (heading, body, routing, answer block) | Every structural line guarded; `parse_entries` finds no new entry |
| Setext headings (`=====` and `-----` underlines) | Caught by `_GUARD_RULE_RE` (`^(?:-{3,}|={3,}|_{3,})\s*$`); backslash-prefixed |
| Lone `\r` endings | Normalised by `replace("\r\n", "\n").replace("\r", "\n")` |
| Null bytes and `\x07` | Stripped by `_CONTROL_RE.sub("", …)` |
| Form feed `\x0c` | Stripped by `_CONTROL_RE` |

The `<!-- answer:N -->` forged-marker test is also covered by `test_hostile_
answer_cannot_forge_the_idempotency_marker`: the hostile answer contains
`<!-- answer:999 -->`; a subsequent call `apply_answer("Q-002", …, 999)` still
applies (the marker inside the escaped block starts with `    \<!--`, which
`ANSWER_MARKER_RE` — anchored at `^ {0,3}<!--` — does not match).

**Escaping was not broken.** Three layers (indent + per-line guard + dynamic
fence) together with the anchored marker regex and inline sanitisation of
caller-supplied fields hold the invariant unconditionally.

---

## Code Quality Notes

- No dead code, no leftover `print`/debug statements, no `TODO`/stub.
- `_parse_duration` correctly avoids widening the shared `roles_config.parse_duration`.
- The lock `open` uses `"a+"` mode, which creates the file without truncating
  it. `LOCK_FILENAME = ".lock"` means the lock file is not named `*.md` and is
  excluded from `scan_files()` by the glob filter — confirmed by
  `test_lock_file_lives_beside_the_queue`.
- The `QuestionsFormat.level` property reads the heading template at call time
  rather than caching it. This is correct and harmless since `QuestionsFormat`
  is frozen.
- `_sanitise_heading_field` converts `[` / `]` to `(` / `)`, which prevents a
  title like `Bad [resolved] title` from being parsed as a status bracket.
  Tested by `test_composed_title_cannot_forge_a_status_bracket`.
- The `every_format_override` test (line 1172) also asserts `\Done it.` in the
  rendered output, confirming that the per-line guard fires on `Done it.` when
  `done` is a declared status token. This is the same cosmetic issue as JC-3.

## Questions for User

None — all decisions can be ruled on the specification.
