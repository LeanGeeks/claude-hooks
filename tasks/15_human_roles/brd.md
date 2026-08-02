# Epic 15 — Human roles for AskUserQuestion

**Status:** ready for implementation · **Owner:** Anton · **Created:** 2026-08-02 · **Rev:** 2

*Rev 2 — `tool_response` shape verified against a live hook payload (§5.5) and
committed as fixtures; live-verification task 15-07 added; component table
updated.*

## 1. Problem & thesis

`AskUserQuestion` today has exactly one human destination: the single Telegram
chat bound to this machine's installation (`installations.telegram_chat_id`,
`db.py:29`, resolved off the caller's token in `app.py:562`). Every question —
"which shade of blue", "do we ship this quarter", "Postgres or SQLite" — reaches
the same person, the operator sitting at the terminal.

That is the wrong shape for a team. A UX call belongs to the designer, a scope
call to the product lead, an architecture tradeoff to the tech lead. Today the
operator either answers on their behalf (guessing) or relays the question by
hand and pastes the answer back.

**Thesis:** give the workspace a small, committed catalog of *human roles*, let
an agent address a question to one of them with an `@alias` prefix on the
question's `header`, and let each machine bind those roles to its own Telegram
destinations. Roles are workspace vocabulary and travel in git; bindings are
machine facts and never leave the machine.

**Compatibility floor:** a workspace with no `.claude/roles.toml` behaves exactly
as it does today — same destination, same message rendering, no new fields. Every
mechanism in this epic is opt-in per workspace.

## 2. Constraints discovered

Two facts about the existing system shaped every decision below. They are
recorded here because they are not obvious and re-deriving them is expensive.

**2.1 — `AskUserQuestion`'s input schema is closed.** Its per-question properties
are exactly `question | header | options | multiSelect`, with
`additionalProperties: false` at both the question level and the top level.
There is no field to put a role in; an extra key is rejected by the harness
before the hook is ever invoked. The role tag must therefore ride inside an
existing string field. `header` is the carrier — it is short, it is already
displayed as a chip in the terminal, and a visible `@ux` usefully tells the
operator the question is not primarily theirs.

**2.2 — Question groups are scoped to a chat on the relay.**
`_load_group_members()` filters `telegram_chat_id = ? AND group_id = ?`
(`app.py:1190`), and `_finalize_group_if_complete()` requires
`len(members) >= group_total`. A group whose members are split across two chats
can never finalize: each chat sees a subset, the count never reaches the total,
and every keyboard stays live forever.

This is why **one `AskUserQuestion` call addresses exactly one role** (§5.3).
That rule keeps one call = one destination = one group, and leaves the relay
server's grouping and finalization logic untouched by this epic.

## 3. Configuration

Two files. The split is deliberate: role *vocabulary* is a property of the
project and belongs in git; role *bindings* are a property of the machine and
must never be committed.

### 3.1 `.claude/roles.toml` — committed, machine-readable

Machine-readable only: aliases, titles, the default role, escalation policy. It
deliberately carries **no role descriptions**. What a role decides, and when an
agent should route to it, is prose for an agent to read — it belongs in
`CLAUDE.md` or any other prompt file, in whatever free form suits the project.
There is no generated block and no sync step to forget.

```toml
workspace_id = "leangeeks"     # optional; defaults to the workspace dir name
default      = "operator"      # required; must name a role below
escalate_after = "30m"         # optional; default for every role

[role.operator]
aliases = ["op"]
title   = "Operator"

[role.ux]
aliases = ["ux", "design", "designer"]
title   = "UX/UI designer"
escalate_after = "15m"         # optional; overrides the top-level default

[role.architect]
aliases = ["arch", "tech-lead"]
title   = "Tech lead / architect"
escalate_after = false         # opt out: never escalate, wait forever
```

The `[role.<id>]` key is itself always a valid alias. Aliases are
case-insensitive and must be unique across roles.

### 3.2 `~/.config/claude-tg-relay/config.toml` — per machine, never committed

Role bindings extend the existing relay client config, so no secret is ever
written inside a repository working tree.

```toml
server_url         = "https://relay.example.com"
installation_token = "rly_aaa..."      # unchanged: the default destination

[roles]                                # machine-wide, all workspaces
ux   = "rly_bbb..."
arch = "operator"                      # alias: same human as the operator

[workspace.leangeeks.roles]            # this workspace only; overrides [roles]
prod = "rly_ddd..."
ux   = "rly_eee..."

[escalate_after]                       # optional machine override of roles.toml
ux = "45m"

[workspace.leangeeks.escalate_after]
ux = "5m"
```

A value in a `roles` table is an **installation token** if it starts with `rly_`
(`tokens.py:8`); anything else is a **reference to another role**, resolved
transitively so one human can back several roles without duplicating a token.

Each role token is obtained and bound exactly as today — `relay-admin` issues an
installation, `relay-client bind --config-path <file>` prints a `BIND-XXXX-XXXX`
code, and the person sends `/bind <code>` in their own chat. Nothing about the
relay server changes.

### 3.3 Resolution precedence

For role `R` in workspace `W`:

1. `[workspace.W.roles].R`
2. `[roles].R`
3. top-level `installation_token`, **only if `R` is the default role**
4. unresolved → fall back to the default role (§5.2)

Escalation for role `R` resolves by the same shape:
`[workspace.W.escalate_after].R` → `[escalate_after].R` →
`[role.R].escalate_after` → top-level `escalate_after` → none. The first level
that *mentions* `R` wins, so an explicit `false` overrides a duration set below
it.

Note that the default role is not necessarily the top-level
`installation_token`: it can carry its own entry in a `roles` table, and then
that binding is where untagged questions and escalations go.

## 4. Agent-facing UX

An agent addresses a role by prefixing the question's `header` with `@alias`:

```
AskUserQuestion({ questions: [{
  header:   "@ux Layout",
  question: "Sidebar or top nav for the settings area?",
  options:  [ ... ],
}]})
```

- Terminal: the chip reads `@ux Layout`, unchanged native UI. Whoever is at the
  keyboard can still answer — that is a feature, not a leak.
- Telegram: the message goes to the designer's chat, headed
  `Question — leangeeks · for UX/UI designer`, with the `@ux ` prefix stripped
  from the rendered header.
- No prefix → the default role, exactly as today.

The roster an agent reads to *choose* an alias is free-form prose in `CLAUDE.md`
or a prompt file. `docs/roles-prompt-example.md` (task 15-06) ships a snippet to
adapt; nothing enforces or generates it.

## 5. Routing behaviour

### 5.1 Everything is stated, nothing is silent

Every reroute prints its reason into the Telegram message body. A typo'd `@uxx`
that quietly reaches the operator is a worse failure than one that says it did.

| Situation | Behaviour |
|---|---|
| Unknown alias | Default role + `⚠️ Unknown role @uxx — routed to Operator.` |
| Role has no binding on this machine | Default role + `⚠️ Intended for UX/UI designer — not reachable from this machine.` |
| Role's token invalid / `not_bound` / send fails | Same as above, reason names the failure |
| Default role also unreachable | Today's behaviour: return `None`, native terminal UI only, no auto-deny |
| Malformed `roles.toml` | Treated as *no roles configured* (today's behaviour), logged to `~/.claude/permission_telegram_errors.log`. `claude-roles` (15-06) is the loud surface. |

A hook must never break the question flow because a config file is wrong.

### 5.2 Fallback is a chain, not a single hop

Alias → role → binding → send. Each step falls to the default role on failure,
and the *default role* is the terminal case. Only when the default is also
unreachable does the flow degrade to terminal-only.

### 5.3 One role per call

Two distinct roles in one call is rejected: the hook emits
`behavior: deny` with a reason naming both aliases and instructing the agent to
split into separate calls. This costs that call's terminal prompt, but the agent
retries immediately and learns the rule; routing the remainder to whichever
alias came first would misroute it silently.

Two questions resolving to the *same* role (including two unknown aliases both
falling back to the default) are fine — this is a check on resolved roles, not
on literal strings.

### 5.4 Escalation is call-level

When `escalate_after` elapses with the call unanswered, the hook sends a **full
duplicate group** to the default destination, banner-prefixed
`⏳ @ux hasn't answered in 30m — you can decide instead`. Both groups are live;
the first group to *finalize* wins; the loser's messages get the winning answers
patched into their bodies and their keyboards stripped.

Call-level, not per-question, because per-question first-wins deadlocks: the
operator answers q1, the designer answers q2, neither group reaches its
`group_total`, and both hang forever (§2.2). Whoever takes over answers the
whole call.

Escalation does not fire when the role already fell back to the default — there
is nobody to escalate to.

### 5.5 The terminal always wins cleanly

If the answer arrives at the keyboard first, every role message is patched with
the terminal's answers before its keyboard is stripped, so the designer's chat
shows what was decided instead of a dead prompt. The answers come from
PostToolUse's `tool_response`, a **structured** object carrying an `answers` map
keyed by question text — captured from a live hook payload on 2026-08-02, with
real fixtures committed under `fixtures/` (see 15-04). Reading is still
defensive: anything unrecognised degrades to `✅ Answered in the terminal` with
the keyboard stripped, never worse than today.

### 5.6 Attribution back to the agent

When an escalated call is answered by the default role rather than the tagged
one, ` (answered by Operator)` is appended to that answer string. Claude Code
composes the tool result itself, so the answer value is the only channel back to
the agent — and an agent acting on a design call should know the designer did
not make it. Unescalated answers are returned verbatim.

This does not extend to a terminal win (§5.5): there the hook returns `None` and
Claude Code takes the answers from its own UI, so there is no value for the hook
to annotate. An agent cannot tell a terminal answer from a Telegram one, and
nothing in this epic changes that.

## 6. Scope of routing

Role routing applies to **`AskUserQuestion` only**.

Permission prompts are about this machine ("may I run this here") and idle
notifications are about this session; neither is a product or design decision,
and both keep going to the default destination through the existing, untouched
code path.

## 7. Components

| # | Task | Changes |
|---|------|---------|
| 15-01 | Role config loader | new `roles_config.py`: both TOML files, alias table, token/reference resolution, escalation lookup |
| 15-02 | Multi-destination transport | `telegram_permission_router.py` client registry; `role` on the state-store row; `posttool_hook.py` revokes with the right client |
| 15-03 | Alias routing in the send path | header parse/strip, destination resolution, mixed-role rejection, message rendering + reroute notes |
| 15-04 | Wait phase | sequential loop → thread per message; reduced `terminal_answers` onto the row, structured answer read, patch-then-cancel |
| 15-05 | Escalation to the default destination | duration timer, duplicate group, first-group-wins, loser finalization |
| 15-06 | Installer, diagnostics, docs | `shell/claude-roles`, `docs/roles.example.toml`, `docs/roles-prompt-example.md`, installer guidance, `architecture.md` |
| 15-07 | Live verification (human) | routing, escalation race, terminal win, no-regression — against a real relay and a second bound chat |

## 8. Out of scope

- **Relay server changes.** No schema migration, no `role` on the wire, no
  role-aware bind codes. The per-token model buys this, and it is the main
  reason it was chosen over server-side named bindings.
- **Role-routed permissions and idle notifications** (§6).
- **Per-question routing within one call** (§2.2, §5.3).
- **Generated role documentation.** No managed CLAUDE.md block, no sync command,
  no drift check (§3.1).
- **Non-Telegram destinations** (email, Slack, PagerDuty).
- **Role-based access control.** A role is a routing target, not a permission
  boundary — anyone with the terminal, and anyone in the default chat after
  escalation, can answer anything.
- **Asking several roles in parallel and continuing work meanwhile.** The hook
  blocks, as it does today.
