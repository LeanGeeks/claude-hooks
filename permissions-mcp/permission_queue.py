#!/usr/bin/env python3
"""The permission-proposal queue (epic 22, task 22-04 §2).

``~/.claude/permission-queue/<project-key>/<utc-ts>_<8hex>.json`` — one JSON
file per entry, written atomically (tmp + rename, the settings-writer
discipline). This is how an agent reaches a workspace it is **not** running in:
brd D6 allows exactly two moves, writing your own checkout (A) or filing a
proposal for the agent that lives in the target checkout (C). Editing,
committing or pushing in a foreign checkout (B) is the rejected option, so
nothing here ever touches the target's files.

Two entry types share the mechanism: ``allowlist_proposal`` (promote a pattern
into that workspace's versioned settings) and ``parser_issue`` (the validator
got a command wrong). The daily reviewer (22-05) drains them.

**Project keys are not computed here.** Callers resolve them with
``project_key.resolve_project_key`` — the epic's single project-identity
function (state.md invariant 7). Passing anything else (a raw path, a
hand-built string) puts entries in a directory nobody drains.

Retention contract: the writer side never deletes. A drained entry is *moved*
to ``processed/`` inside the same directory by ``mark_processed``; the daily
reviewer owns retention there. No locks are needed — one file per entry, rename
is atomic, and the consumer moves an entry before acting on it, so two drains
cannot process the same proposal.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Entry types (the ``type`` field).
TYPE_ALLOWLIST_PROPOSAL = "allowlist_proposal"
TYPE_PARSER_ISSUE = "parser_issue"

# Where the queue lives. The env override exists so tests — and any sandboxed
# run — never write into the developer's real ~/.claude. Read at call time, not
# import time, so a test can patch os.environ around a single call.
QUEUE_DIR_ENV = "CLAUDE_PERMISSION_QUEUE_DIR"
DEFAULT_QUEUE_DIRNAME = "permission-queue"

# Drained entries land here, inside the project's own queue directory.
PROCESSED_DIRNAME = "processed"

# Timestamp prefix: sortable, filesystem-safe, second resolution. Uniqueness
# comes from the random suffix, not the clock.
_TS_FORMAT = "%Y%m%dT%H%M%SZ"


def queue_root() -> Path:
    """The queue root, honoring ``CLAUDE_PERMISSION_QUEUE_DIR``."""
    override = os.environ.get(QUEUE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / DEFAULT_QUEUE_DIRNAME


def queue_dir(project_key: str) -> Path:
    """The queue directory for one project key (from ``resolve_project_key``)."""
    return queue_root() / project_key


def processed_dir(project_key: str) -> Path:
    """Where ``mark_processed`` moves drained entries for this project."""
    return queue_dir(project_key) / PROCESSED_DIRNAME


def _entry_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime(_TS_FORMAT)
    return f"{stamp}_{secrets.token_hex(4)}.json"


def _source(
    session_id: Optional[str], actor_agent: Optional[str], cwd: Optional[str]
) -> Dict[str, Any]:
    return {"session_id": session_id, "actor_agent": actor_agent, "cwd": cwd}


def build_allowlist_proposal(
    *,
    pattern: str,
    scope: str,
    rationale: str,
    session_id: Optional[str],
    actor_agent: Optional[str],
    cwd: Optional[str],
    evidence_request_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """An ``allowlist_proposal`` entry body (task 22-04 §2 schema)."""
    return {
        "type": TYPE_ALLOWLIST_PROPOSAL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": _source(session_id, actor_agent, cwd),
        "pattern": pattern,
        "scope": scope,
        "rationale": rationale,
        "evidence_request_ids": list(evidence_request_ids or []),
    }


def build_parser_issue(
    *,
    command: str,
    observed: str,
    expected: str,
    notes: str,
    session_id: Optional[str],
    actor_agent: Optional[str],
    cwd: Optional[str],
) -> Dict[str, Any]:
    """A ``parser_issue`` entry body (task 22-04 §2 schema)."""
    return {
        "type": TYPE_PARSER_ISSUE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": _source(session_id, actor_agent, cwd),
        "command": command,
        "observed": observed,
        "expected": expected,
        "notes": notes,
    }


def enqueue(project_key: str, entry: Dict[str, Any]) -> Path:
    """Write one entry into ``project_key``'s queue, atomically.

    The temp file is dot-prefixed as well as ``.tmp``-suffixed so a concurrent
    drain globbing ``*.json`` can never see a half-written entry, even in the
    window before the rename.
    """
    target_dir = queue_dir(project_key)
    target_dir.mkdir(parents=True, exist_ok=True)

    name = _entry_filename()
    final_path = target_dir / name
    temp_path = target_dir / f".{name}.tmp"
    try:
        with open(temp_path, "w") as handle:
            json.dump(entry, handle, indent=2, ensure_ascii=False, default=str)
        temp_path.rename(final_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return final_path


def list_entries(project_key: str) -> List[Path]:
    """Undrained entries for a project, oldest first (the filename sorts)."""
    target_dir = queue_dir(project_key)
    if not target_dir.is_dir():
        return []
    return sorted(path for path in target_dir.glob("*.json") if path.is_file())


def read_entry(path: Path) -> Dict[str, Any]:
    """Parse one entry file."""
    with open(path, "r") as handle:
        return json.load(handle)


def mark_processed(path: Path) -> Path:
    """Move a drained entry into ``processed/`` — never delete it.

    The consumer calls this *before* acting on the entry, so a crash mid-apply
    leaves a processed marker rather than a proposal two drains both apply.
    Returns the new path.
    """
    entry_path = Path(path)
    destination_dir = entry_path.parent / PROCESSED_DIRNAME
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / entry_path.name
    entry_path.rename(destination)
    return destination
