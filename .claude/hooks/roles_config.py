#!/usr/bin/env python3
"""Role configuration loader for epic 15 — Human roles for AskUserQuestion.

Pure module: no side effects, no network, no os.environ mutation, no disk
logging. Callers decide what to do with the errors reported here.

Public API
----------
find_roles_file  — locate <workspace>/.claude/roles.toml, honouring CLAUDE_PROJECT_DIR
load_catalog     — parse roles.toml into a RoleCatalog (None = legacy mode)
load_bindings    — parse ~/.config/claude-tg-relay/config.toml into Bindings
resolve_destination — full fallback chain: alias → role → token → Destination
parse_header_alias  — split "@ux Layout" into ("ux", "Layout")
parse_duration      — parse "30m" / "2h" / "90s" / "45" / false / "off" → float | None
roles_report     — structured summary of roles + destinations (no token material)
format_roles_table  — human-readable table rendering of a roles_report dict
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# ── Sentinel ──────────────────────────────────────────────────────────────────

_UNSET = object()  # "this key was absent from config" — distinct from None ("never")

# ── Default paths ─────────────────────────────────────────────────────────────

_DEFAULT_BINDINGS_PATH = Path.home() / ".config" / "claude-tg-relay" / "config.toml"

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Role:
    role_id: str
    title: str                         # display name; defaults to role_id
    aliases: tuple[str, ...]           # lowercased; always includes role_id
    escalate_after: float | None       # seconds; None = never; top-level default merged in


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
    workspace_roles: dict[str, dict[str, str]]           # [workspace.*.roles]
    # Durations are parsed at load time; None means "explicitly never".
    # A key PRESENT with value None means "explicitly never"; ABSENT means
    # "not configured here, try next level".
    escalate_after: dict[str, float | None]              # [escalate_after]
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


# ── parse_header_alias ────────────────────────────────────────────────────────

# Leading tag: @<alias>[:][ rest] or @<alias>[:]<end-of-string>
# Alias grammar: [A-Za-z0-9] then [A-Za-z0-9_-]*
# "@ Layout" does NOT match because the char after @ must be [A-Za-z0-9].
_ALIAS_RE = re.compile(
    r'^@([A-Za-z0-9][A-Za-z0-9_-]*)(?::)?(?:\s+(.*)|\s*$)',
    re.DOTALL,
)


def parse_header_alias(header: str) -> tuple[str | None, str]:
    """Split a leading @alias tag from *header*.

    Returns ``(alias, cleaned_header)``.  ``alias`` is lowercased; it is
    ``None`` when there is no leading tag.  The cleaned header is what gets
    rendered in Telegram; the terminal keeps the raw one.
    """
    m = _ALIAS_RE.match(header)
    if m is None:
        return None, header
    alias = m.group(1).lower()
    rest = m.group(2) if m.group(2) is not None else ""
    return alias, rest


# ── parse_duration ────────────────────────────────────────────────────────────


def parse_duration(value: object) -> float | None:
    """Parse a duration value from config.

    Accepts ``"30m"``, ``"2h"``, ``"90s"``, ``"45"`` (bare seconds), the TOML
    boolean ``False``, and the strings ``"false"`` / ``"off"`` / ``"0"`` — all
    of which mean *never escalate* and return ``None``.

    Raises ``ValueError`` on anything else.
    """
    # TOML boolean false
    if value is False:
        return None

    # Bare integer / float (not bool — bools are ints in Python)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v == 0.0:
            return None
        return v

    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("false", "off", "0"):
            return None
        if not s:
            raise ValueError(f"Invalid duration: {value!r}")
        suffix = s[-1]
        try:
            if suffix == "m":
                return float(s[:-1]) * 60.0
            if suffix == "h":
                return float(s[:-1]) * 3600.0
            if suffix == "s":
                return float(s[:-1])
            # bare number (seconds)
            return float(s)
        except ValueError:
            raise ValueError(f"Invalid duration: {value!r}")

    raise ValueError(f"Invalid duration: {value!r}")


# ── find_roles_file ───────────────────────────────────────────────────────────


def find_roles_file(workspace_dir: str) -> Path | None:
    """Locate the roles.toml for *workspace_dir*.

    When ``CLAUDE_PROJECT_DIR`` is set, only checks
    ``$CLAUDE_PROJECT_DIR/.claude/roles.toml`` — returns ``None`` if that file
    does not exist (does NOT fall through to the filesystem walk). This makes
    test isolation possible: point the env var at a temp dir and the walk is
    suppressed.

    Otherwise walks up from *workspace_dir* to the filesystem root looking for
    ``.claude/roles.toml``.
    """
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        candidate = Path(env_dir) / ".claude" / "roles.toml"
        return candidate if candidate.is_file() else None

    current = Path(workspace_dir).resolve()
    while True:
        candidate = current / ".claude" / "roles.toml"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ── load_catalog ──────────────────────────────────────────────────────────────


def load_catalog(workspace_dir: str, path: Path | None = None) -> RoleCatalog | None:
    """Parse roles.toml and return a :class:`RoleCatalog`.

    Returns ``None`` (legacy mode) when:
    - no roles.toml is found,
    - the file is unparseable TOML,
    - ``default`` key is missing,
    - ``default`` names a role not defined in the file.

    Recoverable problems (duplicate alias, bad per-role duration) populate
    ``errors`` and still return a usable catalog.
    """
    if path is None:
        path = find_roles_file(workspace_dir)
    if path is None:
        return None

    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception:
        return None

    default_role = raw.get("default")
    if not isinstance(default_role, str) or not default_role:
        return None

    errors: list[str] = []

    # workspace_id: use explicit value or fall back to the workspace dir name
    workspace_id = raw.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        # path = <workspace>/.claude/roles.toml → path.parent = .claude → .parent = workspace
        workspace_id = path.parent.parent.name

    # Top-level escalate_after default (sentinel distinguishes "absent" from "never")
    top_ea: object = _UNSET
    raw_top_ea = raw.get("escalate_after")
    if raw_top_ea is not None:
        try:
            top_ea = parse_duration(raw_top_ea)
        except ValueError:
            errors.append(f"Top-level escalate_after {raw_top_ea!r}: invalid duration, ignored")
            top_ea = _UNSET

    # Parse [role.*] sections
    roles_raw = raw.get("role", {})
    if not isinstance(roles_raw, dict):
        return None

    roles: dict[str, Role] = {}
    alias_index: dict[str, str] = {}

    for role_id, role_data in roles_raw.items():
        if not isinstance(role_data, dict):
            errors.append(f"Role {role_id!r}: not a table, skipping")
            continue

        title_raw = role_data.get("title", role_id)
        title = str(title_raw) if title_raw is not None else role_id

        # Build alias list: role_id first, then explicitly listed aliases
        role_id_lower = role_id.lower()
        raw_aliases = role_data.get("aliases", [])
        if isinstance(raw_aliases, list):
            extra = [a.lower() for a in raw_aliases if isinstance(a, str) and a.lower() != role_id_lower]
        else:
            extra = []
        candidate_aliases: list[str] = [role_id_lower] + extra

        # Per-role escalate_after (merged with top-level default at load time)
        raw_role_ea = role_data.get("escalate_after")
        if raw_role_ea is not None:
            try:
                role_ea: float | None = parse_duration(raw_role_ea)
            except ValueError:
                errors.append(f"Role {role_id!r}: invalid escalate_after {raw_role_ea!r}, falling through to default")
                role_ea = top_ea if top_ea is not _UNSET else None  # type: ignore[assignment]
        else:
            role_ea = top_ea if top_ea is not _UNSET else None  # type: ignore[assignment]

        # Register aliases; first definition in file order wins on collision
        registered: list[str] = []
        for alias in candidate_aliases:
            if alias in alias_index:
                existing = alias_index[alias]
                errors.append(
                    f"Alias {alias!r} already claimed by role {existing!r}; skipping for role {role_id!r}"
                )
            else:
                alias_index[alias] = role_id
                registered.append(alias)

        roles[role_id] = Role(
            role_id=role_id,
            title=title,
            aliases=tuple(registered),
            escalate_after=role_ea,
        )

    if default_role not in roles:
        return None

    return RoleCatalog(
        workspace_id=workspace_id,
        default_role=default_role,
        roles=roles,
        alias_index=alias_index,
        errors=tuple(errors),
        path=path,
    )


# ── load_bindings ─────────────────────────────────────────────────────────────


def load_bindings(config_path: Path | None = None) -> Bindings:
    """Parse ``~/.config/claude-tg-relay/config.toml`` into a :class:`Bindings`.

    A missing file returns an all-empty Bindings — role bindings are opt-in.
    Parse errors are recorded in ``errors`` and an empty Bindings is returned.
    """
    path = config_path if config_path is not None else _DEFAULT_BINDINGS_PATH

    _empty = Bindings(
        server_url=None,
        default_token=None,
        roles={},
        workspace_roles={},
        escalate_after={},
        workspace_escalate_after={},
        errors=(),
    )

    if not path.is_file():
        return _empty

    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception as exc:
        return Bindings(
            server_url=None,
            default_token=None,
            roles={},
            workspace_roles={},
            escalate_after={},
            workspace_escalate_after={},
            errors=(f"Failed to parse {path}: {exc}",),
        )

    errors: list[str] = []

    server_url: str | None = raw.get("server_url") or None
    default_token: str | None = raw.get("installation_token") or None

    # [roles]
    roles: dict[str, str] = {}
    for k, v in raw.get("roles", {}).items():
        if isinstance(v, str):
            roles[k] = v

    # [workspace.<id>.roles] and [workspace.<id>.escalate_after]
    workspace_roles: dict[str, dict[str, str]] = {}
    workspace_escalate_after: dict[str, dict[str, float | None]] = {}

    for ws_id, ws_data in raw.get("workspace", {}).items():
        if not isinstance(ws_data, dict):
            continue

        ws_roles_raw = ws_data.get("roles", {})
        if isinstance(ws_roles_raw, dict):
            workspace_roles[ws_id] = {
                k: v for k, v in ws_roles_raw.items() if isinstance(v, str)
            }

        ws_ea_raw = ws_data.get("escalate_after", {})
        if isinstance(ws_ea_raw, dict):
            ws_ea: dict[str, float | None] = {}
            for role_key, val in ws_ea_raw.items():
                try:
                    ws_ea[role_key] = parse_duration(val)
                except ValueError:
                    errors.append(
                        f"[workspace.{ws_id}.escalate_after].{role_key}: invalid duration {val!r}"
                    )
            workspace_escalate_after[ws_id] = ws_ea

    # [escalate_after]
    escalate_after: dict[str, float | None] = {}
    for role_key, val in raw.get("escalate_after", {}).items():
        try:
            escalate_after[role_key] = parse_duration(val)
        except ValueError:
            errors.append(f"[escalate_after].{role_key}: invalid duration {val!r}")

    return Bindings(
        server_url=server_url,
        default_token=default_token,
        roles=roles,
        workspace_roles=workspace_roles,
        escalate_after=escalate_after,
        workspace_escalate_after=workspace_escalate_after,
        errors=tuple(errors),
    )


# ── Token resolution helpers ──────────────────────────────────────────────────


def _find_in_dict(
    role_id: str,
    aliases: tuple[str, ...],
    d: dict[str, str],
) -> tuple[str | None, str | None]:
    """Look up *role_id* (or any of its *aliases*) in *d*.

    Returns ``(value, matched_key)``. Tries the role_id first, then aliases in
    order. Returns ``(None, None)`` when not found.
    """
    if role_id in d:
        return d[role_id], role_id
    for alias in aliases:
        if alias in d:
            return d[alias], alias
    return None, None


def _find_ea_in_dict(
    role_id: str,
    aliases: tuple[str, ...],
    d: dict[str, float | None],
) -> tuple[float | None, bool]:
    """Look up *role_id* (or any of its *aliases*) in an escalation dict *d*.

    Returns ``(value, found)``. ``found=False`` means the key was absent
    entirely — distinct from ``(None, True)`` which means "explicitly never".
    Mirrors :func:`_find_in_dict` for token binding so alias-keyed entries
    in ``[escalate_after]`` work the same way as in ``[roles]``.
    """
    if role_id in d:
        return d[role_id], True
    for alias in aliases:
        if alias in d:
            return d[alias], True
    return None, False


def _resolve_token(
    catalog: RoleCatalog,
    bindings: Bindings,
    role_id: str,
) -> tuple[str | None, str | None]:
    """Chase the token for *role_id* through the full reference chain.

    Returns ``(token, failure_reason)``. ``token=None`` means no binding; when
    ``failure_reason`` is also ``None`` the role simply has no binding on this
    machine.
    """
    visited: set[str] = set()
    chain: list[str] = []  # display names for cycle messages
    current_rid = role_id

    for _ in range(9):  # initial look-up + up to 8 reference hops
        if current_rid in visited:
            # We've landed on a role we already visited → cycle
            chain.append(current_rid)  # the role that closes the cycle
            return None, "alias cycle: " + " → ".join(chain)
        visited.add(current_rid)

        role_obj = catalog.roles.get(current_rid)
        if role_obj is None:
            return None, f'unknown role "{current_rid}"'

        ws_id = catalog.workspace_id
        ws_roles = bindings.workspace_roles.get(ws_id, {})

        # Precedence: workspace_roles → roles → default_token (only for default role)
        val, matched_key = _find_in_dict(current_rid, role_obj.aliases, ws_roles)
        if val is None:
            val, matched_key = _find_in_dict(current_rid, role_obj.aliases, bindings.roles)
        if val is None and current_rid == catalog.default_role and bindings.default_token:
            val = bindings.default_token
            matched_key = current_rid

        if val is None:
            return None, None  # no binding on this machine

        if val.startswith("rly_"):
            return val, None  # found a real token

        # It's a role reference — record the key used for cycle display, then chase
        chain.append(matched_key or current_rid)

        ref_lower = val.lower()
        if ref_lower not in catalog.alias_index:
            return None, f'points at unknown role "{val}"'

        next_rid = catalog.alias_index[ref_lower]

        # Detect cycle early (before the next iteration) so we can build the
        # display chain correctly: the reference value that closes the cycle.
        if next_rid in visited:
            chain.append(val)  # the reference string that points back
            return None, "alias cycle: " + " → ".join(chain)

        current_rid = next_rid

    return None, "reference chain too long (>8 hops)"


# ── Escalation resolution ─────────────────────────────────────────────────────
#
# Shared by resolve_destination (what actually happens at runtime) and by
# roles_report (what the diagnostic table shows).  Both must agree: a table that
# advertises an escalation the resolver suppresses is worse than no column.


def _escalation_is_possible(
    catalog: RoleCatalog,
    bindings: Bindings,
    role_id: str,
    token: str | None,
) -> bool:
    """True when escalating *role_id* would reach a human other than its own.

    False for the default role — there is nobody above it — and false whenever
    the role's binding resolves to the *same* token as the default role, since
    the duplicate group would land in the chat that already holds the original.
    """
    if role_id == catalog.default_role:
        return False
    default_token, _ = _resolve_token(catalog, bindings, catalog.default_role)
    return token != default_token


def _resolve_escalation(
    catalog: RoleCatalog,
    bindings: Bindings,
    role_id: str,
) -> float | None:
    """Four-level escalation lookup for *role_id*, ignoring reachability.

    Precedence — the first level where the key is PRESENT wins, even when its
    value is ``None`` (an explicit "never" beats a duration set below it):
    ``[workspace.<ws>.escalate_after]`` → ``[escalate_after]`` → the role's own
    ``escalate_after`` in roles.toml (which already has the file's top-level
    default merged in at load time).

    Alias-aware, mirroring :func:`_find_in_dict`, so ``arch = "45m"`` resolves
    for a role whose id is ``architect``.

    Callers must gate this on :func:`_escalation_is_possible`; on its own this
    reports what is *configured*, not what will happen.
    """
    ws_id = catalog.workspace_id
    ws_ea = bindings.workspace_escalate_after.get(ws_id, {})
    role_aliases = catalog.roles[role_id].aliases
    ea, found = _find_ea_in_dict(role_id, role_aliases, ws_ea)
    if found:
        return ea
    ea, found = _find_ea_in_dict(role_id, role_aliases, bindings.escalate_after)
    if found:
        return ea
    return catalog.roles[role_id].escalate_after


# ── resolve_destination ───────────────────────────────────────────────────────


def resolve_destination(
    catalog: RoleCatalog,
    bindings: Bindings,
    alias: str | None,
) -> Destination:
    """Resolve an ``@alias`` to a :class:`Destination` via the full fallback chain.

    Steps (§5.2):
    1. Alias → role_id (unknown alias → default + note).
    2+3. Token lookup with reference chasing.
    4. Fallback to default role when no token found (one combined note).
    5. Unreachable default → token=None, caller degrades to terminal-only.
    6. Escalation populated only when: no fallback, role ≠ default, token ≠ default token.
    """
    notes: list[str] = []
    # requested_role_id / requested_title are set only when the resolved role
    # DIFFERS from what was asked for (i.e., a step-4 fallback happened).
    requested_role_id: str | None = None
    requested_title: str | None = None
    fallback_happened = False

    # ── Step 1: resolve alias → role_id ──────────────────────────────────────
    if alias is None:
        role_id = catalog.default_role
    else:
        alias_lower = alias.lower()
        if alias_lower in catalog.alias_index:
            role_id = catalog.alias_index[alias_lower]
        else:
            default_obj = catalog.roles[catalog.default_role]
            notes.append(f"Unknown role @{alias} — routed to {default_obj.title}.")
            role_id = catalog.default_role

    # ── Steps 2+3: token lookup with reference chasing ───────────────────────
    token, reason = _resolve_token(catalog, bindings, role_id)

    # ── Step 4: fallback to default role if unreachable ───────────────────────
    if token is None and role_id != catalog.default_role:
        fallback_happened = True
        failed_obj = catalog.roles[role_id]
        requested_role_id = role_id
        requested_title = failed_obj.title

        if reason is None:
            note = f"Intended for {failed_obj.title} — not reachable from this machine."
        else:
            note = (
                f"Intended for {failed_obj.title} — its binding on this machine is broken"
                f" ({reason})."
            )
        notes.append(note)

        role_id = catalog.default_role
        token, _ = _resolve_token(catalog, bindings, role_id)

    # ── Step 5: unreachable default → token=None (caller degrades) ───────────
    # (no action here; token remains None)

    # ── Step 6: escalation ───────────────────────────────────────────────────
    # Fires only when: no fallback, role ≠ default, token ≠ default token.
    escalate_after: float | None = None
    if not fallback_happened and _escalation_is_possible(catalog, bindings, role_id, token):
        escalate_after = _resolve_escalation(catalog, bindings, role_id)

    role_obj = catalog.roles[role_id]
    return Destination(
        role_id=role_id,
        title=role_obj.title,
        token=token,
        is_default=(role_id == catalog.default_role),
        requested_role_id=requested_role_id,
        requested_title=requested_title,
        escalate_after=escalate_after,
        notes=tuple(notes),
    )


# ── Diagnostic helpers ────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """Format *seconds* as a short human-readable string (``"5m"``, ``"2h"``, …)."""
    secs = int(seconds)
    if secs >= 3600 and secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs >= 60 and secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


def _destination_kind(
    catalog: RoleCatalog,
    bindings: Bindings,
    role_id: str,
) -> tuple[str, str | None]:
    """Return ``(kind, ref)`` describing *role_id*'s immediate binding.

    *kind* is one of:

    * ``'default_token'`` — uses the top-level ``installation_token`` directly.
    * ``'own_binding'``   — has an explicit ``rly_*`` entry in a ``[roles]`` table.
    * ``'reference'``     — binding is a role-alias reference; *ref* is the raw
      value as written in the config.
    * ``'none'``          — no binding on this machine.

    No token material is returned.
    """
    role_obj = catalog.roles[role_id]
    ws_id = catalog.workspace_id
    ws_roles = bindings.workspace_roles.get(ws_id, {})

    val, _ = _find_in_dict(role_id, role_obj.aliases, ws_roles)
    if val is None:
        val, _ = _find_in_dict(role_id, role_obj.aliases, bindings.roles)

    if val is not None:
        if val.startswith("rly_"):
            return "own_binding", None
        return "reference", val

    if role_id == catalog.default_role and bindings.default_token:
        return "default_token", None
    return "none", None


def roles_report(catalog: RoleCatalog, bindings: Bindings) -> dict:
    """Return a structured summary of roles, their destinations, and errors.

    The returned dict is JSON-serialisable.  It never contains token material:
    ``destination`` is one of ``'default token'``, ``'own binding'``,
    ``'-> <ref>'``, or ``'(none)'``.

    This is the data source for both :func:`format_roles_table` and the
    ``claude-roles --json`` output.
    """
    roles_out = []
    for role_id, role_obj in catalog.roles.items():
        kind, ref = _destination_kind(catalog, bindings, role_id)

        if kind == "reference":
            destination = f"-> {ref}"
        elif kind == "own_binding":
            destination = "own binding"
        elif kind == "default_token":
            destination = "default token"
        else:
            destination = "(none)"

        # Escalation: report the EFFECTIVE value — what resolve_destination
        # will actually do — not merely what roles.toml configures. A role can
        # carry `escalate_after = "30m"` and still never escalate: the default
        # role has nobody above it, an unbound role has already fallen back, and
        # a role bound to the default's own token would only duplicate the
        # question into the chat that already has it.
        dest = resolve_destination(catalog, bindings, role_id)
        if dest.role_id != role_id or not _escalation_is_possible(
            catalog, bindings, role_id, dest.token
        ):
            escalate_str = "—"  # em dash: no escalation is possible here
        elif dest.escalate_after is None:
            escalate_str = "never"  # reachable, but escalation is switched off
        else:
            escalate_str = _format_duration(dest.escalate_after)

        # Fallback note: the title of the role that will take over
        fallback_title: str | None = None
        if kind == "none" and role_id != catalog.default_role:
            fallback_title = catalog.roles[catalog.default_role].title

        roles_out.append({
            "role_id": role_id,
            "title": role_obj.title,
            "aliases": list(role_obj.aliases),
            "destination": destination,
            "escalate_str": escalate_str,
            "fallback_title": fallback_title,
        })

    all_errors = list(catalog.errors) + list(bindings.errors)
    return {
        "workspace_id": catalog.workspace_id,
        "path": str(catalog.path) if catalog.path else None,
        "default_role": catalog.default_role,
        "roles": roles_out,
        "errors": all_errors,
        "has_errors": bool(all_errors),
    }


def format_roles_table(report: dict) -> str:
    """Render a :func:`roles_report` dict as a human-readable table string.

    No token material is present in the input dict, so none can appear in the
    output.  The caller may print the result directly.
    """
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    path_part = f"   ({report['path']})" if report.get("path") else ""
    lines.append(f"workspace: {report['workspace_id']}{path_part}")
    lines.append(f"default:   {report['default_role']}")
    lines.append("")

    roles = report.get("roles", [])
    if not roles:
        return "\n".join(lines)

    # ── Column widths ─────────────────────────────────────────────────────────
    col_headers = ["ALIASES", "ROLE", "TITLE", "DESTINATION", "ESCALATE"]
    rows: list[tuple[str, str, str, str, str]] = []
    for r in roles:
        rows.append((
            ", ".join(r["aliases"]),
            r["role_id"],
            r["title"],
            r["destination"],
            r["escalate_str"],
        ))

    widths = [len(h) for h in col_headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # ── Header row ────────────────────────────────────────────────────────────
    hdr = "  ".join(h.ljust(widths[i]) for i, h in enumerate(col_headers[:-1]))
    lines.append((hdr + "  " + col_headers[-1]).rstrip())

    # ── Data rows ─────────────────────────────────────────────────────────────
    dest_offset = sum(widths[i] + 2 for i in range(3))  # ALIASES + ROLE + TITLE
    for role_data, row in zip(roles, rows):
        data = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:-1]))
        lines.append((data + "  " + row[-1]).rstrip())

        # ↳ fallback note, indented under the DESTINATION column
        if role_data.get("fallback_title"):
            lines.append(" " * dest_offset + f"↳ falls back to {role_data['fallback_title']}")

    # ── Errors section ───────────────────────────────────────────────────────
    errors = report.get("errors", [])
    if errors:
        lines.append("")
        lines.append("errors:")
        for err in errors:
            lines.append(f"  {err}")

    return "\n".join(lines)
