# Epic 26 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, tests and done criteria. This file owns **ordering**, the **shared
edit points**, and the **cross-task invariants**.

**No Phase 0.** The decisions that would have needed one are locked in brd §2.2
(no new state, no relay change, no transcript correlation) and in 26-01 §1b (the
bounded-lock fix for H2). The one genuinely open *question* — does the harness
send a catchable signal at all? — is not a blocker: it is answered by shipping
26-01 and reading one log line ([26-01](./26-01-signal-revoke_sonnet.md) §5), and
both tasks are specified to be correct either way.

**Numbering.** 22–25 are reserved by a remote machine; this epic is 26.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 26-01 | [Signal revoke](./26-01-signal-revoke_sonnet.md) | done | — | Layer 1 — the fast path. Small, and carries the probe. Inert if the harness `SIGKILL`s. |
| 26-02 | [Orphan sweep](./26-02-orphan-sweep_sonnet.md) | done | — | Layer 2 — **the guarantee**. Holds under `SIGKILL`, crash, OOM, reboot. Ship this one even if 26-01 is skipped. |
| 26-03 | [Live verification](./26-03-live-verification_human.md) | blocked | 26-01 + 26-02 installed | **human** — five cases, three of which are controls. Records the probe result. |

## Dependency graph

```
26-01 ─┐
       ├─► install ─► 26-03 (human)
26-02 ─┘
```

Both implementation tasks are independent roots and can run in parallel — with
one caveat, below.

## Shared edit points

Two files are touched by both tasks. Run them **sequentially**, or in parallel
only with an agent that re-reads before writing:

| File | 26-01 | 26-02 |
|---|---|---|
| `permission_state_store.py` | `RESOLUTION_SOURCE_INTERRUPTED`; `lock_timeout` on `update_request_state` | `RESOLUTION_SOURCE_ORPHANED`; three owner fields; `_proc_start_ticks`; `sweep_orphaned_requests` |
| `telegram_permission_router.py` | moves `revoke_telegram_message` in from `posttool_hook.py` | consumes it — **or** performs the move if 26-01 has not |

The two new constants land in the same four-line block (`:71-73`), and the move is
**exactly one** task's job. Whichever runs second must check what is already
there rather than re-applying.

## Related work — task 27, adjacent and independent

[`tasks/27_pending_row_crosstalk.md`](../27_pending_row_crosstalk.md) (filed the
same day) is the **opposite** failure in the same code: there, a *live* hook's
card is closed by somebody else's answer, because
`find_pending_request_by_tool_session` matches on session + tool only and two
prompts were open at once.

Neither fix implies the other, and neither blocks the other. Two contact points
to respect:

- Both touch the resolve/revoke path, so **whichever lands second re-runs the
  other's tests** and reports both counts.
- Task 27's H2 is this epic in reverse: if its matcher is over-tightened until it
  matches nothing, every card outlives its hook — which 26-02's sweep would then
  close en masse, hiding the regression. If both are in flight, land 27's
  fallback branch (its §4.4) before trusting a green sweep.

## Recommended order

1. **26-02 first if you only do one.** It is the guarantee (brd §1 thesis), and
   it is unconditional — no assumption about the harness's shutdown behaviour.
2. **26-01 next.** Cheap, cuts the latency from "next hook event" to
   "sub-second", and its debug line is the only way to learn what signal (if any)
   an ESC actually delivers.
3. **Install**, then **26-03**. Nothing is verified until
   `./install-claude-config.sh` has run (brd §3 H6).

## Cross-task invariants

1. **Fail open.** No exception from either layer may escape into a hook's exit
   code, and neither may delay a tool. (brd §3 H1)
2. **The terminal and Telegram always win.** Both layers write through
   `update_request_state`, which is idempotent and refuses terminal rows
   (`:338`). A row answered by a human — in either channel — is never re-labelled
   by cleanup.
3. **Never sweep on a guess.** Unknown liveness (`/proc` unreadable, missing
   `owner_start_ticks`, `PermissionError`, another host, no owner recorded) means
   *leave it alone* and let the 12h TTL have it, exactly as today. (brd §3 H4)
4. **The store does no network I/O.** `sweep_orphaned_requests` returns the rows;
   the hook revokes them. This is what keeps the sweep unit-testable without the
   relay.
5. **State first, buttons second.** In both layers the row is marked terminal
   before any Telegram call, so a process killed mid-cleanup leaves correct state
   and at worst a stale card the reaper collects. (brd §3 H3)
6. **A repo edit is not live.** `.claude/hooks/*` reaches `~/.claude/hooks/` only
   through `./install-claude-config.sh`. Any "it works" claim must say which copy
   was tested. (brd §3 H6)
7. **`resolution_source` says who closed the row.** `terminal` = PostToolUse,
   `telegram` = a human tapped, `timeout` = TTL, `interrupted` = layer 1,
   `orphaned` = layer 2. Nothing branches on the value today — it is the epic's
   evidence trail, and it must stay accurate.

## Log

- **2026-08-26 — epic created.** Filed from a live incident on this machine: two
  `@htl` questions (`82224cce17bc` / msg 5039, `a561ab46d5fa` / msg 5044) were
  answered in the TUI by pressing ESC and typing, and both Telegram cards stayed
  live with working buttons for hours afterwards. Diagnosis in brd §1: PostToolUse
  is the only closer and it does not fire for a rejected tool, while the same
  interrupt kills the hook that could have cleaned up. The store-wide numbers
  (262 of 1634 rows dead at TTL; zero `reached the TTL deadline` log lines since
  2026-08-23) suggest this has been the quiet default for a while rather than a
  one-off. **No code was written** — operator's instruction was a spec only. The
  two rows above are evidence, not a work item (brd §2.2); the relay reaper
  collects them at TTL.
- **2026-08-26 — review pass before handover; five corrections, no scope change.**
  (1) A live-store hazard in the specs themselves: `tests/test_unit_state_store.py`'s
  `setUp` only *looks* like it isolates, the real redirect is at
  `tests/run_all_tests.py:31`, and 26-02's cost probe as first written would have
  run `sweep_orphaned_requests()` against the real 3.7 MB store — the one function
  in this epic that rewrites rows it did not create. Both task files now carry the
  isolation warning and the probe measures a copy. (2) brd §1 finding 4 added: an
  un-revoked card keeps drawing epic-19 nudges (`reaper.py:43`, `:654` select
  `state='open'`), so revoking the message is load-bearing, not cosmetic — the
  store row alone is invisible to the nudge engine. (3) brd §1 finding 6 added: at
  16:45Z the store held exactly two pending rows and no hook process, so
  "pending + owner gone" is measured to select precisely the orphans on this
  machine, not merely argued to. (4) brd §2.3 added: a row the sweep reaches before
  PostToolUse closes as `orphaned` and records no `terminal_answers` — bookkeeping
  only, and explicitly not to be "fixed" with session or transcript guesswork.
  Also checked and found **not** to be problems: `create_request` has only the two
  hook call sites (`notification_hook`/`reply_injector` never touch this store, so
  the detached idle-notification flow cannot be swept), and no code anywhere
  branches on `resolution_source`, so two new values are additive.
  (5) Snippet-level: both hooks' revoke calls were written against the wrong
  module name. `permission_request_hook.py` imports the router **as
  `telegram_router`** (`:43`) and logs through `debug_log`;
  `posttool_hook.py` imports the full `telegram_permission_router` (`:27`) and
  logs through `log_debug`. Both call sites are now spelled out separately, with
  the imports each task must add (`signal`, `OrderedDict`,
  `RESOLUTION_SOURCE_INTERRUPTED` for 26-01; `socket`, `sweep_orphaned_requests`
  for 26-02) — copying a call between the two hooks would not have compiled.
- **2026-08-26 — task 27 filed independently while this epic was in review;
  cross-links added both ways.** `tasks/27_pending_row_crosstalk.md` documents the
  mirror-image defect (a live hook's card closed by another prompt's answer). It
  is not a dependency in either direction, but the two share the resolve/revoke
  path and 27's H2 failure mode would be *masked* by 26-02's sweep — see "Related
  work" above. Its filing also sharpened brd §2.2: the **PostToolUse** payload
  does carry `tool_use_id` (fixture `_payload_keys`), while the
  **PermissionRequest** payload this epic works from does not, so the wording now
  names which event it is talking about.
- **2026-08-26 — sixth correction, found by reading the client rather than the
  spec.** 26-01's handler originally called the revoke inline, under a stated
  budget of "well under a second". `RelayClient` defaults to connect 5 s /
  read 35 s with three backed-off retries
  (`relay-server/relay_server/client.py:93-94`, `:158-182`), so a single
  `cancel_message` against an unreachable relay can exceed a minute — inside a
  process the harness is waiting to reap. The handler now runs the revokes in a
  daemon thread with `join(1.0)` (26-01 decision 5) and brd §3 H3 states the
  measurement instead of asserting a budget the code could not meet.
- **2026-08-27 — 26-02 landed.** Implemented, reviewed and fixed. The
  `revoke_telegram_message` move was performed by this task (26-01 had not run),
  taking `resolve_role_token` with it as `_resolve_role_token`; its debug lines
  now go to `permission_state_debug.log`, which nothing referenced under the old
  destination. Sweep cost measured at **23.6 ms against a copy** of the store, so
  both call sites call it unconditionally with no stamp-file gate. Review found
  0 BLOCKER / 0 HIGH; the two MEDIUMs were coverage gaps on unknown-liveness
  paths (`owner_start_ticks = None` with a live owner, and `PermissionError` from
  `os.kill`) — both now have sweep-context tests that were **watched to fail**
  with the decision inverted before being accepted. Per-call-site `try/except`
  added around the sweep as defence in depth for invariant 1. Suite 1278 → 1291
  passed (+15 cases). Installer re-run. **26-01 still owns
  `RESOLUTION_SOURCE_INTERRUPTED` and `lock_timeout`; the constant block was left
  tidy for it to extend.**
- **2026-08-27 — 26-01 landed; epic engineering complete.** Implemented against
  a checkout where 26-02 had already landed, so the `revoke_telegram_message`
  move was consumed, not re-applied, and `RESOLUTION_SOURCE_INTERRUPTED` replaced
  the placeholder comment 26-02 left at `:99`. Review found 0 BLOCKER / 0 HIGH.
  **The `os._exit(0)` question is settled:** exit 0 with **no stdout** is this
  hook's documented "no decision" path — the harness falls back to its native TUI
  prompt — and the handler never writes to stdout, so an interrupt cannot be read
  as an approval. The revoke runs in a daemon thread with `join(1.0)` per
  decision 5, because `RelayClient`'s 5 s connect / 35 s read with three backed-off
  retries can exceed a minute against an unreachable relay.
  **A pre-existing test leak was found and fixed at its cause:** `TestEscalation`'s
  `_run()` stopped patches in FIFO order, and one method patches
  `update_request_state` twice, so the first mock survived teardown and leaked into
  later classes. Fixed with `reversed(stack)`. All 22 `TestEscalation` methods
  still pass once un-mocked — each installs its own mock via `_run()`, so **none was
  asserting vacuously**. Suite 1291 → 1298 passed (+7). Installer re-run.
- **2026-08-27 — 26-03 moved to `blocked`, awaiting human evidence.** Both
  implementation layers are installed. **The §5 probe log is
  `~/.claude/permission_request_debug.log`** (`permission_request_hook.py:143`) —
  an earlier report named the wrong file; the 26-03 task file was already correct.
  `debug_log` closes the file before returning, so the line is on disk before
  `os._exit(0)`. Presence of `Interrupt: signal` after an ESC means the harness
  delivers a catchable signal and layer 1 fires; absence means `SIGKILL`, layer 1
  is inert by construction, and 26-02's sweep is the only closer. Either outcome
  is a valid result — the epic is specified to be correct both ways.
