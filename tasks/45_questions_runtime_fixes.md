# Task 45 — Async questions: the installed system did not run

**Status:** done · **Type:** bugfix · **Created:** 2026-11-15 · **Rev:** 1
**Scope:** `questions-mcp/server.py`, `questions-mcp/questions_mcp_lib.py`,
`.claude/hooks/questions_listen_lib.py`, `.claude/bin/questions-listen`,
`install.sh` (the systemd seam, the listener unit), `shell/claude-questions`,
`docs/async-questions.md`, `docs/installer.md`, tests.

## Why

`hyppie-flow` was the first workspace to adopt epic 23 (2026-11-15). Its first
`notify` was refused and its listener was not running. Neither was a fault of
that workspace: epic 23's engineering was complete and its live verification
(23-07) had never been run, so two defects in the *installed* system — invisible
to a suite that imports the library from the checkout — had never been met.

## Findings and fixes

### 1. Every `ask` / `notify` was refused: "installation not bound"

`mcp__questions__notify(text, ack=false)` returned `dispatched: false`,
`Relay refused: installation not bound (relay_server package not importable)`.

- **Cause.** `questions-mcp/server.py` is a `uv run --script` file whose only
  declared dependency was `mcp>=2.0,<3`. `relay_server/client.py` imports
  `httpx`. `mcp` 2.x depends on `httpx2` and no longer installs `httpx`
  (verified: `uv run --with 'mcp>=2.0,<3' python -c 'import httpx'` →
  `ModuleNotFoundError`, mcp 2.3.0). `context-mcp` and `permissions-mcp` share the
  header but never import the relay client, so only this server broke.
- **Why the message lied.** The import fallback set `NotBoundError = Exception`
  and `RelayError = Exception`. `except NotBoundError` therefore caught *every*
  failure, including the lib's own "relay_server package not importable".
- **Fix.** `httpx>=0.26` is declared. The fallback now defines distinct stand-in
  classes (`NotBoundError` a strict subclass of `RelayError`), the import error
  is kept in `RELAY_IMPORT_ERROR`, and a missing client raises
  `RelayUnavailableError` — reported as `Relay client unavailable: … could not
  be imported (ModuleNotFoundError: No module named 'httpx') …`, never as
  something the relay said. One function words all three send sites.
- **Test that would have caught it.** `TestServerDeclaresWhatTheRelayClientImports`
  parses the PEP 723 block and `client.py`'s module-scope imports and fails if a
  third-party import is undeclared. Mutation-checked: removing `httpx` from the
  header fails it.

### 2. The listener exited on start, and the docs said it should not

`questions-listen --status`: `running: no, enabled: no … no [questions_listen]
section — listener disabled`, on a machine whose installer manifest had the
`questions-listen` sub-toggle **on** and whose unit was enabled.

- **Cause.** Epic 29 moved the opt-in from `[questions_listen] enabled` in
  `config.toml` to an installer sub-toggle, migrated the key, and documented it
  as inert. The binary was never changed: it still read the key and returned 0
  when the section was absent. On top of that the sub-toggle ran
  `systemctl --user enable` **without starting** the unit, so even a correctly
  configured machine had a dead listener until its next login.
- **Fix — the docs were right, the code now agrees.**
  - The listener has no "enabled" gate. Started, it runs; it needs a token and a
    `server_url`. `[questions_listen]` is optional tuning (`poll_seconds`,
    `max_attempts`); a leftover `enabled` key produces a config note.
  - `ListenConfig.enabled` now *reports* the sub-toggle — read from the installer
    manifest, falling back to the systemd `wants` link, with no `systemctl` call —
    and is what `--status` prints. `ListenConfig.runnable` is the real predicate.
  - No token / no `server_url` exits **78** (`EX_CONFIG`) instead of 1; the unit
    is `Restart=on-failure` + `RestartPreventExitStatus=78`. With `Restart=always`
    a clean exit (the old "disabled" path, and still "another copy holds the
    lock") restarted every five seconds forever.
  - `./install.sh enable questions-listen` enables **and restarts**; `disable`
    disables and stops; every `--yes` replay restarts an enabled listener so the
    resident process never runs a library the installer has replaced.
  - `systemctl --user` is spelled in exactly one place, `ext_systemd_user`,
    which supplies `XDG_RUNTIME_DIR=/run/user/<uid>` when the shell has none. An
    agent's tool shell has none; the installer was failing with "Failed to
    connect to bus" and reporting the unit as not enabled.

### 3. `--check-contract` reported format examples that were already fenced

It exits 2 for a heading that starts with the id prefix and fails the id parse
(`## Q-NNN — <short title>`), and tells the adopter to "move it to a fenced
block" — without tracking fences itself. The reference workspace had three such
lines, all inside fences. The checker now skips lines inside fenced code blocks
(CommonMark's rule: same fence character, at least as long, nothing after it).
The store is unchanged: it ignores a malformed id wherever it is (rule 6).

### 4. A test that had never run

`test_write_preserves_bytes_outside_the_change` called
`Path.read_text(newline="")`, which exists only from Python 3.13. It errored on
3.11/3.12 — the suite's one standing error — and so never checked CRLF
preservation. It uses `open()` now and passes.

## Not here

- The relay's `nudge_max` rejecting the stock four-rung ladder, and `ask` leaving
  an undispatched entry behind — epic 46 (D10) and task 47.

## Verification

- Suites: see the commit; the relay suite is untouched by this task.
- Live (this machine, after `./install.sh --yes`): recorded in
  `agents_output/questions-setup-and-features-report.md`.
