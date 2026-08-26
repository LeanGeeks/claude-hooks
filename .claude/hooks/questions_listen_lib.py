#!/usr/bin/env python3
"""Index + runtime for the async-questions listener (epic 23).

Two halves, one module by design:

* **The index** (23-04) — the on-disk shape of ``~/.claude/async_questions.json``
  and the sole reader/writer helpers that the ``questions`` MCP server and the
  listener both import.  State.md invariant 5 applies to this index as much as
  to the queue files: **one module parses it; nobody else hand-parses the
  JSON**.
* **The listener runtime** (23-05) — config, the long-poll loop, the apply
  pipeline, the pending-retry pass with its sidecar fallback, and the status
  file.  ``.claude/bin/questions-listen`` is process lifecycle only.

23-04 created this module and owns ``messages``; 23-05 extends it and owns
``watermark``/``watermarks`` and ``pending``.  The reader/writer semantics
23-04 depends on (:func:`read_index`, :func:`write_index`,
:func:`add_message_entry`, :func:`count_pending`) are unchanged.

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
          "last_attempt_at": "...",
          "last_error": "id not found",
          "answered_at": "...",
          "token_fp": "9f2c1ab0c4d5e6f7"
        }
      ],
      "watermarks": { "9f2c1ab0c4d5e6f7": 4192 }
    }

``watermarks`` (23-05) is the per-feed watermark keyed by installation-token
fingerprint; ``watermark`` mirrors the primary feed's value.  See :class:`Index`.

An entry with **no ``qid``** is an ack-notification: the listener finalises the
Telegram message but writes nothing to any file (brd D7, invariant 10).

Lock protocol: ``flock`` + tmp + ``os.replace`` — the same discipline
``permission_state_store.py`` uses for its JSONL files and
``questions_store.py`` uses for queue files.

Public API — index (23-04)
-------------------------
INDEX_PATH         — default path for the shared index
IndexEntry         — one ``messages`` entry (``qid`` is None for ack-notifs)
PendingApply       — one item in ``pending``
read_index         — read + parse (returns empty default if absent)
write_index        — atomic replace under ``flock``
add_message_entry  — convenience: read → update ``messages`` → write
count_pending      — read the ``pending`` list length (cheap, no lock needed)

Public API — runtime (23-05)
----------------------------
mutate_index       — read → mutate → write, the whole of it under ``flock``
feed_watermark     — this feed's watermark (``watermarks`` is the truth)
ListenConfig       — the resolved ``[questions_listen]`` section + its feeds
load_listen_config — parse ``~/.config/claude-tg-relay/config.toml``
Listener           — the loop: poll → apply → PATCH → advance the watermark
resolve_target     — re-resolve a workspace **now** from the index entry (inv. 7)
acquire_single_instance — ``flock`` on the listener lock; None when one runs
read_status / format_status — what ``--status`` prints without the lock
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import html
import json
import logging
import os
import random
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Sibling hook modules must import regardless of the caller's cwd or sys.path:
# ``questions-listen`` runs from ``~/.local/bin`` and the MCP server from the
# checkout, and both land this file beside ``questions_store.py``.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import questions_store as qs  # noqa: E402
import roles_config  # noqa: E402

logger = logging.getLogger("questions_listen")


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

    ``answered_at``, ``last_attempt_at`` and ``token_fp`` were added by 23-05
    and are all optional: an entry written without them still parses, and the
    retry pass treats a missing ``last_attempt_at`` as "due now".  ``token_fp``
    is the **fingerprint** of the installation token whose feed carried the
    answer — never the token itself — so a retry knows which relay client to
    finalise the Telegram message with.
    """

    message_id: int
    answer: str
    attempts: int = 0
    first_failed_at: str = ""
    last_error: str = ""
    answered_at: str = ""
    last_attempt_at: str = ""
    token_fp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "answer": self.answer,
            "attempts": self.attempts,
            "first_failed_at": self.first_failed_at,
            "last_error": self.last_error,
            "answered_at": self.answered_at,
            "last_attempt_at": self.last_attempt_at,
            "token_fp": self.token_fp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingApply":
        return cls(
            message_id=int(d.get("message_id", 0)),
            answer=str(d.get("answer", "")),
            attempts=int(d.get("attempts", 0)),
            first_failed_at=str(d.get("first_failed_at", "")),
            last_error=str(d.get("last_error", "")),
            answered_at=str(d.get("answered_at", "")),
            last_attempt_at=str(d.get("last_attempt_at", "")),
            token_fp=str(d.get("token_fp", "")),
        )


@dataclass
class Index:
    """The full parsed index.

    ``watermark`` is the relay message_id up to which all answers have been
    applied (or moved to ``pending``).  23-05 advances it after every
    terminal outcome for an answer — never on a bare read (invariant 2).
    23-04 never touches it.

    **Why two watermark fields.**  The answer feed is *installation*-scoped and
    a machine can hold more than one installation token (``[roles]`` in
    ``config.toml`` binds a role to its own token), so there is one feed — and
    one watermark — per distinct token.  ``watermarks`` maps a token
    *fingerprint* (never the token) to that feed's watermark and is the single
    source of truth; ``watermark`` mirrors the **primary** feed's value so the
    architecture §5 shape and ``--status`` stay truthful in the ordinary
    single-token case.  Read a feed's position with :func:`feed_watermark` and
    write it with :func:`set_feed_watermark`; nothing else should touch either
    field.
    """

    watermark: int = 0
    messages: dict[str, IndexEntry] = field(default_factory=dict)
    pending: list[PendingApply] = field(default_factory=list)
    watermarks: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "watermark": self.watermark,
            "messages": {k: v.to_dict() for k, v in self.messages.items()},
            "pending": [p.to_dict() for p in self.pending],
            "watermarks": dict(self.watermarks),
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
        watermarks: dict[str, int] = {}
        raw_marks = d.get("watermarks", {})
        if isinstance(raw_marks, dict):
            for k, v in raw_marks.items():
                try:
                    watermarks[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return cls(
            watermark=int(d.get("watermark", 0)),
            messages=messages,
            pending=pending,
            watermarks=watermarks,
        )


# ── Lock + atomic I/O ─────────────────────────────────────────────────────────


def _flock_ex(path: Path):
    """Return a context manager that holds an exclusive flock on *path*.

    Usage::

        with _flock_ex(lock_path) as fh:
            ...  # exclusive section

    The yielded file handle is kept open and locked for the duration.
    """

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


# ═══════════════════════════════════════════════════════════════════════════════
# 23-05 — the listener runtime
#
# Everything below is the resident half of the epic: with no session running and
# no AI in the loop, turn an answer given in Telegram into a resolved entry in a
# workspace file.  ``.claude/bin/questions-listen`` is process lifecycle only and
# calls into here.
#
# The properties that make it correct, all load-bearing:
#
#   * **Invariant 2** — the watermark advances only after a *terminal* outcome
#     (applied, or moved to ``pending``), never on a bare read.  The advance and
#     the terminal record are one atomic index write (:meth:`Listener._terminal`),
#     so a crash cannot leave one without the other.
#   * **Invariant 3** — applies are idempotent on relay message id.  A crash
#     between apply and PATCH replays; ``questions_store`` sees its own
#     ``<!-- answer:N -->`` marker and reports ``applied`` without inserting
#     a second block.
#   * **Invariant 7** — the workspace path is re-resolved **now**, at apply time,
#     from the index entry's anchor mode (:func:`resolve_target`).  The path
#     captured at ask time may be a worktree that has since been deleted.
#   * **Invariant 8** — this component spawns nothing.  No ``amux send``, no git
#     subprocess, no branch checkout, no moving or deleting of queue entries.
#     When an answer cannot be applied the only outcomes are retry and sidecar.
#   * **The 23-03 obligation** — ``QuestionsStoreError`` is caught around every
#     store call and routed to ``pending`` exactly like ``not_found``.  A queue
#     file is unreadable for ordinary reasons (a ``git checkout`` landing
#     mid-flight, a partially-synced tree, a permissions change) and one of them
#     must never take down the resident loop.
# ═══════════════════════════════════════════════════════════════════════════════


# ── Runtime paths and tunables ────────────────────────────────────────────────

#: Single-instance lock: a second copy exits rather than double-applying.
LISTEN_LOCK_PATH = Path.home() / ".claude" / "questions-listen.lock"

#: Refreshed by the loop so ``--status`` — a separate process that cannot hold
#: the lock — has something truthful to read.
STATUS_PATH = Path.home() / ".claude" / "questions-listen.status.json"

#: The relay client config; the same file epic 16's listener opts in through.
RELAY_CONFIG_PATH = Path.home() / ".config" / "claude-tg-relay" / "config.toml"

#: Long-poll budget per cycle (architecture §7 — matches the existing chunk).
LONG_POLL_SECONDS = 25

#: Pending-apply attempts before the sidecar fallback (architecture §7).
DEFAULT_MAX_ATTEMPTS = 10

#: Pending retry backoff: 60 s doubling to hourly.
PENDING_BACKOFF_BASE_S = 60.0
PENDING_BACKOFF_MAX_S = 3600.0

#: Network backoff: laptops sleep, so a dropped connection is the normal case.
NET_BACKOFF_BASE_S = 2.0
NET_BACKOFF_MAX_S = 300.0
NET_BACKOFF_JITTER = 0.3

#: ``401`` is fatal-with-slow-retry: the token was revoked, keep asking slowly
#: and say so in ``--status`` rather than spinning or exiting.
AUTH_RETRY_S = 300.0

#: Telegram's text limit is 4096; leave room for the provenance line.
_FINALIZE_TEXT_BUDGET = 3500


# ── Relay client bootstrap (same shape as questions_mcp_lib) ──────────────────


def _ensure_relay_server_on_path() -> None:
    """Make ``relay_server`` importable from a checkout without installing it."""
    try:
        import importlib.util

        if importlib.util.find_spec("relay_server") is not None:
            return
    except (ImportError, ValueError):
        pass

    candidates: list[Path] = []
    env_path = os.environ.get("CLAUDE_RELAY_SERVER_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    here = Path(__file__).resolve()
    candidates.extend(parent / "relay-server" for parent in here.parents)

    for cand in candidates:
        if (cand / "relay_server" / "__init__.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return


_ensure_relay_server_on_path()

try:  # pragma: no cover — exercised by the import itself
    from relay_server.client import (  # type: ignore[import-not-found]
        RelayClient,
        RelayError,
    )
except Exception:  # noqa: BLE001
    RelayClient = None  # type: ignore[assignment]

    class RelayError(Exception):  # type: ignore[no-redef]
        """Stand-in so the module imports without the relay package present."""

        def __init__(self, message: str, *, status_code: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status_code


# ── Fingerprints ──────────────────────────────────────────────────────────────


def token_fingerprint(token: str) -> str:
    """A stable, non-reversible handle for an installation token.

    Written to the index and to the status file so a multi-feed listener can
    keep one watermark per installation **without ever storing token material**
    (brd §8, and the ``--status`` requirement in the task).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# ── Watermark accessors ───────────────────────────────────────────────────────


def feed_watermark(index: Index, fingerprint: str) -> int:
    """This feed's watermark.  ``watermarks`` is the truth; 0 means full replay.

    A fingerprint with no recorded position starts at 0 — ``after=0`` is a full
    replay of everything this installation ever had answered, which is exactly
    the recovery path for a lost index (architecture §2.4).  Replaying is safe
    because applies are idempotent (invariant 3) and an answer with no index
    entry is skipped.
    """
    return int(index.watermarks.get(fingerprint, 0))


def set_feed_watermark(
    index: Index,
    fingerprint: str,
    value: int,
    *,
    primary_fingerprint: str = "",
) -> None:
    """Advance this feed's watermark (monotonic) and mirror the primary feed.

    Monotonic on purpose: a feed page is id-ordered, but a retry or an
    out-of-order row must never move the watermark *backwards* past answers
    already accounted for.
    """
    current = int(index.watermarks.get(fingerprint, 0))
    if value > current:
        index.watermarks[fingerprint] = int(value)
    if primary_fingerprint and fingerprint == primary_fingerprint:
        index.watermark = int(index.watermarks.get(fingerprint, index.watermark))


def mutate_index(
    mutator: Callable[[Index], None],
    *,
    path: Path | None = None,
    lock_path: Path | None = None,
) -> Index:
    """Read → *mutator* → write, the whole of it under the index ``flock``.

    The listener's terminal outcomes (record the pending entry / drop it, and
    advance the watermark) go through **one** call of this, so the two can never
    diverge across a crash.

    Not re-entrant: ``flock`` is taken on a fresh descriptor each time, so a
    nested call from inside *mutator* would deadlock against itself.  Mutators
    are pure index edits for that reason.
    """
    target = path if path is not None else INDEX_PATH
    lpath = lock_path if lock_path is not None else _LOCK_PATH
    with _flock_ex(lpath):
        idx = read_index(target)
        mutator(idx)
        write_index(idx, target)
    return idx


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp + ``os.replace`` for a text file (the sidecar), fsynced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ListenConfig:
    """The resolved ``[questions_listen]`` section plus the feeds to poll.

    ``tokens`` is every distinct installation token this machine holds, primary
    (the top-level ``installation_token``) first.  The answer feed is
    installation-scoped, so a role bound to its own token has its **own** feed;
    polling only the default token would silently strand every answer given to
    that role — the one outcome this component may not produce.
    """

    enabled: bool = False
    server_url: str | None = None
    tokens: tuple[str, ...] = ()
    poll_seconds: int = LONG_POLL_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    config_path: Path | None = None
    errors: tuple[str, ...] = ()

    @property
    def primary_fingerprint(self) -> str:
        return token_fingerprint(self.tokens[0]) if self.tokens else ""

    def redacted(self) -> dict[str, Any]:
        """The resolved config as ``--status`` may print it — no token material."""
        return {
            "enabled": self.enabled,
            "server_url": self.server_url or "",
            "config_path": str(self.config_path) if self.config_path else "",
            "poll_seconds": self.poll_seconds,
            "max_attempts": self.max_attempts,
            "feeds": [token_fingerprint(t) for t in self.tokens],
            "errors": list(self.errors),
        }


def load_listen_config(config_path: Path | None = None) -> ListenConfig:
    """Parse ``~/.config/claude-tg-relay/config.toml`` into a :class:`ListenConfig`.

    A missing file, a missing ``[questions_listen]`` section or
    ``enabled = false`` all yield ``enabled=False`` — the unit can be installed
    without being active (task §2), and an unparseable file degrades to disabled
    with the reason in ``errors`` rather than raising into the loop.
    """
    path = config_path if config_path is not None else RELAY_CONFIG_PATH
    errors: list[str] = []

    if not path.is_file():
        return ListenConfig(config_path=path, errors=(f"{path} does not exist",))

    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 — a bad config disables, never crashes
        return ListenConfig(config_path=path, errors=(f"Failed to parse {path}: {exc}",))

    section = raw.get("questions_listen")
    if not isinstance(section, dict):
        return ListenConfig(
            config_path=path,
            errors=("no [questions_listen] section — listener disabled",),
        )

    enabled = bool(section.get("enabled", False))

    poll_seconds = LONG_POLL_SECONDS
    raw_poll = section.get("poll_seconds", LONG_POLL_SECONDS)
    if isinstance(raw_poll, bool) or not isinstance(raw_poll, (int, float)):
        errors.append("[questions_listen].poll_seconds: expected a number, using default")
    else:
        poll_seconds = max(1, min(300, int(raw_poll)))

    max_attempts = DEFAULT_MAX_ATTEMPTS
    raw_attempts = section.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if isinstance(raw_attempts, bool) or not isinstance(raw_attempts, (int, float)):
        errors.append("[questions_listen].max_attempts: expected a number, using default")
    else:
        max_attempts = max(1, int(raw_attempts))

    # Bindings are parsed by their owning module, never re-parsed here.
    bindings = roles_config.load_bindings(path)
    errors.extend(bindings.errors)

    tokens: list[str] = []
    seen: set[str] = set()

    def _add(value: object) -> None:
        if isinstance(value, str) and value.startswith("rly_") and value not in seen:
            seen.add(value)
            tokens.append(value)

    _add(bindings.default_token)
    for _role, value in sorted(bindings.roles.items()):
        _add(value)
    for _ws, role_map in sorted(bindings.workspace_roles.items()):
        for _role, value in sorted(role_map.items()):
            _add(value)

    if enabled and not tokens:
        errors.append("no installation token in config.toml — nothing to poll")
    if enabled and not bindings.server_url:
        errors.append("server_url not configured in config.toml")

    return ListenConfig(
        enabled=enabled,
        server_url=bindings.server_url,
        tokens=tuple(tokens),
        poll_seconds=poll_seconds,
        max_attempts=max_attempts,
        config_path=path,
        errors=tuple(errors),
    )


# ── Workspace re-resolution (invariant 7) ─────────────────────────────────────


class ResolveError(Exception):
    """The workspace behind an index entry could not be located **now**."""


def _existing_ancestor(path: Path) -> Path | None:
    """*path* if it is a directory, else its nearest existing ancestor."""
    current = path
    while True:
        if current.is_dir():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _find_roles_toml(start: Path) -> Path | None:
    """Nearest ``.claude/roles.toml`` at or above *start*.

    Deliberately **not** ``roles_config.find_roles_file``: that honours
    ``CLAUDE_PROJECT_DIR`` (a test-isolation hook for hooks that run inside a
    session), which would make a resident daemon resolve every workspace to
    whatever directory happened to be exported into its environment.  The
    listener serves many workspaces and must key on the index entry alone.
    """
    current = start
    while True:
        candidate = current / ".claude" / "roles.toml"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def resolve_target(entry: IndexEntry) -> qs.QuestionsStore:
    """Re-resolve the queue's workspace **now**, from the entry's anchor mode.

    Invariant 7: the absolute path captured at ask time is not an address.  A
    worktree is deleted when its branch lands; a checkout is moved or re-cloned.
    So the stored ``root`` is used only as a *probe* into the filesystem — the
    root that is actually written to comes from
    :func:`questions_store.resolve_anchor` run against the workspace's current
    config, with the entry's recorded anchor mode forced back on so an entry
    written under ``anchor = "worktree"`` is not suddenly looked for in the
    primary checkout (or the reverse).

    Raises :class:`ResolveError` — routed to ``pending`` by the caller — when
    the workspace cannot be located.  It never guesses: writing an answer into
    the wrong workspace would be worse than retrying forever in plain sight.
    """
    if not entry.root:
        raise ResolveError("index entry has no root path")

    probe = _existing_ancestor(Path(entry.root).expanduser())
    if probe is None:
        raise ResolveError(f"no existing directory at or above {entry.root}")

    roles_path = _find_roles_toml(probe)
    if roles_path is None:
        raise ResolveError(f"no .claude/roles.toml at or above {probe}")

    config = qs.load_questions_config(str(probe), roles_path=roles_path)
    if config is None:
        raise ResolveError(f"{roles_path} has no [questions] section")

    if entry.anchor in ("repo", "worktree", "path"):
        config = replace(config, anchor=entry.anchor)

    anchor = qs.resolve_anchor(probe, config)

    if entry.workspace_id and anchor.workspace_id != entry.workspace_id:
        raise ResolveError(
            f"workspace_id mismatch at {anchor.root}: index says "
            f"{entry.workspace_id!r}, that checkout says {anchor.workspace_id!r}"
        )

    return qs.store_for_root(config, anchor.root)


# ── Feed state ────────────────────────────────────────────────────────────────

#: ``--status`` connection states.
FEED_STARTING = "starting"
FEED_CONNECTED = "connected"
FEED_DISCONNECTED = "disconnected"
FEED_UNAUTHORIZED = "unauthorized"


@dataclass
class Feed:
    """One installation-scoped answer feed: its token, client and poll state."""

    fingerprint: str
    token: str
    state: str = FEED_STARTING
    last_error: str = ""
    last_poll_at: str = ""
    backoff_s: float = NET_BACKOFF_BASE_S
    next_poll_at: float = 0.0
    client: Any = None

    def public(self, watermark: int) -> dict[str, Any]:
        """The status-file view — fingerprint only, never token material."""
        return {
            "fingerprint": self.fingerprint,
            "state": self.state,
            "watermark": watermark,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
        }


# ── The listener ──────────────────────────────────────────────────────────────


def _default_client_factory(server_url: str, token: str) -> Any:
    if RelayClient is None:  # pragma: no cover — depends on the environment
        raise RelayError("relay_server package not importable")
    return RelayClient(server_url, token)


class Listener:
    """The resident loop: poll → apply → PATCH → advance the watermark.

    Constructed with explicit paths and collaborators so the whole of it is
    testable with a fake relay client, temp workspaces and no network.
    """

    def __init__(
        self,
        config: ListenConfig,
        *,
        index_path: Path | None = None,
        index_lock_path: Path | None = None,
        status_path: Path | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.index_path = index_path if index_path is not None else INDEX_PATH
        self.index_lock_path = (
            index_lock_path if index_lock_path is not None else _LOCK_PATH
        )
        self.status_path = status_path if status_path is not None else STATUS_PATH
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock or time.time
        self._sleep = sleeper or time.sleep
        self._rng = rng or random.Random()

        self.primary_fingerprint = config.primary_fingerprint
        self.feeds: list[Feed] = [
            Feed(fingerprint=token_fingerprint(token), token=token)
            for token in config.tokens
        ]
        self.started_at = self._now_iso()
        self.last_applied: dict[str, Any] | None = None
        self.cycles = 0

    # ── clocks ──

    def _now(self) -> float:
        return self._clock()

    def _now_iso(self) -> str:
        return (
            datetime.fromtimestamp(self._now(), timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    # ── clients ──

    def client_for(self, feed: Feed) -> Any:
        if feed.client is None:
            feed.client = self._client_factory(self.config.server_url or "", feed.token)
        return feed.client

    def feed_by_fingerprint(self, fingerprint: str) -> Feed | None:
        for feed in self.feeds:
            if feed.fingerprint == fingerprint:
                return feed
        return None

    # ── startup migration ──

    def _seed_primary_watermark(self) -> None:
        """Seed ``watermarks[primary_fp]`` from the legacy top-level watermark.

        When 23-04 was the installed version it wrote ``index.watermark = N``
        but had no ``watermarks`` dict.  After upgrading to 23-05 the
        ``watermarks`` map is empty, so ``feed_watermark`` returns 0 and the
        primary feed replays the entire history on the first start.

        This one-time migration reads the index once and, if the primary
        fingerprint is absent from ``watermarks``, seeds it from the existing
        ``watermark`` field so the first poll starts from where 23-04 left off.

        Seeding rules — each exists for a reason:

        - Seed **only** the primary fingerprint.  A role-bound feed has
          genuinely never been polled by 23-04; starting it from 0 (a full
          replay) is safe and correct — seeding from the primary's position
          would silently skip answers that were never processed.
        - Seed **only** when the key is absent.  Never overwrite a recorded
          position; never move a watermark backwards.
        - Safe when ``watermark`` is absent (0), zero (no-op), or malformed
          (``Index.from_dict`` coerces every value to ``int``, defaulting to 0).
        """
        fp = self.primary_fingerprint
        if not fp:
            return

        def _seed(idx: Index) -> None:
            if fp in idx.watermarks:
                return  # already recorded; never overwrite
            legacy = idx.watermark  # int, 0 when absent/malformed — from_dict
            if legacy > 0:
                idx.watermarks[fp] = legacy
                # index.watermark already equals legacy, so the mirror is
                # unchanged and consistent.

        mutate_index(_seed, path=self.index_path, lock_path=self.index_lock_path)

    # ── the loop ──

    def run_forever(self, stop: Callable[[], bool] | None = None) -> None:
        """Poll until *stop* says otherwise.  Never exits on a transient error."""
        while not (stop() if stop is not None else False):
            polled = self.run_cycle()
            if polled == 0:
                # Every feed is backing off; wait for the earliest one rather
                # than spinning.  Capped so a stop() flag is noticed promptly.
                now = self._now()
                delays = [f.next_poll_at - now for f in self.feeds] or [1.0]
                self._sleep(max(0.5, min(float(self.config.poll_seconds), min(delays))))

    def run_cycle(self) -> int:
        """One pass: retry pending, poll every due feed, refresh the status file.

        Returns the number of feeds actually polled — 0 means every feed is in
        backoff and the caller should wait.
        """
        self.cycles += 1
        if self.cycles == 1:
            self._seed_primary_watermark()
        self.retry_pending()

        polled = 0
        now = self._now()
        due = [f for f in self.feeds if f.next_poll_at <= now]
        # Split the long-poll budget across the due feeds so that N feeds still
        # complete a cycle inside ``poll_seconds`` — with the ordinary single
        # feed this is exactly the architecture's ``wait=25``.
        wait = max(1, int(self.config.poll_seconds // max(1, len(due)))) if due else 0
        for feed in due:
            self.poll_feed(feed, wait)
            polled += 1

        self.write_status()
        return polled

    def poll_feed(self, feed: Feed, wait: int) -> list[dict[str, Any]]:
        """``GET /v1/answers?after=<watermark>&wait=N`` and process what came back."""
        after = feed_watermark(read_index(self.index_path), feed.fingerprint)
        try:
            rows = self.client_for(feed).get_answers(after=after, wait=wait)
        except RelayError as exc:
            self._note_feed_error(feed, exc, getattr(exc, "status_code", None))
            return []
        except Exception as exc:  # noqa: BLE001 — httpx/OS errors are the normal case
            self._note_feed_error(feed, exc, None)
            return []

        feed.state = FEED_CONNECTED
        feed.last_error = ""
        feed.backoff_s = NET_BACKOFF_BASE_S
        feed.next_poll_at = self._now()
        feed.last_poll_at = self._now_iso()

        rows = rows or []
        for row in rows:
            if isinstance(row, dict):
                self.process_answer(feed, row)
        return rows

    def _note_feed_error(self, feed: Feed, exc: BaseException, status: int | None) -> None:
        """Backoff bookkeeping.  ``401`` is fatal-with-slow-retry, not fatal."""
        feed.last_poll_at = self._now_iso()
        feed.last_error = f"{type(exc).__name__}: {exc}"
        if status == 401:
            feed.state = FEED_UNAUTHORIZED
            feed.next_poll_at = self._now() + AUTH_RETRY_S
            logger.warning(
                "answer feed %s: token rejected (401); retrying in %.0fs",
                feed.fingerprint, AUTH_RETRY_S,
            )
            return
        feed.state = FEED_DISCONNECTED
        delay = min(feed.backoff_s, NET_BACKOFF_MAX_S)
        jitter = 1.0 + self._rng.uniform(-NET_BACKOFF_JITTER, NET_BACKOFF_JITTER)
        feed.next_poll_at = self._now() + max(0.5, delay * jitter)
        feed.backoff_s = min(feed.backoff_s * 2.0, NET_BACKOFF_MAX_S)
        # Debug, not warning: a laptop that slept drops its connection every
        # time and that is not news (architecture §5.1).
        logger.debug("answer feed %s disconnected: %s", feed.fingerprint, feed.last_error)

    # ── per-answer pipeline (task §3, steps 1-6) ──

    def process_answer(self, feed: Feed, row: dict[str, Any]) -> str:
        """Apply one answer row.  Returns the outcome for tests and logging.

        One of ``skipped`` (not ours), ``acked``, ``applied`` or ``pending``.
        """
        try:
            message_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            return "skipped"
        if message_id <= 0:
            return "skipped"

        answer_text = str(row.get("answer_text") or "")
        answered_at = str(row.get("answered_at") or "") or self._now_iso()

        # ── step 1: index lookup ──
        entry = read_index(self.index_path).messages.get(str(message_id))
        if entry is None:
            # Not ours — the feed carries every answered message of this
            # installation, permission prompts and blocking questions included —
            # or the index was lost.  Either way there is nothing to apply and
            # nothing to retry, so it is skipped and the watermark moves past it.
            # Nothing is deleted, nothing is marked applied.
            logger.debug("answer %s has no index entry; not ours", message_id)
            self._advance_only(feed, message_id)
            return "skipped"

        # ── step 2: no qid → ack-notification, finalize only (brd D7) ──
        if entry.qid is None:
            self._finalize(feed, message_id, answer_text, entry)
            self._advance_only(feed, message_id)
            return "acked"

        # ── steps 3 + 4: re-resolve now, then apply ──
        error = self._apply(entry, message_id, answer_text, answered_at)

        # ── step 5: finalize the Telegram message ──
        if error is None:
            self._finalize(feed, message_id, answer_text, entry)
            self._record_applied(entry, message_id)

        # ── step 6: terminal record + watermark, one atomic write ──
        self._terminal(
            feed,
            message_id=message_id,
            error=error,
            answer=answer_text,
            answered_at=answered_at,
        )
        return "pending" if error is not None else "applied"

    def _apply(
        self,
        entry: IndexEntry,
        message_id: int,
        answer_text: str,
        answered_at: str,
    ) -> str | None:
        """Steps 3 + 4.  Returns ``None`` on success, else the failure reason.

        Every store call is wrapped: ``QuestionsStoreError`` (a corrupt or
        unreadable queue file — the 23-03 obligation), ``not_found`` and
        ``conflict`` all become a reason string and route to ``pending``.  None
        of them may escape into the loop, and none of them is ever treated as an
        apply.
        """
        qid = entry.qid or ""
        try:
            store = resolve_target(entry)
        except ResolveError as exc:
            return f"workspace not resolvable: {exc}"
        except qs.QuestionsStoreError as exc:
            return f"store error while resolving: {exc}"
        except OSError as exc:
            return f"io error while resolving: {exc}"
        except Exception as exc:  # noqa: BLE001 — see below
            logger.exception("unexpected error resolving message %s", message_id)
            return f"unexpected error while resolving: {type(exc).__name__}: {exc}"

        try:
            result = store.apply_answer(
                qid,
                answer_text,
                self._answered_by(entry),
                message_id,
                rel_path=entry.rel_path or None,
                role=entry.role,
                answered_at=answered_at,
            )
        except qs.QuestionsStoreError as exc:
            return f"store error: {exc}"
        except OSError as exc:
            return f"io error: {exc}"
        except Exception as exc:  # noqa: BLE001
            # The catch-all is deliberate and loud.  This is the component whose
            # whole job is to never lose a human decision: an unforeseen error
            # from one workspace's file must become a retryable ``pending``
            # entry, not the death of the resident loop.  ``logger.exception``
            # keeps the traceback in the journal, so it is surfaced, not masked.
            logger.exception("unexpected error applying message %s", message_id)
            return f"unexpected error: {type(exc).__name__}: {exc}"

        if result is qs.ApplyResult.APPLIED:
            return None
        if result is qs.ApplyResult.CONFLICT:
            # Two entries claim this id in the target file.  The store refuses
            # to guess and so does this: retryable, kept in ``pending``, visible
            # in ``pending_answers``.  Never silently applied to a guess.
            return f"conflict: more than one entry for {qid} in {entry.rel_path or '(queue set)'}"
        return f"not_found: no entry for {qid} in {entry.rel_path or '(queue set)'}"

    @staticmethod
    def _answered_by(entry: IndexEntry) -> str:
        """The provenance field: the index entry's role rendered as its alias."""
        return f"@{entry.role}" if entry.role else "telegram"

    def _finalize(
        self,
        feed: Feed | None,
        message_id: int,
        answer_text: str,
        entry: IndexEntry | None,
    ) -> bool:
        """Step 5 — ``PATCH /v1/messages/{id}`` to ``✅ <answer>``.

        The finalization the blocking hook would have done: it strips the
        keyboard and clears the ``#unanswered`` tag.  Best-effort by design —
        the decision is already durable in the queue file, and epic 19's cleanup
        sweep covers the tag if this never lands.  A *crash* here replays (the
        watermark has not moved); a *failure* here is logged and the watermark
        still advances, because a message that cannot be edited must not stall
        every later answer.
        """
        if feed is None:
            logger.info(
                "message %s applied but not finalized: its installation is no "
                "longer configured on this machine",
                message_id,
            )
            return False
        text = self._finalize_text(answer_text, entry)
        try:
            self.client_for(feed).edit_message(message_id, text=text)
            return True
        except RelayError as exc:
            logger.warning("finalize PATCH failed for message %s: %s", message_id, exc)
        except Exception as exc:  # noqa: BLE001 — transport errors are normal
            logger.warning("finalize PATCH failed for message %s: %s", message_id, exc)
        return False

    @staticmethod
    def _finalize_text(answer_text: str, entry: IndexEntry | None) -> str:
        """``✅ <answer>`` plus a provenance line, HTML-escaped for Telegram."""
        body = answer_text.strip()
        if len(body) > _FINALIZE_TEXT_BUDGET:
            body = body[:_FINALIZE_TEXT_BUDGET] + "…"
        escaped = html.escape(body, quote=False) or "(answered)"
        line = f"✅ {escaped}"
        if entry is None:
            return line
        parts = [p for p in (entry.qid or "", entry.workspace_id or "") if p]
        if not parts:
            return line
        return line + "\n\n<i>" + html.escape(" · ".join(parts), quote=False) + "</i>"

    def _record_applied(self, entry: IndexEntry, message_id: int) -> None:
        self.last_applied = {
            "message_id": message_id,
            "qid": entry.qid,
            "workspace_id": entry.workspace_id,
            "rel_path": entry.rel_path,
            "applied_at": self._now_iso(),
        }

    # ── index writes ──

    def _advance_only(self, feed: Feed, message_id: int) -> None:
        """Advance the watermark for an answer with no work attached to it."""

        def _mutate(idx: Index) -> None:
            set_feed_watermark(
                idx,
                feed.fingerprint,
                message_id,
                primary_fingerprint=self.primary_fingerprint,
            )

        mutate_index(_mutate, path=self.index_path, lock_path=self.index_lock_path)

    def _terminal(
        self,
        feed: Feed,
        *,
        message_id: int,
        error: str | None,
        answer: str,
        answered_at: str,
    ) -> None:
        """Record the terminal outcome **and** advance the watermark, atomically.

        Invariant 2 lives here.  Because the ``pending`` upsert (or removal) and
        the watermark advance are one ``flock``-ed read-modify-write, no crash
        can produce a watermark that has moved past an answer nothing is
        tracking, nor a pending entry that a later replay would duplicate.
        """
        now_iso = self._now_iso()
        fingerprint = feed.fingerprint
        primary = self.primary_fingerprint

        def _mutate(idx: Index) -> None:
            existing = next(
                (p for p in idx.pending if p.message_id == message_id), None
            )
            if error is None:
                if existing is not None:
                    idx.pending.remove(existing)
            elif existing is not None:
                existing.attempts += 1
                existing.last_error = error
                existing.last_attempt_at = now_iso
                existing.answer = answer
                existing.answered_at = answered_at or existing.answered_at
                existing.token_fp = fingerprint
            else:
                idx.pending.append(
                    PendingApply(
                        message_id=message_id,
                        answer=answer,
                        attempts=1,
                        first_failed_at=now_iso,
                        last_error=error,
                        answered_at=answered_at,
                        last_attempt_at=now_iso,
                        token_fp=fingerprint,
                    )
                )
            set_feed_watermark(
                idx, fingerprint, message_id, primary_fingerprint=primary
            )

        mutate_index(_mutate, path=self.index_path, lock_path=self.index_lock_path)
        if error is not None:
            logger.warning(
                "answer %s could not be applied (%s); kept in pending", message_id, error
            )

    # ── pending retries and the sidecar (task §4) ──

    def pending_due(self, pending: PendingApply, now: float) -> bool:
        """True when *pending* has waited out its backoff.

        60 s doubling to roughly hourly (architecture §7).  A missing
        ``last_attempt_at`` — an entry written by an older build, or one whose
        timestamp is unparseable — is treated as due, because retrying early is
        harmless and never retrying is not.
        """
        if pending.attempts <= 0 or not pending.last_attempt_at:
            return True
        last = _parse_iso(pending.last_attempt_at)
        if last is None:
            return True
        delay = min(
            PENDING_BACKOFF_BASE_S * (2 ** (pending.attempts - 1)),
            PENDING_BACKOFF_MAX_S,
        )
        return now >= last + delay

    def retry_pending(self) -> int:
        """Re-attempt every due ``pending`` entry.  Returns how many were applied."""
        index = read_index(self.index_path)
        if not index.pending:
            return 0

        applied = 0
        now = self._now()
        for pending in list(index.pending):
            if not self.pending_due(pending, now):
                continue
            entry = index.messages.get(str(pending.message_id))
            feed = self.feed_by_fingerprint(pending.token_fp)

            if entry is None or entry.qid is None:
                # No routing record (or an ack that should never have pended):
                # nothing to apply and nothing the sidecar could be written
                # against.  Kept, counted and surfaced rather than dropped.
                self._bump_pending(
                    pending.message_id, "no routable index entry for this message"
                )
                continue

            error = self._apply(
                entry,
                pending.message_id,
                pending.answer,
                pending.answered_at or self._now_iso(),
            )
            if error is None:
                self._finalize(feed, pending.message_id, pending.answer, entry)
                self._record_applied(entry, pending.message_id)
                self._drop_pending(pending.message_id)
                applied += 1
                continue

            attempts = pending.attempts + 1
            if attempts >= self.config.max_attempts:
                ok, side_error = self._sidecar(entry, pending)
                if ok:
                    self._drop_pending(pending.message_id)
                    continue
                error = f"{error}; sidecar failed: {side_error}"
            self._bump_pending(pending.message_id, error)
        return applied

    def _bump_pending(self, message_id: int, error: str) -> None:
        now_iso = self._now_iso()

        def _mutate(idx: Index) -> None:
            for p in idx.pending:
                if p.message_id == message_id:
                    p.attempts += 1
                    p.last_error = error
                    p.last_attempt_at = now_iso
                    return

        mutate_index(_mutate, path=self.index_path, lock_path=self.index_lock_path)

    def _drop_pending(self, message_id: int) -> None:
        def _mutate(idx: Index) -> None:
            idx.pending = [p for p in idx.pending if p.message_id != message_id]

        mutate_index(_mutate, path=self.index_path, lock_path=self.index_lock_path)

    def _sidecar(self, entry: IndexEntry, pending: PendingApply) -> tuple[bool, str]:
        """Write the answer to ``<dir>/answered/<qid>.md`` (brd D10, §6.3).

        The last resort, after ``max_attempts``: the original entry could not be
        located, so the answer is written beside the queue with a note saying so.
        Visible and reviewable in git, never silently lost.

        It is **not** a queue file — ``answered/`` is scanned for id allocation
        only — but the answer text still goes through the store's escaper
        (invariant 11), and the write takes the store's own directory lock.
        Idempotent: a sidecar already citing this relay message id is success.
        """
        qid = entry.qid or f"msg-{pending.message_id}"
        try:
            store = resolve_target(entry)
        except (ResolveError, qs.QuestionsStoreError, OSError) as exc:
            return False, str(exc)

        try:
            block = qs.render_answer_block(
                store.fmt,
                qid=qid,
                answer_text=pending.answer,
                answered_by=self._answered_by(entry),
                message_id=pending.message_id,
                answered_at=pending.answered_at or self._now_iso(),
            )
        except qs.QuestionsStoreError as exc:
            return False, f"answer block not renderable: {exc}"

        note = (
            f"> The original entry for {qid} could not be located after "
            f"{pending.attempts} attempts. Last error: {pending.last_error or 'unknown'}. "
            f"Recorded here so the answer is not lost (brd D10)."
        )
        header = f"# {qid} — answer recorded without its entry"

        path = store.answered_dir / f"{qid}.md"
        try:
            with store.lock():
                existing = ""
                if path.is_file():
                    existing = path.read_text(encoding="utf-8", errors="replace")
                    for line in existing.split("\n"):
                        m = qs.ANSWER_MARKER_RE.match(line)
                        if m is not None and m.group(1) == str(pending.message_id):
                            return True, ""
                parts: list[str] = []
                if existing:
                    parts.append(existing.rstrip("\n"))
                else:
                    parts.append(header)
                parts.append("")
                parts.append(note)
                parts.append("")
                parts.extend(block)
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(path, "\n".join(parts) + "\n")
        except qs.QuestionsStoreError as exc:
            return False, str(exc)
        except OSError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — a failed sidecar keeps it pending
            logger.exception("unexpected error writing the sidecar for %s", qid)
            return False, f"{type(exc).__name__}: {exc}"

        logger.warning(
            "answer for %s written to the sidecar %s after %s attempts",
            qid, path, pending.attempts,
        )
        return True, ""

    # ── status (task §5) ──

    def status_dict(self) -> dict[str, Any]:
        """Everything ``--status`` prints.  Never token material."""
        index = read_index(self.index_path)
        pending = sorted(index.pending, key=lambda p: p.first_failed_at or "")
        oldest = pending[0] if pending else None
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": self._now_iso(),
            "cycles": self.cycles,
            "watermark": index.watermark,
            "feeds": [
                feed.public(feed_watermark(index, feed.fingerprint))
                for feed in self.feeds
            ],
            "last_applied": self.last_applied,
            "pending_count": len(index.pending),
            "oldest_pending": (
                {
                    "message_id": oldest.message_id,
                    "attempts": oldest.attempts,
                    "first_failed_at": oldest.first_failed_at,
                    "last_error": oldest.last_error,
                }
                if oldest is not None
                else None
            ),
            "config": self.config.redacted(),
            "index_path": str(self.index_path),
        }

    def write_status(self) -> None:
        """Refresh the status file; a failure here must never stop the loop."""
        try:
            _atomic_write_json(self.status_path, self.status_dict())
        except Exception as exc:  # noqa: BLE001 — diagnostics never stop the loop
            logger.debug("could not write status file %s: %s", self.status_path, exc)


def _parse_iso(value: str) -> float | None:
    """Parse a UTC ISO-8601 stamp to epoch seconds; ``None`` when unparseable."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ── Single instance (task §1) ─────────────────────────────────────────────────


def acquire_single_instance(path: Path | None = None):
    """Take the listener lock, or return ``None`` when another copy holds it.

    Returns the open file handle — keep a reference for the process lifetime;
    dropping it releases the lock.  A second copy exiting cleanly is the whole
    point: two listeners would double-apply.
    """
    target = path if path is not None else LISTEN_LOCK_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        pass
    return handle


def listener_running(path: Path | None = None) -> bool:
    """True when some other process holds the listener lock.

    ``--status`` cannot hold the lock, so it probes it instead: if the lock can
    be taken, nothing is listening — which is the single most useful line the
    diagnostic can print when an answer "didn't arrive".
    """
    target = path if path is not None else LISTEN_LOCK_PATH
    if not target.exists():
        return False
    try:
        handle = open(target, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


# ── ``--status`` rendering ────────────────────────────────────────────────────


def read_status(path: Path | None = None) -> dict[str, Any] | None:
    """The last status snapshot the loop wrote, or ``None``."""
    target = path if path is not None else STATUS_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def format_status(
    status: dict[str, Any] | None,
    index: Index,
    *,
    running: bool,
    config: ListenConfig | None = None,
) -> str:
    """Render ``--status`` for a human.

    Takes the status file *and* the index: the index is the truth about the
    watermark and the pending backlog even when the loop is dead, which is
    precisely when someone runs this.
    """
    lines: list[str] = ["questions-listen"]
    lines.append(f"  running:        {'yes' if running else 'no'}")

    cfg_view: dict[str, Any] | None = None
    if config is not None:
        cfg_view = config.redacted()
    elif status is not None and isinstance(status.get("config"), dict):
        cfg_view = status["config"]

    if cfg_view is not None:
        lines.append(f"  enabled:        {'yes' if cfg_view.get('enabled') else 'no'}")
        lines.append(f"  server_url:     {cfg_view.get('server_url') or '(unset)'}")
        lines.append(f"  config:         {cfg_view.get('config_path') or '(none)'}")
        lines.append(
            f"  poll/attempts:  {cfg_view.get('poll_seconds')}s / "
            f"{cfg_view.get('max_attempts')}"
        )
        for err in cfg_view.get("errors") or []:
            lines.append(f"  config warning: {err}")

    lines.append(f"  watermark:      {index.watermark}")

    feeds = (status or {}).get("feeds") or []
    if feeds:
        lines.append("  feeds:")
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            line = (
                f"    {feed.get('fingerprint', '?')}  {feed.get('state', '?'):<13}"
                f" watermark={feed.get('watermark', 0)}"
            )
            if feed.get("last_poll_at"):
                line += f"  last_poll={feed['last_poll_at']}"
            lines.append(line)
            if feed.get("last_error"):
                lines.append(f"      last error: {feed['last_error']}")
    elif cfg_view is not None:
        for fingerprint in cfg_view.get("feeds") or []:
            lines.append(f"    {fingerprint}  (never polled)")

    last = (status or {}).get("last_applied")
    if isinstance(last, dict):
        lines.append(
            "  last applied:   "
            f"{last.get('qid') or '(ack)'} "
            f"({last.get('workspace_id', '?')}/{last.get('rel_path', '')}) "
            f"relay #{last.get('message_id', '?')} at {last.get('applied_at', '?')}"
        )
    else:
        lines.append("  last applied:   (none)")

    lines.append(f"  pending:        {len(index.pending)}")
    if index.pending:
        oldest = sorted(index.pending, key=lambda p: p.first_failed_at or "")[0]
        lines.append(
            f"    oldest: relay #{oldest.message_id} attempts={oldest.attempts}"
            f" since={oldest.first_failed_at or '?'}"
        )
        lines.append(f"    last error: {oldest.last_error or '(none)'}")

    if status is not None and status.get("updated_at"):
        lines.append(f"  status file:    updated {status['updated_at']}")
    elif status is None:
        lines.append("  status file:    (never written)")

    return "\n".join(lines)
