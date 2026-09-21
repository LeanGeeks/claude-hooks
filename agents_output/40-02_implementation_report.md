# Implementation Report — Task 40-02: The card names who raised the prompt

## Summary

`permission_mode` is now recorded on every state-store row and rendered on the
Telegram card, so a card that arrives in `auto` mode says the **harness** flagged
the call instead of claiming "Not in allowlist". The card also names the refused
write-redirect targets 40-01 made structured, in every mode. No decision, mapping,
relay or installer behaviour changed; `pretool_hook.py` was not touched.

Baseline for this work is commit `cf2fe7b` (40-01). **All changes are uncommitted
in the working tree** — no `git add` / `commit` / `checkout` / `stash` / `reset`
was run.

## Files Created

None.

## Files Modified

- `.claude/hooks/permission_state_store.py` — `PermissionRequest.permission_mode:
  Optional[str] = None` next to `agent_id`/`role`; new `permission_mode` keyword on
  `create_request`, passed through to the dataclass. Additive only: `from_dict`
  filters to known fields and defaults the rest, `to_dict` is `asdict`, so every
  existing row loads unchanged and the column persists for free.
- `.claude/hooks/permission_request_hook.py` — `create_request(...)` now passes
  `permission_mode=permission_mode or None` (the variable was already read at
  `:1572`). The `bypassPermissions` comment block was rewritten: after 40-01 the
  branch no longer catches the *unknown* case (that now defers in a bypass
  session, so no request reaches this hook); what still reaches it is a
  **risk-gate** ask (`permissions.ask` match or a refused write redirect). The
  branch itself is kept, untouched in behaviour — it is the only thing that
  records a risk-gate ask raised in a no-prompt session. No mode check was added
  anywhere else in the hook.
- `.claude/hooks/telegram_permission_router.py` — the three card changes (see
  Verification for rendered output):
  - New `_bash_annotations(request) -> BashAnnotations`, a frozen dataclass that
    carries the three per-sub-command buckets **plus** `ask_kind` and
    `redirect_targets` copied off the validator result, and a `failed` flag.
    `_unallowlisted_bash_parts` is retained as the 3-tuple compatibility shape
    (`permission_request_hook._format_non_whitelisted` uses it) and now delegates
    to it.
  - New `_safe_bash_annotations` wraps the derivation so even a bug in the
    derivation itself cannot block a card; `render_permission_body` calls it.
  - `render_permission_body` (a) prints `🤖 Mode: <mode>` under the request id for
    any mode other than `default`/None, (b) renders the honest heading
    `🤖 auto mode — the harness flagged this call; not on the allowlist either:`
    instead of `⚠️ Not in allowlist:` when the row is `auto`/`bypassPermissions`/
    `dontAsk` **and** the breakdown has no `denied`, no `asked` and no refused
    redirect target, and (c) renders `📝 Writes outside the workspace:` with the
    refused redirect targets, in every mode.
  - New `_mode_resolves_ask_candidates` helper naming the three modes.
- `permissions-mcp/permissions_mcp_lib.py` — `permission_mode` added to
  `summarize_row`'s explicit summary dict (the task's §1 one-liner, next to
  `"state"`), **and** to `permission_history`'s own row projection. The second
  spot is required by the same sentence: `permission_history` is the feed the
  daily reviewer actually reads (`mcp__permissions__permission_history`), and the
  task's stated benefit — "the daily reviewer can tell an auto-mode prompt from a
  default-mode one" — does not exist without it. Both are additive keys.
- `architecture.md` — `:112` PreToolUse row now names the four outcomes
  (deny / allow / risk-gate ask / defer for the unknown case in a
  mode that resolves ask-candidates) and says the unknown case defers to the
  session's mode; `:333` flow diagram gains the `deny` and `defer` branches beside
  `allow? ──► yes ──► runs, done`, plus a short note that the stored row carries
  the mode.
- `docs/permission-review-daily.md` — new section "What each feed covers (and what
  it no longer does)": store rows now cover only requests that reached a human,
  and `~/.claude/bash_manual_confirm.log` (carrying `permission_mode` and
  `emitted` per line after 40-01) is the complete parser-level record.
- `docs/prompts/permission-review-daily.md` — one sentence added to the Step 2
  feed list saying the same thing.
- `tests/test_unit_state_store.py` — new `TestPermissionModeField` (4 cases).
- `tests/test_integration_permission_request.py` — new `TestPermissionModeAnnotation`
  (cases 3, 4, 4b, 5, 5-bypass/dontAsk, 5-unknown-mode, 6, 6+, 7, 7b, 8 ×2, 9 ×3,
  escaping, empty-annotations, real-validator keys) plus 4 case-10 pass-through
  cases on `TestBypassPermissionsAutoAllow`. Three pre-existing tests were updated
  to stub the new render seam (`_safe_bash_annotations`) rather than
  `_unallowlisted_bash_parts`, which `render_permission_body` no longer calls
  directly.
- `tests/test_unit_permissions_mcp.py` — two cases pinning that the field reaches
  both MCP projections.

`tasks/40_permission_mode_parity/state.md` shows as modified in `git status` — that
predates my work (it was already ` M` at the start of the session) and I did not
edit it.

## Verification

**Compile/import: PASS.** `python3 -m py_compile` clean on all four Python files
(`.claude/hooks/permission_state_store.py`, `.claude/hooks/permission_request_hook.py`,
`.claude/hooks/telegram_permission_router.py`, `permissions-mcp/permissions_mcp_lib.py`).
All three test modules import and run.

**Tests: 1994 passed, 0 failed** — `python3 tests/run_all_tests.py`, exit 0,
`Ran 1994 tests in 255.9s — OK (skipped=2)`.

| | Tests | Result |
|---|---|---|
| Before (state.md post-40-01 baseline) | 1966 | `OK (skipped=2)` |
| After | 1994 | `OK (skipped=2)` |

**+28 cases** (the two new `TestPermissionModeAnnotation` classes and the MCP
cases), matching the count of new `def test_` lines in the diff.

Both skips are still the **same two tests by name** (verified in the run output,
not inferred from the count):

```
test_all_hook_modules_import (test_unit_python_compat.TestImportUnderOldInterpreter)
    ... skipped 'no pre-3.11 interpreter with tomli available'
test_headless_spawn (test_unit_amx_spawn.TestLiveSpawn.test_headless_spawn)
    ... skipped 'live spawn test (needs tmux + model auth); set AMUX_SPAWN_LIVE_TEST=1'
```

Only the canonical runner was used; pytest was never invoked.

Targeted modules while iterating: `--module unit_state` (59 tests OK),
`--module integration_permission` (180 tests OK), `--module unit_permissions_mcp`
(44 tests OK).

### Mutation proof (state.md invariant 8)

Seven mutations, applied to the working tree and reverted from a `cp` backup each
time — **never** `git checkout` (which would have reverted to HEAD and destroyed
the uncommitted work). After each restore, `diff -q` against the backup confirmed
the file was byte-identical to the pre-mutation state.

| # | Mutation | Caught by |
|---|---|---|
| 1 | Drop the `!= "default"` guard so every mode prints `🤖 Mode:` | case 4 + case 4b (the 40-02 case-3/4 pair) |
| 2 | `if False:` on the honest-heading branch (always blame the allowlist) | case 5 ×3 |
| 3 | Remove the `_safe_bash_annotations` guard so a raising derivation escapes | case 9 ×2 |
| 4 | Stop passing `permission_mode` to `create_request` | case 10 ×4 |
| 5 | Drop `permission_mode` from both MCP projections | MCP ×2 |
| 6 | Drop the `permission_mode` dataclass field | `unit_state` ×29 |
| 7 | Treat `None` mode as a harness-resolving mode (breaks the pre-epic row) | **case 3 alone** — the invariant-8 mutation proof |

Mutations 1 and 7 together are the proof invariant 8 asks for **for 40-02 case 3**:
mutation 7 breaks only case 3 (the `None` row), mutation 1 breaks only case 4 (the
`default` row). Neither test is a duplicate of the other, and neither can be
satisfied by the code drifting in the direction the other guards.

### Rendered card examples (real validator, repo checked out as the workspace)

**A. `default` mode, unknown parts only — UNCHANGED (cases 3 and 4).** Byte-identical
to the pre-change output; asserted against the frozen literal `_baseline_body()`,
not against a re-derivation. Same bytes for `permission_mode=None` (a pre-epic row).

```
<b>claude-hooks</b> <i>auth-refactor</i>

<b>Permission Request</b> <code>a1b2c3d4e5f6</code>

<pre>
mysteryfoo --bar
</pre>

⚠️ <b>Not in allowlist:</b>
<code>mysteryfoo --bar</code>

Approve this command?
```

**B. `auto`, unknown parts only (case 5)** — the harness is named as the source; no
bare "Not in allowlist" heading survives:

```
<b>claude-hooks</b> <i>auth-refactor</i>

<b>Permission Request</b> <code>a1b2c3d4e5f6</code>
🤖 Mode: <b>auto</b>

<pre>
mysteryfoo --bar
</pre>

🤖 <b>auto mode — the harness flagged this call; not on the allowlist either:</b>
<code>mysteryfoo --bar</code>

Approve this command?
```

`bypassPermissions` and `dontAsk` render identically apart from the mode word
(`🤖 Mode: <b>bypassPermissions</b>`). An unrecognised mode (`nonsense-value`)
keeps `⚠️ Not in allowlist:` — it still asks, so the allowlist really is the
reason.

**C. `auto`, a `permissions.ask` pattern (case 6)** — we raised this one, so today's
block stays. Rendered with `echo hi > /etc/passwd` in a workspace whose settings
ask on `Bash(curl:*)`; the stub-based test pins the heading and the command list:

```
<b>claude-hooks</b> <i>auth-refactor</i>

<b>Permission Request</b> <code>a1b2c3d4e5f6</code>
🤖 Mode: <b>auto</b>

<pre>
curl https://api.example.com/v1/x
</pre>

❓ <b>Matches an ask pattern (human review required):</b>
<code>curl https://api.example.com/v1/x</code>

Approve this command?
```

**D. `auto`, a refused write redirect (case 7)** — the new block, rendered through the
real validator. Note the unknown list is absent here: the validator's redirect gate
reports the *target*, not a sub-command, which is exactly why the old card showed no
annotation at all for this ask:

```
<b>claude-hooks</b> <i>auth-refactor</i>

<b>Permission Request</b> <code>a1b2c3d4e5f6</code>
🤖 Mode: <b>auto</b>

<pre>
echo hi &gt; /etc/passwd
</pre>

📝 <b>Writes outside the workspace:</b>
<code>/etc/passwd</code>

Approve this command?
```

Case 4b pins the same block in `default` mode (today's card plus the new block), so
§3c is not an auto-mode special case.

### Done criteria

1. `py_compile` clean on all four Python files — PASS.
2. Full suite green, 28 cases added, before/after reported — PASS.
3. `grep -rn "Not in allowlist" .claude/hooks/` returns four hits: two are the
   rewritten heading in `telegram_permission_router.py` (the comment and the
   `else` branch that now fires only when the allowlist really is the reason or the
   mode asks anyway) and two are `pretool_hook.py`'s own reason strings (40-01's
   file, deliberately untouched). No path claims the allowlist raised a prompt it
   did not raise.
4. Both `architecture.md` spots and both permission-review docs updated; no other
   doc edited — PASS (see the Files Modified list).
5. **Not live until installed.** `./install.sh` was **not** run, in any form
   (neither `--yes` nor `--all`). I tested **the repo checkout** —
   `/data/sync/work/leangeeks-ai/claude-hooks/.claude/hooks/*` — via the test
   runner. `~/.claude/hooks/` still holds the pre-epic-40 copy, so nothing in
   epic 40 is live on this machine yet; the epic installs once after this task
   (state.md "Recommended order" step 3).

## Decisions

1. **Mode line wording and position.** `🤖 Mode: <b>auto</b>` on its own line
   directly under the request-id line, before the command `<pre>`. It reads as a
   header ("who raised this"), stays out of the command block, and derives solely
   from `request.permission_mode` — the stored row — per brd H5.
2. **Heading wording.** `🤖 <b>auto mode — the harness flagged this call; not on the
   allowlist either:</b>` — the task's suggested wording kept literally. Two
   variants were considered and rejected: a per-mode line ("bypass mode — the
   harness allowed this" would be **wrong**, the harness does not "flag" a bypass
   call) and dropping "not on the allowlist either" (which would hide from the
   operator that the command is also not allowlisted, losing real context).
3. **Heading rule is deliberately more conservative than the mode check.** The
   honest heading requires the mode to be a deferring one **and** the breakdown to
   have no `denied`, no `asked` and no refused redirect target. A risk-gate part or
   a refused target means *we* raised the ask, so the old heading stays even in
   `auto` — the card must not credit the harness for a prompt our own gate raised.
4. **`quiet` on absence, not on `default`.** The mode line is suppressed for
   `default` **and** for `None`. That keeps cases 3 and 4 identical (a pre-epic row
   and an explicit `default` row render the same), which is the strongest reading
   of "their cards must not move a byte".
5. **`BashAnnotations` is a frozen dataclass** rather than the 3-tuple extended to
   a 5-tuple: the renderer needs four independent decisions (`denied` / `asked` /
   `unknown` / `redirect_targets`) plus `ask_kind`, and positional tuples at that
   width are exactly the drift hazard the task's §3a warning is about.
6. **`_unallowlisted_bash_parts` kept, not deleted.** It carries the documented
   fail-open contract that several tests pin, and
   `permission_request_hook._format_non_whitelisted` still calls it. The card does
   not call it (it needs the wider object), which is why the three pre-existing
   render tests now stub `_safe_bash_annotations`.
7. **Two MCP spots, not one.** See Files Modified — `permission_history` is the
   reviewer's actual feed, so `_summarize`'s projection alone would have satisfied
   the letter of the task and missed its stated purpose.

## Blockers

None.

## Questions for User

None.

---

**Nothing is live until `./install.sh --yes` runs** (brd H6; never `--all`). Until
then `~/.claude/hooks/` is the pre-epic copy and the card on Telegram is unchanged.
