# 16-06 — Spawn execution and the wizard

**Status:** todo · **Depends on:** 16-05 (runtime), 16-03 (`--plain`, `--json`), 16-04 (seen-store)
**Read first:** [brd.md](./brd.md) §3.1, §5.2–5.3, §5.5–5.6 · [architecture.md](./architecture.md) §3.3–3.4

## Goal

Turn a `spawn` command into a running session: fill in whatever the user did not
type, check it, run the launcher, report the outcome.

Every question this task asks is an **ordinary relay message** created by the
listener (`RelayClient.send_message` + `wait_for_answer`), so TTL, cancellation
and threaded-reply routing are the mechanisms the permission and question flows
already use. No new UI primitive, and nothing new on the server.

## Scope

### Modifier resolution

`payload.modifiers` arrives as raw `+`-stripped tokens in order (16-02 does not
interpret them). For each token, in this order:

1. matches a name in `lib.profile_names()` → the profile;
2. matches a tier in `cfg.model_tiers` → the `--model` value;
3. neither → refuse the whole command, listing valid profiles and tiers;
4. matches **both** → refuse, naming the collision and asking for disambiguation
   (brd §5.3: passing through would silently pick one meaning).

Two profiles or two tiers in one command → refuse. Unspecified profile →
`cfg.default_profile`; unspecified tier → `cfg.default_model` (empty means: pass
no `--model` at all and let the profile's `ANTHROPIC_MODEL` govern, which is what
amux's `--no-default-model` exists for).

### Workspace resolution

`telegram_workspaces.find(name)`:

| Matches | Behaviour |
|---|---|
| 1 | use it |
| >1 | workspace picker listing full paths (this is why `resolve` reports `ambiguous`) |
| 0 | workspace picker over `list_recent()`, headed "no workspace called `<name>`" |

No name at all → the picker. **No path from Telegram is ever accepted** (brd
§5.6): a picker choice is an index into a list the listener built, never a string
the user typed.

### Wizard steps

Each step: `send_message(kind="question", keyboard=…, ttl_sec=1800)`, then
`wait_for_answer(message_id, timeout=1800, long_poll_chunk=25)`.

| Step | When | Shape |
|---|---|---|
| Workspace | name missing or unresolved | one button per workspace (recency order, cap ~8), `[✖ Cancel]` |
| Model | bare `/new` only | `[default] [fable] [opus] [sonnet] [haiku]` from `cfg.model_tiers` |
| Profile | bare `/new` **and** >1 profile configured | one button per profile name |
| Prompt | prompt missing | `reply_required=True`, body names workspace + machine, `[✖ Cancel]` |

The model and profile rows appear **only on a bare `/new`** (brd §5.3) — the
"I don't remember the syntax" path. `/new <ws> <prompt>` and `/new <ws>` use the
configured defaults silently: a phone user should not tap through a question they
already answered in config, and `+tokens` are there for the exception. Explicit
modifiers always win and always suppress the rows.

Answer payloads (relay-recorded shapes, do not re-invent):

- button → `{"option_idx", "label", "value", "via": "button"}` (`app.py:1790`)
- free text → `{"text", "via": "reply"}` (`app.py:1711`)
- terminal states → `Answer.state` is `expired` / `cancelled`; treat both as
  "user walked away": no session, no chat noise beyond what the relay already
  said.

A `[✖ Cancel]` tap (`value == "cancel"`) reports `ok=false, summary="cancelled"`,
and 16-02 renders it as a plain "cancelled — nothing was started".

The prompt step must accept **multi-line** text unchanged. Unlike task 09's
injector, which flattens newlines because `send-keys` would submit early
(`reply_injector.py:63-70`), a seeded prompt goes through amux's create path and
should survive intact — see the open question below.

### Preflight

Before spawning, in this order, refusing with a specific reason:

1. workspace resolved to an absolute path **present in the seen-store**;
2. the directory still exists;
3. `caps_check` (16-05) passes;
4. `lib.resolve_profile(profile)` succeeds (a `ValueError` names the bad profile);
5. the prompt is non-empty after stripping.

### Running the launcher — subprocess, not an import

```
amux-spawn spawn --plain --json --dir <abs> [--profile P] [--model T] -- <prompt>
```

**It must be a subprocess.** `cmd_spawn` does `os.environ.update(profile_env)`
(`.claude/bin/amux-spawn:173-180`) to put the profile's vars where amux's
`update-environment` allowlist can copy them. In a short-lived CLI that is
correct; in a listener that lives for weeks it would leak one spawn's auth into
the next spawn's environment. A child process gets the mutation and takes it to
the grave.

Read `--json` from stdout for the created name and directory (16-03), with a
timeout (~60 s) and the child's stderr captured for `detail`. On non-zero exit or
a timeout: report `ok=false` with the stderr tail; do not retry (a half-created
session is worse than a clear failure), and do not write the ledger.

On success: `ledger_record(name, abs_dir)`, then `report_command_result(ok=True,
data={"name", "dir", "profile", "model"})`. 16-02 renders the ack; this task
supplies facts, not chat text.

### What happens next is not this task's problem

The seeded turn ends, `notification_hook.py` fires, the idle notification goes out
with a reply injector armed (task 09), and the conversation continues. Nothing
here subscribes to that or waits for it — the command is done the moment the
session is up.

## Open question to settle during implementation

**Does a multi-line prompt survive `amux exec … <prompt>`?** `_amux_create_detached`
passes it as a single argv element and the comment claims multi-line stays intact
(`.claude/bin/amux-spawn:355-360`), but amux persists launch state to
`~/.amux/sessions/<name>.env`, and a newline in a shell-sourced env file is a
plausible break. amux was not installed on the machine where this epic was
designed, so it is unverified.

Verify first, in ten minutes, with a real two-line prompt. If it breaks, prefer
in this order: (a) fix the fork (epic 12 already owns amux changes); (b) seed a
short prompt and follow with the full text via `amux send` after the session is
up; (c) last resort, flatten as task 09 does and say so in the ack. Record the
answer in the task file — a multi-line prompt is the *common* case for this
feature (brd §3.1), not an edge case.

## Testing

Extend `tests/test_unit_amux_listen.py` (or a sibling), faking the relay client
and `subprocess.run`:

- Modifier resolution: profile only, tier only, both, unknown token, two profiles,
  a token that is both a profile and a tier — each with the exact refusal text.
- Workspace: single match, ambiguous match (picker with full paths), no match
  (picker over recents), no name (picker), empty seen-store (refusal that says
  the machine has no known workspaces yet).
- Wizard: each step's answer shape; cancel at each step; expiry at each step;
  a multi-line prompt reaching the launcher argv unchanged.
- Model and profile rows are skipped for `/new <ws> <prompt>` and for
  `/new <ws>`, shown for bare `/new` (profile row only with >1 profile), and
  suppressed by explicit modifiers.
- Preflight: each of the five checks refuses with its own message; a workspace
  outside the seen-store is refused even when the directory exists.
- Launcher: argv is exactly as specified (assert on the list, including `--plain`
  and the `--` separator); non-zero exit reports failure with stderr tail and
  writes no ledger entry; timeout reports failure; success writes the ledger and
  reports `data`.
- No `os.environ` mutation in the listener process across a spawn (assert the
  environment is byte-identical before and after).

## Done criteria

- [ ] `/new <ws> <prompt>` spawns with no further interaction.
- [ ] `/new <ws>` asks for the prompt and accepts multi-line text.
- [ ] Bare `/new` walks workspace → (model) → prompt, cancellable at every step.
- [ ] Modifiers resolve per the ordered rules, with collisions refused.
- [ ] Only seen-store workspaces can be spawned into; no typed path is accepted.
- [ ] Preflight failures each produce a specific, actionable refusal.
- [ ] The launcher runs as a subprocess with `--plain --json`; the listener's own
      environment is never mutated.
- [ ] Failures report `ok=false` with a usable `detail` and leave no ledger entry.
- [ ] The multi-line seeding question is answered in this file before the task
      is marked done.
