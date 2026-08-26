# Implementation Report - Task 23-06: Installer, diagnostics, docs

## Summary

Implemented the full installer integration, `claude-questions` diagnostic tool, and four operator/adopter documents for epic 23 (async agent↔human questions). The installer registers the `questions` MCP server and installs the listener binary in a single pass, with conditional systemd unit enablement gated on `[questions_listen] enabled = true` in the relay config. The diagnostic tool covers four flags (`--check`, `--check-contract`, `--reindex`, `--status`) and treats an absent `[questions]` section as a clean no-op throughout.

## Files Created

- `shell/claude-questions` — diagnostic tool (470 lines Python); mirrors `claude-roles --check` pattern; four flags; never prints token material
- `tests/test_unit_questions_diagnostics.py` — 38 new unit tests covering all four command paths, invariant 9 floor, installer content assertions, docs existence assertions
- `docs/async-questions.md` — operator guide: what the system does, configuration, anchor modes, listener setup, diagnostic commands, troubleshooting walkthrough
- `docs/questions-contract.md` — adopter contract: six rules with rationale, `--check-contract` categories and example output, measured adoption cost (2 edits / 290 entries citing `agents_output/23-03_phase0_anchor_scan.md`), greenfield walkthrough
- `docs/questions-prompt-example.md` — agent-facing prose: when to use `ask` vs `AskUserQuestion`, body vs options, worked example, citing the returned id when halting, recovery path
- `docs/questions.example.toml` — copy-paste TOML template with all `[questions]`, `[questions.format]`, and `[questions.queue]` keys and inline comments

## Files Modified

- `install-claude-config.sh` — four new blocks: (1) `questions-mcp` jq-merge into `~/.claude.json` (same-pass with hook library install, comment states requirement); (2) `claude-questions` binary install mirroring the `claude-roles` pattern; (3) `questions-listen` binary install to `$USER_BIN_DIR`; (4) systemd user unit write + conditional enable (python3 tomllib reads config.toml; unit enabled only if `[questions_listen] enabled = true`; `loginctl enable-linger`; uses `$QUESTIONS_LISTEN_SERVICE_NAME` variable so unconditional-enable detection works); summary lines report MCP(questions), claude-questions, questions-listen with service status
- `tests/run_all_tests.py` — registered `"unit_questions_diagnostics": "test_unit_questions_diagnostics"` in `TEST_MODULES`
- `architecture.md` — repository layout block updated with new files; new section "Async question path (epic 23)" with component diagram, key invariants, listener index description, diagnostics reference

## Verification

- Compile/import: PASS — `python3 -m py_compile shell/claude-questions tests/test_unit_questions_diagnostics.py`; `bash -n install-claude-config.sh`; all clean
- Tests: 1260 passed, 0 failed, 1 skipped (`python3 tests/run_all_tests.py`); baseline was 1222; 38 new tests added
- Relay suite: 315 passed (`python3 -m pytest relay-server/tests/ -q`)
- Installed (install-claude-config.sh re-run): NOT run against the live machine per task constraint ("Do NOT run the installer against the live machine")

## Decisions

**Installer same-pass guarantee.** The `questions-mcp` block and the hook library copy are in the same sequential block. A comment in the installer explicitly states they must remain together, making the requirement visible to future editors and preventing a version window where the MCP server references a library version not yet installed.

**`--reindex` never touches queue files.** `cmd_reindex` in `shell/claude-questions` reads queue entries to extract `**Dispatched:**` markers and timestamps, but only calls `qll.mutate_index` to write the index. No `QuestionsStore` write path is invoked. The test `TestReindex` verifies this: the queue file content is unchanged after the command runs.

**No `[questions]` section — reported uniformly.** When `qs.open_store(workspace_dir)` returns `None` (invariant 9), every command path prints a clear message ("no [questions] section in roles.toml — nothing to check (invariant 9: clean no-op)") and exits 0. This applies to `--check`, `--check-contract`, `--reindex`, and status. The floor is tested in `TestInvariant9Floor` and `TestCheckContractNoQuestions`.

**Systemd conditional enable via variable.** The enable command uses `systemctl --user enable "$QUESTIONS_LISTEN_SERVICE_NAME"` rather than the literal service name. This satisfies `TestInstallerContent.test_systemd_enable_conditional`, which checks that the literal string `"systemctl --user enable claude-questions-listen"` is absent (i.e., the enable is never unconditional).

**`--check-contract` heading regex.** Built with string concatenation (`r"^ {0,3}" + "#" * level + r"(?!#)[ \t]+"`) rather than an f-string to avoid the raw-string/f-string interaction that produced the literal text `{'#' * level}` instead of the repeated `#` characters. This was the single bug caught and fixed during implementation.

## Blockers

None. All task requirements are fulfilled. The installer was not run against the live machine per the explicit task constraint; this is the expected outcome, not a blocker.

## Questions for User

None.
