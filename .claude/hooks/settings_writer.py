#!/usr/bin/env python3
"""Atomic permission-list writer for Claude Code settings files (task 22-04).

Generalizes the writer that used to live only in
``telegram_permission_router.update_settings_local_json``: same
read-tolerate-corrupt → dedupe → tmp+rename discipline, now parameterized by

* **which file** — ``settings.json`` (git-versioned, what agents propose into)
  or ``settings.local.json`` (unversioned, what the Telegram Whitelist button
  keeps writing — changing that is explicitly out of scope, brd §3.2);
* **which list** — ``allow`` (default), ``ask`` or ``deny``.

The router delegates to this module, so there is exactly one implementation of
"add a permission pattern to a settings file" in the codebase.

Two deliberate differences from the code this replaces, both in the
malformed-input corner and neither reachable from a well-formed settings file:

* a ``permissions`` key that is not an object, or a permission list that is not
  an array, returns ``False`` instead of raising ``TypeError`` at the caller —
  and returns it **without writing**, so a hand-broken file is reported, not
  overwritten;
* the corrupt-JSON backup and the log lines name the file actually being
  written rather than always saying ``settings.local.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# The two settings files this writer targets. Anything else is a caller bug.
VERSIONED_SETTINGS = "settings.json"
LOCAL_SETTINGS = "settings.local.json"

# Claude Code evaluates deny → ask → allow; all three are writable lists.
PERMISSION_LISTS = ("allow", "ask", "deny")


@dataclass(frozen=True)
class SettingsWriteResult:
    """Outcome of one pattern write.

    ``ok`` is the router-compatible boolean: ``True`` also when the pattern was
    already present (a dedupe is a success — the settings say what the caller
    wanted them to say). ``added`` tells the two apart for callers that report
    to an agent.
    """

    ok: bool
    added: bool
    path: str
    list_name: str
    pattern: str
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "added": self.added,
            "path": self.path,
            "list": self.list_name,
            "pattern": self.pattern,
            "error": self.error,
        }


def settings_path(workspace_dir: str, settings_filename: str = VERSIONED_SETTINGS) -> Path:
    """``<workspace>/.claude/<settings_filename>``."""
    return Path(workspace_dir) / ".claude" / settings_filename


def add_permission_pattern(
    workspace_dir: str,
    permission_pattern: str,
    *,
    settings_filename: str = VERSIONED_SETTINGS,
    list_name: str = "allow",
    log: Optional[Callable[[str], None]] = None,
) -> SettingsWriteResult:
    """Add ``permission_pattern`` to one permission list, atomically.

    Reads the existing file (tolerating a corrupt one by moving it aside to
    ``<name>.json.backup``), dedupes, then writes through a ``.json.tmp``
    sibling and renames it into place, so a reader never sees a half-written
    settings file.
    """

    def _log(message: str) -> None:
        if log is not None:
            log(message)

    if list_name not in PERMISSION_LISTS:
        return SettingsWriteResult(
            ok=False,
            added=False,
            path="",
            list_name=list_name,
            pattern=permission_pattern,
            error=f"unknown permission list {list_name!r}; expected one of {', '.join(PERMISSION_LISTS)}",
        )

    path = settings_path(workspace_dir, settings_filename)

    settings: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "r") as handle:
                settings = json.load(handle)
        except json.JSONDecodeError as exc:
            _log(f"Error parsing {settings_filename}: {exc}")
            backup_path = path.with_suffix(".json.backup")
            path.rename(backup_path)
            settings = {}
        except Exception as exc:  # noqa: BLE001 — unreadable file: start fresh, do not crash the caller.
            _log(f"Error reading {settings_filename}: {exc}")
            settings = {}

    if not isinstance(settings, dict):
        message = f"{path} does not contain a JSON object; refusing to overwrite it"
        _log(message)
        return SettingsWriteResult(
            ok=False,
            added=False,
            path=str(path),
            list_name=list_name,
            pattern=permission_pattern,
            error=message,
        )

    if "permissions" not in settings:
        settings["permissions"] = {}
    if not isinstance(settings["permissions"], dict):
        message = f"{path} has a 'permissions' key that is not an object; refusing to overwrite it"
        _log(message)
        return SettingsWriteResult(
            ok=False,
            added=False,
            path=str(path),
            list_name=list_name,
            pattern=permission_pattern,
            error=message,
        )

    if list_name not in settings["permissions"]:
        settings["permissions"][list_name] = []
    target_list = settings["permissions"][list_name]
    if not isinstance(target_list, list):
        message = (
            f"{path} has a 'permissions.{list_name}' key that is not an array; "
            "refusing to overwrite it"
        )
        _log(message)
        return SettingsWriteResult(
            ok=False,
            added=False,
            path=str(path),
            list_name=list_name,
            pattern=permission_pattern,
            error=message,
        )

    if permission_pattern in target_list:
        _log(f"Permission pattern already exists: {permission_pattern}")
        return SettingsWriteResult(
            ok=True,
            added=False,
            path=str(path),
            list_name=list_name,
            pattern=permission_pattern,
        )

    target_list.append(permission_pattern)
    _log(f"Added permission pattern: {permission_pattern}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    try:
        with open(temp_path, "w") as handle:
            json.dump(settings, handle, indent=2)
        temp_path.rename(path)
    except Exception as exc:  # noqa: BLE001 — a failed write is reported, never partial.
        _log(f"Error writing {settings_filename}: {exc}")
        if temp_path.exists():
            temp_path.unlink()
        return SettingsWriteResult(
            ok=False,
            added=False,
            path=str(path),
            list_name=list_name,
            pattern=permission_pattern,
            error=str(exc),
        )

    return SettingsWriteResult(
        ok=True, added=True, path=str(path), list_name=list_name, pattern=permission_pattern
    )
