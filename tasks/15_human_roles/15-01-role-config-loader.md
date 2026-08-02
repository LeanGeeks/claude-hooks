# 15-01 — Role config loader

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §3 (configuration), §5.1–5.2 (fallback rules)

## Goal

A new pure module `.claude/hooks/roles_config.py` that reads the two config
files, builds the alias table, and answers one question for the send path:
*given this `@alias`, which installation token do I send to, what do I tell the
human about it, and when do I escalate?*

No side effects, no network, no `os.environ` mutation, no logging to disk. The
caller decides what to do with the errors this module reports.

## Scope

### Data types

```python
@dataclass(frozen=True)
class Role:
    role_id: str                       # the [role.<id>] key
    title: str                         # display name; defaults to role_id
    aliases: tuple[str, ...]           # lowercased, always includes role_id
    escalate_after: float | None       # seconds; None = never. The top-level
                                       # roles.toml default is merged in here at
                                       # load time, the way [all-profiles] is
                                       # merged in epic 13 — so this field is
                                       # always the final roles.toml answer.

@dataclass(frozen=True)
class RoleCatalog:
    workspace_id: str
    default_role: str
    roles: dict[str, Role]             # keyed by role_id
    alias_index: dict[str, str]        # lowercased alias -> role_id
    errors: tuple[str, ...]            # non-fatal config problems, human-readable
    path: Path | None                  # the roles.toml that was loaded

@dataclass(frozen=True)
class Bindings:
    server_url: str | None             # top-level server_url
    default_token: str | None          # top-level installation_token
    roles: dict[str, str]              # [roles]; raw values, tokens or role refs
    workspace_roles: dict[str, dict[str, str]]        # [workspace.*.roles]
    # Durations are parsed and validated at load time, so resolution never has
    # to. A key PRESENT with value None means "explicitly never"; a key ABSENT
    # means "not configured here, try the next level". Invalid durations are
    # dropped and recorded in `errors`.
    escalate_after: dict[str, float | None]           # [escalate_after]
    workspace_escalate_after: dict[str, dict[str, float | None]]
    errors: tuple[str, ...]

@dataclass(frozen=True)
class Destination:
    role_id: str                       # role actually being sent to
    title: str                         # title of role_id
    token: str | None                  # None = unreachable, caller degrades
    is_default: bool                   # role_id == catalog.default_role
    requested_role_id: str | None      # what the agent asked for, if different
    requested_title: str | None
    escalate_after: float | None       # seconds, or None
    notes: tuple[str, ...]             # reroute reasons, rendered into the message
```

### Public API

- `find_roles_file(workspace_dir: str) -> Path | None`
  `$CLAUDE_PROJECT_DIR/.claude/roles.toml` when the env var is set and the file
  exists; otherwise walk up from `workspace_dir` to the filesystem root looking
  for `.claude/roles.toml`. `None` when nothing is found.

- `load_catalog(workspace_dir: str, path: Path | None = None) -> RoleCatalog | None`
  `None` when no `roles.toml` exists, or when one exists but is unusable
  (unparseable TOML, missing `default`, `default` naming a role that isn't
  defined). `None` means **legacy mode** — the caller behaves exactly as it does
  today. Recoverable problems (a duplicate alias, one bad duration) populate
  `errors` and still return a usable catalog.

- `load_bindings(config_path: Path | None = None) -> Bindings`
  Reads `~/.config/claude-tg-relay/config.toml`. A missing file yields an
  all-empty `Bindings` (not an error — role bindings are opt-in). This is the
  **only** parser of that file: it also returns `server_url` and the top-level
  `installation_token`, which 15-02 needs to build a client per role token.

- `resolve_destination(catalog, bindings, alias: str | None) -> Destination`
  The whole fallback chain (§5.2). See rules below.

- `parse_header_alias(header: str) -> tuple[str | None, str]`
  `("ux", "Layout")` for `"@ux Layout"`. `(None, header)` unchanged when there
  is no leading tag.

- `parse_duration(value) -> float | None`
  Shared helper; see rules below.

### One installer line, here and not in 15-06

Add `roles_config.py` to `REQUIRED_HOOKS` in `install-claude-config.sh:163`.

It belongs with the file that creates it, not with the installer task. From
15-02 onward `telegram_permission_router` imports this module, and the installer
copies only what that explicit list names. If the entry waited until 15-06, any
`install-claude-config.sh` run mid-epic — which `docs/prompts/implementer.md` §5
actively encourages — would deploy a router that cannot import its own
dependency, disabling Telegram entirely until 15-06 landed.

## Alias parsing rules

Only a **leading** tag counts — `"Layout @ux"` has no tag.

| `header` | alias | cleaned header |
|---|---|---|
| `"@ux Layout"` | `"ux"` | `"Layout"` |
| `"@ux: Layout"` | `"ux"` | `"Layout"` |
| `"@ux"` | `"ux"` | `""` |
| `"@UX Layout"` | `"ux"` | `"Layout"` |
| `"Layout"` | `None` | `"Layout"` |
| `"Layout @ux"` | `None` | `"Layout @ux"` |
| `"@ Layout"` | `None` | `"@ Layout"` |
| `""` | `None` | `""` |

Alias grammar: `@` then `[A-Za-z0-9]` then `[A-Za-z0-9_-]*`, then optional `:`,
then whitespace or end of string. Aliases are lowercased for lookup. The cleaned
header is what gets rendered in Telegram; the terminal keeps the raw one.

## Duration rules

`parse_duration` accepts `"30m"`, `"2h"`, `"90s"`, `"45"` (bare = seconds), and
the TOML boolean `false` or the strings `"false"` / `"off"` / `"0"`, all of which
mean **never escalate** → `None`. Anything else raises `ValueError`.

Both loaders call it at load time and convert a `ValueError` into an entry in
their `errors`, dropping the key entirely — so an invalid duration falls through
to the next precedence level rather than silently meaning "never".

## Token resolution rules

`resolve_destination(catalog, bindings, alias)`:

1. **Requested role.** No alias → `catalog.default_role`. Alias present and
   known → that role. Alias present and unknown → default role, and append the
   note `Unknown role @uxx — routed to Operator.`
2. **Token lookup** for the requested role, in precedence order:
   `bindings.workspace_roles[catalog.workspace_id][role]` →
   `bindings.roles[role]` →
   `bindings.default_token` *only if the role is the default role*.
3. **Reference chase.** A looked-up value starting with `rly_` is a token. Any
   other value is a role alias — resolve it through `alias_index` and repeat
   step 2 for that role. Track visited role ids; a cycle, an unknown reference,
   or a chain longer than 8 hops yields no token and a *reason* carried into the
   step-4 note.
4. **Fallback.** Requested role resolved to no token and is not the default →
   switch to the default role, keep `requested_role_id` / `requested_title`,
   append exactly one note, and run step 2 for the default role. One note, not
   two — the reason belongs in the same sentence as the reroute:

   | why there is no token | note |
   |---|---|
   | no binding on this machine | `Intended for Product lead — not reachable from this machine.` |
   | alias cycle | `Intended for Tech lead / architect — its binding on this machine is broken (alias cycle: arch → prod → arch).` |
   | reference to an unknown role | `Intended for Tech lead / architect — its binding on this machine is broken (points at unknown role "designr").` |

5. **Unreachable.** The default role has no token either → return a Destination
   with `token=None`. The caller degrades to terminal-only.
6. **Escalation.** `escalate_after` is populated **only** when no fallback
   happened (`requested_role_id is None`), the resolved role is not the default,
   and the resolved token differs from the default role's token — `arch =
   "operator"` (§3.2) points at the same human, so escalating would just message
   them twice. Otherwise `None`: there is nobody to escalate to (§5.4).

   Lookup takes the **first level where the key is present**, even when its value
   is `None` (an explicit "never" must beat a lower-level duration):
   `bindings.workspace_escalate_after[ws][role]` →
   `bindings.escalate_after[role]` → `catalog.roles[role].escalate_after`
   (which already folds in the roles.toml top-level default).

## Worked example

```toml
# <workspace>/.claude/roles.toml
workspace_id   = "leangeeks"
default        = "operator"
escalate_after = "30m"

[role.operator]
aliases = ["op"]
title   = "Operator"

[role.ux]
aliases = ["ux", "design"]
title   = "UX/UI designer"
escalate_after = "15m"

[role.architect]
aliases = ["arch"]
title   = "Tech lead / architect"
escalate_after = false

[role.prod]
title = "Product lead"
```

```toml
# ~/.config/claude-tg-relay/config.toml
installation_token = "rly_operator"

[roles]
ux   = "rly_designer"
arch = "operator"

[workspace.leangeeks.escalate_after]
ux = "5m"
```

| alias | role_id | token | escalate_after | notes |
|---|---|---|---|---|
| `None` | `operator` | `rly_operator` | `None` (is default) | — |
| `"design"` | `ux` | `rly_designer` | `300.0` (workspace override) | — |
| `"arch"` | `architect` | `rly_operator` (via reference) | `None` (`false`) | — |
| `"prod"` | `operator` | `rly_operator` | `None` | `Intended for Product lead — not reachable from this machine.` |
| `"nope"` | `operator` | `rly_operator` | `None` | `Unknown role @nope — routed to Operator.` |

## Implementation notes

- `tomllib` (stdlib, 3.11+), same as the profile loader.
- `workspace_id` defaults to the name of the directory *containing* the
  `.claude/` dir the roles file was found in — not the session `cwd`, which may
  be a subdirectory. It is matched against `[workspace.<id>]` keys exactly:
  case-sensitive, no normalisation. A mismatch is not an error — it just means
  no workspace-scoped bindings apply — so `claude-roles` (15-06) printing the
  resolved `workspace_id` is how a user debugs it.
- The `[role.<id>]` key is always an alias for itself; add it to `aliases`
  before checking for duplicates.
- Duplicate alias across two roles: first definition in file order wins, loser
  recorded in `errors`. Do not raise — a bad alias must not break the question
  flow (§5.1).
- Keep `Destination.notes` as display-ready sentences. This module owns the
  wording so 15-03 has nothing to invent, and the tests assert on it.
- No caching. A hook process is short-lived and reads each file at most twice.

## Testing

New file `tests/test_unit_roles_config.py`; register it in
`tests/run_all_tests.py` as `"unit_roles_config": "test_unit_roles_config"`.

Build every fixture as a temporary TOML file (`tmp_path`). Never read the real
`~/.config/claude-tg-relay/config.toml` or a real `.claude/roles.toml`.

**Two escape routes this module opens, which tests must close.** `find_roles_file`
walks *up* the filesystem and honours `CLAUDE_PROJECT_DIR`, so a test that passes
a real path — or runs with that env var set, which it is inside a Claude Code
session — can silently read a `roles.toml` outside the fixture. The suite runs
from the repo, and this repo may itself grow a `.claude/roles.toml`. So:

- Point `CLAUDE_PROJECT_DIR` at a temp dir (or delete it) for the whole module,
  following the `os.environ.setdefault` pattern in `tests/conftest.py` and
  `tests/run_all_tests.py:29`.
- Never pass `os.getcwd()` or a repo path as `workspace_dir`.
- Include one test that asserts `find_roles_file` on a `tmp_path` with no
  `.claude/` returns `None` — i.e. that the upward walk did not escape into the
  developer's real tree.

## Done criteria

- [ ] `parse_header_alias` matches every row of the table above.
- [ ] `parse_duration` handles `30m` / `2h` / `90s` / `45` / `false` / `"off"`,
      and raises on garbage.
- [ ] `load_catalog` returns `None` for: no file, unparseable TOML, missing
      `default`, `default` naming an undefined role.
- [ ] `load_catalog` returns a usable catalog with populated `errors` for a
      duplicate alias and for an unparseable per-role duration.
- [ ] `workspace_id` defaults to the workspace directory name when unset.
- [ ] `find_roles_file` walks up from a nested subdirectory and honours
      `CLAUDE_PROJECT_DIR`.
- [ ] `load_bindings` on a missing file returns empty tables, no error; on a
      real file it returns `server_url` and `default_token` alongside the role
      tables.
- [ ] `resolve_destination` reproduces every row of the worked-example table,
      including exact note wording.
- [ ] Reference chains resolve; a two-role cycle and a reference to an unknown
      role each fall back to the default with the single combined note from the
      step-4 table, and neither hangs.
- [ ] An invalid duration is dropped and falls through to the next precedence
      level; an explicit `false` at a higher level beats a duration at a lower
      one.
- [ ] Escalation is `None` whenever a fallback happened, the role is the
      default, or the role's token *is* the default token; and follows the
      four-level precedence otherwise.
- [ ] `roles_config.py` is listed in `REQUIRED_HOOKS` in
      `install-claude-config.sh`.
