# Task 40-03 — YOLO, and whether the CLI should know about it

**Status:** optional — **do not run without the operator's answer to §3**
**Depends on:** [40-01](./40-01-pretool-defers-to-the-mode_sonnet.md)
**Read first:** [brd.md](./brd.md) §1.4, §2.2 · [state.md](./state.md) invariant 5
**Agent:** **human** for §3 — do not spawn an implementer until the operator has
answered. If the answer is (b) or (c), the manager then spawns a **sonnet**
implementer with §4's scope for that option; if the answer is (a), the manager
may hand (a)'s two-line doc edit to the same sequence or do it itself.

## 1. The anomaly

`/yolo` sets a flag in `session_yolo_store`, and
`permission_request_hook.py:350-364` auto-allows every subsequent request in that
session. The terminal, meanwhile, still shows **"manual mode"** — the harness has
no idea. Under the epic's thesis (brd §1.4: the two surfaces agree), that is the
one place where they deliberately do not.

It also has a functional edge, now that `auto` exists: in an auto-mode session
the classifier can **deny** a call outright, which raises no prompt, so the
YOLO branch is never consulted and the call is blocked anyway. A session the
operator believes is in "allow everything" is not.

## 2. What the harness now makes possible

A `PermissionRequest` hook's **allow** decision may carry `updatedPermissions`,
and one of the update kinds is `setMode` — accepting any of the six modes
(`acceptEdits`, `auto`, `bypassPermissions`, `default`, `dontAsk`, `plan`) with a
`destination` of `session`:

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
  "decision": {"behavior": "allow",
    "updatedPermissions": [{"type": "setMode",
                            "mode": "bypassPermissions",
                            "destination": "session"}]}}}
```

Two caveats read from the bundle, both of which mean the store stays as the
fallback rather than being replaced:

- The harness **strips** permission updates for tools that declare
  `suppressesAllPermissionUpdates` / `suppressesAlwaysAllowRule`, so a `setMode`
  riding on such a tool's approval silently does nothing.
- `/yolo` itself is a slash command with no permission request in flight
  (`.claude/commands/yolo.md`), so it can only write the store. Only a *Telegram
  tap* can carry a `setMode`.

## 3. The question for the operator — ask before implementing

**Once a session is in `bypassPermissions`, nothing prompts — so no hook runs —
so `/yolo-off` can no longer undo it from Telegram.** The operator would have to
be at the keyboard (Shift+Tab) or end the session. Today `/yolo-off` works from
anywhere.

> Which do you want?
>
> **(a) Leave YOLO alone; document the auto-mode edge.** No code. `/yolo`'s
> output and `docs/` gain a line: YOLO suppresses *prompts*; in auto mode the
> classifier can still refuse a call, and nothing on Telegram can override that.
> Costs nothing, keeps `/yolo-off` working from the phone. **Recommended
> default.**
>
> **(b) Promote YOLO to the native mode.** The YOLO tap's allow also carries
> `setMode: bypassPermissions`. The terminal's indicator finally tells the truth
> and the classifier stops refusing things. One-way from Telegram.
>
> **(c) Add a separate, honestly-labelled button.** Keep YOLO exactly as it is,
> and add a distinct "Bypass this session" action that carries the `setMode`.
> Two different things, two different buttons, nothing silently changes meaning
> — at the cost of one more button on every card.

## 4. Scope, per answer

**(a)** `.claude/commands/yolo.md` + the YOLO paragraph in `docs/` gain the
caveat. No Python changes. Done when `/yolo`'s one-line output still fits on one
line and the caveat is somewhere the operator will actually read it.

**(b)** `permission_request_hook.py:350-364` adds `updatedPermissions` to the
`yolo` action's payload; `session_yolo_store` stays (the harness may strip the
update — see §2). `.claude/commands/yolo-off.md` must tell the operator that the
session mode has to be changed at the keyboard. Tests: the emitted payload shape,
and that the store branch still auto-allows when the update is stripped.

**(c)** As (b), but behind a new relay action rather than the existing `yolo`
one — which also touches `telegram_permission_router.py`'s keyboard
(`_PERMISSION_ACTIONS`) and the relay's action vocabulary. **Check whether the
relay needs a matching change before committing to this option**; if it does,
this stops being a this-repo-only task and needs its own epic.

## 5. Report

Whichever option is chosen, write
`agents_output/40-03_implementation_report.md` naming the option, what changed,
and — for (b) or (c) — one pasted example of the emitted payload.
