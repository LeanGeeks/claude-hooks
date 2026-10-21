# Task 43 — The own-memory write root applies in every mode

**Status:** todo · **Type:** follow-up · **Created:** 2026-10-21 · **Rev:** 1
**Priority:** low. The change is `ask` → `allow` for one directory, and that
directory is already allowed in deferring modes.
**Suggested worker:** one implement → review loop. The code change is one line;
most of the work is the tests.
**Scope:** `.claude/hooks/pretool_hook.py` (`compute_write_roots`),
`tests/test_unit_write_roots.py`, epic 41 brd H5 / D-text if it names the mode
restriction, this file.
**Read first:** [epic 41 brd](./41_redirect_gate_parity/brd.md) H5 and §2 ·
[41 state.md](./41_redirect_gate_parity/state.md) invariants 4 and 5
**Origin:** epic 41-04's H5 live check, 2026-10-21.

## Finding

On 2026-10-21 a live check ran a `default`-mode session in
`/data/sync/work/leangeeks-ai/ai-playground-2` (installed hooks at
`8539904`, Claude Code 2.1.306). The session used the Write tool to create
`~/.claude/projects/-data-sync-work-leangeeks-ai-ai-playground-2/memory/live-check-h5.md`.
Claude Code wrote it **without a prompt**: the transcript shows "Wrote 1
memory", and no PermissionRequest row exists for that session.

41-02 adds the session's own memory directory to the write roots only in
`DEFERRING_MODES` (`pretool_hook.py:764`, `if mode in DEFERRING_MODES and
settings_root:`), pending exactly this check. So in `default` mode today,
`echo x >> <own memory>/MEMORY.md` still raises a redirect-gate ask (and a
Telegram card), even though Claude Code writes the same file through Write
without asking. Invariant 5 says Telegram mirrors the terminal, so the gate
should not be stricter than Claude Code here.

## Change

Drop the mode condition on the `_memory_roots` call, and update the docstring's
item 3. Keep everything else as it is: user-settings-only `autoMemoryDirectory`
/ `autoMemoryEnabled`, the worktree registration + key self-check, sibling
memory directories never a root.

Check one thing before landing: whether Claude Code writes memory in `plan`
mode. If it does not, keep `plan` excluded rather than allowing more than the
terminal does.

## Acceptance

- `default` / `acceptEdits`: `echo x >> <own memory>/MEMORY.md` → `allow`.
  `>> <sibling project memory>/MEMORY.md` and `>> ~/.claude/settings.json` still
  ask.
- The existing deferring-mode tests are unchanged. A mutation that restores the
  mode condition fails a test.
- Full suite green with counts, following the cp-backup mutation protocol
  (41 state.md invariant 6).
