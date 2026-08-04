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
| 15-04 | [Wait phase](./15-04-wait-phase_opus.md) | done | 15-03 | Sequential loop → thread-per-message; terminal answers patched into the role's chat |
| 15-05 | [Escalation](./15-05-escalation_opus.md) | done | 15-04 | Deadline, duplicate group to the default, first-group-wins |
| 15-06 | [Installer, diagnostics, docs](./15-06-installer-diagnostics-docs.md) | done | 15-05 | `shell/claude-roles`, example TOML, prompt snippet, installer, `architecture.md` |
| 15-07 | [Live verification](./15-07-live-verification_human.md) | todo | 15-06 | **human** — needs a real relay and a second bound chat; no agent can provision either |

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
