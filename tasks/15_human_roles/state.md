# Epic 15 — Human roles: execution state

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
There is **no epic-level `architecture.md`** — the design that would live in one
is brd §2 (constraints) and §5 (routing behaviour); pass those sections to every
implementer and reviewer. The top-level `architecture.md` is *updated by* 15-06,
not read as input.

**No Phase 0.** Every cross-cutting decision is already locked (below). Nothing
needs resolving before the first agent is spawned.

Each task file is written for a fresh-context agent and carries its own
"read first" refs, done criteria, and tests.

`fixtures/posttool_askuserquestion.json` holds real captured hook payloads.
15-04 requires its tests be built from that file — pass it to that implementer.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 15-01 | [Role config loader](./15-01-role-config-loader.md) | done | — | Pure `roles_config.py`: both TOML files, alias table, token/reference chase, escalation precedence. Also adds the one `REQUIRED_HOOKS` installer line — see Ordering |
| 15-02 | [Multi-destination transport](./15-02-multi-destination-transport_opus.md) | done | 15-01 | Token-keyed client registry, `role` on the state-store row, role-aware PostToolUse revoke |
| 15-03 | [Alias routing](./15-03-alias-routing.md) | done | 15-02 | Header parse/strip, destination resolution, mixed-role deny, send-failure retry, rendering |
| 15-04 | [Wait phase](./15-04-wait-phase_opus.md) | done | 15-03 | Sequential loop → thread-per-message; terminal answers patched into the role's chat. **Amended 2026-08-06** — G3 fix, see *Post-mortem* below |
| 15-05 | [Escalation](./15-05-escalation_opus.md) | done | 15-04 | Deadline, duplicate group to the default, first-group-wins |
| 15-06 | [Installer, diagnostics, docs](./15-06-installer-diagnostics-docs.md) | done | 15-05 | `shell/claude-roles`, example TOML, prompt snippet, installer, `architecture.md` |
| 15-07 | [Live verification](./15-07-live-verification_human.md) | done | 15-06 | **human** — first run 2026-08-06: G3 failed. Fixed same day under **15-04** + the relay server (`3de5d1e`). **Full re-run 2026-08-06 with h02 redeployed: all eight steps pass (six briefed + two added live for the uncontaminated terminal win), G1–G4 pass, error-log gate clean (delta 0 bytes).** See *Live re-run — 2026-08-06* below |

## Implementer model

15-02, 15-04 and 15-05 carry the `_opus` filename suffix. Per
`docs/prompts/implementation_manager.md`, that selects the Implementer model:

- **15-02** — cross-cutting across the router, the state-store schema, and
  PostToolUse, with a threading-visible client registry.
- **15-04** — concurrency rewrite plus a state-store field. The class of change
  the manager prompt names explicitly.
- **15-05** — a race between two live groups, decided across threads.

Everything else is sonnet. Reviewer stays sonnet throughout.

## Ordering

Strictly sequential. Each task edits the code the next one builds on, and the
dependency column is the whole story.

Two seams that look like misplacements and are not:

- **15-03 does not touch the wait loop's structure** (only two call sites, to
  pass a token) because **15-04 replaces it wholesale**. Written into both task
  files. An implementer who "helpfully" threads 15-03 will have that work deleted
  and reviewed twice.
- **15-01 adds the `REQUIRED_HOOKS` installer line**, not 15-06. From 15-02
  onward the router imports `roles_config`, and the installer copies only what
  that list names — so an `install-claude-config.sh` run between 15-02 and 15-06
  would otherwise deploy a router that cannot import its own dependency and
  silently lose Telegram. 15-02 additionally guards the import as a second layer.

Two things can start early with spare capacity:

- **15-06's `docs/roles.example.toml` and `docs/roles-prompt-example.md`** need
  only the brd. Writing them alongside 15-01 is a useful check on the config
  format — if the example is awkward to write, the format is wrong.
- **15-01 is fully independent** of the relay and of every hook: pure functions
  over TOML, buildable and testable with nothing else in place.

## Decisions locked

Settled in design review. Do not relitigate without a brd revision.

1. **`@alias` on `header` is the routing tag.** Not a new tool input field —
   `AskUserQuestion`'s schema is closed (brd §2.1). Not `metadata.source`, which
   is documented as analytics and could be filtered.
2. **One installation token per role, held in the machine's relay config.** No
   relay-server schema change, no `role` on the wire, no role-aware bind codes.
   Rejected alternative: server-side named bindings — cleaner in the abstract,
   but it needs a schema migration and rework of the webhook's
   chat→installation attribution for a first cut.
3. **`roles.toml` carries no descriptions.** Aliases, titles, default,
   escalation. The prose an agent reads to *choose* a role is free-form in
   `CLAUDE.md` or any prompt file. No generated block, no sync command, no drift
   check — the two files answer different questions and are allowed to differ.
4. **One role per call.** Mixed → `behavior: deny` with an explanatory reason.
   This is what keeps one call = one group = one chat and leaves the relay's
   group finalization untouched (brd §2.2).
5. **Wait indefinitely by default.** No auto-deny, ever — the native terminal UI
   is always live. `escalate_after` is opt-in per role.
6. **Unreachable ≠ unanswered.** A role with no binding, a broken alias chain, or
   a failed send reroutes to the default *immediately*, with the reason printed
   in the message. Only genuine silence waits.
7. **Terminal answers propagate.** Whoever was asked sees what was decided, not a
   dead prompt.
8. **Permissions and idle notifications stay default-only** (brd §6).
9. **No `roles.toml` = today's behaviour, byte for byte.** Every task asserts
   this; it is the first done criterion in 15-03 for a reason.

## Standing risks

Two things every reviewer on this epic should check, because they cross task
boundaries and no single task owns them:

- **The compatibility floor.** A workspace with no `roles.toml` must produce
  identical relay calls, not merely a working flow. Assert on the recorded calls.
- **Test isolation.** `find_roles_file` walks *up* the filesystem and honours
  `CLAUDE_PROJECT_DIR`, which is set inside a Claude Code session. A test that
  passes a real path can read config outside its fixture — and this repo may
  itself gain a `.claude/roles.toml`. 15-01 specifies the isolation; later tasks
  must keep it.

## Verified during planning

`PostToolUse.tool_response` for `AskUserQuestion` is a **dict**
`{questions, answers, annotations}` — captured from a live hook payload on
2026-08-02 and confirmed byte-identical to the transcript's `toolUseResult`.
Real payloads for three answer shapes (option selected, free text, notes-only)
are committed at `fixtures/posttool_askuserquestion.json`; 15-04's tests are
built from them.

This started as an assumption that the prose string the *model* sees
(`"Q"="A"`) was also what the hook receives. It is not, and a parser written
against it would have silently mangled roughly half of real calls — the
regex matched 2 of 4 answers on a real multi-question call. Worth remembering
the next time a hook payload shape looks obvious.

## Live run — 2026-08-06

Workspace `ai-playground-2`; operator = installation `anton-t480s` (id=2, the
default token), ux = installation `anton-roles-test` (id=5), role `ux` carrying
`escalate_after = "1m"`. Pre-flight green: `claude-roles --check` reported both
roles `bound`, and no deployed hook differed from the repo (only
`test_task03.py`, which is not deployed). Driven live by Anton at the keyboard
with both chats open; full turn-by-turn log in
`ai-playground-2/15-07-live-run-notes.md`.

**G1 pass · G2 pass · G3 FAIL · G4 pass.**

| Gate | Result | Evidence |
|---|---|---|
| G1a — tagged | **pass** | Landed in the **ux chat only**, operator empty. Body carried italic `for: UX/UI designer`; header rendered `Layout` with `@ux ` stripped; terminal chip kept raw `@ux Layout`. Agent received `…="Top nav"`, no attribution suffix |
| G1b — untagged | **pass** | Landed in the **operator chat only**, ux empty. Header `Deploy target`. A `for: Operator` line **is** rendered on untagged questions. Agent received `…="Staging"`, no suffix |
| G2a — escalation, operator answers | **pass** | Fired 14:52:40, duplicate in operator chat at ~14:53:40, first line exactly `⏳ @ux (UX/UI designer) hasn't answered in 1m — you can decide instead.` followed by `for: Operator`. ux chat's keyboard still live at that moment. After answering the duplicate, ux message patched to `✅ Boxed in card`, keyboard gone. Agent received **`Boxed in card (answered by Operator)`** |
| G2b — answered in time | **pass** | Fired 15:16:13, answered in ux chat 15:16:46 (33s). Watched operator chat to ~15:18:30 — **no duplicate ever appeared**, nothing at all. Agent received `…="Filled red"`, no suffix |
| G2c — duplicate, ux answers anyway | **pass** | Fired 15:21:41, duplicate with ⏳ banner and live keyboard in operator chat ~15:22:41. Answered in ux chat → operator duplicate patched to `✅ Inline per field`, keyboard gone. Agent received `…="Inline per field"`, no suffix |
| G3a — terminal win, option picked | **FAIL** | Run twice (15:26:01, 15:36:45), identical both times. Answered `Amber and charcoal` in the terminal, Telegram untouched. **ux chat body left byte-identical and unpatched, no `✅` line at all**; keyboard stripped. Captured `PostToolUse` payload shows `answers: {"Which accent palette should the dashboard use?": "Amber and charcoal"}` — the input is well-formed |
| G3b — terminal win, notes only | **FAIL** (notes sub-case **not exercisable**) | Re-run 15:49:58, two tagged questions, answered entirely in the terminal. **Neither message patched**, and `@ux Typography`'s **keyboard was left live** while `@ux Density`'s was stripped. Payload again well-formed (`annotations: {}`). The notes-without-selection case could not be produced by the picker UI — see below |
| G4a — permission prompt | **pass** | `chmod` was auto-approved without prompting; `frobnicate-15-07 --check` raised a real request. Arrived in the **operator chat only**, ux empty, **no `for:` role line** (correct per §6). Tapped **Allow** → message finalized, command reached the shell (exit 127, `command not found`) |
| G4b — idle notification | **pass** | Arrived 16:29 in the **operator chat only**, ux empty: `ai-playground-2` / `💤 Idle — waiting for input` plus the quoted last message. Unchanged from pre-epic behaviour |

Two optional extras were also run, both **pass**: an unknown alias (`@uxx`)
reached the operator chat carrying `⚠️ Unknown role @uxx — routed to Operator.`
with the bad tag stripped from the rendered header; and a mixed-role call
(`@ux` + `@op`) was denied with *"addressed 2 different human roles (@ux ->
UX/UI designer, @op -> Operator) … Split this into one AskUserQuestion call per
role"*, sending **nothing to either chat** and flashing the terminal picker for
about a second before it withdrew.

### The failure — G3, terminal win (owning task: 15-04)

**Expected** (brd §5.5): when the answer arrives at the keyboard, every role
message is patched with the terminal's answers and then has its keyboard
stripped, degrading at worst to `✅ Answered in the terminal`.

**Observed, in the ux chat, on all three terminal-answered runs:** the answer is
never patched in. The body stays byte-identical to what was sent — no `✅` line,
not the real answer, not the generic fallback, not `(notes only)`. The keyboard
is stripped inconsistently: stripped on both single-question runs and on the
second message of the two-question run, but **left live on the first message**
of the two-question run, leaving an orphaned prompt inviting an answer to an
already-finished question.

Nothing was written to `~/.claude/permission_telegram_errors.log` during any
terminal-win run — the failure is entirely silent.

The discriminator is **terminal vs Telegram**, not question count. Every
Telegram-answered run in the session patched correctly (G1a, G1b, G2a, G2b,
G2c, the unknown-alias extra, and one mis-executed G3b attempt); no
terminal-answered run did. An early reading that blamed single-question calls
was retracted once that mis-executed run was identified — it had been answered
in Telegram, which its `2.` button-index prefix and `✍️` text-reply marker
gave away.

Per the task file, the payload was captured rather than guessed at: a
`PostToolUse` stdin-dumping hook was added to
`ai-playground-2/.claude/settings.local.json` and the case re-fired. The
captured `tool_response` is exactly the shape `fixtures/posttool_askuserquestion.json`
documents, with a correctly keyed `answers` map — so **15-04's input is not the
defect**; the answers are simply never applied to the role chat's messages.
Dumps are in `ai-playground-2/15-07-posttool-dump.log`.

**The `(notes only)` sub-case is real — but not reproducible on demand.**
*(Corrected 2026-08-06 after the diagnosis below.)* The live run could not
produce it through either known picker variant, and this section previously
concluded it might not occur at all. It does:
`fixtures/posttool_askuserquestion.json` case 3 is a **live capture from
2026-08-02** carrying `answers[q] == "(notes only)"` with the real content in
`annotations[q]["notes"]` — two of the four questions in that call, in fact.
Which picker variant produces it is still unknown, so **G3b's notes sub-case is
retired from the manual gate set** and covered by unit test instead
(`test_notes_only_answer_reaches_the_chat_as_the_note`). Re-run G3b for the
option-selected and free-text shapes only.

### New lines in `~/.claude/permission_telegram_errors.log`

Baseline 214443 bytes → 214749 bytes. Three lines, all identical in kind:

```
[2026-08-06T10:42:47Z] Relay cancel_message failed: relay HTTP 500: Internal Server Error
[2026-08-06T10:55:02Z] Relay cancel_message failed: relay HTTP 500: Internal Server Error
[2026-08-06T11:23:04Z] Relay cancel_message failed: relay HTTP 500: Internal Server Error
```

Local time is UTC+4, so these map to **G1a (14:42), G2a (14:55) and G2c
(15:23)** — the three runs where an escalation duplicate existed and a losing
group had to be finalized. All three of those runs **passed** every visible
assertion: both chats were patched and both keyboards came off. So a
`cancel_message` is returning HTTP 500 on the loser-finalization path without
any user-visible consequence. Not a gate failure and not diagnosed here, but it
is a real server-side error being swallowed, and it is worth a look under
**15-05 / 15-02** before this epic closes.

*(Diagnosed and fixed 2026-08-06 — see Fix B below. The attribution above is
right: all three are `_finalize_losing_groups`.)*

## Post-mortem and fixes — 2026-08-06

Diagnosed from `~/.claude/permission_request_debug.log`, which had recorded the
whole thing. The live report located the 500s correctly (`_finalize_losing_groups`)
but **neither defect worked the way anyone expected**, and G3's was not in the
code the report pointed at.

### G3 root cause: a terminal win arrives disguised as group death

`_finalize_on_terminal_win` was never the problem — it was never *called*. The
log for the 11:26 UTC run (G3a), start to finish:

```
11:26:07  Sent 1 question messages; waiting for answers
11:26:13  Question 583214c0d539 relay state=cancelled
11:26:13  Every question group is unanswerable; falling back
11:26:13  AskUserQuestion: no Telegram answer; native UI will handle
```

`posttool_hook` does two things in order: `resolve_via_terminal` writes
`terminal_answers` and flips the row, **then** `revoke_telegram_message` cancels
the relay message. The parked wait loop's child thread sees that cancel and
reports `state=cancelled`; the main loop's drain marks the group unanswerable
and returns at the group-death exit — *before* looping back to the terminal
check, which sat only at the top of a tick. The terminal-win path lost to the
group-death path by one iteration, deterministically, every time.

So the keyboard stripping the report observed was **PostToolUse's own
single-message revoke**, not the wait loop. That also explains the G3b
inconsistency exactly: with two questions PostToolUse revokes one row
(`find_pending_request_by_tool_session` returns a single row), one cancel is
enough to trip `all(unanswerable[gk] for gk in groups)`, and the sibling it
never touched keeps a live keyboard forever. Confirmed in the 11:50 log:
`Sent 2 question messages`, one `state=cancelled`, immediate fallback.

**Fix A** — `permission_request_hook.py`. Terminal detection is now `_terminal_win()`
and is consulted at **every** exit from the wait loop, not just the top of a
tick. The group-death exit gets a 1.5s grace re-poll (`TERMINAL_GRACE_SECONDS`):
the ordering already guarantees the row is written before the cancel is
observable, but the costs are asymmetric — a wrongly-negative answer silently
leaves a chat unpatched, a needless second on a dead group costs nothing.

### Fix B: `cancel_message` 500s whenever a finalize succeeds

`finalize_message` PATCHes the body and then cancels. The PATCH is
`editMessageText` with no `reply_markup`, **which already removes the keyboard**
— so the cancel that follows asks Telegram to strip a keyboard that is no longer
there, and gets `400 Bad Request: message is not modified`. `TelegramApiError`
was caught nowhere in `app.py`, so FastAPI turned it into a bare HTTP 500. That
is the happy path of every finalize, which is why all three 500s sat on runs
that passed every visible assertion.

Fixed in `relay-server/relay_server/app.py`: `cancel_message` and
`patch_message` treat "not modified" as success (both endpoints state an intent
— *this message has no keyboard* — not a diff), surface any other
`TelegramApiError` as a **502** instead of an anonymous 500, and wake the
long-pollers on the failure path too.

Worth knowing for the re-run: `client.py:186` retries 5xx, so each of those
three logged lines was actually several round trips. The fix removes the retry
storm along with the log line.

**Fix A and Fix B are coupled**: `_finalize_on_terminal_win` uses the same
`finalize_message`, so without Fix B the restored terminal-win path would 500 on
every single call. It would still *look* correct — the PATCH does the visible
work — and only the error log would show it.

### Fix C: `revoke_telegram_message` reported success unconditionally

It returned `set_message_reaction`, a no-op shim that always returns True, and
discarded the actual `remove_inline_buttons` result — making `posttool_hook`'s
"Revoked Telegram message" log line unfalsifiable. Now returns the cancel's own
result.

### Why the unit tests were green through all of this

They covered the *pure* pieces thoroughly — `reduce_tool_response`,
`parse_terminal_answers`, the 8KB backstop, the row round-trip — and
`_finalize_on_terminal_win` in isolation. Nothing drove `_wait_for_group_answers`
to the exit under which a terminal win actually arrives. The four new tests in
`TestConcurrentWaitPhase` force PostToolUse's exact interleaving (the relay
parks until the loop has performed one terminal check, and only then reports
`cancelled`); **each was confirmed to fail against the pre-fix code** — the
first-pass version of them passed against it, because the row was flipped early
enough that the top-of-tick check won and the exit under test was never reached.
A regression test for a race is worth exactly as much as its proof that it fails.

### Status *(superseded — the re-run happened; see *Live re-run — 2026-08-06* below, where 15-07 passes and closes)*

15-07 stays **blocked pending the re-run**. Fixes A–C are in with tests
(hooks 691 pass, relay 136 pass), so the **entire gate set is now re-run** — per
15-04's own instruction, since these paths share the wait loop.

Two things to do before the re-run:

1. **Deploy the relay server.** Fix B lives on
   `claude-hooks-tg-relay.h02.activecdn.net`; the hooks alone do not carry it.
   Without it G3 will still *appear* to pass and quietly re-fill the error log.
2. **Re-install the hooks** (`./install-claude-config.sh`).

And re-baseline `~/.claude/permission_telegram_errors.log` by byte count first —
that delta is what caught Fix B, and this run should produce **zero** new lines.

## Live re-run — 2026-08-06

Workspace `ai-playground-2`; operator = installation `anton-t480s` (id=2, default
token), ux = installation `anton-roles-test` (id=5), role `ux` carrying
`escalate_after = "30s"` (halved from the first run). Pre-flight green:
`claude-roles --check` reported both roles `bound`, and no deployed hook differed
from the repo (only `test_task03.py`, which is not deployed). **Anton confirmed
the relay on `claude-hooks-tg-relay.h02.activecdn.net` was redeployed since
`3de5d1e`**, so Fix B was live for this run — which §6 then corroborates
independently. Driven live by Anton at the keyboard with both chats open; full
turn-by-turn log in `ai-playground-2/15-07-live-run-notes.md`.

**All eight steps pass · §6 clean (delta 0 bytes) · G1–G4 all pass.**
(Six from the re-run brief, plus steps 7 and 8 added live at Anton's request to
capture the *uncontaminated* terminal win — see the table.)

| Step | Result | Evidence |
|---|---|---|
| 1 — terminal win, two tagged questions (G3, 15-04) | **pass** | Both `@ux Palette` and `@ux Typeface` landed in **ux only**, both bodies carried `for: UX/UI designer`, both keyboards live, terminal chips kept the raw `@ux …`. Answered **both in the terminal picker**. Both ux messages patched to `✅` with the **real labels** — not `Answered in the terminal`, not `(notes only)` — and **both** keyboards gone (confirmed explicitly for the sibling). Agent received `…="Amber and charcoal", …="Inter"`, no suffix. **All three 2026-08-06 failure modes absent.** |
| 1b — bonus: terminal win *after* escalation | **pass** | Anton exceeded 30s at the picker, so the duplicate group fired into **operator** mid-step. The terminal win then had to finalize **two** groups: both operator duplicates were also patched to `✅ <real label>` with keyboards gone. Banner first line verified verbatim: `⏳ @ux (UX/UI designer) hasn't answered in 30s — you can decide instead.` |
| 2 — tagged, answered in Telegram, no escalation (G1a, 15-03) | **pass** | `@ux Layout` landed in **ux only**, operator empty. `for: UX/UI designer` present; header rendered `Layout` with `@ux ` stripped; terminal chip kept raw `@ux Layout`. Answered in ux within 20s, then watched operator a further 60s — **no duplicate, no question of any kind**. Agent received `…="Left sidebar"`, **no** suffix |
| 3 — untagged (G1b, 15-03) | **pass** | `Deploy target` landed in **operator only**, ux empty. A `for:` line **is** rendered on untagged questions — italic `for: Operator`, same as the first run. Agent received `…="Staging"`, **no** suffix |
| 4 — escalation, operator answers the duplicate (G2a, 15-05) | **pass** | Fired 19:09:24, duplicate due 19:09:54 and arrived on schedule in **operator**, first line exactly `⏳ @ux (UX/UI designer) hasn't answered in 30s — you can decide instead.` followed by `for: Operator`. **ux keyboard confirmed still live at that moment** — a genuine race. Answered the duplicate in operator → ux message patched to `✅ Comfortable`, keyboard gone. Agent received **`"How dense should the default table layout be?"="Comfortable (answered by Operator)"`** |
| 5 — escalation, ux answers anyway (G2c, 15-05) | **pass** | Fired 19:13:37, duplicate due 19:14:07, arrived in **operator** with ⏳ banner and live keyboard; **ux keyboard also still live** at that moment. Answered in **ux** → operator duplicate patched to `✅ 8px scale`, keyboard gone. Agent received `…="8px scale"`, **no** suffix |
| 6 — permission prompt (G4a, 15-02) | **pass** | `chmod 644` was auto-approved silently again; `frobnicate-15-07 --check` raised a real request. Arrived in **operator only**, ux empty, **no `for:` line** (correct per brd §6). Named the command; tapped **Allow** from Telegram → message finalized and the command reached the shell (exit 127, `command not found`) |
| 7 — **clean** terminal win, ONE question, no escalation (G3a, 15-04) | **pass** | *Added at Anton's request — step 1 was terminal-answered but contaminated by its escalation overrun, so the uncontaminated case was untested.* `@ux Motion`, answered in the terminal in 15s, well inside the 30s deadline. ux message patched to `✅ Fade` (real label), keyboard gone; **operator stayed empty for 60s** — no duplicate ever fired. Agent received `…="Fade"`, no suffix |
| 8 — **clean** terminal win, TWO questions, no escalation (G3b orphan, 15-04) | **pass** | `@ux Radius` + `@ux Empty states`. Q1 option-selected, **Q2 answered as free text** (`Cropped ilustration`, typo Anton's) — so this call covers the option-selected **and** free-text shapes the post-mortem asked for. Answer landed 0.8s inside the deadline, so **no escalation fired**. Both ux messages patched — `✅ Rounded 12px` and `✅ Cropped ilustration`, the real free-text string, **not** `(notes only)`, **not** `Answered in the terminal` — and **both keyboards gone**. Operator empty. **This is the G3b orphan under its exact conditions**: two questions, only **one** `state=cancelled` (PostToolUse's single-row revoke), no duplicate group to change the `all(unanswerable[gk] …)` arithmetic |

### §6 — error-log gate: **PASS**

`~/.claude/permission_telegram_errors.log`: baseline **219590** bytes →
**219590** bytes, **delta 0**. No new lines of any kind; in particular **none** of
the three `Relay cancel_message failed: relay HTTP 500` lines the first run
produced. Rotation/truncation ruled out — the byte count is *identical* rather
than merely small, and the log's last entry is `2026-08-06T14:09:27Z`, an hour
before this run began (19:09 local = 15:09 UTC).

This is the observation that certifies the rest: both directions of
loser-finalization (steps 4 and 5) plus a terminal win over two live groups
(step 1b) ran **silent as well as visibly correct**, which is exactly what Fix B
was for.

### Fix A, visible in `~/.claude/permission_request_debug.log`

Steps 7 and 8 put the repair on the record at the exact line that failed. Step 8:

```
[15:33:36.850] Sent 2 question messages; waiting for answers
[15:34:06.026] Question 20251d8d020a relay state=cancelled
[15:34:06.038] AskUserQuestion resolved via terminal; finalizing messages
[15:34:06.604] AskUserQuestion: no Telegram answer; native UI will handle
```

The pre-fix log's `Every question group is unanswerable; falling back` is
**replaced** by `resolved via terminal; finalizing messages`, 12ms after the
cancel (7ms on step 7). Only **one** question logged `state=cancelled` and the
finalize still covered the whole call — the orphan condition, handled.

### Notes worth keeping

- **The tool-result wrapper does not indicate the answer path.** An earlier draft
  of this section claimed `The user answered: …` marked a non-`None` hook return
  (escalation) vs `Your questions have been answered: …` for terminal answers.
  **That is wrong** — step 8 is a terminal answer and used the former. The
  discriminator is whether the answer string matches an offered option label:
  step 4's `Comfortable (answered by Operator)` did not (suffix appended) and
  step 8's `Cropped ilustration` did not (free text); both drew the cautionary
  wrapper. Exact label matches drew the other. **An agent cannot infer the answer
  path from the wrapper** — consistent with brd §5.6's closing point.
- **Step 1's overrun was luck worth having.** It exercised a combination the
  step set does not otherwise cover — a terminal win that must finalize the
  escalation duplicates as well as the originals — and it passed. But it also
  meant the *uncontaminated* terminal win was untested, which is why steps 7 and
  8 were added; they are the ones that actually load-test Fix A's repair.
- **Wall-clock stamps taken by the driving agent are unreliable to ±20s.** The
  pre-fire `date` ran ~7s before the hook sent, and the post-return `date` was
  ~21s of model-generation latency late. On step 8 that produced a confident but
  wrong reading that escalation had fired. `permission_request_debug.log` has the
  real timestamps; prefer it for any timing claim in future runs.
- **`✍️` vs `✅` for typed answers — idea, not a defect.** Anton expected
  `✍️ Cropped ilustration` for an answer he typed rather than picked. Current
  behaviour is per spec (`✅ <answer text>`); `✍️` is the Telegram *text-reply*
  marker, not a terminal-input marker. Distinguishing typed from picked in the
  role's chat is a reasonable refinement — follow-up idea against **15-04**.
- The retired G3b notes-only sub-case was **not** hunted for, per the re-run
  brief; it stays covered by
  `test_notes_only_answer_reaches_the_chat_as_the_note`. The free-text shape it
  was to be re-run alongside **is** now covered, by step 8's Q2.
