# Epic 26 — A permission card never outlives the hook that sent it

**Status:** todo · **Owner:** Anton · **Created:** 2026-08-26 · **Rev:** 1
**Type:** this repo only (hooks + store; no relay change, no `amux` change)

> Broken down into tasks — see [state.md](./state.md) for ordering, the shared
> edit points and the invariants.

**Numbering.** Epics **22–25 are reserved by a remote machine**, so this one is
26. Nothing here depends on those epics; the gap is bookkeeping, not a
dependency.

**Related.** [Task 27](../27_pending_row_crosstalk.md) fixes the mirror
failure — `find_pending_request_by_tool_session` closing *another* live
prompt's row when two are open in one session. Independent fix, same
`resolve_via_terminal` neighbourhood: whichever lands second re-runs the
other's tests.

**Provenance.** Found on 2026-08-26 while answering "this Telegram question still
shows buttons, but I already answered it in the TUI". Every claim in §1 was read
off this machine's logs and store on that date; the two that were not are
labelled **unverified**.

## 1. Problem & thesis

A Telegram permission card is closed by exactly one mechanism: the **PostToolUse**
hook (`posttool_hook.py:171-244`) finds the pending row, calls
`resolve_via_terminal` (`permission_state_store.py:801`), and revokes the message.
That fires when the tool **ran**. It does not fire when the human answers by
*rejecting* the tool — ESC in the TUI — because there is no tool result to post.

The same interrupt also kills the parked `PermissionRequest` hook, so the process
that owns the card cannot clean up after itself either. The card is then live,
with working buttons, addressed to a question that has already been answered, for
the full 12-hour TTL.

**Measured, 2026-08-26** (session `a3c5303c`, `~/hyppie-flow/hyppie-flow`):

| Row | Msg | Asked | ESC'd | State at 16:35Z |
|---|---|---|---|---|
| `82224cce17bc` | 5039 | 13:39:02Z | 13:50:02Z | `pending`, buttons live |
| `a561ab46d5fa` | 5044 | 13:58:57Z | 14:00:37Z | `pending`, buttons live |

Both questions were *answered* — as free text, in the terminal, within minutes —
and the agent acted on both. The store rows still read
`decision: null, resolution_source: null, terminal_answers: null`.

1. **The hook dies.** Neither process was in `ps` five hours later, despite a
   12h TTL, and `permission_request_debug.log` stops at
   `Sent 1 question messages; waiting for answers` (`permission_request_hook.py:1238`).
   Python's default `SIGTERM` disposition skips `finally:` and `atexit`, which is
   why the existing `finally: stop.set()` (`:1010`) never ran.
2. **A normal terminal answer is fine precisely because the hook survives it.**
   PostToolUse marks the row and cancels the message; the still-parked hook
   notices through `_terminal_win` (`:673`, called from `:998`). That path is not
   in question and must not regress.
3. **The mechanism is not question-specific.** A Bash permission prompt denied
   with ESC leaves the same orphan for the same reason — no tool ran, no
   PostToolUse. *(Reasoned from the same code path, not separately reproduced —*
   ***unverified***.*)*
4. **An orphaned card is not merely stale — it keeps asking.** The relay's nudge
   pass (19-04) selects `state = 'open' AND next_nudge_at < now`
   (`relay-server/relay_server/reaper.py:43`, `:654`), so a card nobody closes
   goes on reminding a human about a question answered hours ago — that is the
   `#unanswered` tag both rows above still carried. `remove_inline_buttons` →
   `cancel_message` (`telegram_permission_router.py:749-764`) takes the relay row
   out of `open`, and that is what stops the nudges. **Both layers must revoke
   the message, not merely fix the store row** — a store row is invisible to the
   nudge engine.
5. **Scale.** Of 1634 rows in `~/.claude/permission_requests.jsonl`, **262 (16%)
   ended `expired` / `timeout`** — 150 Bash, 106 AskUserQuestion — i.e. they were
   never resolved by anybody and died at TTL. In the window the current debug log
   covers (since 2026-08-23) there are 2 expired AskUserQuestion rows and
   **zero** `reached the TTL deadline` log lines, so no hook survived to its own
   TTL in that window. Consistent with orphaning rather than patient waiting;
   suggestive, not conclusive — some of the 262 are genuinely questions nobody
   ever answered.
6. **A hook that exits normally leaves no pending row** — which is what makes
   layer 2's rule safe rather than merely plausible. At 16:45Z on the incident
   day the live store held **exactly two** pending rows (the two above) and no
   `permission_request_hook.py` process was running at all. Every ordinary exit
   path already marks its rows terminal before returning (`:417`, `:484`, `:812`,
   `:971`, `:1187`, `:1442`). So "pending **and** owner gone" is not a heuristic
   for an orphan: on this machine, on that day, it selected precisely the two
   orphans and nothing else.

**Thesis — two layers, because neither alone is sufficient.**

- **Layer 1 — the hook cleans up after itself** on a catchable signal
  (`SIGTERM`/`SIGHUP`/`SIGINT`): mark the rows terminal, strip the buttons, exit.
  Fast (sub-second) and exact, but **conditional on the harness sending a
  catchable signal** — unmeasured today, and worth nothing under `SIGKILL`.
  Its first debug line doubles as the probe that settles that question.
- **Layer 2 — the store notices the owner is gone.** Stamp each row with the
  creating process's identity, and let any later hook invocation sweep rows whose
  owner no longer exists. Slower (next hook event, typically seconds) but
  **unconditional**: it holds under `SIGKILL`, a crash, an OOM kill, or a power
  cut.

Layer 2 is the guarantee. Layer 1 is the latency.

## 2. Scope

### 2.1 In scope

- A signal handler in `permission_request_hook.py` that revokes its own live rows
  (task [26-01](./26-01-signal-revoke_sonnet.md)).
- Owner identity on every row + `sweep_orphaned_requests()` + its two call sites
  (task [26-02](./26-02-orphan-sweep_sonnet.md)).
- Two new `resolution_source` values, `interrupted` and `orphaned`, so the store
  says *which* layer closed a row — the epic's own evidence trail.
- Live verification on this machine, including the control cases that prove the
  normal paths still work (task [26-03](./26-03-live-verification_human.md)).

### 2.2 Out of scope — deliberately

- **The relay.** Its reaper (`relay-server/relay_server/reaper.py:316`) expires
  open messages at TTL and stays exactly as it is: the 12h last resort for
  anything both layers miss. It cannot do this job — it cannot see local PIDs.
- **Repairing the two rows in §1.** They are evidence, not a work item; the
  reaper collects them. The operator has said they are not the concern.
- **A new request state.** `RESOLVED_TERMINAL` already exists and every consumer
  already treats it as terminal (`permission_request_hook.py:356`, `:508`,
  `:668`). Adding a state would mean auditing all of them for a distinction with
  no behavioural difference: in both cases *the terminal dealt with it*.
- **Changing what the card says.** Buttons come off, same as the PostToolUse
  path. Rendering the *reason* on the card ("interrupted in the terminal") needs
  transcript correlation and is a later revision, not this one.
- **Reading the transcript to tell "rejected" from "crashed".** The
  *PermissionRequest* payload carries `session_id`, `transcript_path` and
  `prompt_id` but **no `tool_use_id`** — measured from the raw input logged at
  `permission_request_debug.log` on the incident day. (PostToolUse's payload
  *does* carry one, per `tasks/15_human_roles/fixtures/posttool_askuserquestion.json`,
  but rows are created at PermissionRequest time, before it exists.) So the
  correlation would be heuristic, and both cases want the same action — close
  the card — so the distinction buys nothing yet.

### 2.3 Accepted consequence

When the sweep reaches an orphaned row before PostToolUse does, PostToolUse finds
nothing pending and records no `terminal_answers`. Nothing consumes that field
once the hook that would have patched it into the chat is dead
(`permission_request_hook.py:522`, `parse_terminal_answers`), so the loss is
bookkeeping. The visible effect is that such a row closes as `orphaned` rather
than `terminal`. Do **not** "fix" this by making the sweep session-aware or by
having it consult the transcript — that trades a measurable fact (the owner is
gone) for a guess, which is what H4 exists to prevent.

## 3. Constraints & hazards

- **H1 — fail open, always.** Both layers run on the path every tool call takes.
  No exception may escape into the hook's exit code, and nothing here may delay a
  tool. Wrap in `try/except Exception` and log; a cleanup that can break tool
  execution is worse than the leak it fixes.
- **H2 — the signal handler can deadlock the store.** `_acquire_lock`
  (`permission_state_store.py:148`) is a blocking `fcntl.flock(LOCK_EX)`. Signals
  are delivered on the main thread *between bytecodes* — including while that
  thread is inside a locked section. A handler that re-enters
  `update_request_state` then opens the file again and blocks on a lock its own
  process holds through another fd. **Forever.** The handler must therefore use a
  bounded, non-blocking acquire and give up on failure, falling through to layer
  2. This is the single subtlest thing in the epic.
- **H3 — the handler runs on a process that is already dying, and the revoke it
  wants to make is not fast.** `RelayClient` defaults to connect 5 s / read 35 s
  (`relay-server/relay_server/client.py:93-94`) and retries transport failures up
  to three times with backoff (`:158-182`), so one `cancel_message` against an
  unreachable relay can exceed a minute. Budget the handler explicitly: local row
  writes first, network revoke second and **bounded by the caller** (26-01
  decision 5), never trusted to return. If the revoke does not land, the row is
  still terminal and the reaper takes the buttons at TTL — degraded, not broken.
- **H4 — PID reuse.** `os.kill(pid, 0)` alone is not liveness. Pair the PID with
  `/proc/<pid>/stat` field 22 (`starttime`) captured at creation; a recycled PID
  then reads as a *different* process. Unreadable `/proc` must mean "cannot
  tell" → leave the row alone. Never sweep on a guess.
- **H5 — cost on the hot path.** `posttool_hook` already makes one full pass over
  a **3.7 MB / 1634-row** JSONL per tool call
  (`find_pending_request_by_tool_session`, `permission_state_store.py:738`). The
  sweep adds a second pass. It must be measured, and gated behind a stamp file if
  it is not cheap — see 26-02 §4.
- **H6 — a repo edit is not live.** `.claude/hooks/*` reaches `~/.claude/hooks/`
  only when `./install-claude-config.sh` runs. Any claim that something "works
  now" must say whether it was tested against the repo copy or the installed one.
- **H7 — backward compatibility is already handled, do not re-invent it.**
  `PermissionRequest.from_dict` (`:110`) filters to known fields and defaults
  missing ones, so new columns load against all 1634 existing rows. Rows written
  before this epic carry no owner and must simply be skipped by the sweep.

## 4. Acceptance

The epic is done when, on this machine, with both halves installed:

1. Pressing ESC on a Telegram-routed `AskUserQuestion` removes that card's
   buttons without human action, and the row reads `resolved_terminal` with
   `resolution_source` naming the layer that did it.
2. The same holds for a Bash permission prompt dismissed with ESC.
3. **Controls unchanged:** a question answered in the TUI still resolves
   `terminal` via PostToolUse; a question answered in Telegram still resolves
   `telegram`; neither is swept out from under itself.
4. `python3 tests/run_all_tests.py` is green, with before/after counts reported
   and new cases covering both layers.
5. `state.md` records whether a catchable signal actually arrives on ESC — the
   question layer 1 exists to answer — with the log line that proves it either
   way.
