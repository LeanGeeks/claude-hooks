# 29-01 — Permission hooks: local mode (no Telegram)

**Status:** todo · **Depends on:** — (independent root; no installer work)
**Read first:** [brd.md](./brd.md) §2.2, D9 · [architecture.md](./architecture.md)
§2.1 · [state.md](./state.md) invariants 1, 6 · `.claude/hooks/permission_request_hook.py`
(read the whole file — `main()` starts at `:1504`) ·
`.claude/hooks/telegram_permission_router.py:176-210` (`load_telegram_config`,
the existing disabled-state machinery) · `docs/prompts/implementer.md` §Step 5

> **Installing your change is allowed here, unlike the rest of the epic.** This
> task edits a Python hook, not the installer, so `docs/prompts/implementer.md`
> §Step 5 applies normally: re-run `./install-claude-config.sh` to make the edit
> live and say so in your report. That script is frozen for the epic (29-02 §0)
> and stays safe to run. Do **not** run `install.sh` — that is the one under
> construction ([state.md](./state.md), bootstrapping hazard).
>
> Land the tests first. Installing this change swaps the permission hook under
> the developer's running sessions, and the failure you are fixing lives in the
> auto-allow path.

## Goal

Make `permission_request_hook.py` load and behave correctly on a machine where
the Telegram feature is **not installed**, so that the
`permission-hooks` feature can exist as a toggle independent of `telegram`.

This is a **behaviour fix first and an installer prerequisite second.** Do not
treat it as plumbing — see §2.

## Scope

### 1. The imports are unconditional

`permission_request_hook.py:45` (`import telegram_permission_router as
telegram_router`) and `:64-73` (`from telegram_permission_router import` — eight
names) are hard imports. When `telegram` is not installed, that module is not in
`~/.claude/hooks/` at all (architecture §2.1 `MODULE_OWNERS`), so the hook raises
`ModuleNotFoundError` at import time and the PermissionRequest event fails.

Guard both so the module imports cleanly without the router present. Follow the
existing precedent in the codebase rather than inventing one:
`telegram_permission_router.py:93` already guards `from relay_server.client
import ...` for exactly this reason, and `:53` guards `import roles_config`.

When the router is absent, the hook must behave as if `TELEGRAM_ENABLED` were
`False` — that state is already fully modelled (`load_telegram_config` sets it on
three separate failure paths at `:185-198`), so introduce **no second notion** of
"Telegram is off". One predicate, one meaning.

### 2. The disabled-exit runs before the auto-allow — fix it

`main()` currently does, in this order:

- `:1550-1558` — `if not telegram_router.TELEGRAM_ENABLED:` → log → `sys.exit(0)`
- `:1602-1615` — the YOLO / `bypassPermissions` auto-allow block

So **whenever the relay is unreachable or unconfigured, the yolo and bypass
auto-allow paths never execute.** A session launched with
`--dangerously-skip-permissions` (what `amux-spawn --yolo` expands to) is asked
by Claude Code anyway whenever PreToolUse returns `ask` — the comment at
`:1591-1597` says exactly this — and the block at `:1602` is the only thing that
stops it prompting. With Telegram down, that block is unreachable and the session
prompts for everything.

Fix the ordering so the auto-allow paths are evaluated regardless of relay state.

**Two constraints on how:**

1. The auto-allow block needs the `request` row created just above it
   (`create_request`, ~`:1575`). Do not hoist it above the row creation — the
   audit trail keeps every request, and both paths deliberately record a normal
   `ALLOW` row (`:1585-1587`). Move the *gate*, not the row.
2. The `AskUserQuestion` branch sits **between** the current exit and the
   auto-allow block, and it must keep falling back to the terminal when Telegram
   is off. A naive "move the exit down past the auto-allow" makes
   `AskUserQuestion` attempt a send with no relay. Read the branch structure
   before choosing where the gate goes; a per-send-site gate is likely cleaner
   than one early exit, but that is your call as an implementation choice.

Preserve the existing operator-facing `error_log` text at `:1553-1557` for the
case it actually describes (relay configured but unusable). Do **not** emit it on
a machine that simply never installed the Telegram feature — that message tells
the user to run `relay-client config init`, which is wrong advice there. The two
cases need distinguishable log lines.

### 3. Local mode is "decline to decide", never "fabricate"

With no relay and no yolo/bypass, the hook must exit without a decision so Claude
Code's own terminal prompt runs. That is the current behaviour at `:1558`
(`sys.exit(0)`) and it is correct — epic 23 brd §2.1 records that *no path
fabricates an answer the human never gave*, and that invariant holds here.

### 4. The lazy `roles_config` import

`:1171-1172` imports `roles_config` inside a `try:`. Confirm the `except` catches
`ImportError` (not only a narrower error), because on a `permission-hooks`-only
machine that module is absent too (it is owned by `telegram questions`,
architecture §2.1). Fix if it does not; leave it alone if it does.

### 5. Do not touch

`posttool_hook.py` and `reply_injector.py` are owned by the `telegram` feature
and are only ever installed alongside the router. They keep their hard imports.

## Done when

- `python3 -c "import permission_request_hook"` succeeds in a directory
  containing the `permission-hooks` module set but **no**
  `telegram_permission_router.py` and **no** `roles_config.py`.
- With the router absent and yolo enabled for the session, a PermissionRequest
  event auto-allows and records an `ALLOW` row.
- With the router absent, `permission_mode == 'bypassPermissions'` auto-allows.
- With the router absent and neither yolo nor bypass, the hook exits 0 with no
  decision and no traceback.
- With the router **present** and the relay configured, every existing path is
  byte-identical in behaviour to before this task.
- The "relay configured but unusable" and "Telegram feature not installed" cases
  produce different log lines.

## Tests

Extend `tests/test_unit_session_yolo.py` (yolo semantics live there) and
`tests/test_integration_permission_request.py` (hook-level flows). Keep the suite
conventions from `docs/prompts/implementer.md` §Step 4 — `FakeTelegramBackend`,
patched `RelayClient`, no network, the runner's isolated state-store env vars.

- Import the hook with `telegram_permission_router` and `roles_config` hidden
  from `sys.modules` and absent from the path; assert clean import.
- Router absent × {yolo on, bypass mode, neither} → auto-allow, auto-allow,
  exit-without-decision. Assert the state-store row for each.
- **Regression for the §2 defect:** router *present* but `TELEGRAM_ENABLED` false
  × {yolo on, bypass mode} → auto-allow. This fails on `main` today; it is the
  test that proves the ordering fix.
- `AskUserQuestion` with Telegram disabled still falls back to the terminal and
  sends nothing.
- Full-suite regression: `python3 tests/run_all_tests.py` — the permission and
  integration modules must not regress.
