# Task 40-02 — The card names who raised the prompt

**Status:** todo · **Depends on:** [40-01](./40-01-pretool-defers-to-the-mode_sonnet.md)
**Read first:** [brd.md](./brd.md) §1.3, §2.3, §3 **H4/H5/H6** ·
[state.md](./state.md) invariants 5–7 · 40-01 §1 (the `ask_kind` /
`redirect_targets` keys this task consumes)

## Goal

After 40-01, a Telegram card that arrives in `auto` mode was raised by the
**harness**, not by our allowlist — the validator deferred. The card still says
"⚠️ Not in allowlist", because it re-derives its own reason by re-running the
validator (brd H4). Fix that, and record the mode on the row so the card, the
MCP surfaces and the daily reviewer can all tell the two cases apart.

Small task, three files plus docs. No behaviour change to any decision.

## Scope

### 1. `permission_state_store.py` — one additive field

```python
permission_mode: Optional[str] = None   # session mode at request time
```

on `PermissionRequest` (`:110-146`, next to `agent_id` / `role`), and as a
keyword on `create_request` (`:275-284`). `from_dict` (`:153`) already filters to
known fields and defaults missing ones, and `to_dict` is `asdict` (`:148-150`),
so all existing rows load unchanged and the new column persists for free — the
same additive pattern epic 26 used for the owner columns.

**It does not surface by itself.** `permissions-mcp/permissions_mcp_lib.py`'s
`_summarize` (`:437-460`) builds an explicit dict, so
`mcp__permissions__list_permission_requests` / `permission_history` will not show
the field unless you add it there. One line, next to `"state"` — without it this
task's stated benefit (the daily reviewer can tell an auto-mode prompt from a
default-mode one) does not exist.

### 2. `permission_request_hook.py` — pass it, and correct one comment

- `create_request(...)` at `:1609-1617` gains `permission_mode=permission_mode`.
  The variable is already read at `:1572`.
- The comment block at `:1626-1639` explains the `bypassPermissions` branch with
  *"Claude asks us anyway whenever a PreToolUse hook returns `ask` — that
  decision outranks the CLI flag"*. After 40-01 that is true only for a
  **risk-gate** ask (a `permissions.ask` match or a refused write redirect); the
  unknown case now defers and never reaches this hook in a bypass session.
  Rewrite the comment to say that, and keep the branch (brd §2.2 — it is the
  only thing that records a risk-gate ask raised in a no-prompt session).

**Do not** add a mode check anywhere else in this hook. `auto` needs no branch
here: by the time this hook runs, the classifier has already declined to resolve
the call, so forwarding is exactly right (brd §1.3).

### 3. `telegram_permission_router.py` — say what actually raised it

`render_permission_body` (`:477-531`) currently renders three annotation blocks
derived from `_unallowlisted_bash_parts` (`:377-432`). Two changes:

**a. Name the mode when it is not `default`.** One line under the title, derived
only from `request.permission_mode` (brd H5 — the body must stay reconstructable
from the stored row, because the agent-decision finalization re-renders it).

**b. Stop the "Not in allowlist" block from claiming credit it has not earned.**
When `request.permission_mode` is one of `auto` / `bypassPermissions` /
`dontAsk` **and** the re-derived breakdown has no `denied` and no `asked` parts,
the validator deferred — the harness raised this prompt. The unknown list is
then context, not cause, and the heading must say so.

Suggested wording; exact phrasing is yours, the constraint is that it must not
tell the operator the allowlist raised a prompt it did not raise:

```
🤖 auto mode — the harness flagged this call
⚠️ Not on the allowlist either:
<code>some_unknown_command --flag</code>
```

**c. Name the refused write targets.** A redirect-gate ask is one of the few
things we still raise in `auto` mode, and today the card shows no annotation for
it at all — `_unallowlisted_bash_parts` only reads per-sub-command results.
Consume 40-01's `redirect_targets` and render them, e.g.:

```
📝 Writes outside the workspace:
<code>/etc/passwd</code>
```

Keep `_unallowlisted_bash_parts`'s best-effort contract (`:396-398`): any failure
returns empty and the card still sends. Computing an annotation must never block
a prompt.

### 4. Docs

- **`architecture.md:112`** (hook table, `PreToolUse` row): it currently reads
  *"All allowed → allow; otherwise let Claude Code surface a `PermissionRequest`"*.
  Say what the four outcomes now are, and that the unknown case defers to the
  session's mode.
- **`architecture.md:333`** (the "Permission request" flow diagram): the
  `allow? ──► yes ──► runs, done` branch needs the `defer` path beside it.
- **`docs/permission-review-daily.md`** and
  **`docs/prompts/permission-review-daily.md:185-191`**: one sentence that store
  rows now cover only requests that actually reached a human, and that
  `~/.claude/bash_manual_confirm.log` — which carries `permission_mode` and
  `emitted` per line after 40-01 — is the complete parser-level record
  (brd §2.3.1).

### 5. Do not touch

`pretool_hook.py` (40-01 owns it — including the validator result shape), the decision mapping, the relay, the
installer, `session_yolo_store.py` (40-03 may own it).

## Testing

`tests/test_unit_state_store.py` for the field,
`tests/test_integration_permission_request.py` for the pass-through, and the
router's rendering tests (find them with `grep -rn "render_permission_body" tests/`).
Targeted runs: `--module unit_state`, `--module integration_permission`,
`--module unit_permissions_mcp`. Run through
`python3 tests/run_all_tests.py`; report before/after counts against the
baseline in [state.md](./state.md) "Testing baseline".

| # | Case | Expect |
|---|---|---|
| 1 | `create_request(..., permission_mode="auto")` | round-trips through the JSONL and `from_dict` |
| 2 | a pre-epic row with no `permission_mode` | loads, field is `None`, nothing raises |
| 3 | `render_permission_body` with `permission_mode=None`, no refused redirect targets | **byte-identical** to today's output |
| 4 | `permission_mode="default"`, no refused redirect targets | byte-identical to today's output |
| 4b | `permission_mode="default"` **with** a refused redirect target | today's output **plus** the new write-target block — §3c is an improvement in every mode, not an auto-mode special case |
| 5 | `permission_mode="auto"`, unknown parts only | names the harness as the source; no bare "Not in allowlist" heading |
| 6 | `permission_mode="auto"`, an `asked` part present | keeps today's ask-pattern block — we raised it |
| 7 | `permission_mode="auto"`, a refused redirect target | renders the write-target block |
| 8 | same row rendered twice | identical strings (H5 — reconstructable) |
| 9 | `_unallowlisted_bash_parts` raising | card still renders and sends |
| 10 | hook payload with `permission_mode` | lands on the stored row |

Case 3 and case 4 together are the regression guard: 94 of the last 416 requests
were `default` and their cards must not move a byte.

## Done criteria

1. `python3 -m py_compile` clean on all four Python files.
2. `python3 tests/run_all_tests.py` green, ~11 cases added, before/after counts.
3. `grep -rn "Not in allowlist" .claude/hooks/` shows the heading only on the
   path where the allowlist really is the reason.
4. The two `architecture.md` spots and both permission-review docs updated; no
   other doc edited.
5. **Not live until installed** — `./install.sh --yes`, never `--all` (brd H6): say which copy you tested.

## Report

Write `agents_output/40-02_implementation_report.md`: what changed, before/after
test counts, the exact new card wording (paste one rendered example per mode),
and any blocker.
