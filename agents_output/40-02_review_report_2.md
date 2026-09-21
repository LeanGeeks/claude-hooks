# Review Report — Task 40-02: The card names who raised the prompt (fix-pass re-review)

## Verdict: PASS

All five issues from the first review are resolved. One **new LOW** was
introduced by this fix pass (a duplicated test method that silently shadows
itself) — cosmetic test maintenance, not a behaviour defect, and explicitly
named by the coordinator as the kind of thing a re-review is for. Nothing else
new. The task's original requirements remain met and its regression guards still
pass.

Baseline rules honoured: the diff was taken over the working tree
(`git status --short` + `git diff cf2fe7b --`), never `git diff HEAD~1`; no
repo-wide git command ran; `./install.sh` was not run in any form. Every
mutation used the `cp`-to-`/tmp` protocol with `md5sum -c` restore verification.

## What changed in this fix pass

Three files moved: `.claude/hooks/telegram_permission_router.py`,
`tests/test_integration_permission_request.py`, `architecture.md`. Same 11 files
in the whole 40-02 diff as before — **no new file, no file dropped**, and
`pretool_hook.py` still appears 0 times in `git diff cf2fe7b --name-only`.

`git diff --stat` moved +37 lines in the router (201 → 225 changed) and +99 in
the test file (353 → 452). I audited every deleted line in both diffs: the test
file's deletions are exactly the four pre-existing `_unallowlisted_bash_parts`
stub swaps and one `create_request` patch already accounted for; the router's
deletions are the `_unallowlisted_bash_parts` rewrite and the two render blocks
that were restructured. **No test was deleted to make room for the new ones** —
which matters, because that is this repo's characteristic way of shipping a
green suite that proves nothing (state.md invariant 8).

## Verification of each fix

### Issue 1 — [MEDIUM] false call relationship in the render comment · RESOLVED

`.claude/hooks/telegram_permission_router.py:613-620`. The comment now reads
that the call "goes through `_safe_bash_annotations` directly — not through its
callee `_unallowlisted_bash_parts`, which is a thin adapter kept only for
`permission_request_hook._format_non_whitelisted`. The two share the same
fail-open guarantee, so neither may be deleted on the assumption that the other
carries this path's crash-proofing."

Checked against the code: `render_permission_body` calls
`_safe_bash_annotations` at `:621`; `_unallowlisted_bash_parts` (`:512-522`) is
still defined and still delegates to the same helper. The topology the comment
describes is the topology in the file. Comment-only, as claimed — no code line
changed for this issue.

### Issue 2 — [LOW] the honest heading named a mode the card contradicted · RESOLVED

`telegram_permission_router.py:646-657`. The heading is now
`🤖 <b>The harness flagged this call — the session's mode resolved it; not on the allowlist either:</b>`,
with an in-code comment explaining why it is mode-neutral. The `🤖 Mode:` line
(`:604`) is again the only place a mode word appears.

**I verified the §3b constraint myself, not by reading the tests.** I rendered
thirteen (mode × breakdown) combinations through the real renderer with the
derivation stubbed, and asserted, for each, whether the honest heading appears
and whether it is ever paired with a bare "Not in allowlist" claim:

| breakdown | `auto` | `bypass` | `dontAsk` | `default` | `None` | `acceptEdits` | `plan` | `nonsense` |
|---|---|---|---|---|---|---|---|---|
| unknown only | honest | honest | honest | allowlist | allowlist | allowlist | allowlist | allowlist |
| + an `asked` part | allowlist | — | — | — | — | — | — | — |
| + a `denied` part | allowlist | — | — | — | — | — | — | — |
| + a refused target | allowlist | — | — | — | — | — | — | — |

**Constraint violations: none. Cards naming a mode they are not in: none.** The
honest heading never appears when our own risk gate (`asked`) or redirect gate
raised the ask, and never when the mode does not defer. The bypass card now
renders correctly:

```
<b>ws</b> <i>sess</i>

<b>Permission Request</b> <code>i</code>
🤖 Mode: <b>bypassPermissions</b>

<pre>
echo hi; mysteryfoo --bar
</pre>

🤖 <b>The harness flagged this call — the session's mode resolved it; not on the allowlist either:</b>
<code>u</code>

Approve this command?
```

The fix also added the assertion that pins the exact regression I reported:
`tests/test_integration_permission_request.py:392`, `self.assertNotIn("auto mode", body)`
inside the bypass/dontAsk subTest loop.

**Cases 3/4 byte-identical still pass**, and I re-confirmed them by output
equality rather than trusting the runner: `_render(None)` and
`_render("default")` both `== _baseline_body()`, the hand-written frozen
literal — unchanged from the first review, so the fix pass did not disturb the
regression guard.

### Issue 3 — [LOW] the mode sets could drift apart silently · RESOLVED

`.claude/hooks/telegram_permission_router.py:678-694`. `_mode_resolves_ask_candidates`
now does a guarded `from pretool_hook import DEFERRING_MODES` (`:690`) and
returns membership in it (`:694`), replacing the duplicated literal. The guard
is fail-open in the conservative direction: on `ImportError` it logs and
returns `False`, which keeps today's "Not in allowlist" card rather than
inventing a claim.

**Import direction is sane — I checked both ends.** `pretool_hook.py`'s
module-level imports are stdlib only (`io, json, re, shlex, sys, os, datetime,
typing`) plus `bash_command_parser` and `settings_loader` inside guarded blocks;
it does **not** import `telegram_permission_router`, so there is no cycle. The
import is lazy (inside the function body), so it cannot create an import-order
hazard at module load either. `permissions-mcp/permissions_mcp_lib.py:83`
already imports `pretool_hook` at module scope, so the dependency is
established, not novel.

**The pin test exists** — `test_honest_heading_set_is_pretool_deferring_modes`
(`tests/test_integration_permission_request.py:574-618`). It drives pretool's
real defer decision (`build_pretool_output`), the router predicate, and the
rendered body for every mode in `DEFERRING_MODES` plus `default`, `acceptEdits`,
`plan`, `nonsense-value` and an absent mode, and asserts all three agree. The
probe set is derived from the set itself, so it survives the set growing.

**Teeth confirmed by mutation — and I proved the important half that the fix
report could not.** Two mutations, `cp` protocol:

| # | Mutation | Result |
|---|---|---|
| A | router **hard-codes** the old tuple + `DEFERRING_MODES` gains `plan` | **FAIL** at `mode='plan'`, 2 failures — the pin test catches drift |
| B | only `DEFERRING_MODES` gains `plan`; router **imports** it | **green** (182 OK) |

B is the half that matters: with the import in place, pretool growing a mode
makes the card follow automatically, so the only way to reintroduce brd H4 is to
change `DEFERRING_MODES` itself — which is the property the fix claims, now
independently demonstrated rather than asserted. `architecture.md:336-338` was
updated to say the three modes are `pretool_hook.DEFERRING_MODES`, imported
rather than restated, so the doc stays true as the set grows.

### Issue 4 — [LOW] `BashAnnotations` claimed immutability it does not have · RESOLVED

`telegram_permission_router.py:389-394`. The docstring now says `frozen=True`
"buys *rebinding* protection only": assigning a field raises
`FrozenInstanceError`, but the instance is **not** hashable (list fields) and a
list it received can still be mutated — "this record is not reassigned after it
is built", not a value object. It also points a future author at the
`__post_init__` normalisation pattern for any new field with a mutable default.

Accurate as stated. I re-verified the three claims mechanically: field
assignment raises `FrozenInstanceError`; `hash(...)` raises
`TypeError: unhashable type: 'list'`; a caller's list is aliased into the
instance and a later mutation is visible through it. Comment-only, as claimed.

### Issue 5 — [LOW] case 4's byte-identical pair no longer touched the real validator · RESOLVED

`tests/test_integration_permission_request.py:339-364`. The new
`test_case4c_default_mode_with_the_real_validator_is_byte_identical` runs
`_bash_annotations` with **no stubs** against the repo's own allowlist
(`cwd` = repo root), asserts the real breakdown is the one the frozen literal
describes (no `denied`/`asked`/`redirect_targets`, `failed is False`,
`unknown == ["mysteryfoo --bar"]`), and then asserts
`render_permission_body(req, "ws", "sess") == _baseline_body()` — full rendered
equality against the same frozen literal cases 3/4 use. That is exactly the gap
I named, and it is closed with the right assertion shape.

**The test exercises the real validator and has teeth.** I proved it by
isolating it: I neutralised the two *other* tests that also happen to catch a
bucket drift (the pre-existing real-validator categorisation test and the
ask_kind/redirect test), then injected `unknown.append(cmd + " --drifted")`.
`test_case4c` failed — as the sole remaining guard — and the two stubbed cases
3/4 stayed green, which is precisely the hole the test fills. Restored from
`cp`, `md5sum -c` clean.

## Regression and integrity checks

- **Compile:** `python3 -m py_compile` clean on the router, the test file, and
  all four implementation files (exit 0).
- **Targeted modules** (canonical runner only; pytest never invoked):
  `--module unit_state` **59 OK** · `--module integration_permission`
  **182 OK** · `--module unit_permissions_mcp` **44 OK** — matching the
  coordinator's expectation exactly.
- **Full suite:** `python3 tests/run_all_tests.py` →
  `Ran 1996 tests in 245.274s` · `OK (skipped=2)` · exit 0. The +2 over the 1994
  post-40-02 baseline is exactly the two tests added here (case 4c and the pin
  test).
- **Skips by name, not count:** still `test_all_hook_modules_import`
  (test_unit_python_compat) and `test_headless_spawn`
  (test_unit_amux_spawn.TestLiveSpawn) — the same two the task names. No test
  stopped running.
- **Mutation residue:** after every restore, `md5sum -c` on all four files
  (router, pretool, test file, architecture.md) was clean and `git status
  --short` listed the same 11 files. `pretool_hook.py` is byte-identical to its
  backup and absent from the diff. Backups cleaned up.

## Issues Found

### Issue 1 (new, introduced by this fix pass): [severity: LOW] a duplicated test method silently shadows itself

- **File:** `tests/test_integration_permission_request.py:395` and `:401` —
  `TestPermissionModeAnnotation.test_case5_unknown_mode_still_blames_the_allowlist`
  is defined **twice**, with byte-identical bodies.
- **Problem:** Python class bodies are `dict`s, so the second definition wins:
  `fn.__code__.co_firstlineno` is `401`, and the copy at `395` is never bound to
  a name. `dir(cls)` therefore exposes 20 unique test names while the class body
  contains 21 `def test_` statements, and `unittest`'s loader — which discovers
  by name — runs the test **once**. The runner's `Ran 182 tests` is honest (I
  confirmed no duplicate `Test.id()` exists in the loaded suite), but any count
  derived from `grep -c "def test_"` overstates it by one, and a reviewer
  reading the file sees a test that is not a test. This is the same class of
  defect as the `[MEDIUM]` I reported against the implementation report: a count
  that does not mean what it appears to mean.
- **Impact:** cosmetic. No requirement is unverified, and the surviving copy
  asserts exactly what the shadowed one did. It does not affect the +2 delta:
  the runner says 1996 and the delta is 1994 → 1996, which is correct.
- **Evidence it is a duplicate and not intended:**
  `python3 -c "import ast, collections; ..."` over the class reports
  `raw defs in class: 21 | unique: 20`, and the pin test
  `test_honest_heading_set_is_pretool_deferring_modes` appears exactly once.
  Only this one name is duplicated, in this one class; the other two changed
  test files are clean.
- **Fix:** delete the copy at lines 395–399 (or 401–405). One line of cleanup.

## Code Quality Notes

- The retained `ask_kind` field on `BashAnnotations` is still copied and unread
  by the renderer (my first review's note). The fix report correctly declined to
  chase it as out of scope; a test pins it, so it cannot be dropped silently.
  Fine to leave.
- The fix chose the `import` over my "optional" test-only `assertEqual`, and
  made the case for it in the report ("removes the failure mode structurally
  rather than adding a tripwire"). I agree with the reasoning and verified the
  cycle and coupling concerns are unfounded. This is a better fix than the one I
  suggested, and mutation B is the evidence.
- The `_harness_heading()` helper in the test file (`:254`) is a genuine
  improvement: five tests assert the heading's presence or absence, and they can
  no longer disagree about the wording. It is what makes the wording a
  one-string change in future, which the fix report also notes.
- Issue 2's new wording keeps the "not on the allowlist either" clause. I agree
  with that call — the clause is real operator context, and dropping it would
  have removed information to fix a cosmetic complaint.
- No dead code, no commented-out blocks, no dropped `except` branches, no
  signature changes. The fail-open posture is unchanged and, in the new import
  guard, deliberately conservative (returns `False`, i.e. today's card).

## Questions for User

None. Issue 1 is a three-line test cleanup and can be done at leisure; nothing
about it warrants holding the task. The fix pass is otherwise complete and
correct.

## Verdict rationale

PASS. The five reported issues are resolved, and I verified each against the
code and against my own independent probes rather than against the fix report or
the green suite. The two claims that carried the most risk if wrong — that the
§3b heading constraint still holds for every mode, and that the `DEFERRING_MODES`
import really makes the card follow pretool rather than merely adding a
tripwire — I tested directly and both hold. Cases 3/4 remain byte-identical by
full-output equality against an unchanged frozen literal. The one new defect is
a duplicated, shadowed test method: a real but cosmetic test-hygiene issue that
does not weaken any assertion, named above rather than waved through.
