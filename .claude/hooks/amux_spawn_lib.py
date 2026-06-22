#!/usr/bin/env python3
"""Shared helpers for the ``amux-spawn`` launcher and its producer/read hooks.

This is the single source of truth (Phase-0 decision 2) for:

- amux paths: the sessions dir (``~/.amux/sessions``) and the spawn handle
  registry (``~/.amux/spawn``), with the global spawn lock.
- name <-> handle <-> workspace-prefix resolution.
- the ``amux-<name>`` parent lookup (cf. ``resolve_amux_session`` in
  ``notification_hook.py``) and reading ``CC_DIR`` / ``CC_FLAGS`` from a parent
  session's ``.env``.
- atomic handle read/write (tmp file + rename) against the schema in
  architecture s6.0.
- deterministic transcript path computation (encodes both ``/`` and ``.``).

10-01 owns this module; 10-02 (producer) / 10-03 (reads) / 10-05 (attach) import
it rather than re-implementing any of the above. **No task invents handle fields
outside the architecture s6.0 list.**

Everything here is fail-soft: helpers return ``None`` / empty rather than raising
on a missing or malformed file, so a hook built on top can keep the fail-open
convention.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────────────

# amux honors CC_HOME (see /usr/local/bin/amux); mirror it so amux-spawn and amux
# always agree on where session env files live.
AMUX_HOME = Path(os.environ.get("CC_HOME", str(Path.home() / ".amux")))
AMUX_SESSIONS_DIR = AMUX_HOME / "sessions"
# Spawn handle registry (tracked sessions only) + the single global spawn lock.
SPAWN_DIR = AMUX_HOME / "spawn"
SPAWN_LOCK = SPAWN_DIR / ".lock"

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"

AMUX_TMUX_PREFIX = "amux-"

# Fork-bomb backstop (Decision 4 / architecture s8): per-workspace cap on
# concurrent live tracked sessions.
DEFAULT_MAX_SESSIONS = 16

# Default --stuck-after, in seconds (D-Stuck: 10m). Mirrors the BRD default.
DEFAULT_STUCK_AFTER_S = 600


def ensure_dirs() -> None:
    """Create the amux sessions + spawn dirs if missing (idempotent)."""
    AMUX_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    SPAWN_DIR.mkdir(parents=True, exist_ok=True)


# ── Transcript path ─────────────────────────────────────────────────────────

def encode_project_dir(abs_dir: str) -> str:
    """Encode an absolute workspace dir the way Claude Code names its project dir.

    Verified against ``~/.claude/projects/``: **both** ``/`` and ``.`` map to
    ``-`` while ``_`` is kept. E.g. ``/home/anton/.local/x`` ->
    ``-home-anton--local-x`` (the leading ``/`` and the dot in ``.local`` each
    become a ``-``, yielding the double dash). A slash-only formula breaks on a
    dir containing a dot (architecture s6 note).
    """
    return re.sub(r"[/.]", "-", abs_dir)


def transcript_path_for(abs_dir: str, session_id: str) -> str:
    """Deterministic transcript path = projects/<enc-dir>/<session_id>.jsonl.

    Prefer capturing the real path from the first ``Stop`` payload (10-02); this
    is the spawn-time best guess so the handle is never empty.
    """
    enc = encode_project_dir(abs_dir)
    return str(CLAUDE_PROJECTS_DIR / enc / f"{session_id}.jsonl")


def transcript_mtime(transcript_path: str | None) -> float | None:
    """Return the transcript file's mtime (epoch float), or None if absent."""
    if not transcript_path:
        return None
    try:
        return os.path.getmtime(transcript_path)
    except OSError:
        return None


# ── tmux / amux name resolution ───────────────────────────────────────────────

def tmux_session_name(name: str) -> str:
    """The tmux session name amux wraps a session in: ``amux-<name>``."""
    return f"{AMUX_TMUX_PREFIX}{name}"


def tmux_has_session(name: str) -> bool:
    """True iff the amux session ``name`` has a live tmux session.

    Uses the ``=`` exact-match prefix so ``foo`` does not match ``foo-worker``
    (same guard amux's ``is_running`` uses).
    """
    if not shutil.which("tmux"):
        return False
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={tmux_session_name(name)}"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return False
    return result.returncode == 0


def resolve_amux_session() -> str | None:
    """Return the *current* pane's amux session name, or None.

    Mirrors ``notification_hook.resolve_amux_session``: the current pane's tmux
    session name is the source of truth (``CC_DIR``/``CC_NAME`` are not exported
    into the session). Returns the bare amux name (``claude-hooks-2``), or None
    when not in tmux / not an ``amux-`` session.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane or not shutil.which("tmux"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    session_name = result.stdout.strip()
    if not session_name.startswith(AMUX_TMUX_PREFIX):
        return None
    amux_name = session_name[len(AMUX_TMUX_PREFIX):]
    return amux_name or None


# ── amux session .env parsing ────────────────────────────────────────────────

def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY="value"`` amux session env file into a dict.

    amux writes simple ``KEY="value"`` lines (double-quoted). We strip a single
    matching pair of surrounding quotes; comments / blanks are ignored. Returns
    an empty dict on any error (fail-soft).
    """
    env: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            env[key] = val
    return env


def read_session_env(name: str) -> dict[str, str]:
    """Read ``~/.amux/sessions/<name>.env`` into a dict (empty if absent)."""
    return _parse_env_file(AMUX_SESSIONS_DIR / f"{name}.env")


def parent_cc_dir(parent_name: str) -> str | None:
    """Absolute ``CC_DIR`` of a parent amux session, or None."""
    env = read_session_env(parent_name)
    cc_dir = env.get("CC_DIR", "").strip()
    return cc_dir or None


def parent_cc_flags(parent_name: str) -> list[str]:
    """Tokenized ``CC_FLAGS`` of a parent amux session (empty if absent)."""
    env = read_session_env(parent_name)
    flags = env.get("CC_FLAGS", "").strip()
    if not flags:
        return []
    return flags.split()


def extract_model_flag(flags: list[str]) -> str | None:
    """Return the value of ``--model`` within a flag token list, else None."""
    for i, tok in enumerate(flags):
        if tok == "--model" and i + 1 < len(flags):
            return flags[i + 1]
        if tok.startswith("--model="):
            return tok.split("=", 1)[1]
    return None


# ── Naming ────────────────────────────────────────────────────────────────────

def workspace_prefix(abs_dir: str) -> str:
    """Naming prefix for a workspace = ``basename(resolved-dir)`` (D-Name)."""
    return Path(abs_dir).name


def list_amux_names() -> set[str]:
    """All amux session names known to amux (from ``amux ls``).

    Parses the leading ``  N  <name>  <dir>`` columns. Best-effort: returns an
    empty set if amux is unavailable.
    """
    names: set[str] = set()
    if not shutil.which("amux"):
        return names
    try:
        result = subprocess.run(
            ["amux", "ls"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return names
    if result.returncode != 0:
        return names
    for line in result.stdout.splitlines():
        parts = line.split()
        # Expected shape: "<index> <name> <dir> ...".
        if len(parts) >= 2 and parts[0].isdigit():
            names.add(parts[1])
    return names


def name_in_use(name: str) -> bool:
    """True if ``name`` is taken in amux's *full* namespace.

    A name is taken if amux knows it (registered .env) OR a live tmux session
    exists for it. Checking both closes the create-detached TOCTOU window
    (architecture s8): tmux existence is the reservation, and amux ``exec``
    overwrites the ``.env`` unconditionally, so the name must be confirmed free
    inside the lock against both sources.
    """
    if (AMUX_SESSIONS_DIR / f"{name}.env").exists():
        return True
    if name in list_amux_names():
        return True
    return tmux_has_session(name)


def pick_free_name(prefix: str, suffix: str | None = None) -> str:
    """Pick a free amux name for ``prefix`` (D-Name).

    With an explicit ``suffix``: the literal ``<prefix>-<suffix>`` (validated
    free by the caller under the lock; this just composes it). Without one:
    ``<prefix>`` then ``<prefix>-2``, ``<prefix>-3`` ... — first one free.

    Must be called inside the global spawn lock so two concurrent spawns cannot
    pick the same name (the name is reserved by creating the tmux session while
    still holding the lock).
    """
    if suffix:
        return f"{prefix}-{suffix}"
    if not name_in_use(prefix):
        return prefix
    n = 2
    while True:
        candidate = f"{prefix}-{n}"
        if not name_in_use(candidate):
            return candidate
        n += 1


# ── Handle registry (architecture s6.0 schema) ───────────────────────────────

# Authoritative field list (architecture s6.0). Used for schema sanity only.
HANDLE_FIELDS = (
    "name",
    "session_id",
    "run_id",
    "dir",
    "transcript_path",
    "stuck_after_s",
    "state",
    "last_state",
    "last_message",
    "background_tasks",
    "permission_pending",
    "mtime_at_stop",
    "created_at",
    "updated_at",
)


def handle_path(name: str) -> Path:
    """Path to a tracked session's handle JSON."""
    return SPAWN_DIR / f"{name}.json"


def iso_now() -> str:
    """Current wall-clock time as an ISO-8601 string (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def mint_uuid() -> str:
    """Mint a fresh UUID4 string.

    The minted ``--session-id`` MUST be a valid UUID — Claude 2.1.185 rejects a
    non-UUID at startup ("Invalid session ID. Must be a valid UUID.").
    """
    return str(uuid.uuid4())


def read_handle(name: str) -> dict[str, Any] | None:
    """Read a tracked session's handle, or None if missing/malformed."""
    path = handle_path(name)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_handle(name: str, handle: dict[str, Any]) -> None:
    """Atomically write a handle (tmp file + rename) into the registry.

    Atomic everywhere (architecture s6.0): write to a temp file in the same dir
    then ``os.replace`` so a concurrent reader never sees a partial file.
    """
    SPAWN_DIR.mkdir(parents=True, exist_ok=True)
    path = handle_path(name)
    fd, tmp = tempfile.mkstemp(dir=str(SPAWN_DIR), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(handle, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_handle(
    *,
    name: str,
    session_id: str,
    run_id: str,
    abs_dir: str,
    transcript_path: str,
    stuck_after_s: int,
) -> dict[str, Any]:
    """Build a fresh handle in state ``spawning`` (architecture s6.0).

    10-01 creates the handle in ``spawning`` with ``stuck_after_s`` persisted so
    10-03 can read it. ``mtime_at_stop`` is None until the first ``Stop``
    (10-02); its absence keeps the session ``active`` until then (s6.1 step 2).
    """
    now = iso_now()
    return {
        "name": name,
        "session_id": session_id,
        "run_id": run_id,
        "dir": abs_dir,
        "transcript_path": transcript_path,
        "stuck_after_s": stuck_after_s,
        "state": "spawning",
        "last_state": None,
        "last_message": None,
        "background_tasks": [],
        "permission_pending": False,
        "mtime_at_stop": None,
        "created_at": now,
        "updated_at": now,
    }


# ── Fork-bomb cap ─────────────────────────────────────────────────────────────

def max_sessions() -> int:
    """Per-workspace concurrent-tracked-session cap (env-overridable)."""
    raw = os.environ.get("AMUX_SPAWN_MAX_SESSIONS")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_MAX_SESSIONS


def live_tracked_count(abs_dir: str) -> int:
    """Count live tracked sessions in a workspace (keyed on absolute CC_DIR).

    Counts only handles whose ``dir`` matches ``abs_dir`` AND whose tmux session
    is alive (``tmux has-session`` true). Dead handles linger (no auto-cleanup,
    D-Cleanup) and are filtered out here. Plain human sessions have no handle, so
    they never count (architecture s8 / FR9).
    """
    if not SPAWN_DIR.is_dir():
        return 0
    target = os.path.abspath(abs_dir)
    count = 0
    for entry in SPAWN_DIR.glob("*.json"):
        try:
            with open(entry) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if os.path.abspath(str(data.get("dir", ""))) != target:
            continue
        if tmux_has_session(str(data.get("name", ""))):
            count += 1
    return count


# ── Handle enumeration (10-03 reads) ──────────────────────────────────────────

def list_handles() -> list[dict[str, Any]]:
    """Return every parseable handle in the registry (no filtering, no cleanup).

    Fail-soft: skips unreadable/malformed files. The registry accumulates dead
    handles (no auto-cleanup, D-Cleanup) — callers filter by ``dir`` / ``run_id``
    and mark liveness via ``tmux has-session``.
    """
    handles: list[dict[str, Any]] = []
    if not SPAWN_DIR.is_dir():
        return handles
    for entry in sorted(SPAWN_DIR.glob("*.json")):
        try:
            with open(entry) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            handles.append(data)
    return handles


# ── Transcript tail parsing (open-turn detection + in-flight tool) ────────────
#
# Shared by 10-03 (status reads) and 10-04 (supervise). The transcript is a JSONL
# file; each line is one event with a ``type`` (``user`` / ``assistant`` / ...)
# and a ``message`` dict carrying ``role`` + ``content`` (string or list of typed
# blocks: ``text`` / ``thinking`` / ``tool_use`` / ``tool_result``). All readers
# here are fail-soft: a missing/unreadable transcript yields the empty / None
# result, never an exception.

# How many lines from the transcript tail to scan for open-turn / in-flight-tool
# detection. Bounded so a huge transcript is cheap to read.
TRANSCRIPT_TAIL_LINES = 400


def read_transcript_tail(transcript_path: str | None, max_lines: int = TRANSCRIPT_TAIL_LINES) -> list[dict[str, Any]]:
    """Return up to ``max_lines`` parsed JSON entries from the transcript tail.

    Fail-soft: returns ``[]`` if the file is missing/unreadable. Malformed lines
    are skipped individually.
    """
    if not transcript_path:
        return []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _content_blocks(message: Any) -> list[Any]:
    """Return a message's content as a list of blocks (wrapping a bare string)."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _is_real_user_turn(entry: dict[str, Any]) -> bool:
    """True iff ``entry`` is a genuine *user* turn that opens work.

    A real user turn carries user-authored text or a real prompt — it is NOT a
    tool_result delivery (that rides on a ``user`` entry too) and NOT a synthetic
    background-completion notification injected by the harness (``isMeta`` / a
    ``task-notification`` payload). Those do not constitute an open turn.
    """
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta") is True:
        return False
    if entry.get("isSidechain") is True:
        return False
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    blocks = _content_blocks(message)
    if not blocks:
        return False
    # A user entry that carries ONLY tool_result blocks is the harness delivering
    # results, not a new human/agent turn.
    saw_text = False
    for block in blocks:
        if not isinstance(block, dict):
            # A bare string already became a {"type":"text"} block above.
            saw_text = True
            continue
        btype = block.get("type")
        if btype == "tool_result":
            continue
        if btype == "text":
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            # Background-completion notifications arrive as user text containing a
            # <task-notification> / task-id payload; those are NOT an open turn.
            low = text.lower()
            if "task-notification" in low or "<task-id>" in low:
                continue
            saw_text = True
        else:
            # Any other content block (e.g. image) counts as authored input.
            saw_text = True
    return saw_text


def _collect_tool_use_ids(blocks: list[Any]) -> list[tuple[str, str]]:
    """Return ``(tool_use_id, tool_name)`` for every ``tool_use`` block."""
    out: list[tuple[str, str]] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tid = block.get("id")
            if isinstance(tid, str) and tid:
                out.append((tid, str(block.get("name", "")) or "tool"))
    return out


def _collect_tool_result_ids(blocks: list[Any]) -> set[str]:
    """Return the set of ``tool_use_id``s answered by ``tool_result`` blocks."""
    ids: set[str] = set()
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            rid = block.get("tool_use_id")
            if isinstance(rid, str) and rid:
                ids.add(rid)
    return ids


def _last_assistant_text_index(entries: list[dict[str, Any]]) -> int:
    """Index of the last main-agent assistant message carrying real *text*.

    That is the point the last ``Stop`` fired (Stop records
    ``last_assistant_message``). An open turn must be NEWER than this — work that
    came before it already ended in that ``Stop``. Returns -1 if none found (no
    Stop boundary in the tail, so the whole tail is "after").
    """
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if entry.get("type") != "assistant" or entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for block in _content_blocks(message):
            if isinstance(block, dict) and block.get("type") == "text" \
                    and str(block.get("text", "")).strip():
                return i
    return -1


def detect_open_turn(transcript_path: str | None, max_lines: int = TRANSCRIPT_TAIL_LINES) -> bool:
    """True iff the transcript tail shows an OPEN TURN (architecture §6 step 2).

    An open turn = a **user message** (genuine, not a tool_result delivery and not
    a background-completion notification) **or** a ``tool_use`` with no matching
    ``tool_result`` — **newer than the last ``Stop``'s assistant message**. Work
    before that boundary already ended in that ``Stop``; a lone background-completion
    notification does NOT count (else an idle session would false-flip to
    running/stuck).

    The caller gates this with ``current_mtime > mtime_at_stop`` (same fs clock) so
    a stable idle transcript is never even parsed.
    """
    entries = read_transcript_tail(transcript_path, max_lines)
    if not entries:
        return False

    # Only consider entries AFTER the last Stop boundary (last assistant text). A
    # dangling tool_use itself has no text block, so the gated/in-flight tool stays
    # in this window; a finished turn's user-msg+assistant-text sit before it.
    boundary = _last_assistant_text_index(entries)
    after = entries[boundary + 1:]
    if not after:
        return False

    # 1) A genuine user turn after the boundary = open turn (re-activation via
    #    `amux send` appends exactly such a user message after the last Stop).
    for entry in after:
        if _is_real_user_turn(entry):
            return True

    # 2) A tool_use with no matching tool_result after the boundary = an in-flight
    #    foreground tool (incl. a permission-gated tool that never returned).
    pending_tool_ids: set[str] = set()
    for entry in after:
        message = entry.get("message")
        blocks = _content_blocks(message)
        if entry.get("type") == "assistant":
            for tid, _name in _collect_tool_use_ids(blocks):
                pending_tool_ids.add(tid)
        for rid in _collect_tool_result_ids(blocks):
            pending_tool_ids.discard(rid)
    return bool(pending_tool_ids)


def inflight_foreground_tool(transcript_path: str | None, max_lines: int = TRANSCRIPT_TAIL_LINES) -> dict[str, Any] | None:
    """Return the last in-flight foreground ``tool_use`` (no matching result), or None.

    Shape: ``{tool, command, tool_use_id}``. ``command`` is best-effort from the
    tool input (Bash ``command``, file ``file_path``, else None). Reason-context
    pairs this with the frozen transcript mtime (age) at the call site.

    Like :func:`detect_open_turn`, only entries AFTER the last main-agent
    assistant-text boundary (the last ``Stop``) are scanned.  This avoids a
    false "foreground tool" when the 400-line tail window cuts a
    tool_use/result pair at the seam — result scrolled out but the tool_use
    is still present.  (This function is reason-context only; state derivation
    uses :func:`detect_open_turn`, which is already boundary-aware.)
    """
    entries = read_transcript_tail(transcript_path, max_lines)
    if not entries:
        return None

    # Mirror detect_open_turn: only consider entries after the last Stop
    # boundary (last main-agent assistant text).  boundary == -1 means no
    # prior Stop in the tail, so after == entries (the whole tail is "after").
    boundary = _last_assistant_text_index(entries)
    after = entries[boundary + 1:]
    if not after:
        return None

    results: set[str] = set()
    uses: list[dict[str, Any]] = []
    for entry in after:
        message = entry.get("message")
        blocks = _content_blocks(message)
        if entry.get("type") == "assistant":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if isinstance(tid, str) and tid:
                        uses.append(block)
        results |= _collect_tool_result_ids(blocks)

    for block in reversed(uses):
        tid = block.get("id")
        if tid in results:
            continue
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        command = None
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or tool_input.get("file_path")
        return {
            "tool": str(block.get("name", "")) or "tool",
            "command": str(command) if command is not None else None,
            "tool_use_id": tid,
        }
    return None


# ── Harness background-task output files (reason-context) ──────────────────────

def session_tasks_dir(transcript_path: str | None) -> Path | None:
    """The per-session harness tasks dir (sibling of the transcript): ``.../tasks``.

    Background Bash/Agent output files live there (e.g. ``tasks/<id>.output``).
    Returns None if there is no transcript path.
    """
    if not transcript_path:
        return None
    return Path(transcript_path).parent / "tasks"


def read_task_output_tail(tasks_dir: Path | None, task_id: str, max_lines: int = 20) -> dict[str, Any] | None:
    """Best-effort read of a background task's output file.

    Looks for ``<tasks_dir>/<task_id>.output`` (and a ``.log`` fallback). Returns
    ``{output_file, tail, mtime}`` with a BOUNDED tail, or None if no file is
    found / readable. Fail-soft.
    """
    if tasks_dir is None or not task_id:
        return None
    candidates = [tasks_dir / f"{task_id}.output", tasks_dir / f"{task_id}.log"]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            with open(path, errors="replace") as f:
                lines = f.readlines()
            tail = "".join(lines[-max_lines:]).rstrip("\n")
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        return {"output_file": str(path), "tail": tail, "mtime": mtime}
    return None
