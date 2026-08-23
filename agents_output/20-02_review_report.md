# Review Report - 20-02 Provider-aware launcher

## Verdict: PASS

The implementation successfully introduces `--provider codex` support while maintaining byte-for-byte Claude compatibility and respecting the ownership boundary. All critical constraints are satisfied, tests are comprehensive and pass, and the rollback logic is sound.

## Completeness Check

### Work Items (from task 20-02)
1. **✅ MET** - `--provider claude|codex` validated, defaults claude (.claude/bin/amux-spawn:93-102, test_provider_validation)
2. **✅ MET** - Provider and bounded-exec mode passed through amux's public options (.claude/bin/amux-spawn:496-573, test_codex_argv_uses_only_public_amux_options)
3. **✅ MET** - Collision-safe mode-0600 artifacts (.claude/hooks/amux_spawn_lib.py:710-756, test_umask_cannot_loosen_the_mode)
4. **✅ MET** - Provider-aware handle under existing spawn lock (.claude/bin/amux-spawn:324-429, test_handle_records_provider_and_artifacts)
5. **✅ MET** - YOLO delegated, never synthesized (.claude/bin/amux-spawn:296-311, test_codex_argv_never_contains_a_provider_command_or_yolo_expansion)
6. **✅ MET** - Multiline prompts preserved (test_multiline_prompt_boundary, adversarial prompt test)
7. **✅ MET** - Clean rollback on partial launch failure (.claude/bin/amux-spawn:605-634, test_*_rolls_everything_back)

### Done When Criteria (from task 20-02)
1. **✅ MET** - Unit tests assert exact amux argv (test_claude_argv_is_unchanged, test_codex_argv_uses_only_public_amux_options)
2. **✅ MET** - Existing fixtures byte-for-byte equivalent (test_claude_argv_is_unchanged pins exact argv)
3. **✅ MET** - No Claude transcript guess or fake UUID (test_no_transcript_guess_and_no_minted_thread_id)
4. **✅ MET** - Stub JSONL stream populates thread ID (test_stub_event_stream_populates_the_thread_id_after_launch)
5. **✅ MET** - Concurrent naming/cap tests green (test_codex_spawn_counts_against_the_workspace_cap, test_second_codex_spawn_gets_its_own_name_and_artifacts)

## Ownership boundary audit

Grep results for Codex argv fragments:
- `codex exec` - Only in comments/docs, never in production code construction
- `--json`, `-o`, `-C`, `--cd`, `--profile`, `resume`, `--sandbox` - Only in comments/docs, forbidden in test assertions
- `--dangerously-bypass-approvals-and-sandbox` - Only in help text/comments, never constructed
- `--dangerously-skip-permissions` - Only in Claude-only flag refusal and help text

**✅ CONFIRMED**: No Codex argv is constructed in this repository. The only executable invoked is `amux` itself. YOLO is fully delegated as `--yolo` for both providers.

Production code paths:
- `.claude/bin/amux-spawn:496-573` - `_amux_create_detached` builds only `amux ...` argv
- `.claude/bin/amux-spawn:296-311` - YOLO forwarded verbatim, never expanded
- `.claude/bin/amux-spawn:82-90, 246-252` - Claude permission flags refused for Codex

## Claude compatibility evidence

**✅ VERIFIED**: The no-`--provider` path produces identical behavior:
- `test_claude_argv_is_unchanged` pins exact argv structure element-for-element
- Claude argv: `["amux", "exec", <name>, "--no-attach", "--no-default-model", "--dir", <dir>, "--session-id", <uuid>, <prompt>]`
- All pre-existing fixtures still pass (805 passed / 0 failed / 1 skipped)
- No drift in defaults, lock acquisition, handle field order, or artifact behavior

Byte-for-byte equivalence is proven by:
1. Exact argv structure assertions (not string rendering)
2. All 64 pre-existing unit_amux_spawn tests still pass
3. No changes to Claude path in `_amux_create_detached` except conditional branching
4. Session ID minting logic unchanged for Claude (line 327)

## Restart-seam audit

**✅ CLEAN** - No duplicated/orphaned/half-wired code found:
- Functions are defined once, no duplicates detected
- No orphaned helpers - all new functions have callers
- Handle schema updated consistently across `new_handle`, `write_handle`, readers
- Test module extended cleanly, no mid-class appends
- Consistent naming between CLI (`--provider`) and lib (`PROVIDERS`, `PROVIDER_CLAUDE`, `PROVIDER_CODEX`)

The +327/+127/+683 line diffs show clean addition, not duplication artifacts.

## Rollback analysis

**✅ COMPREHENSIVE** - All failure points covered:

Failure points enumerated (`.claude/bin/amux-spawn:605-634`):
1. **amux exec non-zero** - Lines 385-394, rollback with `handle_written=False`
2. **Launch not confirmed** - Lines 396-408, rollback with `handle_written=False`
3. **Handle write failure** - Lines 410-429, rollback with `handle_written=True`

Cleanup order (reverse of creation):
1. Handle first (so readers don't see tracked session pointing to destroyed artifacts)
2. `amux rm` (tmux kill-session, never SIGKILL)
3. Artifacts last

Tests forced at realistic points:
- `test_amux_create_failure_rolls_everything_back` - Simulates amux exec failure
- `test_launch_not_confirmed_rolls_everything_back` - Simulates session not starting
- `test_handle_write_failure_rolls_everything_back` - Simulates handle write failure

Each test asserts:
- rc=1 (failure)
- `amux rm` was called
- No handle files remain
- No artifact files remain
- No live session

**✅ NO HOLES** - The classic failure-between-session-creation-and-handle-write is covered by the `handle_written=False` rollback.

## --model finding assessment

**✅ CONFIRMED PRE-EXISTING** - The `--model X` space form issue is epic-10 behavior:
- Verified present in current Claude path (test_space_form_model_keeps_the_pre_existing_cli_behavior)
- argparse optional `suffix` positional eats the value
- `--model=value` form round-trips correctly for BOTH providers (test_explicit_codex_model_is_forwarded_verbatim)

**For 20-04 to know**: This is not a Codex-specific issue. It affects both providers identically and has existed since epic-10. Any fix would be a CLI ergonomics improvement, not a Codex integration issue.

## Independent verification

**✅ ALL VERIFIED**:
- **Compile/import**: PASS - `python3 -m py_compile` succeeds on all changed files
- **Full suite**: 805 passed, 0 failed, 1 skipped (baseline 762 + 43 new, not 35 as claimed)
- **Test isolation**: CONFIRMED - All tests use `tempfile.TemporaryDirectory()`, mock tmux, fake amux via `_redirect_amux_home()`, and capture argv via `fake_run` - no real `~/.amux` or tmux server touched
- **Live codex usage**: CONFIRMED ABSENT - 0 live Codex turns, `--dangerously-bypass-approvals-and-sandbox` never passed or run, only reducer fixtures and stub codex used

## Issues Found

### Issue 1: [severity: LOW]
- **File:** agents_output/20-02_implementation_report.md:179
- **Problem:** Test count discrepancy - report claims 797 passed with +35 new tests, but actual count is 805 passed with +43 new tests (805 - 762 baseline = 43)
- **Fix:** Update report to reflect accurate test count

### Issue 2: [severity: LOW]
- **File:** agents_output/20-02_implementation_report.md:35
- **Problem:** Report claims "737 added / 0 deleted lines" in tests, but git diff shows +737 lines which is the net change
- **Fix:** Clarify this is net change or show actual added/modified/deleted breakdown

## Test quality assessment

**HIGH QUALITY** - All new tests are substantive and would survive a broken implementation:

Strong tests that would catch broken implementations:
- `test_codex_argv_never_contains_a_provider_command_or_yolo_expansion` - Would catch any direct Codex construction
- `test_codex_argv_uses_only_public_amux_options` - Would catch wrong option passing
- `test_no_transcript_guess_and_no_minted_thread_id` - Would catch fake UUID creation
- `test_stub_event_stream_populates_the_thread_id_after_launch` - Would catch wrong thread ID handling
- `test_umask_cannot_loosen_the_mode` - Would catch mode enforcement failures
- `test_allocation_is_exclusive_and_never_reuses_a_stem` - Would catch collision issues
- All three rollback tests - Would catch incomplete cleanup
- Adversarial prompt test - Would catch prompt boundary violations

No vacuous tests found. Each test asserts specific, verifiable behavior.

## Questions for User

None. The implementation is sound and ready for 20-03.