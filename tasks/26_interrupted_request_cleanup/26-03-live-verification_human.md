# Task 26-03 — Live verification on this machine

**Status:** todo · **Type:** human/live · **Depends on:** 26-01 and 26-02, both
**installed** (`./install-claude-config.sh`, no sudo)
**Read first:** [brd.md](./brd.md) §4 (acceptance) · [26-01](./26-01-signal-revoke_sonnet.md) §5

## Why this is human

Every case starts with a person pressing ESC in a TUI, and the thing being
verified is a real Telegram card in a real chat. Neither half can be faked by the
suite: the suite calls `_on_interrupt` directly (26-01 testing note) precisely
because it must not signal its own runner.

## Procedure

Run each case, then check the row with
`grep '<request_id>' ~/.claude/permission_requests.jsonl | python3 -m json.tool`.

| # | Do | Expect |
|---|---|---|
| 1 | Ask a Telegram-routed `AskUserQuestion`, press **ESC** | buttons gone from the card; row `resolved_terminal` + `interrupted` **or** `orphaned` |
| 2 | Trigger a Bash permission prompt, dismiss it with **ESC** | same |
| 3 | **Control:** ask a question, answer it **in the TUI** | row `resolved_terminal` + `terminal` (PostToolUse, unchanged) — see note |
| 4 | **Control:** ask a question, answer it **in Telegram** | row `allow`/`reply` + `telegram`, and the answer still reaches the agent |
| 5 | **Control:** ask a question and leave it alone for a few minutes while other tools run | row stays `pending` — a live hook is never swept |

Case 5 is the one that catches a wrong liveness check. Cases 3 and 4 are the
regression guards for the paths that already worked; if either changes, stop and
report rather than adjusting the acceptance.

**Note on case 3.** `terminal` is the expected answer *while the hook is alive*,
which is the normal case. If that same question's hook had already exited — a
failed Telegram send, or the TTL fallback handing back to the native prompt — the
sweep gets there first and the row closes `orphaned` instead. That is brd §2.3,
accepted and not a failure; the card is cancelled either way. Report which one you
saw and whether a hook was still running (`ps -ef | grep permission_request_hook`).

## The probe

For case 1, immediately after the ESC:

```
grep "Interrupt: signal" ~/.claude/permission_request_debug.log | tail -5
```

Record in [state.md](./state.md) which of these happened, with the literal line:

- **a signal line, then `interrupted`** — the harness sends a catchable signal;
  note the number (15 = `SIGTERM`);
- **no signal line, then `orphaned`** — the harness `SIGKILL`s; layer 1 is inert
  on this harness version and layer 2 is carrying the epic. Say so plainly — it
  is the finding, and it should stop anyone re-proposing the handler later;
- **neither, row still `pending`** — the epic has not worked. Report before
  changing anything.

Note the harness version (`claude --version`) with the result: this is a
statement about a specific build's shutdown behaviour, not a permanent fact.

## Latency

For whichever layer fired, note roughly how long the card kept its buttons
(seconds for layer 1; for layer 2, until the next hook event — do a trivial tool
call and watch). If layer 2's latency is felt as long, that is the input for
whether the §4 stamp-file gate in 26-02 needs a shorter interval.

## Done criteria

1. All five cases run, each with its observed row state pasted in.
2. The probe result recorded in `state.md`'s Log, with the CLI version.
3. Any case that failed reported **without** a fix attempt — the fix belongs in
   the task that owns the code.
4. Say which cards you cleaned up by hand, if any.
