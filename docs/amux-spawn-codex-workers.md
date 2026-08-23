# amux-spawn Codex workers — operator guide

How to launch, watch, resume, and reap bounded OpenAI Codex workers through
`amux-spawn` (epic 20). A Codex worker is a **noninteractive, bounded**
`codex exec` turn running inside an amux tmux session: it takes one prompt,
streams its lifecycle as JSONL events into a file, writes its final assistant
message to an artifact, and exits. There is no TUI to attach to and nothing to
type at.

For Claude (the default provider) nothing here applies — `amux-spawn spawn`
without `--provider` is byte-for-byte the epic-10 behavior.

## Quick reference

```bash
amux-spawn spawn review-123 \
  --provider codex \
  --dir /path/to/project \
  --detach \
  -- "Read the project instructions and review task 123"

amux-spawn status review-123        # derived state + reason context
amux-spawn last review-123          # the final assistant message
amux-spawn resume review-123 -- "Address the remaining issue"
amux-spawn rm review-123            # reap when you are done with the thread
```

`--provider codex` workers are **always tracked and detached** — even at a
TTY — and always require a prompt after `--` (a bounded run is exactly one
seeded turn).

## Detached spawn

`--detach` is implied for Codex; shown above for clarity. The spawn prints the
resolved session name, the run id, and the two artifacts that carry the
worker's lifecycle:

```text
amux-spawn: spawned tracked session 'myproject-review-123' in /path/to/project
  provider:   codex (bounded exec)
  run_id:     3f9c…
  events:     ~/.amux/spawn/myproject-review-123.events.jsonl
  result:     ~/.amux/spawn/myproject-review-123.result.md
```

The artifacts (plus the `.err` / `.rc` siblings amux's launch wrapper writes)
are mode 0600 and keyed by session name under `~/.amux/spawn/`. The event log
is append-only; a resume adds a segment after the previous one.

## status / last

State is derived at read time from evidence — never from terminal text:

- `turn.completed` in the event log → **idle**, regardless of the exit code
  (a SIGKILLed parent whose orphan child finished the turn is still a
  completed turn; conversely exit 0 alone is NEVER success);
- `turn.failed` → **terminated**, regardless of the exit code;
- pane alive → **running** (or **stuck** after the activity threshold,
  minutes by design — a tool-using turn is legitimately silent for a long
  time);
- pane gone with no completion evidence → **terminated**, exit code carried
  as reason context only.

```bash
amux-spawn status review-123            # one-line human summary
amux-spawn status review-123 --json     # provider, state, thread_id, attempt,
                                        # exit_code, signals, failure
amux-spawn last review-123              # final assistant message (exit 1 if
                                        # none was produced)
amux-spawn last review-123 --json
amux-spawn ls                           # per-workspace rows incl. attempt
```

`last` reads the explicit final-message artifact (bounded at 256 KiB), never
Codex's internal transcript format.

## wait

`--wait` (alias `--notify`; there is no separate `wait` subcommand) blocks
until the seeded turn reaches true idle and prints the final message to
stdout — stdout IS the result payload:

```bash
amux-spawn spawn review-123 --provider codex --dir . --wait -- "review it"
```

Exit codes: `0` success (final message on stdout), `1` error (detail on
stderr), `3` caller timeout (the line `AMUX_WAIT_TIMEOUT` on stdout). The
intended async pattern is running the `--wait` invocation as a background task
and reading its output file when the harness reports completion.
`resume --wait` has the same contract.

## resume

`resume` starts the NEXT bounded turn on the SAME Codex thread:

```bash
amux-spawn resume review-123 -- "now update the changelog"
amux-spawn resume review-123 --wait -- "and answer with one word"
```

The thread id comes from amux's authoritative capture in
`~/.amux/sessions/<name>.meta.json` (`codex_session_id`); a missing,
malformed, or evidence-contradicting id is refused — resume never silently
starts a fresh, unrelated conversation (Codex itself would happily do that on
a non-UUID id and exit 0). Evidence is append-only: the previous segment's
events and result stay readable, `last` returns the newest successful turn,
and `status` reports the attempt counter. A failed resume preserves the
previous result and evidence.

Refused on resume (with actionable errors): `--yolo` (fixed at spawn time and
inherited by every resumed run), `--profile`/`-p`, `-C/--cd`, `-s/--sandbox`
(codex-cli rejects them on `exec resume`), and `--dir` (the workspace is
persisted with the session).

## Model and profile selection

No Codex model is ever injected. With no explicit `--model`, Codex's own
configuration decides (`~/.codex/config.toml`, profiles, built-in default).
To pin one, use the unambiguous equals form:

```bash
amux-spawn spawn reviewer --provider codex --model=gpt-5.5 -- "…"
amux-spawn resume reviewer --model=gpt-5.5 -- "…"      # resume accepts both
                                                       # --model=X and -m X
```

Prefer `--model=X` on `spawn`: the space form's value is captured by the
optional `<suffix>` positional (pre-existing behavior on the Claude path).
`amux-spawn --profile` is a Claude `~/.claude/profiles.toml` concept and is
refused for `--provider codex`; configure Codex's own `~/.codex/config.toml`
for defaults.

## YOLO mode — full host access warning

```bash
amux-spawn spawn scraper --provider codex --yolo -- "fetch and summarize …"
```

`--yolo` is provider-neutral in `amux-spawn` and expanded by amux after it
resolves the provider: for Codex it becomes
`--dangerously-bypass-approvals-and-sandbox`. **That means exactly what it
says: no approval prompts and no Codex sandbox — the worker has full host
filesystem, network, and Docker access, and that is intentional.** Run YOLO
Codex workers only on hosts you would trust with an unattended shell. Live
verification of this epic deliberately used only disposable files, a harmless
network request, and a throwaway container — despite the worker having full
access. YOLO cannot be toggled on resume; it is a spawn-time choice persisted
with the session. Claude's `--dangerously-skip-permissions` is refused on the
Codex path, and amux-spawn never forwards one provider's permission flag to
the other.

## No Telegram permission prompts — by design

Codex workers have **no Telegram permission prompts, and none are planned as
a prerequisite for anything** in this stack. Permission gating via the relay
is a Claude-session feature; porting it to Codex is an explicit non-goal of
epic 20. A Codex worker that needs human input simply returns a normal
blocked/HALT result in its final message — the **foreground Claude workflow
owns human interaction** and decides what to do with it (answer it by
resuming the worker, re-spawn, or drop it). Nothing here waits on a prompt
that can never arrive.

## rm — cleanup

```bash
amux-spawn rm review-123          # refused while running/stuck — ask --force
amux-spawn rm review-123 --force  # kill (tmux kill-session) + reap
```

`rm` stops the tmux session (via `amux rm`, which uses `tmux kill-session` —
never a bare `kill -9`, which would orphan Codex's forked child), deletes the
registry handle, and removes exactly the artifacts the handle names (event
log, `.err`/`.rc` siblings, result file) — no broad globs, nothing of other
sessions.

## Required amux revision (pin)

The Codex path is verified against amux revision **`11a8426a014e8b9ca30134758e66e3912628b647`**
on branch `feat/epic-10-amux-extensions` (sibling epic
[`01_codex_cli_provider`](../../amux/tasks/01_codex_cli_provider/state.md)
complete; amux suite 398 passed / 0 failed at that revision). The provider
argv rules, `<name>.env` keys, `codex_session_id` capture, `.rc` semantics
and the rc-66 thread-mismatch quarantine that `amux-spawn` builds on are
documented in the sibling repo's `docs/codex-provider.md`.

Drift is checked two ways:

- `tests/test_amux_pin.py` (part of this repo's suite) fails with fix
  instructions when a sibling checkout exists at `../amux` and its HEAD no
  longer contains the pin; it skips cleanly when there is no sibling
  checkout.
- `./install-amux.sh` reports the installed clone's position relative to its
  `AMUX_PIN` (default: the pin above).

To check by hand:

```bash
git -C ../amux merge-base --is-ancestor \
    11a8426a014e8b9ca30134758e66e3912628b647 HEAD && echo "pin contained"
```

Moving the pin is deliberate: update it in `tests/test_amux_pin.py`,
`install-amux.sh`, this file, and `tasks/20_codex_background_workers/state.md`
Phase 0 together, then re-run both suites at the new revision.
