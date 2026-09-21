# Task 40-01 — The hook defers to the session's permission mode

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §1 (all), §2.2, §3 **H1/H2/H3/H7** ·
[state.md](./state.md) invariants 1–4

## Goal

Stop `pretool_hook.py` from saying **"ask"** when what it means is **"I don't
recognise this command"**. In a session whose mode resolves ask-candidates by
itself — `auto`, `bypassPermissions`, `dontAsk` — emit the harness's `defer`
instead, so the pipeline (rules → mode → auto-mode classifier) gets to decide,
exactly as it would if this hook were not installed.

Everything else stays: `deny` is unconditional, the risk gates still ask in
every mode, and `default` / `plan` / `acceptEdits` behave byte-identically to
today.

**This is the whole epic's fix.** If nothing else in epic 40 ships, this task
still resolves the reported problem.

## Scope — one file, four edits

### 1. `validate_bash_command` gains an *additive* ask kind

Do **not** change the `decision` values (brd H2 — `permissions_mcp_lib.py:352`
raises on a fourth value, and the pretool suite pins all three). Add one key to
the returned dict instead:

```python
'ask_kind': 'risk' | 'unknown' | None   # None unless decision == 'ask'
```

Assign it at the three existing `decision = 'ask'` sites (`:644-671`):

| Site | Today's reason | `ask_kind` |
|---|---|---|
| `:648` — a sub-command matched `permissions.ask` | "Matches an ask pattern: …" | `'risk'` |
| `:652` — a write redirect escapes the workspace / `/tmp` / `/dev/null` | "Redirects output outside…" | `'risk'` |
| `:666` — unknown or empty sub-commands | "Not in allowlist — review before approving: …" | `'unknown'` |

Keep the key present (as `None`) for `allow` / `deny` so every consumer sees a
stable shape.

While you are in this dict, also return the write-redirect targets the gate
refused — they exist only inside the `reason` string today (`:652-654`), and
[40-02](./40-02-the-card-names-the-asker.md) needs them structured to name the
real reason on the Telegram card:

```python
'redirect_targets': [...]   # the disallowed_targets list; [] when none
```

Both keys are additive. This task owns the shape of the validator's result; no
other task in the epic edits `pretool_hook.py`.

**Why the split.** A `permissions.ask` match is the operator's own configured
floor and a redirect escaping the workspace is this repo's own safety opinion —
both are things the harness does not know, and both prompt in the terminal too,
so forwarding them is parity (state.md invariant 4). "Not on the allowlist" is
not a risk judgement at all; it is the absence of one, which is precisely what
`defer` exists to express.

### 2. One emission helper, used by both callers

`main()` (the emission block, `:2089-2131`) and `replay_from_log()` (`:479-506`) build the same three
payloads today, by hand, twice. Collapse them into one pure function so the new
branch cannot drift between them:

```python
DEFERRING_MODES = frozenset({'auto', 'bypassPermissions', 'dontAsk'})


def build_pretool_output(result, permission_mode):
    """Map a validator result + the session's mode onto a PreToolUse payload.

    Returns the dict to print, or None to print nothing.
    """
```

Rules, in order:

1. `deny` → `permissionDecision: 'deny'` + `permissionDecisionReason`. **Never
   mode-aware** (state.md invariant 1).
2. `allow` → `permissionDecision: 'allow'`.
3. `ask` **and** `ask_kind == 'unknown'` **and** `permission_mode` is in
   `DEFERRING_MODES` → `permissionDecision: 'defer'`.
4. any other `ask` → `permissionDecision: 'ask'` + reason, as today.
5. anything else → `None` (the current silent fallback).

Include `permissionDecisionReason` on the `defer` payload too. The harness drops
it — `defer` is recorded as "this hook passed" and the reason is not surfaced —
but it costs nothing and it lands in our own debug log, which is where anyone
debugging this will look.

**`permission_mode` comes from the hook payload**: `input_data.get('permission_mode', '')`,
alongside the existing `tool_name` / `session_id` reads in `main()`. An absent,
empty or unrecognised value must fall through to rule 4 — `ask` — with no
exception raised (brd H1, state.md invariant 3). The existing fixtures in
`tests/fixtures.py` carry no `permission_mode`, which is the regression test for
that: they must keep asking.

`replay_from_log` has no payload, so give it an explicit
`--mode <default|auto|bypassPermissions|dontAsk|acceptEdits|plan>` argparse flag
next to `--dry-run` (`:437-439`). Default: the `permission_mode` recorded on the
log entry being replayed (edit 3 starts writing it), else `default`. That makes
`cat ~/.claude/bash_manual_confirm.log | pretool_hook.py` replay history exactly
as it happened rather than as `default` pretends it happened.

### 3. The confirm log records what was *emitted*, not just what was validated

`log_manual_confirmation` (`:394-420`) fires for every non-`allow` validator
decision and is the complete parser-level record the daily reviewer falls back
on (brd §2.3.1). After this task a line reading `"decision": "ask"` may well
have produced a `defer` and no prompt at all. Add two keys to the entry:

```python
'permission_mode': permission_mode or None,
'emitted': <the permissionDecision actually printed, or None>,
```

Additive only. `permissions_mcp_lib.py:594` filters these lines on
`entry.get("decision") == "deny"` and must keep working untouched — do not
rename or repurpose `decision`.

**This forces a reordering in `main()`.** `log_manual_confirmation` is called at
`:2086-2087`, *before* the emission block decides anything, so `emitted` is not
knowable where the call sits today. Compute the payload first, then log, then
print:

```python
output = build_pretool_output(result, permission_mode)
if result['decision'] != 'allow':
    log_manual_confirmation(command, result, workspace_dir, session_id,
                            permission_mode=permission_mode, output=output)
if output is not None:
    print(json.dumps(output))
```

Keep the existing debug lines (`ALLOWING…` / `DENYING…` / `ASKING…`) and add a
`DEFERRING…` one — 40-04 greps for exactly these.

### 4. Honour `CLAUDE_MANUAL_CONFIRM_LOG` (brd H7)

`:47` is `os.path.expanduser('~/.claude/bash_manual_confirm.log')`, ignoring the
redirect that `tests/run_all_tests.py:35` sets and that both
`permission_state_store.py:52` and `permissions_mcp_lib.py:551` already honour.
Every end-to-end pretool test with a non-allow decision therefore appends to the
operator's real 6.7 MB log — and this task adds tests of exactly that shape.

```python
MANUAL_CONFIRM_LOG = os.environ.get(
    'CLAUDE_MANUAL_CONFIRM_LOG'
) or os.path.expanduser('~/.claude/bash_manual_confirm.log')
```

Read it at module import, matching the other two modules. `permissions_mcp_lib`
imports this constant by name (`:83`) — keep the name.

### 5. Do not touch

`BashCommandParser`, any allow/deny matching, the deny list, `_is_redirect_target_allowed`,
`permission_request_hook.py`, the installer. The PreToolUse matcher stays
`Bash|Monitor` (`install.sh:286`) — no installer change is needed by this task.

## Testing

`tests/test_integration_pretool.py` (it already runs the hook as a subprocess via
`run_hook_with_options`, which is where the payload-level cases belong) and the
validator-level cases in the same file. Targeted run while iterating:
`python3 tests/run_all_tests.py --module integration_pretool`; also re-run
`--module unit_permissions_mcp` before reporting, since `permissions_mcp_lib.py`
imports `MANUAL_CONFIRM_LOG` from this file by name (`:83`).

Run **only** through `python3 tests/run_all_tests.py` — the runner's env
redirects are the only isolation there is, and after edit 4 they finally cover
the confirm log too. Report before/after counts against the baseline in
[state.md](./state.md) "Testing baseline", which also names the failures that
were already there before you started.

| # | Case | Expect |
|---|---|---|
| 1 | unknown command, payload **without** `permission_mode` | `ask` — every existing fixture keeps working |
| 2 | unknown command, `permission_mode: "default"` | `ask` |
| 3 | unknown command, `permission_mode: "auto"` | **`defer`** |
| 4 | unknown command, `permission_mode: "bypassPermissions"` | **`defer`** |
| 5 | unknown command, `permission_mode: "dontAsk"` | **`defer`** |
| 6 | unknown command, `permission_mode: "acceptEdits"` / `"plan"` | `ask` |
| 7 | unknown command, `permission_mode: "nonsense-value"` | `ask`, no exception, exit 0 |
| 8 | denied command (`dd if=… of=/dev/sda`), mode `auto` | `deny` + reason — mode makes no difference |
| 9 | denied command, mode `bypassPermissions` | `deny` |
| 10 | allowlisted command, mode `auto` | `allow` |
| 11 | `permissions.ask` match, mode `auto` | `ask` (`ask_kind == 'risk'`) |
| 12 | write redirect outside the workspace (`echo hi > /etc/passwd`), mode `auto` | `ask` (`ask_kind == 'risk'`) |
| 13 | mixed: one allowlisted + one unknown sub-command, mode `auto` | `defer` |
| 14 | mixed: one denied + one unknown, mode `auto` | `deny` (deny outranks) |
| 15 | `validate_bash_command` return shape | `ask_kind` and `redirect_targets` present on all five decision paths; `decision` still one of `allow|deny|ask` |
| 15b | case 12's result | `redirect_targets == ['/etc/passwd']`, and the same targets still appear in `reason` |
| 16 | confirm-log entry for case 3 | `decision: "ask"`, `emitted: "defer"`, `permission_mode: "auto"` |
| 17 | confirm log respects `CLAUDE_MANUAL_CONFIRM_LOG` | size + mtime of the **real** `~/.claude/bash_manual_confirm.log` unchanged across a hook run that logs (stat before, stat after) |
| 17b | `Monitor` tool, unknown command, mode `auto` | `defer` — the matcher is `Bash\|Monitor` (`install.sh:286`) and both carry `tool_input['command']` |
| 18 | `replay_from_log --mode auto` on an unknown command | prints the `defer` payload |

Case 1 is the load-bearing one: it is what proves 94 default-mode requests a
week keep behaving exactly as they do today. Case 17 is brd H7 — assert against
the real path, not the redirected one.

## Done criteria

1. `python3 -m py_compile .claude/hooks/pretool_hook.py` clean.
2. `python3 tests/run_all_tests.py` green, ~21 cases added, before/after counts
   reported.
3. `grep -rn "validate_bash_command" --include=*.py .` — every existing consumer
   still gets `allow|deny|ask`; `permissions_mcp_lib.py:310-352` is untouched and
   its guard still reaches all three branches.
4. `main()` and `replay_from_log` both route through `build_pretool_output`; no
   payload is constructed by hand in two places any more.
5. **Not live until installed:** say so in your report. Run `./install.sh --yes`
   only if the epic manager asks — never `--all`, which would install features
   the operator deliberately skipped (brd H6; the script is `install.sh`,
   `install-claude-config.sh` was deleted in epic 29-09).
6. No edit outside `pretool_hook.py` and the two test files.

## Report

Write `agents_output/40-01_implementation_report.md`: what changed, the
before/after test counts, anything you found that contradicts brd §1 (especially
if the installed CLI rejects `defer` — brd H3), and any blocker.
