# Task 27 — PostToolUse resolves the *wrong* pending row when two prompts are open in one session (bug)

**Status:** todo · **Type:** bug · **Created:** 2026-08-26 · **Rev:** 2
**Priority:** high · **Suggested worker:** sonnet · **Size:** S–M
**Scope:** this repo only — `permission_state_store.py`, `posttool_hook.py`,
tests. No relay change, no store schema change, no `amux` change.
**Read first:** §1 (the measured case), §4 (the fix — *including §4.1's trap*),
§6 **H1/H2**, §9 (handover).

**Why a task and not an epic.** One root cause, one missing discriminator, one
production caller. No new request state, no new stored field (the rows already
carry the evidence), no process-identity work, no two-layer design — which is
what makes [Epic 26](./26_interrupted_request_cleanup/) an epic. Two regressions
plus three controls settle it in one session.

**Numbering.** Epics **22–25 are reserved by a remote machine**; 26 is the
interrupt-cleanup epic. This is 27, and depends on neither.

**Relation to Epic 26.** Adjacent code, opposite direction, independent fix.
Epic 26 is *a card outlives its dead hook*. This is *a live hook's card is
closed by somebody else's answer*. Both touch `resolve_via_terminal`, so
whichever lands second re-runs the other's tests.

## 1. Symptom (measured on this machine, 2026-08-26)

Session `ced7c9c4`, `~/hyppie-flow/hyppie-flow`. The agent fired **two
`AskUserQuestion` calls in parallel** in one turn — transcript
`e8c22d32`/`0366e7ac`, assistant text *"fire the AskUserQuestion calls in
parallel — heartbeat+deploy ask to operator, Q-531 to @htl"*:

| Row | Msg | Question | Role | Sent |
|---|---|---|---|---|
| `42abc561894c` | 5059 | Heartbeat / deploy cut | `operator` | 16:43:51Z |
| `1c83289e64ad` | 5060 | **Q-531** (biome `tests/**`) | `htl` | 16:43:57Z |

Both delivered — `Sent 1 question messages; waiting for answers` at
`16:43:51.347` and `16:43:57.311` — and both to the *same chat*, since
`hyppie-flow` binds `htl = "operator"`.

The operator answered **the heartbeat, in Telegram**, at `17:04:13.582Z`. What
happened to **Q-531**, which nobody touched:

- its row reads `state: resolved_terminal`, `resolution_source: terminal`,
  written at `17:04:13.804Z`;
- its `terminal_answers` holds **the heartbeat's** question text and answer
  (`"Cut it (nothing blocking)"`);
- message 5060 lost its keyboard and was patched with
  `TERMINAL_ANSWER_FALLBACK_TEXT` — *"✅ Answered in the terminal"* — because the
  heartbeat's answer did not key-match Q-531's question
  (`permission_request_hook.py:736-748`);
- its own parked hook logged `Question 1c83289e64ad relay state=cancelled`
  (`17:04:13.977`), read that as a terminal win (`:990-999`), and exited with no
  decision (`:1417`), dropping Q-531 into the native TUI prompt.

Operator-visible result: **the Telegram card for Q-531 silently became an
already-answered card for a question it never asked**, and the question it *did*
ask came back to the terminal. Reported as *"this question never got forwarded to
Telegram"* — it had been, twenty minutes earlier.

## 2. Root cause

`find_pending_request_by_tool_session` (`permission_state_store.py:738`) matches
on `session_id + tool_name + cwd + agent_id` only, then returns **the most recent
pending row** (`:797-798`). `posttool_hook.py:203` calls it with the answered
tool's identity but **not its `tool_input`**, which it already holds (`:188`), so
nothing distinguishes two prompts of the same tool in the same session.

The heartbeat's own row was flipped to `reply` by its own hook at `17:04:13.582`,
**0.22 s before** PostToolUse ran, so the only pending candidate left was Q-531's.

The consequence is spelled out — as an assumption, not a bug — at
`permission_request_hook.py:660-663`: *"The PostToolUse hook only flips the most
recent pending child … but that signals the whole AskUserQuestion was answered at
the keyboard."* True while at most one call per session is in flight. False for
parallel calls, which this workspace's PM loop issues **by design** (heartbeat to
`operator` + a role-tagged push in one turn), so it recurs.

The defence that *does* exist is scoped one level too narrowly: each child of a
group is marked `REPLY` as its answer arrives, with the comment *"so the
PostToolUse hook's pending-request sweep won't later try to cancel its
(already group-finalized) relay message"* (`:964-972`). That protects siblings
**within** a call. Nothing protects a row belonging to a **different** call.

Nothing downstream is wrong: `_terminal_win` and `_finalize_on_terminal_win`
(`:673`, `:704`) only ever inspect their own group's children. They were handed a
row corrupted before they saw it. **Fixing the match fixes both symptoms**, and no
change is needed in `permission_request_hook.py` beyond a stale comment (§9).

## 3. Blast radius

- **Any two prompts of the same tool open at once in one session.** The matcher
  is tool-agnostic.
- **Bash is the same shape and far more common** — parallel gated commands are
  routine. Approving one in Telegram strips the *other* card's buttons by the
  same path; the victim hook's `wait_for_response` (`:327`) then sees
  `resolved_terminal`, cancels its own message and returns `None`, so that
  command silently falls back to the terminal prompt. *(Reasoned from the code
  path;* ***not separately reproduced***.*)*
- **Sub-agents are already safe** — `agent_id` is part of the match. That is the
  precedent this fix follows: add the discriminator that exists.
- **No other production caller is affected.** `find_pending_request_by_tool_session`
  has exactly one (`posttool_hook.py:203`); `get_pending_request_for_session` /
  `find_pending_request_by_session` are referenced only from
  `tests/test_unit_state_store.py`. `reply_injector.py` does not look rows up at
  all. (Verified by grep over `.claude/` and `tests/`, 2026-08-26.)

## 4. Fix

Give the matcher the discriminator it is missing.

### 4.1 The trap — do not "fall back to most recent" on no match

The obvious shape ("prefer rows whose `tool_input` matches, else today's
behaviour") **reproduces this exact bug**. In §1 the answered row was already
`reply`, so a match-preferring matcher finds *nothing* to prefer, falls through,
and picks Q-531 again. The fallback must never be reachable by a row that has been
**definitively excluded**.

The correct rule is three-valued, per candidate row:

| Verdict | Meaning | Eligible? |
|---|---|---|
| **same call** | the row's stored input identifies this call | yes — preferred |
| **different call** | the row's stored input identifies a *different* call | **no — excluded outright** |
| **cannot tell** | no comparator for this shape, or nothing to compare | yes — legacy fallback |

Then: **prefer "same call"; otherwise use "cannot tell"; if the eligible set is
empty, return `None`** (PostToolUse then writes nothing, which is the correct
outcome for a call whose row is already resolved). Most-recent-by `created_at`
stays the tiebreak within whichever set is used.

With `tool_input=None` every row is "cannot tell", so **existing callers and
tests behave exactly as today**.

**Returning `None` loses no cleanup.** On the Telegram path the answering hook
has already stripped its own keyboard and marked its own row
(`permission_request_hook.py:383`, `:967-972`, plus the relay's own group
finalize), so PostToolUse finding nothing is the *correct* no-op — today it only
appears to do work there because it is closing somebody else's card.

### 4.2 Comparators — verified shapes only

- **`AskUserQuestion`** — the row's `tool_input['question']` string against the
  posted `questions[*]['question']`. Present → same/different call; absent or
  posted `questions` unusable → cannot tell.
  **Compare the question text, never the whole dict.** Two reasons, both live:
  role-routed rows store the *alias-stripped* header (`permission_request_hook.py:1150-1163`
  — Q-531's row holds `'Q-531'` where the payload says `'@htl Q-531'`), and a
  Telegram-answered call reaches PostToolUse with `updatedInput` applied, i.e.
  `{**tool_input, 'answers': {...}}` (`:1342-1352`). The question strings survive
  both; the dict does not. Verified across the store: **all 721** AskUserQuestion
  rows carry a `question` key.
- **`Bash`** — `tool_input['command']` equality. Both non-empty → same/different
  call; otherwise cannot tell. `command` is the identity; `description` is model
  prose and buys nothing.
- **Everything else** — cannot tell. Add a tool here only after its payload has
  been observed on both sides (§4.4); an unverified comparator that returns
  "different call" wrongly is the H2 failure.

### 4.3 Legitimate multiple matches

A multi-question call creates one child row per question (`:1153`), and
escalation (15-05) sends a *duplicate group* with fresh rows for the same
questions (`:897-919`). Several rows matching is normal — keep the `created_at`
tiebreak. Flipping one member is exactly what the group finalize path expects
(`:660-663`, `:723-727`).

### 4.4 Probe first (≈5 min, optional but cheap)

`~/.claude/posttool_debug.log` does not exist — PostToolUse runs without
`CLAUDE_HOOK_DEBUG=1`, so **no real PostToolUse `tool_input` has been read on this
machine**; §4.2's `updatedInput` claim is read off the hook's own return value,
not off a captured payload. Add the env var to the PostToolUse entry in
`~/.claude/settings.json`, run one gated Bash, and diff the logged `tool_input`
against the stored row. That settles the Bash comparator outright. The
AskUserQuestion comparator does not depend on it (question text is immune either
way), so this does not block the work.

### 4.5 Call site

`posttool_hook.py:203` passes `tool_input=tool_input` (in scope at `:188`).
`resolve_via_terminal` and the revoke path are untouched.

## 5. Out of scope

- **Store schema.** Rows already carry `tool_input`; no new field, no migration,
  no `from_dict` change.
- **`tool_use_id` correlation.** The PostToolUse payload carries one
  (`tasks/15_human_roles/fixtures/posttool_askuserquestion.json` `_payload_keys`),
  but `PermissionRequest` does not, so a row cannot record it without reading the
  transcript — the heuristic Epic 26 §2.2 already declined. `tool_input` is on
  both sides today; use it.
- **Repairing rows `1c83289e64ad` / `42abc561894c`.** Evidence, not work items.
- **The relay.** It behaved correctly: it was told to cancel 5060.
- **Telling the operator *why* a card closed.** Same call as Epic 26 §2.2.

## 6. Hazards

- **H1 — fail open.** This runs on the path every tool call takes. No exception
  may escape the comparator; on any error treat the row as "cannot tell" and log.
- **H2 — a wrong "different call" verdict is worse than the bug.** It leaves the
  card live with no owner — Epic 26's orphan, manufactured at scale. This is why
  §4.2 ships two comparators and not a generic one, and why the single-prompt
  controls (§7.3–7.5) are not optional.
- **H3 — cost on the hot path.** Already one full pass over a 3.7 MB / 1641-row
  JSONL per tool call (Epic 26 H5). This adds a string compare per candidate and
  no second pass. Confirm, don't assume.
- **H4 — identical parallel questions.** Two byte-identical questions asked in
  parallel stay indistinguishable and `created_at` picks one. Harmless — the
  answer fits either — but say so in a comment rather than leaving it to be
  rediscovered.
- **H5 — a repo edit is not live.** `.claude/hooks/*` reaches `~/.claude/hooks/`
  only via `./install-claude-config.sh`. Any "works now" claim must name which
  copy it was tested against.

## 7. Acceptance

1. **Regression — AskUserQuestion.** Two pending rows, same session/tool/cwd,
   different questions; the first is resolved (`reply`) as Telegram does it, then
   PostToolUse fires with the *first* call's `tool_input`. It must find **no row**
   and write nothing: the second row stays `pending`, `terminal_answers` stays
   `null`, its message is not revoked.
2. **Regression — Bash.** Same, with two different `command`s.
3. **Control — terminal answer.** A single pending `AskUserQuestion` answered in
   the TUI still resolves `terminal`, still records the reduced answers, still
   revokes its message.
4. **Control — group.** A multi-question call still finalizes: PostToolUse flips
   one member, the parked hook patches and strips every sibling
   (`test_integration_permission_request.py:567`
   `test_handle_ask_user_question_terminal_revokes_all_group_messages` must stay
   green).
5. **Control — legacy rows.** A row whose `tool_input` is empty (3 exist in the
   live store, argument-less MCP calls) is still matched, and every existing
   `find_pending_request_by_tool_session` call with no `tool_input`
   (`test_integration_permission_request.py:712,721,730,758,767`) still behaves
   as before.
6. `python3 tests/run_all_tests.py` green, before/after counts reported.
7. **Live.** Installed with `./install-claude-config.sh`, then the §1 scenario
   re-run: heartbeat + role-tagged question in parallel, heartbeat answered in
   Telegram, **the second card still answerable in Telegram**. Record the log
   lines and say which copy of the hooks was exercised.

## 8. Evidence trail

- `~/.claude/permission_requests.jsonl` — rows `42abc561894c` (`reply`,
  `17:04:13.582683Z`), `1c83289e64ad` (`resolved_terminal`, `17:04:13.804374Z`).
- `~/.claude/permission_request_debug.log` — `16:43:51.347`, `16:43:57.311`,
  `17:04:13.643`–`17:04:14.079`.
- Transcript `~/.claude/projects/-home-anton-hyppie-flow-hyppie-flow/ced7c9c4-8f1f-479b-ba47-c59874a0ee99.jsonl`
  — `e8c22d32` and `0366e7ac`: two `AskUserQuestion` `tool_use` blocks, one turn.
- No `~/.claude/posttool_debug.log`: PostToolUse's side is inferred from the rows
  it wrote, not read from a log (§4.4).

## 9. Handover

**Files.** `permission_state_store.py` (comparator + the three-valued filter in
`find_pending_request_by_tool_session`), `posttool_hook.py` (`:203`, pass
`tool_input`), `tests/test_integration_permission_request.py` (new cases; the
existing ones must stay untouched and green).

**Where the tests go.** `TestCrossAgentIsolation`
(`test_integration_permission_request.py:689`) is the same family — *don't match
the wrong row* — and already builds rows with `create_request` and asserts on the
finder directly. Extend it, or add a sibling class next to it.

**Test at the store level.** Nothing exercises `posttool_hook.main()` end to end
today: coverage is `reduce_tool_response` (`tests/test_unit_terminal_answers.py`)
and `revoke_telegram_message` (`TestPostToolRoleRevoke`, `:1346`). So assert
§7.1/§7.2 as *the finder returns `None`* — no row found is what makes the revoke
unreachable. A `main()`-level test would need a new stdin/exit harness; worth it
only if it falls out cheaply.

**Stale comments to correct in the same change** — both encode the assumption
this task removes, and will otherwise re-teach it to the next reader:
`permission_request_hook.py:660-663` ("the most recent pending child … signals the
whole AskUserQuestion was answered") and
`tests/test_integration_permission_request.py:576-578` and `:590-591` ("the
PostToolUse hook only flips the *most recent* child" / "mirrors
`find_pending_request_by_tool_session` returning the most recent row").

**Sequencing.** Independent of Epic 26; either order. If 26-02 lands first its
`sweep_orphaned_requests` will also be reading these rows — re-read before
writing.

**Definition of done.** §7.1–7.6 green against the repo copy, §7.7 recorded
against the installed copy, and this file updated with the probe result from §4.4
(what a real PostToolUse `tool_input` contains) whether or not it changed the
comparator.

---

## 10. Implementation log

- **2026-08-27 — landed.** Implemented, reviewed and fixed; committed on `main`.
  Landed **second** relative to epic 26, so per that epic's cross-link the sweep,
  bounded-lock and signal-handler classes were re-run: **22/22 green**. Full suite
  1300 → 1308 ran / 1307 passed (1 pre-existing unrelated error).
  Review confirmed §4.1's trap is closed: a **"different call"** row is excluded
  outright at `permission_state_store.py:1163` and is unreachable by any fallback,
  tiebreak or exception path — verified by code trace, by an ad-hoc probe of the
  §1 scenario, and by direct invocation of `_classify_candidate`.
  Both AskUserQuestion payload transformations named in §4.2 now have tests that
  were **watched to fail** under whole-dict comparison: the alias-stripped header
  (`'Q-531'` stored vs `'@htl Q-531'` posted) and the `updatedInput` answers merge.
  Comparator errors fall to `cannot_tell`, never to exclusion, and now log the tool
  name and row id.

**Still open — operator only:**

- **§4.4 probe (deliberately not run).** It requires adding `CLAUDE_HOOK_DEBUG=1`
  to the PostToolUse entry in the live `~/.claude/settings.json`; no agent edited
  that file. Consequently the **Bash** comparator's payload shape rests on
  code-reading rather than a captured payload. The **AskUserQuestion** comparator
  does not depend on the probe — question text survives both transformations
  either way.
- **§7.7 live end-to-end.** The heartbeat + role-tagged parallel-question scenario
  in a real Telegram chat. Not reproducible from an agent.
