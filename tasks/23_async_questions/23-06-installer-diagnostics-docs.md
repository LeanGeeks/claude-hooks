# 23-06 — Installer, diagnostics, docs

**Status:** todo · **Depends on:** 23-04, 23-05
**Read first:** [brd.md](./brd.md) §5 · [architecture.md](./architecture.md) §5.2,
§6 · [state.md](./state.md) invariant 9 · `install-claude-config.sh` (MCP
registration and hook install patterns) · `shell/claude-roles` (the diagnostic to
mirror) · `docs/roles.example.toml`, `docs/roles-prompt-example.md`

## Goal

Make the epic installable, inspectable and adoptable by a workspace that has
never seen it.

## Scope

### 1. Installer

- Register `questions-mcp` user-scoped, in place off the checkout, following the
  existing MCP registration pattern.
- Add `questions_store.py` and `questions_listen_lib.py` to `REQUIRED_HOOKS`;
  symlink `questions-listen` into `~/.local/bin/`.
- Install `claude-questions-listen.service` and enable it when the client config
  opts in; `loginctl enable-linger`. Never enable it silently for an installation
  that has not asked.
- Idempotent re-run, like every other installer step.

### 2. Diagnostic — `shell/claude-questions`

Mirrors `claude-roles` (architecture §6): resolved anchor and root, the role→queue
map with each file's existence and open-entry count, the id high-water mark, the
listener's connection state and watermark, pending applies with last error. Never
prints token material. `--check` probes the relay for each distinct token, like
`claude-roles --check`.

This is the tool a human runs when an answer "didn't arrive" — make its output
answer that question directly.

### 3. Docs

- `docs/async-questions.md` — the operator guide: what the two tools do, the
  `[questions]` config with a worked example, the anchor modes and when to pick
  each, how answers travel, what `pending` means, and how to run the listener.
  Include the **adoption walkthrough** for a greenfield workspace: a flat
  `questions.md`, no roles, `dir = …` and nothing else (brd §5 adoption test).
- `docs/questions.example.toml` — copy-paste `[questions]` block, in the shape of
  `docs/roles.example.toml`.
- Agent-facing prose to adapt into `CLAUDE.md`, in the shape of
  `docs/roles-prompt-example.md`: when to prefer `ask` over `AskUserQuestion`,
  what belongs in `body` vs `options`, citing the returned id when halting.
- Top-level `architecture.md`: a new section for the async path, and an update to
  the `Repository layout` block. Keep it to what changes.

## Done when

- A clean install on a machine with no `[questions]` anywhere changes no
  behaviour (invariant 9), and `claude-questions` says so clearly.
- A workspace adopts by adding four lines and gets a working `ask`.
- `claude-questions` diagnoses each of: no config, unbound role, listener down,
  pending applies.

## Tests

Installer idempotency; registration present and correct; the diagnostic against
fixtures for each failure mode; a docs lint that the example TOML parses and its
keys all exist in the loader.
