# Epic 40 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, tests and done criteria. This file owns **ordering**, the **shared
edit points**, and the **cross-task invariants**.

**No Phase 0.** The one cross-cutting decision — *which modes get `defer`* — is
locked in [40-01](./40-01-pretool-defers-to-the-mode_sonnet.md) §2 and derives
directly from brd §1.4/§1.5. The one genuinely open *question* (does a subagent's
payload carry `permission_mode`? brd H8) is not a blocker: the conservative
default already handles "absent", and [40-04](./40-04-live-verification_human.md)
measures it.

**Numbering.** Highest existing epic is 39; this is 40. Nothing here depends on
22–25 (reserved by a remote machine).

**No epic `architecture.md`.** The manager prompt says to read one; this epic
does not have one and does not need one — it changes no structure, only which of
four existing decision values one hook prints. The repo-root `architecture.md`
is the relevant architecture document, and 40-02 updates the two places in it
that describe this hook.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 40-01 | [Pretool defers to the mode](./40-01-pretool-defers-to-the-mode_sonnet.md) | todo | — | **The fix.** One emission site, one additive field, one log-path correction. Ship this even if nothing else lands. |
| 40-02 | [The card names the asker](./40-02-the-card-names-the-asker.md) | todo | 40-01 | Records `permission_mode` on the row, stops the card claiming the allowlist raised a prompt it did not raise (brd H4), updates the docs. |
| 40-03 | [YOLO on the native mode](./40-03-yolo-native-mode.md) | **optional** | 40-01 | Decision-gated. **Ask the operator the §3 question before running it** — it is one-way from Telegram. |
| 40-04 | [Live verification](./40-04-live-verification_human.md) | todo | 40-01 + 40-02 installed | **human** — six modes, and it answers brd H8. |

## Dependency graph

```
40-01 ──► 40-02 ──► install ──► 40-04 (human)
   └────► 40-03 (optional, operator decision first)
```

40-01 and 40-02 are **sequential, not parallel** — 40-02's card wording only
makes sense against 40-01's behaviour, and both touch the same reasoning about
why a prompt fired.

## Shared edit points

| File | 40-01 | 40-02 | 40-03 |
|---|---|---|---|
| `pretool_hook.py` | emission site (`main()` `:2089-2131`), the ask *kind*, `MANUAL_CONFIRM_LOG` | — | — |
| `permission_state_store.py` | — | `permission_mode` field + `create_request` kwarg | — |
| `permission_request_hook.py` | — | passes `permission_mode`; corrects the bypass-branch comment | the `yolo` action (`:350-364`) |
| `telegram_permission_router.py` | — | `render_permission_body` / `_unallowlisted_bash_parts` wording | — |
| `permissions-mcp/permissions_mcp_lib.py` | — | `_summarize` gains the field (`:437-460`) | — |
| `architecture.md` (repo root) | — | hook table `:112`, flow diagram `:333` | — |

Nothing is edited by two tasks at once. If 40-03 is skipped, no file loses an
edit it needed.

## Recommended order

1. **40-01.** It is the whole fix and it is small. Everything else is
   consequence management.
2. **40-02.** Without it the card keeps giving the old reason for a new kind of
   prompt (brd §2.3.3).
3. **`./install.sh --yes`** — nothing is verified until the installed copy is the
   new one (brd H6). `--yes` replays the recorded manifest and is the only form
   an agent should run; **never `--all`**, which installs features the operator
   deliberately skipped. Not `install-claude-config.sh`; that script was deleted
   in epic 29-09.
4. **40-04.** Six modes, one of which answers H8.
5. **40-03 only if the operator says so** (state.md task table marks it
   optional; the manager prompt says to ask rather than run it).

## Testing baseline

Measured on this machine **2026-09-21**, on `main` with a clean tree, before any
epic-40 edit. Report before/after against these, not against a remembered number.

| Command | Result |
|---|---|
| `python3 tests/run_all_tests.py` | **Ran 1941 tests in 335 s — `OK (skipped=2)`**, exit 0 |
| `python3 tests/run_all_tests.py --module integration_pretool` | **379 tests in 100 s**, green — the module 40-01 extends |

Two things worth knowing before you read a failure as yours:

1. **The canonical runner is green.** The six order-dependent failures this repo
   is known for (`TestAgentWrittenDecisions` ×4, `TestEndToEndWithWaitLoop` ×2)
   belong to the **pytest** path — `python3 -m pytest tests/`, measured
   2026-09-07 at 6 failed / 1518 passed / 1 skipped. They pass in isolation
   (re-verified 2026-09-21: `pytest -k TestAgentWrittenDecisions` → 7 passed).
   `run_all_tests.py` does not hit them. Use the runner; reach for pytest only
   to isolate a single class, and do not chase those six as part of this epic.
2. **Account for the two skips by name.** A skip count that moves is a test that
   stopped running — this repo's characteristic way of shipping a green suite
   that proves nothing (invariant 8).

The full run takes about six minutes, which fits an ordinary agent tool timeout;
use `--module` while iterating anyway, because a 100-second loop is a different
way to work than a six-minute one.

## Cross-task invariants

1. **Deny is unconditional.** A PreToolUse `deny` outranks every mode, including
   `bypassPermissions`. No task may make the deny path mode-aware.
2. **The validator's vocabulary is frozen.** `validate_bash_command` returns
   `allow` / `deny` / `ask` and nothing else — `permissions_mcp_lib.py:352`
   raises on a fourth value and the pretool suite pins all three (brd H2). The
   mode translation lives in `main()`, at the point of printing.
3. **Absent or unrecognised `permission_mode` means `default`, means `ask`.**
   Fail conservative, fail open (brd H1, H8).
4. **Risk gates keep asking in every mode.** A `permissions.ask` match and a
   write redirect escaping the workspace/`/tmp`/`/dev/null` still emit `ask`.
   Only the *unknown* case is deferred.
5. **Telegram mirrors the terminal.** Never forward what the terminal would not
   have prompted for; never suppress what it would. This is the tie-breaker for
   any question these files do not answer (brd §1.4).
6. **`AskUserQuestion` is forwarded in every mode.** It asks the operator a
   question rather than a permission; nothing in this epic touches that branch
   (`permission_request_hook.py:1594-1606`).
7. **A repo edit is not live** until `./install.sh --yes` has run. Say which copy
   you tested (brd H6).
8. **A green suite is not evidence on its own.** This repo's recurring failure
   mode is a test that accommodates the implementation instead of asserting the
   requirement. For the load-bearing cases — 40-01's case 1 (no
   `permission_mode` still asks) and case 3 (auto defers), 40-02's case 3
   (default-mode card unchanged) — require a **mutation proof**: copy the file
   aside with `cp`, break the behaviour, confirm the test fails, restore from
   the copy. **Never restore with `git checkout`** — it reverts to HEAD, not to
   the pre-mutation state, and destroys uncommitted work. And if a requirement
   is awkward to test, say so in the report rather than quietly weakening it
   into something testable.

## Log

- **2026-09-21 — epic created.** From the operator's report that permission
  forwarding predates Auto mode and defeats it. Diagnosis in brd §1: the
  `hookAskFloor` mechanism, measured at 69 of 86 auto-mode requests on this
  machine between 2026-09-14 and 2026-09-21. Two proposals were **rejected**
  during design and are recorded in brd §2.2 so they are not re-proposed: a
  `PermissionDenied` hook forwarding classifier denials to Telegram (a denial
  asks the terminal operator nothing, so it must not ring a phone), and deleting
  the `bypassPermissions` branch (it still catches risk-gate asks in a
  no-prompt session). No code was written; this is a spec only.
- **2026-09-21 — review pass before handover; fourteen corrections, no scope
  change.** Two were factual errors in 40-04 that would have produced a false
  failure: `bypassPermissions`/P3 was written as "prompt + card" when the bypass
  branch auto-allows *before* any Telegram send, and the `plan` row claimed bash
  is not reached in plan mode. Two probes were rewritten because a verification
  command whose failure mode is a wiped disk (`> /etc/passwd`,
  `dd …of=/dev/sda`) is not a probe — `dd --version` matches the same
  prefix-based `Bash(dd:*)` deny rule and costs nothing when the guard does not
  fire. Two were internal contradictions in 40-02: a card block added in every
  mode versus its own byte-identical assertion, and a claimed MCP benefit that
  `_summarize` (`permissions_mcp_lib.py:437-460`) would not have delivered. One
  was an ordering gap in 40-01 — `log_manual_confirmation` is called before the
  payload exists, so `emitted` could not have been logged where the call sits.
  The rest: `install.sh --yes` everywhere (the implementer prompt forbids
  `--all`), a `Monitor` case, 40-03's agent line, the missing-architecture.md
  note, invariant 8 (mutation proof), and the Testing baseline above — measured
  rather than remembered, which corrected the standing belief that a full run
  carries six failures: it does not, they are pytest-path only.
