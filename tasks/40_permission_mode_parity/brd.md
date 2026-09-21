# Epic 40 — Telegram mirrors the terminal, in every permission mode

**Status:** todo · **Owner:** Anton · **Created:** 2026-09-21 · **Rev:** 1
**Type:** this repo only (hooks + store + docs; no relay change, no `amux` change)

> Broken down into tasks — see [state.md](./state.md) for ordering, the shared
> edit points and the invariants.

**Provenance.** Filed 2026-09-21 from the operator's observation: *"permission
forwarding predates Auto mode; the hooks intercept tool calls before Auto mode
kicks in and forward to Telegram even when the call would have been
auto-allowed."* Everything in §1 was read off this machine on that date — the
installed CLI bundle (`2.1.278`) and `~/.claude/permission_request_debug.log`.
Claims that were reasoned rather than measured are labelled **unverified**.

## 1. Problem & thesis

### 1.1 The mechanism has a name: `hookAskFloor`

`pretool_hook.py` returns `permissionDecision: "ask"` for every Bash/Monitor
command that is not on the allowlist (decided at `:666`, emitted at `:2115-2126`).
In current Claude Code that is **not** "no opinion" — it is a floor. The
auto-mode classifier still runs, but its `allow` verdict is downgraded back to
`ask`. The CLI says so itself:

```
Hook returned 'ask' for <tool>, but ask rule/safety check requires full
permission pipeline (hookAskFloor — a classifier allow re-surfaces as this ask)
```

So the prompt is raised, the ask path runs, `permission_request_hook.py` fires,
and the card lands on Telegram — for a call auto mode was ready to allow
silently. Auto mode cannot express an opinion about Bash at all while this hook
is installed.

### 1.2 Measured footprint on this machine

`~/.claude/permission_request_debug.log` covers **2026-09-14 → 2026-09-21** and
holds **416** `PermissionRequest` invocations:

| `permission_mode` | Requests | Of which `Bash` |
|---|---|---|
| `bypassPermissions` | 236 | — |
| `default` | 94 | — |
| `auto` | 86 | **69** |

The 69 are the bug. A Bash call the validator *allows* never reaches
`PermissionRequest` (the hook short-circuits it — modulo the flag-gated funnel
in §2.2, which reads default-off), and one it *denies* never gets there either —
so a Bash row in auto mode essentially always sits behind a PreToolUse `ask`.
The tool split was read with
`grep -B 3 "Permission mode: auto" ~/.claude/permission_request_debug.log | grep -o "Tool: .*"`,
and 69 + 17 accounts for all 86, which is what makes the extraction trustworthy. The other 17 auto-mode rows are `AskUserQuestion`, which asks
the operator a question in **every** mode and is correctly forwarded.

How many of the 69 the classifier would have allowed is **not measurable from
the logs** — the classifier never got to vote. The 236 `bypassPermissions` rows
are the same mechanism in a session that asked for no prompts at all: the hook's
`ask` outranks `--dangerously-skip-permissions`, and
`permission_request_hook.py:1640` then auto-allows what the harness would have
allowed by itself.

### 1.3 What is *not* broken

- **`PermissionRequest` ordering is already right.** It runs on the ask path
  (`decideLocation:"ask-path"`), i.e. *after* rules → mode → classifier, racing
  the terminal dialog. Remove the pretool floor and it stops seeing calls the
  classifier resolved. No re-plumbing is needed.
- **`permission_mode` reaches the hooks.** `permission_request_hook.py:1572`
  already reads it, and the three values above prove it arrives.
- **Deny still means deny.** A PreToolUse `deny` short-circuits the pipeline
  before any mode logic, `bypassPermissions` included. The guard is unaffected
  by anything in this epic.

### 1.4 Thesis — one rule, applied everywhere

**Telegram mirrors the terminal, one-to-one.** Whatever the local pipeline
resolves without the operator — a classifier allow, a classifier deny, a
`bypassPermissions` allow, a `dontAsk` refusal, an `acceptEdits` write — stays
silent on **both** surfaces. Whatever would raise a prompt in the terminal is
forwarded. Nothing added, nothing subtracted.

That rule decides every open question in this epic, including the ones it
*rejects* (§2.2). The change it demands is small: the hook must stop saying
"ask" when what it means is "I don't recognise this".

### 1.5 The harness already has the vocabulary for "I don't recognise this"

`defer` is a fourth PreToolUse decision value, schema-validated for command
hooks alongside `allow` / `deny` / `ask`, and documented in the CLI's own hook
help text. It means *I looked, I have no opinion, run the normal pipeline* —
and, unlike staying silent, it is explicit, logged, and it **outranks** other
hooks: across several PreToolUse hooks the precedence is
`deny > defer > ask > allow`.

The harness also states, in one function, what each mode does with an
ask-candidate: `auto → classify`, `bypassPermissions → allow`, `dontAsk → deny`,
everything else → `ask`. That mapping *is* the rule this epic implements: emit
`defer` exactly for the modes that resolve an ask-candidate on their own.

### 1.6 Evidence trail (re-verifiable)

All CLI claims come from the installed bundle. To re-read them:

```bash
strings -n 6 ~/.local/share/claude/versions/2.1.278 > /tmp/cc.txt
grep -c 'hookAskFloor' /tmp/cc.txt                 # the floor
grep -o '"allow","deny","ask","defer"' /tmp/cc.txt # the schema
grep -o 'decideLocation:"ask-path"' /tmp/cc.txt    # where PermissionRequest runs
```

**Minified symbol names are version-specific — the behaviour is the contract,
not the names.** Re-run the greps after a CLI upgrade before trusting any
sentence in this file that quotes the bundle.

## 2. Scope

### 2.1 In scope

- `pretool_hook.py` emits `defer` instead of `ask` for the *unknown* case when
  the session's mode resolves ask-candidates itself
  (task [40-01](./40-01-pretool-defers-to-the-mode_sonnet.md)).
- `permission_mode` recorded on the state-store row and surfaced on the Telegram
  card, so a card that *does* arrive says who raised it
  (task [40-02](./40-02-the-card-names-the-asker.md)).
- Docs brought in line: `architecture.md` hook table + flow diagram, and the
  daily-reviewer note about where the complete record now lives (40-02).
- An optional, decision-gated modernisation of `/yolo` onto the native
  `setMode` (task [40-03](./40-03-yolo-native-mode.md)) — **not** to be run
  without the operator's answer to the reversibility question in that file.
- Live verification across all six modes (task
  [40-04](./40-04-live-verification_human.md)).

### 2.2 Out of scope — deliberately

- **A `PermissionDenied` hook.** The harness fires this event *only* for
  auto-mode classifier denials, and a hook may answer `{"retry": true}`. It was
  proposed and **rejected**: a classifier denial asks the operator nothing in
  the terminal, so under §1.4 it must not ring a phone. The model gets its
  error and reformulates, exactly as it does on the CLI. Do not re-add it
  without revisiting the thesis.
- **Changing the validator's vocabulary.** `validate_bash_command` keeps
  returning `allow` / `deny` / `ask`. See H2 — a fourth value breaks the
  agent-decision guard and several hundred tests. The mode translation happens
  at the emission site only.
- **Weakening the deny guard or the risk gates.** Deny stays deny in every mode.
  The `permissions.ask` match and the write-redirect gate keep asking in every
  mode: they are opinions the harness does not hold, and they prompt in the
  terminal too, so forwarding them is parity, not excess.
- **Removing the `bypassPermissions` branch** in `permission_request_hook.py`
  (`:1626-1660`). After 40-01 it should go quiet for the unknown case, but it
  still catches a risk-gate `ask` raised inside a no-prompt session, and it is
  the only thing that records those. Leave the code, fix the comment.
- **The relay, the extension, `amux`.** Untouched.
- **Chasing the `hookAllowVouched` funnel.** In auto mode the harness *may* run
  a hook's `allow` past the classifier anyway; it is gated on a server-side flag
  that reads default-off today. Nothing to build — but it is why an allowlisted
  command could still be refused in auto mode, and 40-04 should note it if seen.

### 2.3 Accepted consequences

1. **Fewer store rows.** In `auto` / `bypassPermissions` / `dontAsk` sessions,
   requests the harness resolves by itself no longer create a
   `permission_requests.jsonl` row — that is the point, but it does narrow the
   daily reviewer's store-side view (`mcp__permissions__permission_history`).
   `~/.claude/bash_manual_confirm.log` still records **every** non-allowlisted
   command with the validator's verdict, so the parser-level record stays
   complete; 40-01 adds the emitted decision and the mode to those lines so the
   two can be told apart, and 40-02 points the reviewer's docs at it.
2. **Less visibility while away.** In auto mode the operator stops seeing the
   calls the classifier waved through. That is the mode's contract, and it is
   what the terminal shows too. Anyone who wants the old firehose should switch
   the session to `default`, not re-add a hook floor.
3. **A prompt may arrive with a different reason than before.** In auto mode the
   card's "⚠️ Not in allowlist" annotation is re-derived from the validator and
   is no longer *why* the prompt fired (H4). 40-02 fixes the wording; between
   40-01 and 40-02 the card is merely imprecise, never wrong about the command.

## 3. Constraints & hazards

- **H1 — fail open, always.** Both hooks sit on the path every tool call takes.
  No exception from mode handling may escape into a hook's exit code, and
  nothing here may delay a tool. Unknown mode ⇒ behave as `default` ⇒ `ask`.
- **H2 — the validator's vocabulary is load-bearing.**
  `permissions-mcp/permissions_mcp_lib.py:310-352` branches on
  `validate_bash_command`'s `decision` and **raises** on an unexpected value
  (`human-only: unexpected validator decision ...`) — that is epic 22's guard
  deciding whether an agent may answer a permission request at all. Some
  hundreds of assertions in `tests/test_integration_pretool.py` pin the same
  three values. Therefore: `validate_bash_command` returns `allow|deny|ask`
  unchanged, gains an additive *kind* for the ask, and `main()` alone decides
  what to print.
- **H3 — `defer` is newer than some CLIs, and that is survivable.** A CLI that
  does not know the value throws `Unknown hook permissionDecision type` while
  parsing hook stdout; the hook is recorded as a non-blocking failure and the
  pipeline proceeds as though the hook decided nothing — which is precisely what
  `defer` means. Worst case on an old CLI is today's behaviour plus a log line.
  The installed `2.1.278` accepts it. *(The degradation path was read from the
  bundle, not reproduced against an old CLI — **unverified**.)*
- **H4 — the Telegram card re-derives its own reason.**
  `telegram_permission_router.py:377-416` (`_unallowlisted_bash_parts`) re-runs
  the validator because the harness does not forward
  `permissionDecisionReason` to the `PermissionRequest` hook, and `:513-528`
  renders the result. After 40-01, in auto mode, that derivation explains the
  *validator's* silence, not the prompt. 40-02 owns the fix; do not "improve"
  the wording anywhere else.
- **H5 — `render_permission_body` must stay reconstructable.** The agent-decision
  finalization calls it a second time to rebuild the exact body it sent
  (`:484-489`). Anything 40-02 adds must derive from the stored row, never from
  live state that can change between the two calls.
- **H6 — a repo edit is not live.** `.claude/hooks/*` reaches `~/.claude/hooks/`
  only when `./install.sh` runs (`install-claude-config.sh` was deleted in epic
  29-09 — do not resurrect that name). Any "it works" claim must say which copy
  was tested.
- **H7 — pretool's confirm log is not test-redirected.**
  `pretool_hook.py:47` hardcodes `~/.claude/bash_manual_confirm.log` via
  `expanduser`, ignoring the `CLAUDE_MANUAL_CONFIRM_LOG` redirect that
  `tests/run_all_tests.py:35` sets and that `permission_state_store.py:52` and
  `permissions_mcp_lib.py:551` both honour. Every end-to-end pretool test with a
  non-allow decision therefore appends to the operator's real 6.7 MB log today.
  40-01 closes this, because it also changes what those lines contain.
- **H8 — subagent mode is unverified.** Whether a subagent's tool call carries
  the parent session's `permission_mode` in the hook payload has **not** been
  measured. 40-04 measures it. Until then, assume nothing: absent ⇒ `default`
  ⇒ `ask` (H1) is already the safe answer.

## 4. Acceptance

The epic is done when, on this machine, with the hooks installed:

1. In an **auto**-mode session, a non-allowlisted command the classifier is
   happy with runs **with no Telegram card and no terminal prompt**, and
   `~/.claude/bash_hook_debug.log` shows the hook emitted `defer`.
2. In the same session, a command that trips a **deny pattern** is still blocked
   by the hook, and one that trips a **risk gate** (ask pattern or a write
   redirect escaping the workspace) still prompts — on both surfaces.
3. In a **default**-mode session, behaviour is byte-identical to today.
4. `AskUserQuestion` is still forwarded in every mode.
5. A card that does arrive in auto mode says the harness flagged it, not "not in
   allowlist" (unless the allowlist really is the reason).
6. `python3 tests/run_all_tests.py` is green, with before/after counts reported.
7. [state.md](./state.md) records the answer to H8 and the post-change
   `Permission mode:` tally, so the 86/69 numbers in §1.2 have a successor.
