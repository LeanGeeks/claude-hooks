# 46-04 — Operations, MCP tools, CLI flags

**Status:** done · **Depends on:** 46-01 (client signature), 46-02, 46-03
**Read first:** [brd.md](./brd.md) D1, D3, D4, D10, D11, §4 · [architecture.md](./architecture.md) §5 ·
`questions-mcp/`, `shell/claude-questions`

## Scope

- `.claude/hooks/questions_ops.py` — `withdraw`, `claim`, `sync`. File first
  (through `questions_git.run`), relay second (`edit_message` then
  `cancel_message(final_text=…)`, falling back to the one-argument cancel for a
  relay client that predates 46-01). The token is the one the question was sent
  with: the index entry's role, else the role whose queue file holds the entry.
  A relay failure is a result (`relay_cancelled: false`), and a second call
  retries only the relay.
- `questions_mcp_lib.ask` — both writes (`create_entry`, `mark_dispatched`) go
  through `questions_git.run`; `id=` for a claimed id; a 422
  `invalid nudge_schedule` is retried once without the ladder and reported under
  `warnings` (D10). `sync` and `warnings` appear in the result only when there is
  something to say — a `sync = "off"` workspace gets the result shape it always
  had.
- `questions-mcp/server.py` — tools `withdraw` and `claim`; `ask` gains `id`.
  The docstrings are the agent's only documentation: they say when *not* to use
  each.
- `shell/claude-questions` — `--withdraw ID --reason TEXT` (`relay:<N>` for an
  ack-notification), `--claim [N] [--note TEXT]` (ids alone on stdout),
  `--sync`; exit 2 when not fully carried out. The overview shows `sync`, a
  pending debt, the ledger and a leftover legacy lock file.
- `install.sh` — `questions_git.py` and `questions_ops.py` in `REQUIRED_HOOKS`,
  `feature_questions_modules` and `MODULE_OWNERS`.

## Tests

`tests/test_unit_questions_ops.py` (fake relay recording calls),
additions to `test_unit_questions_mcp.py` and `test_unit_questions_diagnostics.py`
(the flags driven through `main(argv)`).

## Done when

- A session and a human at a shell withdraw and claim through the same function.
- `ask` against a relay that rejects the ladder sends the question and says so.
