# 15-06 — Installer, diagnostics, docs

**Status:** todo · **Depends on:** 15-05
**Read first:** [brd.md](./brd.md) §3 (configuration), §5.1 (nothing is silent)

## Goal

Make the feature installable, inspectable, and discoverable by an agent. Roles
fail *quietly by design* inside the hooks (brd §5.1) — a bad `roles.toml` must
never break a question. This task builds the loud surface that compensates:
one command that says exactly where each role goes and what is broken.

## Scope

### Where the logic lives

`shell/claude-roles` has no `.py` extension (matching `shell/claude-history`), so
it cannot be imported and therefore cannot be unit-tested. Put the thinking in
`roles_config.py`, which is importable, and keep the script thin:

```python
# added to roles_config.py — still pure, still no network
def roles_report(catalog, bindings) -> dict     # the whole picture, as data
def format_roles_table(report: dict) -> str     # the human rendering
```

`shell/claude-roles` is then argparse + these two calls + the `--check` network
probe. `--check` is the only part that cannot live in `roles_config`, because
that module is specified as network-free (15-01).

### `shell/claude-roles` — the diagnostic

A standalone Python script, same shape as `shell/claude-history`.

```
claude-roles [--workspace-dir DIR] [--check] [--json]
```

Offline by default: loads the catalog for `DIR` (default `$PWD`) plus the
machine bindings and prints one row per role.

```
workspace: leangeeks   (/data/sync/work/leangeeks-ai/.claude/roles.toml)
default:   operator

ALIASES              ROLE       TITLE                   DESTINATION      ESCALATE
op, operator         operator   Operator                default token    —
ux, design, designer ux         UX/UI designer          own binding      5m
arch, tech-lead      architect  Tech lead / architect   -> operator      —
prod                 prod       Product lead            (none)           —
                                                        ↳ falls back to Operator

errors:
  duplicate alias "ux" in [role.prod]; kept the definition from [role.ux]
```

**Never print token material — not even a truncated prefix.** Diagnostics get
pasted into issues and chats. `DESTINATION` is one of `default token`,
`own binding`, `-> <role>` (a reference, brd §3.2), or `(none)`. That is enough
to debug routing, and it leaks nothing.

`ESCALATE` reports the **effective** value — what the hook will actually do —
not what `roles.toml` configures. `—` means no escalation is possible: the
default role has nobody above it, an unbound role has already fallen back
(brd §5.4), and a role resolving to the default's own token would only duplicate
the question into the chat that already holds it. That last case is why
`architect` above shows `—` and not `never`, despite carrying
`escalate_after = false`: `arch = "operator"` already decided the outcome.
`never` is reserved for the one shape where escalation *could* fire and was
switched off — a role with its own distinct binding and `escalate_after = false`.
A column that advertised an escalation the resolver suppresses would be worse
than no column.

`--check` calls `GET /v1/installations/me` with each distinct token and appends
a status: `bound`, `not bound — run relay-client bind`, or `invalid token (401)`.
On success it also shows the relay's **installation label and id** from that
response — non-secret, and far more useful for confirming *who* a role points at
than any part of the token would be. This is the only networked part; keep it
behind the flag so the offline path stays instant and usable without a relay.

`--json` emits the same data as one object, under the same no-token rule.

Exit codes: `0` clean, `1` any catalog or binding error, `2` (with `--check`) any
role whose token is unbound or invalid. A workspace with no `roles.toml` prints
`no roles configured (default destination only)` and exits `0` — that is a valid
state, not a problem.

Import `roles_config` from `~/.claude/hooks/`, falling back to a path relative
to the script when run from a repo checkout — the same two-step resolution
`telegram_permission_router.py:53` uses for `relay_server`.

### `install-claude-config.sh`

- `roles_config.py` was already added to `REQUIRED_HOOKS` by 15-01 (it had to be,
  or a mid-epic install would ship a router that cannot import it). Verify the
  entry is present; do not add a second one.
- Install `shell/claude-roles` to `$CLAUDE_SHELL_DIR` (`:393`) with `chmod +x`,
  alongside the existing `.bash` snippets, and symlink it into `~/.local/bin`
  when that directory exists and is on `PATH`.
- Add roles to the final summary (`:767`): whether the current workspace has a
  `roles.toml`, and the `claude-roles` invocation.
- **Do not** create or copy any `roles.toml`. It is per-workspace, committed,
  and authored by hand; a template that appears by magic would get committed
  half-edited. `docs/roles.example.toml` is the template, and the summary points
  at it.
- Nothing is added to `.gitignore`: bindings live in
  `~/.config/claude-tg-relay/config.toml`, outside every repo (brd §3.2).

### `docs/roles.example.toml`

Annotated, copy-paste-able `.claude/roles.toml` covering the four example roles
from the brd, with comments explaining `workspace_id`, `default`,
`escalate_after` (including `false`), and alias uniqueness. Add a header comment
stating what does **not** belong here: descriptions, tokens, chat ids.

### `docs/roles-prompt-example.md`

The agent-facing half — free-form prose to adapt into a workspace `CLAUDE.md` or
any prompt file (brd §3.1, §4). Nothing generates or validates it; that is the
point. It must cover:

- The `@alias` convention, with a worked `AskUserQuestion` example.
- One paragraph per role describing **what it decides and when to route there** —
  the judgement an agent cannot get from `roles.toml`, which is exactly why the
  catalog does not carry descriptions.
- That an untagged question goes to the default role, so tagging is for
  exceptions, not for every question.
- One call, one role — and that mixing two is rejected with an explanatory deny
  (brd §5.3).
- Keep aliases 2–4 characters: the harness asks for a `header` of ~12 characters
  and `@designer ` would eat most of it.

### `architecture.md`

- New section after *Model profiles (epic 13)*: the two config files, the
  `@alias` → role → token chain, the fallback ladder, escalation, and the two
  constraints from brd §2 — those are exactly the facts a future reader will
  otherwise re-derive from the relay source.
- Update the repository-layout block for `roles_config.py` and
  `shell/claude-roles`.
- Update the *AskUserQuestion* flow paragraph, which currently states a single
  destination.

## Testing

No new test module. Add cases to `tests/test_unit_roles_config.py` (15-01) for
`roles_report` and `format_roles_table`, reusing that module's fixtures and its
`CLAUDE_PROJECT_DIR` isolation. Do not shell out to `claude-roles`, and do not
test `--check` — it is the network path.

## Done criteria

- [ ] `claude-roles` in a workspace with no `roles.toml` prints the
      no-roles line and exits `0`.
- [ ] It renders every role with its aliases, destination and escalation, and
      shows the fallback line for an unbound role.
- [ ] Catalog and binding errors are printed and force exit `1`.
- [ ] `--check` reports `bound` / `not bound` / `invalid token` per distinct
      token, shows the installation label and id on success, and exits `2` when
      any is not bound.
- [ ] No output path — table, `--json`, or error text — contains any part of a
      token. Assert this against a fixture whose tokens are recognisable
      strings.
- [ ] `--json` output round-trips through `json.loads` and carries the same
      facts as the table.
- [ ] `roles_report` and `format_roles_table` live in `roles_config.py`, are
      unit-tested, and perform no I/O beyond what 15-01 already does.
- [ ] The script runs both from a repo checkout and from an installed
      `~/.claude/hooks` layout.
- [ ] A fresh `install-claude-config.sh` run installs `roles_config.py` (listed
      since 15-01, exactly once) and `claude-roles`, and the summary mentions
      roles.
- [ ] Re-running the installer is idempotent and creates no `roles.toml`.
- [ ] `docs/roles.example.toml` parses with `tomllib` and loads cleanly through
      `load_catalog` with zero errors.
- [ ] `architecture.md` no longer describes AskUserQuestion as single-destination.
