#!/usr/bin/env python3
"""Index module for the async-questions listener (epic 23).

Defines the on-disk shape of ``~/.claude/async_questions.json`` and provides
the sole reader/writer helpers that 23-04 (the MCP server) and 23-05 (the
listener loop) both import.  State.md invariant 5 applies to this index as
much as to the queue files: **one module parses it; nobody else hand-parses the
JSON**.

On-disk shape (architecture.md §5):

.. code-block:: json

    {
      "watermark": 4192,
      "messages": {
        "4187": {
          "workspace_id": "hyppie-flow",
          "anchor": "repo",
          "root": "/data/sync/work/natasha/hyppie-flow",
          "rel_path": "docs/questions/for-product-lead.md",
          "qid": "Q-042",
          "role": "hpl",
          "created_at": "2026-08-26T14:02:11Z"
        }
      },
      "pending": [
        {
          "message_id": 4187,
          "answer": "...",
          "attempts": 3,
          "first_failed_at": "...",
          "last_error": "id not found"
        }
      ]
    }

An entry with **no ``qid``** is an ack-notification: the listener finalises the
Telegram message but writes nothing to any file (brd D7, invariant 10).

Lock protocol: ``flock`` + tmp + ``os.replace`` — the same discipline
``permission_state_store.py`` uses for its JSONL files and
``questions_store.py`` uses for queue files.

Public API
----------
INDEX_PATH         — default path for the shared index
IndexEntry         — one ``messages`` entry (``qid`` is None for ack-notifs)
PendingApply       — one item in ``pending``
read_index         — read + parse (returns empty default if absent)
write_index        — atomic replace under ``flock``
add_message_entry  — convenience: read → update ``messages`` → write
count_pending      — read the ``pending`` list length (cheap, no lock needed)
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Paths ─────────────────────────────────────────────────────────────────────

INDEX_PATH = Path.home() / ".claude" / "async_questions.json"

#: Sibling flock file so concurrent reads do not corrupt a write in progress.
_LOCK_PATH = Path.home() / ".claude" / "async_questions.json.lock"


# ── Data shapes ───────────────────────────────────────────────────────────────


@dataclass
class IndexEntry:
    """One entry in the ``messages`` dict (keyed by relay message_id string).

    ``qid`` is ``None`` for ack-notifications: the listener sees a ``None``
    and finalises the Telegram message without touching any queue file.
    """

    workspace_id: str
    anchor: str        # "repo" | "worktree" | "path"
    root: str          # absolute path to the resolved anchor root
    rel_path: str      # queue-file path relative to ``root``
    qid: str | None    # None → ack-notification only, no file write
    role: str | None   # role_id that was targeted
    created_at: str    # UTC ISO-8601

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "anchor": self.anchor,
            "root": self.root,
            "rel_path": self.rel_path,
            "created_at": self.created_at,
        }
        if self.qid is not None:
            d["qid"] = self.qid
        if self.role is not None:
            d["role"] = self.role
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IndexEntry":
        return cls(
            workspace_id=str(d.get("workspace_id", "")),
            anchor=str(d.get("anchor", "repo")),
            root=str(d.get("root", "")),
            rel_path=str(d.get("rel_path", "")),
            qid=d.get("qid") or None,
            role=d.get("role") or None,
            created_at=str(d.get("created_at", "")),
        )


@dataclass
class PendingApply:
    """A failed answer-apply that the listener retries on each wake.

    Architecture §5.1: a failed apply is kept in ``pending`` and retried with
    backoff.  After a configurable number of attempts the answer is written to
    ``<dir>/answered/<qid>.md`` as a sidecar and dropped from ``pending``.
    """

    message_id: int
    answer: str
    attempts: int = 0
    first_failed_at: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "answer": self.answer,
            "attempts": self.attempts,
            "first_failed_at": self.first_failed_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingApply":
        return cls(
            message_id=int(d.get("message_id", 0)),
            answer=str(d.get("answer", "")),
            attempts=int(d.get("attempts", 0)),
            first_failed_at=str(d.get("first_failed_at", "")),
            last_error=str(d.get("last_error", "")),
        )


@dataclass
class Index:
    """The full parsed index.

    ``watermark`` is the relay message_id up to which all answers have been
    applied (or moved to ``pending``).  23-05 advances it after every
    successful terminal outcome for an answer.  23-04 never touches it.
    """

    watermark: int = 0
    messages: dict[str, IndexEntry] = field(default_factory=dict)
    pending: list[PendingApply] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "watermark": self.watermark,
            "messages": {k: v.to_dict() for k, v in self.messages.items()},
            "pending": [p.to_dict() for p in self.pending],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Index":
        messages: dict[str, IndexEntry] = {}
        for k, v in d.get("messages", {}).items():
            if isinstance(v, dict):
                messages[str(k)] = IndexEntry.from_dict(v)
        pending: list[PendingApply] = []
        for p in d.get("pending", []):
            if isinstance(p, dict):
                pending.append(PendingApply.from_dict(p))
        return cls(
            watermark=int(d.get("watermark", 0)),
            messages=messages,
            pending=pending,
        )


# ── Lock + atomic I/O ─────────────────────────────────────────────────────────


def _flock_ex(path: Path):
    """Return a context manager that holds an exclusive flock on *path*.

    Usage::

        with _flock_ex(lock_path) as fh:
            ...  # exclusive section

    The yielded file handle is kept open and locked for the duration.
    """
    import contextlib

    @contextlib.contextmanager
    def _cm():
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield fh
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    return _cm()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* atomically (tmp + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Sync the directory entry.
    try:
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# ── Public readers / writers ──────────────────────────────────────────────────


def read_index(path: Path | None = None) -> Index:
    """Read and parse the index from *path* (default ``INDEX_PATH``).

    Returns an empty ``Index`` when the file does not exist or is unreadable.
    Never raises on missing/corrupt files — callers depend on a usable default.
    """
    target = path if path is not None else INDEX_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return Index()
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            return Index()
        return Index.from_dict(d)
    except (json.JSONDecodeError, ValueError):
        return Index()


def write_index(index: Index, path: Path | None = None) -> None:
    """Write *index* atomically.  Caller must hold the flock."""
    target = path if path is not None else INDEX_PATH
    _atomic_write_json(target, index.to_dict())


def add_message_entry(
    message_id: int,
    entry: IndexEntry,
    *,
    path: Path | None = None,
    lock_path: Path | None = None,
) -> None:
    """Read → insert/update ``messages[message_id]`` → write, under flock.

    Safe for concurrent callers: the flock serialises the read-modify-write.
    """
    target = path if path is not None else INDEX_PATH
    lpath = lock_path if lock_path is not None else _LOCK_PATH

    with _flock_ex(lpath):
        idx = read_index(target)
        idx.messages[str(message_id)] = entry
        write_index(idx, target)


def count_pending(path: Path | None = None) -> int:
    """Return the length of the ``pending`` list (no lock needed for a count).

    Reads the file once without holding the write lock — fine for a snapshot.
    """
    return len(read_index(path).pending)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
