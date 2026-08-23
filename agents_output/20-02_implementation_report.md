# Implementation Report - 20-02 Provider-aware launcher

## Summary

`amux-spawn spawn --provider codex` now creates a tracked, bounded Codex worker
entirely through amux's public option surface (`--provider`, `--agent-mode`,
`--event-log`, `--output-last-message`, `--yolo` verbatim), allocates
collision-safe mode-0600 event/result artifacts under the existing
`~/.amux/spawn/` registry, writes the provider-aware handle under the existing
spawn lock with no fake identity, and rolls back handle/artifacts/session on
any partial launch failure. Every existing no-`--provider` call is byte-for-
byte unchanged (verified by exact-argv assertions plus all pre-existing
fixtures passing unmodified).

## Files Created / Modified

- `/data/sync/work/leangeeks-ai/claude-hooks/.claude/bin/amux-spawn` —
  `--provider` parsing/validation (`resolve_provider`, line 93), Codex
  combination refusals (prompt required; `--profile`/`--wait`/`--notify`/
  Claude permission flags refused, lines 218–252), provider-aware
  `_amux_create_detached` (line 496), bounded-launch confirmation
  `_launch_confirmed` (line 582), `_rollback_launch` (line 605), Codex handle
  construction (lines 412–429), and the Codex report block. Claude defaults
  reproduce the epic-10 argv verbatim.
- `/data/sync/work/leangeeks-ai/claude-hooks/.claude/hooks/amux_spawn_lib.py` —
  artifact allocator: `codex_artifact_paths` (689), `codex_artifact_siblings`
  (702), `allocate_codex_artifacts` (710, `O_CREAT|O_EXCL` + chmod 0600),
  `remove_codex_artifacts` (758); `new_handle` now accepts `session_id=None` /
  `transcript_path=None` for the Codex no-identity rule.
- `/data/sync/work/leangeeks-ai/claude-hooks/tests/test_unit_amux_spawn.py` —
  43 new hermetic tests (pure addition; +737 net lines on the file, no
  existing test touched): provider validation, exact amux argv for both
  providers, adversarial prompt boundary, artifact allocation/mode/collision,
  handle contents, stub-stream thread-id population, three forced-failure
  rollback cases, cap + naming.
- `tasks/20_codex_background_workers/state.md` — was already `in_progress`
  before this task started (epic manager's edit, present in the initial git
  status); not modified by me.

No installer change is needed: both changed files are existing deployed paths
(`install-claude-config.sh` copies them; it must be re-run before live use).

## amux invocation contract

Claude (unchanged, asserted exactly in
`test_claude_argv_is_unchanged`):

```
["amux", "exec", <name>, "--no-attach", "--no-default-model", "--dir", <dir>,
 "--session-id", <uuid>, <forward_flags>..., <prompt>]
```

Codex bounded worker (asserted exactly in
`test_codex_argv_uses_only_public_amux_options`):

```
["amux", "exec", <name>, "--no-attach",
 "--provider", "codex", "--agent-mode", "exec", "--dir", <dir>,
 "--event-log", <E>, "--output-last-message", <R>,
 <forward_flags>..., "--", <prompt>]
```

Mapping to amux's documented public options at the pinned revision
(`docs/codex-provider.md`):

- "`CC_PROVIDER` | `claude`, `codex`" / "`CC_AGENT_MODE` | `interactive`,
  `exec`" — amux persists these from `--provider` / `--agent-mode` (§2); we
  pass both, so the session's provider identity survives restart/resume.
- "bounded worker: `amux register w --provider codex --agent-mode exec --yolo
  --dir … --event-log /abs/path/events.jsonl --output-last-message
  /abs/path/result.md`" (§1) — the same options reach `amux exec`, which
  registers + starts in one step (`cmd_exec` → `write_session_env` →
  `cmd_start`).
- "Parent directories must already exist — amux validates that both paths are
  absolute and their directories exist" (§4) — satisfied by allocating under
  the already-existing `~/.amux/spawn/`.
- "All four are created with mode `0600` (`umask 077`) before the pane starts"
  (§4) — we pre-create `E`/`R` ourselves at 0600; amux creates `E.err` and
  writes `E.rc` under its own 077 umask (verified 0600 in the integration
  check).
- `amux exec`'s usage line: `[--provider claude|codex] [--agent-mode
  interactive|exec] …` — both options are on `exec`'s public surface; amux's
  `cmd_start` then refuses per-start `--provider`/`--event-log` overrides,
  which is why they are passed at exec/register time only.
- `--no-attach` (E5) on both forms; `--no-default-model`/`--session-id` are
  Claude-only at this revision (amux dies if combined with another provider),
  so the Codex form carries neither.

The prompt rides as ONE argv element after amux's documented `--` separator
("`amux start w -- "read the failing test and fix it"`", §1); amux's
`cmd_start` drops exactly one leading `--` for an exec-mode session and its
`build_codex_exec_command` appends it as the single trailing positional.

## Ownership boundary

No Codex argv is constructed anywhere in this repository:

- `.claude/bin/amux-spawn:496–553` — `_amux_create_detached` builds only the
  `amux …` argv. The Codex branch emits exactly amux's own options
  (`--provider`, `--agent-mode`, `--dir`, `--event-log`,
  `--output-last-message`), then caller flags, then `--` + prompt. There is no
  `codex`, `--json`, `-o`, `-C`, or `resume` token anywhere in this file's
  construction path.
- `.claude/bin/amux-spawn:82–90, 246–252` — `--dangerously-skip-permissions`
  (and `--allow-dangerously-skip-permissions`) are refused outright for
  `--provider codex` with a message pointing at `--yolo`; nothing ever
  forwards Claude's permission flag to Codex.
- YOLO is delegated: `.claude/bin/amux-spawn:296–311` forwards `--yolo` as the
  provider-neutral alias for both providers and never spells either expansion.
  The mapping is amux's and pinned there by
  `amux/tests/test_provider_command_builder.py::test_yolo_mapping_matches_server`
  and `::test_codex_yolo_flag_is_the_official_bypass_flag` /
  `::test_neither_side_maps_codex_to_the_claude_permission_flag` at the pinned
  revision (not duplicated here).
- Proof by execution (integration check, pinned amux + stub codex): the stub
  recorded `["exec", "--json", "-C", <dir>, "-o", <result>, <prompt>]` — the
  entire `codex` command line was built by amux; our argv contained none of it.
- Model policy: no model is injected, and a Claude parent's tier is NOT
  inherited into a Codex spawn (`.claude/bin/amux-spawn:312–318`); an explicit
  caller model rides along verbatim.

## Artifact allocation

`.claude/hooks/amux_spawn_lib.py:675–773`:

- Scheme: `~/.amux/spawn/<name>.events.jsonl` + `~/.amux/spawn/<name>.result.md`
  (amux adds `.events.jsonl.err` / `.events.jsonl.rc`). Keyed by session name
  inside the existing registry (architecture §3); the `.jsonl`/`.md` suffixes
  cannot collide with the `*.json` handle glob used by `live_tracked_count` /
  `list_handles` (asserted by `test_artifacts_do_not_collide_with_the_handle_glob`).
- Collision safety: the whole four-file set must be absent for a stem — amux
  APPENDS to the event log, so a stale stem would mix two runs' evidence; on
  collision the next sequence `<name>.2.events.jsonl` is tried
  (`test_allocation_is_exclusive_and_never_reuses_a_stem`,
  `test_a_stale_amux_sibling_also_forces_a_new_stem`). Files are created with
  `O_CREAT | O_EXCL`, so a concurrent racer cannot win the same stem.
- Mode: `ARTIFACT_MODE = 0o600`, enforced with an explicit `chmod` after
  `os.open` because open's mode is umask-masked — proven under `umask 0` by
  `test_umask_cannot_loosen_the_mode`.
- Atomicity: `E`/`R` are created empty-and-exclusive before launch (never
  appended by us); the handle recording the allocated paths is written via the
  existing atomic tmp+`os.replace` `write_handle`. JSONL stays append-only
  (amux owns all writes).
- Allocation happens under the same global spawn lock as the name pick, so
  (name, artifacts) is claimed atomically (`.claude/bin/amux-spawn:356–367`).

## Rollback

Failure points handled (`.claude/bin/amux-spawn:605–634`):

1. `amux exec` non-zero (session may be half-registered, `.env` written).
2. launch not confirmed (`_launch_confirmed` false: no tmux session AND no
   `.rc` AND no event bytes — for Codex, a finished-fast run counts as
   confirmed because completion evidence beats liveness, architecture §5).
3. handle write failure (disk full etc.).

Cleanup ordering (reverse of creation): handle first (so no reader sees a
tracked session about to be destroyed) → `amux rm` (tmux kill-session, never
SIGKILL, plus registration + `meta.json`) → artifacts last (nothing can still
write them). Fail-soft throughout. Claude launches keep their exact epic-10
failure behavior — `_rollback_launch` returns immediately for
`provider != codex` (`test_rollback_never_touches_a_claude_launch`).

Tests: `test_amux_create_failure_rolls_everything_back`,
`test_launch_not_confirmed_rolls_everything_back`,
`test_handle_write_failure_rolls_everything_back` — each asserts rc=1, the
`amux rm` call happened, the spawn registry contains nothing (no handle, no
artifacts), and no live session remains. Additionally proven end-to-end
against the real pinned amux with a tmux `new-session` failure injected after
`amux exec` had already written the `.env`: rc=1, zero leftovers in
`~/.amux/spawn` and zero `myproj*` files in `~/.amux/sessions`.

## Verification

- Compile/import: **PASS** (`python3 -m py_compile` on `.claude/bin/amux-spawn`,
  `.claude/hooks/amux_spawn_lib.py`, `.claude/hooks/codex_event_reducer.py`,
  `tests/test_unit_amux_spawn.py`; clean import of the CLI as a module).
- Full suite: **805 passed, 0 failed, 1 skipped**
  (reviewer-verified count; the implementer's run reported 797 from before the
  final test additions landed — correction recorded after review). The skip is
  the pre-existing `AMUX_SPAWN_LIVE_TEST` live test. Baseline was 762/0/1; the
  +43 are this task's new tests.
- Baseline suites (`python3 -m unittest tests/test_<m>` from `tests/`):
  - unit_amux_spawn: 64 OK (1 pre-existing skip)
  - unit_spawn_producer: 18 OK
  - unit_codex_reducer: 42 OK
  - unit_amux_reads: 30 OK
  - unit_amux_supervise: 21 OK
  - unit_amux_rm: 22 OK
- Argv-level assertions: `cli.subprocess.run` is replaced by a recorder, so
  tests assert the literal `list` handed to the real `amux` binary — never a
  rendered string (`TestAmuxArgvBothProviders` asserts both providers' full
  argv element-for-element, including `--session-id <uuid>` on Claude and the
  `--` separator position on Codex). Cross-checked end-to-end against the
  pinned amux (stub codex recorded the final `codex exec` argv amux built).
- Adversarial prompt:
  `review 'this' "thing" $HOME $(touch <sentinel>) `id` *.py ; echo pwned\nsecond line\twith a tab`
  — arrives at amux as ONE argv element for BOTH providers (newline, quotes,
  `$HOME`, backticks, glob, `;` all intact; `call.count(prompt) == 1`), and the
  `$(touch …)` sentinel file was never created (nothing executed). The stub
  codex run through real amux + a real pane shell received the identical string
  as its last argv element.
- Live codex turns run: **0**. Every Codex path in tests uses the vendored
  reducer fixtures or the sibling epic's stub `codex` script (fixture-event
  replayer) on a private tmux socket with isolated `HOME`/`CC_HOME`. The
  developer's real `~/.claude`, `~/.amux`, and real tmux server were never
  touched; `--dangerously-bypass-approvals-and-sandbox` was never passed or
  run (the integration check ran without `--yolo`).

## Decisions

- **Codex is always tracked + detached, even at a TTY** — a bounded worker has
  no TUI to attach to; amux itself never attaches an exec-mode session. The
  plain-human path stays Claude-only.
- **`--provider codex` requires a prompt** — amux dies with "a bounded Codex
  run needs a prompt" otherwise; refusing earlier in amux-spawn gives the
  actionable message before anything is created (BRD §4.1).
- **`--wait`/`--notify` refused for Codex (rc 1, actionable message)** — Codex
  status derivation is task 20-03; forwarding `--wait` today would poll a
  Claude-transcript-shaped derivation. Same for `--profile` (a Claude
  profiles.toml concept).
- **Claude→Codex `--model` inheritance suppressed** — model tiers are
  provider-specific vocabularies; BRD §3 requires Codex's own config to decide
  when no explicit model is given (asserted in
  `test_codex_forwards_explicit_model_but_inherits_none`).
- **`--model X` (space form) left as-is** — argparse's optional `suffix`
  positional eats the value and forwards a bare `--model`; this is pre-existing
  epic-10 CLI behavior, identical on the Claude path today (amux then dies with
  a usage error and, for Codex, our rollback cleans up). Fixing it would change
  existing Claude behavior, which the byte-for-byte constraint forbids; the
  `--model=value` form round-trips verbatim (both pinned by test).
- **`.rc`/event-byte fallback in `_launch_confirmed`** — a bounded run may
  legitimately finish before the liveness check, so completion evidence beats
  liveness (architecture §5) rather than reporting a false launch failure.
- **Handle-written rollback uses `handle_written=True`** — `write_handle` is
  atomic (tmp+rename), so either the file exists (deleted, idempotent) or it
  never did; treating it as written is the safe direction.
- **run_id is still minted for Codex** — it is our workflow-chain id (not an
  agent identity), so inheritance semantics are unchanged.

## Blockers

None.

## Questions for User

None.
