# amux-spawn — model selection reference

Reference for an agent choosing **which model, which backend and how much
thinking** a spawned worker gets. Companion to
[`amux-spawn-agent-reference.md`](./amux-spawn-agent-reference.md), which
covers orchestration (run-ids, `watch`, supervision).

Three independent knobs, none of which implies the others:

| Knob | What it selects | Scope |
|------|-----------------|-------|
| `--profile <name>` | **backend** — base URL, auth, and the tier alias→id map | amux-spawn's own flag, spawn only |
| `--model <alias\|id>` | **tier** within whatever backend is active | pass-through to `claude` |
| `--effort <level>` | **thinking budget** | pass-through to `claude` |

The one-line rule for an automated spawn:

```bash
amux-spawn spawn <suffix> --dir <workspace> --profile claude \
    --model=opus --effort=high -- "seed prompt"
```

Always use the **`--flag=value` form** for `--model` and `--effort`. Always
pass both. Both reasons are below.

## Discovering what profiles exist

Do **not** read `~/.claude/profiles.toml` to answer "what can I spawn on" —
there is a command, and it prints the full tier map:

```bash
amux-spawn profiles
```

```
claude             anthropic
  fable       claude-opus-5
  opus        claude-opus-4-6
  sonnet      claude-sonnet-4-6

claude-glm         api.z.ai
  model       glm-5.3[1m]        (ANTHROPIC_MODEL)
  fable       glm-5.3[1m]
  opus        glm-5.3[1m]
  sonnet      glm-5.3-flash[1m]
  haiku       glm-5.3-flash
  small-fast  glm-5.3-flash

claude-oc          claude-router.localhost
  fable       ocg-glm-5.3
  opus        ocg-glm-5.2
  sonnet      ocg-deepseek-v4-flash
  haiku       deepseek-v4-flash-haiku
  small-fast  deepseek-v4-flash-haiku
  effort      high                     (CLAUDE_CODE_EFFORT_LEVEL, overrides --effort)
```

One block per profile: `name  provider`, then every model tier that profile
sets. The label on the left is what you pass to `--model`, so the block reads
directly as "`--profile claude-glm --model sonnet` → `glm-5.3-flash[1m]`".
Tiers a profile does not set are omitted — those fall through to the harness
default.

Two lines are not tiers and are annotated as such:

- `model` — the profile sets `ANTHROPIC_MODEL`, a hard model pin rather than a
  tier alias.
- `effort` — the profile sets `CLAUDE_CODE_EFFORT_LEVEL`, which **overrides
  the `--effort` flag** of every worker spawned under it (see
  [`--effort`](#--effort--thinking-budget)).

A profile that pins no model at all prints `(no model pinned — tiers come from
the harness default)`.

`amux-spawn profiles --json` emits `[{name, env}]` with each profile's fully
resolved environment — use it when you need a key the blocks do not show.
Credential values are replaced with `<redacted>`:

```json
[{"name": "claude-glm", "env": {
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<redacted>",
    "ANTHROPIC_MODEL": "glm-5.3[1m]"}}]
```

Keys are preserved — only values go — so you can still see *which* vars a
profile sets. A key is treated as a credential when any underscore-delimited
segment of its name is one of `TOKEN`, `SECRET`, `KEY`, `PASSWORD`, `PASS`,
`PAT`, `AUTH`, `CREDENTIAL` (so `GITHUB_MCP_PAT` is redacted and `PATH` is
not).

`--json --no-redact` prints the real values. Do not use it in an agent
context: its output is a live credential, and anything you print becomes part
of a transcript. Both the human output and redacted `--json` are safe to
quote.

`~/.claude/profiles.toml` is the file you **edit** (see [Editing
profiles](#editing-profiles)); `amux-spawn profiles` is the file you **read**.

## `--profile` — what it does and does not pin

A profile is a named bundle of env vars, merged `[all-profiles]` →
`[profile.<name>]`, exported into amux-spawn's own environment before the
session is created. It typically carries:

- `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` — which backend answers
- `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` — the tier alias→id map
- `ANTHROPIC_MODEL` — a hard model pin, if the profile sets one
- anything else (`API_TIMEOUT_MS`, feature flags, …)

**A profile does not pin the model by itself.** The default `claude` profile
carries only the alias→id map, so `--profile claude` selects a *backend* and
leaves the tier to the harness default — which floats between sessions
(measured: it moved twice in one day). Only a profile that sets
`ANTHROPIC_MODEL` (e.g. `claude-glm`) pins a model on its own.

**A profile is spawn-time only.** `--profile` is refused on `resume` and
refused with `--provider codex` — both with actionable errors. Profiles reach
the child through amux's env allowlist (prefix-based over
`ANTHROPIC_*` / `CLAUDE_*`), never through argv, so nothing leaks to `ps`.

**Profiles do not inherit.** A worker spawned by a profiled session gets the
profile's env because env is inherited by the process tree — but a worker
spawned by a *non*-profiled session gets nothing. `[all-profiles]` only
applies to a spawn that names a profile.

## `--model` — tier within the backend

`--model` is a pass-through `claude` flag. Its value is a tier alias
(`opus`, `sonnet`, `haiku`, `fable`) resolved through the active profile's
alias→id map, or a literal model id.

So `--profile claude-glm --model opus` means "the opus-tier model *of the GLM
profile*" — `glm-5.3[1m]`, not Claude Opus. Tier and backend compose; they do
not conflict.

**Inheritance:** when the caller passes no `--model`, a tracked child inherits
its parent's explicit `--model` from the parent's `CC_FLAGS`
(`inherited_model_flag()`), so an opus parent spawns opus children. Only the
long `--model` spelling is recognised — **`-m opus` neither pins nor
inherits**. Inheritance is Claude-path only; no model is ever injected into a
Codex worker.

### The space-form trap

`spawn` has an optional `<suffix>` positional, and it is parsed before the
pass-through flags. With no explicit suffix, the space form's value is eaten
as the suffix and the child receives a **bare, valueless flag**:

| You type | suffix | forwarded to `claude` |
|---|---|---|
| `spawn --model opus` | `opus` | `--model` ← broken |
| `spawn --model=opus` | *(auto)* | `--model=opus` ← correct |
| `spawn w1 --model opus` | `w1` | `--model opus` ← also fine |
| `spawn --effort high` | `high` | `--effort` ← broken |
| `spawn --effort=high` | *(auto)* | `--effort=high` ← correct |

This is pre-existing argparse behaviour, identical for both providers. Use
`=` and it never bites. (`resume` has no suffix positional, so both spellings
work there.)

## `--effort` — thinking budget

Levels: `low`, `medium`, `high`, `xhigh`, `max`.

**Effort has no inheritance path.** A child that passes no `--effort` reads
the harness default — the operator's last interactive `/model` write — no
matter what its parent ran at. There is no `inherited_effort_flag()`; the
generalisation was specified and deliberately declined
(`tasks/21_spawn_pin_hardening/21-03-*`). Pass `--effort=<level>` on every
automated spawn.

**`CLAUDE_CODE_EFFORT_LEVEL` overrides `--effort`, in both polarities.**
Measured: `CLAUDE_CODE_EFFORT_LEVEL=low claude --effort high` serves *low*.
The var reaches children through the prefix-based allowlist, so a profile that
sets it (e.g. `claude-oc`) silently un-pins the `--effort` flag of every
worker spawned under it. This is why amux-spawn does **not** count the env var
as a pin — see the warnings below.

## The two spawn warnings

A non-TTY (i.e. agent) spawn warns on stderr — it never refuses:

```
amux-spawn: warning: non-TTY (agent) spawn pins no model — the child reads
  the harness default, which floats between sessions; pass --model <alias>
amux-spawn: warning: non-TTY (agent) spawn pins no effort — the child reads
  the harness default; pass --effort <level>
```

| Warning | Silenced by |
|---|---|
| pins no model | `--model=<alias>` on this spawn, an inherited parent `--model`, or `ANTHROPIC_MODEL` in the environment (which a profile may set) |
| pins no effort | `--effort=<level>` on this spawn — **and nothing else**, deliberately |

If you see either warning from your own spawn, the worker is running on a
value nobody chose. Fix the spawn; do not ignore it.

## Codex workers

`--provider codex` has its own rules: no model is ever injected, `--profile`
is refused, and `~/.codex/config.toml` decides when `--model=` is absent. See
[`amux-spawn-codex-workers.md` §Model and profile
selection](./amux-spawn-codex-workers.md#model-and-profile-selection).

## Editing profiles

`~/.claude/profiles.toml`, three sections:

```toml
[vars]              # interpolation-only; ${name} refs, never exported
[all-profiles]      # applied to every profile; per-profile keys win
[profile.<name>]    # <name> becomes the shell alias
```

Changes take effect in a **new shell** (aliases are generated at source-time
by `shell/amux-spawn.bash`); no install script rerun. The installer seeds the
file from `shell/profiles.example.toml` only if it does not exist — it never
overwrites. Architecture: `architecture.md` §"Model profiles (epic 13)".

## What not to do

- **Do not spawn an automated worker without `--model=` and `--effort=`.**
  Unpinned means "whatever the operator's last `/model` wrote", which floats.
- **Do not assume `--profile` pins a model.** Most profiles pin only the
  backend and the tier map.
- **Do not use the space form** (`--model opus`) on `spawn` without an
  explicit suffix — the value is swallowed.
- **Do not use `-m`.** It is invisible to both the pin check and inheritance.
- **Do not set `CLAUDE_CODE_EFFORT_LEVEL`** to "help" — it overrides the
  explicit `--effort` of every descendant.
- **Do not reach for `profiles --json --no-redact`.** Plain `--json` redacts
  credentials and still shows every key; `--no-redact` puts a live token in
  your transcript.
- **Do not read `profiles.toml` to discover models.** Use `amux-spawn
  profiles`.
