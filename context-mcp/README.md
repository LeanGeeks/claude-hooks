# context-mcp

One tool, `get_context_usage`: how full is the context window, read out of the
session's own transcript on disk.

- `server.py` — tool registration only (uv inline script, needs `mcp`).
- `context_mcp_lib.py` — all logic, importable without `mcp` so the repo's
  plain suite can test it (`tests/test_unit_context_mcp.py`).
- `models.json` — context window per model-id glob.

## What it can and cannot see

An MCP server is **one process per session**. Every tool call from that session
arrives on the same stdio pipe and carries **no caller identity**, so the server
cannot tell a main agent from one of its subagents. It therefore reports the
**main session** and labels it: `scope: "main-session"`, `caller_identified:
false`, and a `warning` whenever subagents exist.

A subagent cannot measure itself here. What it gets instead is better: the
payload carries a `subagents` array with each subagent's **own** measured usage,
fullest first, so a supervisor can see its whole chain in one read.

This matters because the failure is silent. On 2026-09-14, in leads-platform
unit 034, two manager subagents read `fill_percent` 10.5 and 12.9 and reported
themselves "well within budget" while actually sitting at 165.2 K and 112.3 K;
one read byte-identical numbers four hours apart because its parent had not
taken a turn in between. Their chain auto-compacted four times — 166 K, 167 K,
167 K, 173 K — against a reported window of 1,000,000 that had never been
measured. Downstream that constant had been written into project doctrine as a
budget ladder whose rungs sat above the window the sessions actually died at.

The sibling permissions-mcp states the principle: *a guard that cannot identify
its caller cannot enforce the self-decision rule* — it fails closed. A read tool
cannot fail closed and stay useful, so this one fails **loud**.

## Reading the output

| field | meaning |
|---|---|
| `scope` | always `main-session` — whose numbers these are |
| `caller_identified` | always `false`; the server cannot know who called |
| `session_resolution` | `session-id` (exact) or `fallback-most-recent` (a guess — see below) |
| `context_window_source` | `env` · `models.json` · `default-fallback` |
| `fill_percent` | `effective_context_tokens` ÷ `context_window` |
| `subagents[]` | each subagent's own measured usage, fullest first |
| `warnings[]` | present whenever any of the above makes the reading unsafe |

`effective_context_tokens` is prompt-side only (`input` + `cache_creation` +
`cache_read`) — what occupies the window on the next request. `output_tokens`
is reported beside it, not folded in.

`fallback-most-recent` means `CLAUDE_CODE_SESSION_ID` did not resolve and the
server fell back to the most recently modified transcript in the project
directory. In a fleet that can be a **different unit's session**. Treat those
numbers as unattributed.

## models.json

Patterns are `fnmatch` globs against the model id, **first match wins**, so the
file is ordered most-specific first. Unmatched models fall through to
`DEFAULT_CONTEXT_WINDOW` and are reported as `default-fallback` — that is a
placeholder, not a measurement. Add the model rather than trusting it.
`CONTEXT_WINDOW_MAP` (`pattern=tokens,…`) overrides the file.

**`[` opens an fnmatch character class.** The long-context variants must be
matched as `*[[]1m]*`, not `*[1m]*` — the latter reads as "contains a `1` or an
`m`" and matches `claude-haiku-4-5-20251001`, sizing a 200K model at 1M. A test
pins this.

Window provenance: `claude-opus-4-*` and `claude-sonnet-4-*` are 200000 because
a sonnet-4-6 chain was observed auto-compacting at 166–173 K on 2026-09-14; they
were previously declared 1000000, which is what hid those compactions. The
remaining entries are inherited and unverified — correct them as evidence
arrives, and prefer a measured compaction event over any vendor claim.
