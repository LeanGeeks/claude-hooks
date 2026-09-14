# Task 39 — `get_context_usage` answers a subagent with its parent's numbers (bug)

**Status:** done · **Type:** bug · **Created:** 2026-09-14 · **Rev:** 1
**Priority:** high — the tool is used to pace work against a context budget,
and it silently reports the wrong session's fill to every subagent that asks;
a real chain auto-compacted four times while its managers read "well within
budget"
**Suggested worker:** one implement → review loop; self-contained
**Read first:** §1 · §2
**Scope:** `context-mcp/server.py`, `context-mcp/context_mcp_lib.py` (new),
`context-mcp/models.json`, `context-mcp/README.md` (new),
`tests/test_unit_context_mcp.py` (new), this file. Nothing else.
**Origin:** production post-mortem, 2026-09-14 (leads-platform unit 034).
Reported and approved for fix by the repo owner.

## 1. The defect

An MCP server is one process per **session**. Every tool call from that session
arrives on the same stdio pipe, whether it came from the main agent or from one
of its subagents, and the call carries **no caller identity**.

`_find_session_jsonl` resolved the transcript from the process environment:

```python
session_id  = os.environ.get("CLAUDE_CODE_SESSION_ID")
project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD")
```

Both are inherited by subagents, so a subagent always resolved its **parent's**
transcript. Subagent transcripts live at
`<projects>/<encoded>/<session-id>/subagents/agent-<id>.jsonl` — a path this
function never constructs and could not, because the agent id is not in the
environment.

The result was returned with no indication of whose numbers it was. Three
further defects compounded it:

1. **The window was a guess presented as a fact.** `models.json` declared
   `"claude-opus-4-*"` and `"claude-sonnet-4-*"` at `1000000`, and any model
   unmatched by the table fell through to a literal
   `DEFAULT_CONTEXT_WINDOW = 1_000_000`. Nothing in the payload distinguished a
   table hit from the fallback constant.
2. **The session fallback could return a stranger.** With no usable session id
   the server globbed `*.jsonl` in the project dir and took the most recently
   modified — in a fleet, potentially a different unit's session — and reported
   it identically to an exact hit.
3. **No way to see a chain.** A supervisor could not read its subagents' usage
   even though those transcripts sit in a known directory.

## 2. What it cost

leads-platform unit 034, 2026-09-14. Two manager subagents called the tool and
received `session_id 9e7fc6e8…` (their parent), `context_window 1000000`,
`fill_percent` 10.5 and 12.9. Their real contexts were 165.2 K and 112.3 K. One
read byte-identical numbers at 08:48 and 12:37 — four hours apart — because the
parent had not taken a turn in between; nothing marked the reading stale. The
resume manager recorded *"Context at 12.9% — well within budget"*.

Meanwhile the chain auto-compacted four times: implementer at 166 K, 167 K,
167 K and fixer at 173 K, each dropping to ~32–35 K and re-reading its working
set. That rebuild-and-repeat is a large part of why the unit spent 4.5 h on one
task and a probe.

Downstream, the 1,000,000 had been copied into the consuming project's doctrine
as *"W = 1M confirmed first-hand"* and a 120/240/300 K budget ladder built on
it — rungs sitting above the window the sessions actually died at, so the
safety mechanism could never fire.

## 3. The fix

The sibling permissions-mcp states the principle — *a guard that cannot
identify its caller cannot enforce the self-decision rule* — and fails closed.
A read tool cannot fail closed and stay useful, so this one fails **loud**:

- `scope: "main-session"` and `caller_identified: false` on every payload. The
  server does not imply an identity it cannot establish.
- `warnings[]` whenever the reading could mislead: subagents exist, the window
  came from the fallback constant, or the session was guessed.
- `subagents[]` — each subagent's **own** measured usage (model, window, fill,
  turns, agentType/description from its `.meta.json`), fullest first. A
  subagent still cannot measure itself, but its supervisor now can, which is
  the read that was actually needed.
- `context_window_source`: `env` · `models.json` · `default-fallback`.
- `session_resolution`: `session-id` · `fallback-most-recent`.
- `models.json`: `claude-opus-4-*` and `claude-sonnet-4-*` corrected to
  `200000` on the measured compaction evidence; long-context variants matched
  first via `*[[]1m]*`.
- Logic moved to `context_mcp_lib.py` so it is testable without the `mcp`
  dependency (the `permissions_mcp_lib` convention); `server.py` is
  registration only, bumped to 2.0.0.

Deliberately **not** done: no attempt to infer the calling subagent. The
most-recently-modified agent transcript would identify it *usually*, and unit
034 ran a manager and a qa-runner concurrently — a guess of exactly the kind
that caused this bug. Identity has to come from the harness or not at all.

## 4. Verification

- `tests/test_unit_context_mcp.py` — 19 cases: window resolution and its
  provenance, session resolution incl. the most-recent guess, transcript
  dedupe, and the unit-034 regression reproduced in miniature (parent at
  ~129 K / 65 %, subagent at ~173 K / 86 %; the payload must carry both and say
  which is which).
- A test pins the fnmatch bracket trap: `*[1m]*` is a character class meaning
  "contains a `1` or an `m`" — it matches `claude-haiku-4-5-20251001` and would
  size a 200 K model at 1 M. Caught by the suite during this task, against the
  first draft of the fix.
- End-to-end over stdio through `uv run --script server.py` with the real `mcp`
  dependency: `initialize` → `tools/call`, against this machine's live unit-034
  transcripts. Returned `scope main-session`, `context_window 200000`,
  `context_window_source models.json`, `fill_percent 77.6`, and 10 subagent
  rows topped by the fixer at 79.2 %.
- Full suite A/B, **in place** so both sides share one environment (a
  `git worktree` of HEAD was tried first and rejected as a baseline: it lacks
  the repo's untracked runtime files and hung on a subprocess-spawning test):

  | | failed | passed | skipped |
  |---|---|---|---|
  | HEAD `82f058d`, changes stashed | 212 | 1713 | 1 |
  | with this task's changes | 212 | 1732 | 1 |

  Identical failure count; `passed` +19, exactly this task's new cases. The 212
  are pre-existing in `test_unit_installer.py` and `test_unit_permissions_mcp.py`
  and are untouched by this scope. Shadowing was ruled out separately: nothing
  in the repo imports a bare `server`, and the test's `sys.path` insert follows
  the same convention `test_unit_allowlist_queue.py` uses for `permissions-mcp`.
