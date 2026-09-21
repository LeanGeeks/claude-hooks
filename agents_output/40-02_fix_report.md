# Fix Report — Task 40-02: The card names who raised the prompt

Resolves the five issues from `agents_output/40-02_review_report.md`
(1 MEDIUM + 4 LOW). Fix the named issues only — no drive-by refactors, no scope
expansion. All changes remain uncommitted in the working tree against baseline
`cf2fe7b`; no `git add` / `commit` / `checkout` / `stash` / `reset` was run, and
`./install.sh` was not run in any form.

## Files touched by this fix pass

| File | Issues |
|---|---|
| `.claude/hooks/telegram_permission_router.py` | 1, 2, 3, 4 |
| `tests/test_integration_permission_request.py` | 2, 3, 5 |
| `architecture.md` | 3 (keeps the doc true after the import) |

`pretool_hook.py` was **not** touched (`git diff cf2fe7b --name-only` lists it 0
times). No other file moved.

---

## Issue 1 — [MEDIUM] false call relationship in the render comment

**File:** `.claude/hooks/telegram_permission_router.py` (comment above the
`ann = _safe_bash_annotations(request)` call in `render_permission_body`).

**What I changed.** Rewrote the second half of the comment. It previously said
the call "sits behind `_unallowlisted_bash_parts` so that the crash-proofing on
that helper … covers this path too" — false, and load-bearing in the wrong
direction: a reader deleting `_unallowlisted_bash_parts` (a thin adapter with one
external caller) would believe the card's crash-proofing went with it. The
comment now states the real topology: the call goes through
`_safe_bash_annotations` directly, not through its callee
`_unallowlisted_bash_parts`, and the two **share** the same fail-open guarantee,
so neither may be deleted on the assumption that the other carries this path.

This was comment-only; no code changed for Issue 1.

## Issue 2 — [LOW] the honest heading named a mode the card had just contradicted

**Files:** `.claude/hooks/telegram_permission_router.py` (heading string),
`tests/test_integration_permission_request.py` (the tests that pin it).

**What I changed.** Replaced the literal heading

```
🤖 <b>auto mode — the harness flagged this call; not on the allowlist either:</b>
```

with a mode-neutral sentence that still leads with the raiser:

```
🤖 <b>The harness flagged this call — the session's mode resolved it; not on the allowlist either:</b>
```

Chosen over the review's suggested wording ("the session's mode resolved this
call — the allowlist is not why it prompted") for two reasons: it keeps the
task's suggested lead ("the harness flagged this call", §3b) and it deliberately
**keeps** the "not on the allowlist either" clause, which the implementation
report's Decision 2 argues is real operator context — the command genuinely is
not allowlisted, and dropping it would lose that. The `🤖 Mode: <b>…</b>` line
above stays the single place the mode word appears, which is what makes a
`bypassPermissions` card correct.

Added a shared `_harness_heading()` helper in the test file so the five tests
that assert its presence/absence cannot disagree about the wording, and added
`self.assertNotIn("auto mode", body)` to the bypass/dontAsk case to pin the
regression the review actually found (a card naming a mode it is not in).

## Issue 3 — [LOW] the mode sets could drift apart silently

**Files:** `.claude/hooks/telegram_permission_router.py`
(`_mode_resolves_ask_candidates`), `tests/test_integration_permission_request.py`
(new test), `architecture.md` (one sentence).

**Decision: import, not restate.** `_mode_resolves_ask_candidates` now does a
guarded `from pretool_hook import DEFERRING_MODES` and tests membership in it,
replacing the duplicated `("auto", "bypassPermissions", "dontAsk")` literal. I
chose the import over the review's "optional" alternative (a test-only
`assertEqual`) because the import removes the failure mode structurally rather
than adding a tripwire for it: with one source of truth, pretool deferring for a
new mode makes the card follow automatically, so the only way to reintroduce brd
H4 is to change `DEFERRING_MODES` itself. The guard is fail-open in the
conservative direction — if `pretool_hook` cannot be imported, the helper
returns False and the card keeps saying "Not in allowlist", i.e. today's
behaviour rather than an invented claim. Verified safe: `pretool_hook.py` has
only stdlib imports at module level (`io, json, re, shlex, sys, os, datetime,
typing`) and is imported at module scope already by
`permissions-mcp/permissions_mcp_lib.py`, so there is no cycle and no new
dependency; cost is ~25 ms, and the call site is not hot. When `pretool_hook` is
absent entirely the surrounding module's own Bash-annotation path is already
unavailable (it does the same import inside `_bash_annotations`), so the guarded
import adds no new failure mode.

I did **not** change the `Mode:` line's condition to share a predicate. The two
conditions must be different: Issue 3's own note says the mode line is
"suppress only for `None`/`default`", which for `acceptEdits`/`plan` (ask but do
not defer) prints a mode line while the heading stays "Not in allowlist" — which
is correct, not drift.

**Guard test added** — `test_honest_heading_set_is_pretool_deferring_modes`. It
drives `build_pretool_output` (pretool's real defer decision) and
`_mode_resolves_ask_candidates` and the rendered body for each mode, and asserts
all three agree. The probe set is derived from `DEFERRING_MODES` itself plus
modes outside it (`default`, `acceptEdits`, `plan`, `nonsense-value`), so the
test survives the set growing and fails the moment the card's policy stops
following it. **Teeth confirmed by mutation** (cp-backup protocol, no git):
adding a fourth mode (`plan`) to `DEFERRING_MODES` *and* restoring a hard-coded
tuple in the router fails the test at `mode='plan'` (2 failures). With the
import in place and the same fourth mode added, the card follows and the suite
stays green — which is the behaviour the fix is for.

`architecture.md` (the sentence next to the permission-request flow) now says the
three modes are `pretool_hook.DEFERRING_MODES`, imported rather than restated,
so the doc does not go stale as the set grows.

## Issue 4 — [LOW] `BashAnnotations` claimed immutability it does not have

**File:** `.claude/hooks/telegram_permission_router.py` (docstring of
`BashAnnotations`).

**What I changed.** The review said "None required"; I kept `frozen=True`
(nothing is broken by it, and dropping it would be an unforced behaviour change)
and instead corrected the *claim*, since the review's complaint is precisely
that a reader can be misled. The docstring now says `frozen=True` buys
rebinding protection only: assigning a field raises `FrozenInstanceError`, but
the instance is unhashable (list fields) and a received list can still be
mutated, so it is "this record is not reassigned after it is built", not a value
object. It also points a future author at the `__post_init__` normalisation
pattern for any new field with a mutable default, which is the one place that
pattern matters. Comment-only.

## Issue 5 — [LOW] case 4's byte-identical pair no longer touches the real validator

**File:** `tests/test_integration_permission_request.py`.

**What I changed.** Added
`test_case4c_default_mode_with_the_real_validator_is_byte_identical`: no stubs —
it runs `_bash_annotations` against the repo's own allowlist
(`cwd` = repo root, user-level settings really loaded) for
`permission_mode="default"`, asserts the real breakdown is the one the frozen
literal describes (`unknown == ["mysteryfoo --bar"]`, no `denied`/`asked`/
`redirect_targets`, `failed is False`), and then asserts
`render_permission_body(...) == _baseline_body()` — the same frozen literal
cases 3/4 use. This closes exactly the gap the review named: the unstubbed
`default` path where the two routers' agreement had rested on the review's
manual check.

**Teeth confirmed by mutation** (cp-backup protocol): a drift in the bucket
logic (`unknown.append(cmd + " --drifted")`) fails the new test — as well as the
two pre-existing real-validator tests — with the stubbed cases 3/4 staying
green, which is precisely the hole the test fills.

---

## Verification

**Compile.** `python3 -m py_compile .claude/hooks/telegram_permission_router.py
tests/test_integration_permission_request.py` → clean, exit 0.

**Targeted modules** (canonical runner only; pytest never invoked):

| Module | Before fix | After fix |
|---|---|---|
| `--module unit_state` | 59 OK | 59 OK |
| `--module integration_permission` | 180 OK | 182 OK |
| `--module unit_permissions_mcp` | 44 OK | 44 OK |

**Full suite** `python3 tests/run_all_tests.py`:

| | Tests | Result |
|---|---|---|
| Post-40-02 baseline (as given) | 1994 | `OK (skipped=2)` |
| After this fix pass | **1996** | `OK (skipped=2)`, exit 0, 262.7 s |

The **+2** is exactly the two tests added here (case 4c, the DEFERRING_MODES
pin). Both skips are still the same two tests by name, read from the run output:
`test_all_hook_modules_import` (test_unit_python_compat) and `test_headless_spawn`
(test_unit_amux_spawn.TestLiveSpawn). No test stopped running.

**Rendered cards after the fix** (stub-free except where noted). `None` and
`default` are byte-identical to the frozen `_baseline_body()`; the three
deferring modes carry the mode once, above the command, and the neutral heading:

```
<b>ws</b> <i>sess</i>

<b>Permission Request</b> <code>a1b2</code>
🤖 Mode: <b>bypassPermissions</b>

<pre>
mysteryfoo --bar
</pre>

🤖 <b>The harness flagged this call — the session's mode resolved it; not on the allowlist either:</b>
<code>mysteryfoo --bar</code>

Approve this command?
```

**Mutation checks** (all via `cp` to `/tmp/40-02-fix-backup/`, restore verified
with `diff -u` against the backup — never `git`; zero residue, confirmed by the
final `diff` showing only the intended Issue-4 docstring):

| # | Mutation | Result |
|---|---|---|
| A | `DEFERRING_MODES` +`plan`, router **imports** it | green (the card follows — the point of the fix) |
| B | `DEFERRING_MODES` +`plan`, router hard-codes its old tuple | **FAIL** at `mode='plan'` ×2 — the pin test has teeth |
| C | Mode line unconditional (`if True:`) | **FAIL** cases 4, 4b, **4c** + 7 errors — case 4c is a real guard, not decoration |
| D | Bucket drift in the unknown path | **FAIL** case **4c** + 2 pre-existing real-validator tests — closes Issue 5's gap |

**Scope.** `git status --short` lists the same 11 files as the review's baseline
audit (the four implementation files, three test files, two docs,
`architecture.md`, and the orchestrator's `state.md` — which I did not edit).
`pretool_hook.py` appears 0 times in `git diff cf2fe7b --name-only`.

**Nothing is live.** `./install.sh` was not run in any form, so `~/.claude/hooks/`
still holds the pre-epic copy; everything above was tested from the repo
checkout.

## Residual concerns

1. **The import adds a (cold-path) module dependency.** `_mode_resolves_ask_candidates`
   now imports `pretool_hook` at call time. It is stdlib-only, already imported
   at module scope elsewhere in the tree, and the guarded failure direction is
   conservative, so I consider this strictly better than the literal it
   replaced. If a future reviewer prefers zero new coupling, the alternative is
   the review's test-only `assertEqual`, which leaves the drift possible.
2. **`ask_kind` is still carried but unread** by the renderer (the review's
   Code Quality note, not an issue on the worklist). I did not touch it —
   wiring it in or dropping it is a behaviour/scope change beyond these five
   issues, and a test pins the field.
3. **Issue 2's wording is a judgement call**, not a contract. It is mode-neutral
   and keeps the "not on the allowlist either" clause; if the operator-facing
   phrasing is wanted shorter, it is one string in
   `telegram_permission_router.py` plus `_harness_heading()` in the test file.

## Manager addendum — review-pass-2 LOW

The confirm-pass review found one new LOW: `test_case5_unknown_mode_still_blames_the_allowlist` was defined twice (lines 395/401), the second definition self-shadowing the first. Removed the duplicate (one deletion, no behaviour change — the shadowed copy never ran, so the module count stays 182). Verified: py_compile clean, `--module integration_permission` 182 OK. Fixed directly by the manager rather than spawning a fixer for a one-line deletion.
