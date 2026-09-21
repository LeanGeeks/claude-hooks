# Review Report - Task 40-02: The card names who raised the prompt

## Verdict: PASS

No BLOCKER or HIGH issues. One MEDIUM (documentation over-claim) and four LOW.
All nine task cases plus case 10 are implemented, tested with assertions that
have teeth (independently mutation-proved, four mutations, `cp` protocol), and
the scope is exactly what the task names.

**Baseline note (important for the counts below).** The task file says the
baseline for this diff is commit `cf2fe7b` (40-01, committed). The 40-02 changes
are the uncommitted working tree. A task doc (`tasks/40_permission_mode_parity/
state.md`) was ALSO modified in the working tree, but it is the *orchestrator's*
edit stamping 40-01 `done (cf2fe7b)` and 40-02 `in_progress` — not an
implementer edit. The implementer correctly reported not touching it; I checked
the diff and I concur. It is not a scope violation.

## What was verified independently

Every command below was run by me in `/data/sync/work/leangeeks-ai/claude-hooks`.

1. **Compile.** `python3 -m py_compile` clean on all four Python files plus the
   three test files (exit 0).
2. **Full canonical suite.** `python3 tests/run_all_tests.py` →
   `Ran 1994 tests in 262.879s` · `OK (skipped=2)` · exit 0. Matches the
   implementer's claim exactly. Baseline post-40-01 was 1966; delta **+28**.
3. **Skips by name, not count.** Both skips are still
   `test_all_hook_modules_import` (test_unit_python_compat) and
   `test_headless_spawn` (test_unit_amux_spawn.TestLiveSpawn) — the same two the
   task names. No test stopped running.
4. **Targeted modules.** `--module unit_state` → 59 OK · `--module
   integration_permission` → 180 OK · `--module unit_permissions_mcp` → 44 OK.
   The per-file `def test_` deltas (+4 / +22 / +2 = **+28**) account for the
   whole suite delta, so no test was deleted to make room.
5. **Scope.** `git diff cf2fe7b --name-only` lists exactly 11 files: the four
   the task names, the two docs, `architecture.md`, the three test files, and
   the orchestrator's `state.md`. `pretool_hook.py` appears **0** times —
   untouched, as required.
6. **Mutation proofs** — four, all with `cp` to `/tmp`, never `git`:

   | # | Mutation | Caught by |
   |---|---|---|
   | 1 | Mode line unconditional (`if True:`) | case 3 **and** case 4 independently (plus 6 pre-existing annotation tests) |
   | 2 | `if False:` on the honest-heading branch | case 5 ×3 (`unknown_only`, `bypass`, `dontAsk`) |
   | 3 | Delete the `_safe_bash_annotations` try/except | case 9 ×2 |
   | 4 | `from_dict` drops `permission_mode` | case 8 round-trip **with `pytest` semantics off**: exactly one failure — the strict H5 test |

   After each restore: `md5sum -c` on all four files OK, `git status --short`
   byte-identical to the pre-mutation list, `integration_permission` green again.

7. **Case 3/4 assertions are full-output equality, and the frozen literal is
   genuinely today's output.** I did NOT take the `_baseline_body()` literal on
   trust. I extracted the pre-change router from
   `git show cf2fe7b:.claude/hooks/telegram_permission_router.py` into
   `/tmp/40-02_oldcode/` (read-only; no repo mutation) and rendered the same
   `PermissionRequest` through both:

   ```
   OLD(None)      == frozen literal : True
   NEW(None)      == old output     : True
   NEW("default") == old output     : True
   NEW(None)      == NEW("default") : True    (mode line correctly absent)
   ```

   The assertions in `test_case3`/`test_case4`
   (`tests/test_integration_permission_request.py:295,299`) are
   `assertEqual(self._render(mode), _baseline_body())` — whole-string equality
   against a hand-written literal, not a substring and not "no exception". This
   is the real regression guard the task asked for.

8. **Case 4b against the REAL validator** (no stubs). I rendered four command
   shapes through both routers with a real workspace (`cwd` = this repo):

   | command | None | `default` | `auto` |
   |---|---|---|---|
   | `mysteryfoo --bar` (unknown-only) | identical | identical | differs (expected) |
   | `echo hi; dd --version` (denied part) | identical | identical | differs (mode line only) |
   | `curl …` (ask pattern) | identical | identical | differs (mode line only) |
   | `echo hi > /etc/passwd` (redirect) | differs (new block) | differs (new block) | differs |

   This independently confirms two things the stub-based tests alone would not:
   (a) the redirect case produces **no** sub-command bucket at all, which is
   exactly why today's card showed nothing for a redirect-gate ask — the new
   block is a real improvement, not decoration; (b) the case-4b spec ("today's
   output **plus** the new block") holds byte-for-byte — no old line is removed.
   I also confirmed the test's composed expectation equals the code's actual
   output by re-deriving it independently: `True`.

9. **Case 8 renders twice and compares.** `test_case8…identical_strings_twice`
   (`:412`) calls `render_permission_body` twice and `assertEqual(first, second)`.
   The second case (`:427`) is stricter — it round-trips the row through the
   JSONL store and then re-renders, which is what the agent-decision
   finalization actually does (`permission_request_hook.py:448`). Mutation 4
   proved that test is not vacuous; it is the only test that fails when the
   field is lost on load.

10. **Case 9 (best-effort).** `_safe_bash_annotations`
    (`telegram_permission_router.py:489-501`) wraps the derivation; `_bash_
    annotations` itself has an inner `except` around the validator
    (`:452-456`) and returns `failed=True`. Both paths are driven (`:450`,
    `:476`), and mutation 3 confirms the outer guard is load-bearing.

11. **The `_summarize` one-liner is present.** There is no function literally
    named `_summarize` — the task's `:437-460` reference resolves to
    `summarize_row` (`permissions-mcp/permissions_mcp_lib.py:425`), whose
    projection is hand-built. The field is at `:442`, next to `"state"` (`:441`),
    exactly as the task asked. The implementer additionally added it to
    `permission_history`'s own projection at `:647`; I checked that
    `permission_history` builds its row dict literal independently of
    `summarize_row` (`:644-663`), so the second site is genuinely necessary for
    the task's stated benefit (that feed is what the daily reviewer reads).
    Both are additive keys; no consumer pins an exact key set (checked).

12. **Docs.** `architecture.md:112` names the four outcomes and the defer path
    correctly; `architecture.md:335` adds the `defer` branch beside
    `allow? ──► yes`. `docs/permission-review-daily.md:144` (new section) and
    `docs/prompts/permission-review-daily.md:187-193` each carry the
    one-sentence pointer to `~/.claude/bash_manual_confirm.log` with
    `permission_mode` + `emitted`. I verified 40-01 really does log both
    (`pretool_hook.py:429-430`, emitted computed at `:2185`), so the docs are
    accurate. No duplicate section was introduced in the daily doc.

13. **`grep -rn "Not in allowlist" .claude/hooks/`** returns 4 hits: two in
    `pretool_hook.py` (40-01's file, untouched) and two in the router — the
    explanatory comment (`:624`) and the `else` branch (`:643`). The heading
    only prints when the allowlist really is the cause or the mode asks anyway.

## Completeness Check

| # | Requirement | Status | Evidence |
|---|---|---|---|
| §1 | `permission_mode` field on `PermissionRequest`, additive | **met** | `permission_state_store.py:143`; `from_dict` filters to known fields (`:166-168`), `to_dict` is `asdict` |
| §1 | `permission_mode` kwarg on `create_request` | **met** | `permission_state_store.py:293`, passed through `:354` |
| §1 | `_summarize` (→ `summarize_row`) surfaces the field | **met** | `permissions_mcp_lib.py:442` |
| §2 | `create_request(permission_mode=…)` in the hook | **met** | `permission_request_hook.py:1617`, normalised with `or None` |
| §2 | Rewrite the `bypassPermissions` comment; keep the branch | **met** | `permission_request_hook.py:1626-1641` rewritten; branch intact `:1648-1666`; no mode check added elsewhere |
| §3a | Mode line under the title, from the stored row only | **met** | `telegram_permission_router.py:596-599`; reads `request.permission_mode` |
| §3b | Honest heading when mode ∈ {auto, bypass, dontAsk} **and** no denied/asked/redirect | **met** | `:632-643`; heading text `:639-641` |
| §3c | Render refused write targets, every mode | **met** | `:651-656`; confirmed by real-validator render |
| §3 | `_unallowlisted_bash_parts` best-effort contract kept | **met** | `:504-514` (3-tuple kept for `_format_non_whitelisted`); `_safe_bash_annotations:489` |
| §4 | `architecture.md:112` hook table | **met** | four outcomes named |
| §4 | `architecture.md:333` flow diagram defer path | **met** | `:335-343` |
| §4 | Both permission-review docs | **met** | `docs/permission-review-daily.md:144`, `docs/prompts/permission-review-daily.md:187-193` |
| §5 | `pretool_hook.py` untouched | **met** | `git diff cf2fe7b --name-only` — absent |
| T1 | `create_request(..., permission_mode="auto")` round-trips | **met** | `test_unit_state_store.py:725` |
| T2 | pre-epic row loads, field `None`, nothing raises | **met** | `test_unit_state_store.py:742` |
| T3 | `None` mode byte-identical | **met** | `test_integration_permission_request.py:295` + my OLD/NEW comparison |
| T4 | `"default"` byte-identical | **met** | `:299` + my OLD/NEW comparison |
| T4b | `"default"` + refused target = today **plus** block | **met** | `:303`; independently reproduced |
| T5 | `auto`, unknown-only names the harness; no bare heading | **met** | `:328`, `:339`, plus `:348` for an unknown mode |
| T6 | `auto`, an `asked` part keeps today's block | **met** | `:356`, `:367` |
| T7 | `auto`, refused redirect renders the write block | **met** | `:380`, `:390` |
| T8 | same row twice → identical (H5) | **met** | `:412`, `:427` |
| T9 | derivation raising must not block the card | **met** | `:450`, `:466`, `:476` |
| T10 | hook payload `permission_mode` lands on the row | **met** | `:821`, `:829`, `:837`, `:849` |
| Done 1 | `py_compile` clean on all four | **met** | reproduced, exit 0 |
| Done 2 | runner green, ~11 cases added, before/after reported | **met** | 1994 OK skipped=2; +28 reported and verified |
| Done 3 | `grep "Not in allowlist"` shows the heading only where true | **met** | 4 hits, reasoned above |
| Done 4 | two `architecture.md` spots + both docs; no other doc | **met** | diff-name-only lists exactly those |
| Done 5 | not live until installed; which copy tested | **met** | report §Done-5 names the repo checkout; no `install.sh` was run by me either |

## Issues Found

### Issue 1: [severity: MEDIUM] The `_unallowlisted_bash_parts` docstring over-claims a contract the card does not use

- **File:** `.claude/hooks/telegram_permission_router.py:504-514`, and the
  in-body comment at `:610-612`.
- **Problem:** The docstring says the helper is "Retained as the compatibility
  shape for callers that only want the three buckets
  (`permission_request_hook._format_non_whitelisted`)". That is accurate. But
  the comment inside `render_permission_body` says the card's call "sits behind
  `_unallowlisted_bash_parts` so that the crash-proofing on that helper —
  including its fail-open contract, which tests pin — covers this path too."
  That is false: `render_permission_body` calls `_safe_bash_annotations`
  directly (`:613`), and `_unallowlisted_bash_parts` is its *callee*, not its
  caller. The two share the same guard, so nothing is broken — but the comment
  states a call relationship that does not exist, and an implementer deleting
  `_unallowlisted_bash_parts` would believe the card's crash-proofing goes with
  it.
- **Fix:** Reword to "shares `_safe_bash_annotations`' crash-proofing with
  `_unallowlisted_bash_parts`" — or simply delete the second clause.

### Issue 2: [severity: LOW] The two honest-heading modes mismatch the mode lines

- **File:** `.claude/hooks/telegram_permission_router.py:596` vs `:662-670`;
  text at `:639-641`.
- **Problem:** A `bypassPermissions` or `dontAsk` row renders
  `🤖 Mode: <b>bypassPermissions</b>` followed, nine lines later, by
  `🤖 <b>auto mode — the harness flagged this call…</b>`. The card says "auto
  mode" on a card that has just named a different mode. The report's Decision 2
  defends the *word* "flagged" (correctly — none of the three modes' prompt was
  ours), but not the literal `auto`. The task's suggested wording was "exact
  phrasing is yours", so this is cosmetic, not a contract breach.
- **Fix:** Make the sentence mode-neutral, e.g. "the session's mode resolved
  this call — the allowlist is not why it prompted", letting the `🤖 Mode:`
  line above carry the mode word.

### Issue 3: [severity: LOW] The mode-line and heading-mode sets can drift apart silently

- **File:** `.claude/hooks/telegram_permission_router.py:596` (literal
  `!= "default"`) and `:670` (literal tuple); `.claude/hooks/pretool_hook.py:443`
  (`DEFERRING_MODES`).
- **Problem:** `_mode_resolves_ask_candidates` duplicates `pretool_hook.
  DEFERRING_MODES` as a literal instead of importing it, and no test pins the
  two together or pins the mode *policy* (which modes get a `🤖 Mode:` line)
  against a single list. If a future CLI adds a fourth deferring mode, pretool
  would defer and the card would keep saying "⚠️ Not in allowlist" —
  reintroducing exactly brd H4 — and no test would notice. `grep -rn
  "DEFERRING_MODES" tests/` returns nothing.
- **Fix:** Import or `assert` equality with `pretool_hook.DEFERRING_MODES` in
  one test. Optional: make the mode-line condition share the same predicate
  (suppress only for `None`/`default`) so the two render decisions cannot
  diverge.

### Issue 4: [severity: LOW] `BashAnnotations` claims immutability it does not have

- **File:** `.claude/hooks/telegram_permission_router.py:379-415`.
- **Problem:** Direct field assignment does raise `FrozenInstanceError` (I
  verified), so the frozen form is real. But the class is **unhashable** —
  `hash(BashAnnotations(...))` raises `TypeError: unhashable type: 'list'` —
  and the lists remain mutable, so a reader can alias into a "frozen" object.
  Nothing in the codebase takes a `BashAnnotations` as a dict key or set member
  today (checked), and `__post_init__` normalises `redirect_targets` correctly.
  Purely a "this type is not as immutable as its decorator suggests" hazard for
  a future reader.
- **Fix:** None required. Either drop `frozen=True` (nothing depends on it), or
  store tuples and convert at the render boundary.

### Issue 5: [severity: LOW] Case 4's assertion no longer refers to the real validator

- **File:** `tests/test_integration_permission_request.py:299` (and `:295`).
- **Problem:** Both byte-identical tests stub `_safe_bash_annotations`, so a
  future change to `_bash_annotations` that alters the bucket contents would not
  be caught by the byte-identical pair. The pair is honest about the *rendering*
  contract (that is what it is for), but nothing pins "real validator +
  `default`/`None` ⇒ frozen literal". I closed that gap by hand (item 8 of my
  verification) and it passes.
- **Fix:** Optional: add one un-stubbed case asserting the same literal for
  `permission_mode="default"` with `cwd` = the repo. Low value now, but it is
  the one path where the two routers' agreement rests on my manual check rather
  than on the suite.

## Code Quality Notes

- The `_safe_bash_annotations` / `_bash_annotations` / `_unallowlisted_bash_parts`
  triad is slightly more layering than the requirement needs (`_unallowlisted_
  bash_parts` is now a thin adapter kept only for one external caller). It is
  defensible — it preserves a documented, test-pinned contract — and the
  implementer says so in Decision 6. Worth collapsing in a later pass if
  `_format_non_whitelisted` ever changes.
- `ask_kind` is copied onto `BashAnnotations`, documented, and never read by the
  renderer (the heading decision re-derives the same information from `asked` /
  `denied` / `redirect_targets`, which is the more conservative test). Either
  wire it into the decision or drop it; as-is it is carried dead weight, and
  `test_annotations_carry_ask_kind_and_redirect_targets_from_the_validator`
  (`:503`) pins it, so it cannot be dropped silently. Not a defect — the
  conservative re-derivation is what invariant 5 asks for — but the field earns
  its keep only if the heading rule is ever simplified.
- Escaping is consistently applied to every new interpolation (mode word `:598`,
  write targets `:655`), and `test_write_targets_are_html_escaped` (`:401`)
  proves it. Good.
- The `failed`/`empty` distinction on `BashAnnotations` is a clean way to say
  "we could not compute this" without rendering a wrong annotation; case 9
  asserts both flags.
- Real mode policy from Item 8 for the record: `default` and `None` cards are
  byte-identical for the unknown, denied and ask-pattern shapes; the redirect
  shape gains the new block in *every* mode, which case 4b explicitly
  anticipates ("§3c is an improvement in every mode, not an auto-mode special
  case"). No unintended regression found.
- No dead code, no commented-out blocks, no dropped `except` branches, no
  signature changes to any pre-existing helper. `_format_non_whitelisted`'s
  3-tuple unpack still matches `_unallowlisted_bash_parts`' return shape.

## Questions for User

None. Issue 1 is a comment fix and Issue 2 is cosmetic wording; neither changes
behaviour, and neither is worth holding the task for. Issues 3–5 are
future-proofing notes.

## Verdict rationale

PASS. The requirement this task exists for — the card stops claiming the
allowlist raised a prompt the harness raised — is implemented, is guarded by
tests whose assertions are full-output equality rather than "no exception", and
those tests survive an independent mutation attempt from me. The one
load-bearing claim I could have been fooled by (the frozen baseline literal) I
re-derived from the pre-change code and it is correct. Scope is clean; nothing
outside the named files moved; `pretool_hook.py` is untouched.
