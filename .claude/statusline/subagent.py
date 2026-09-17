#!/usr/bin/env python3
"""
Claude Code per-subagent status line.

Reads the agent-panel row context as JSON on stdin and prints one JSON object
per line — {"id": ..., "content": ...} — decorating each subagent row in the
agent panel (ctrl-t) with model, effort, context usage, elapsed time and how
much prompt-cache life the agent has left.

Claude Code replaces the *whole* row with this content apart from the leading
status glyph, so the agent name and description are rendered here too.

Layout (metadata first, description last so only the description truncates):

    Explore-2      Opus · high     ctx 38%    1m12s   warm 58m   searching for callers

Set CC_STATUS_DEBUG=1 to emit diagnostic output on stderr.
Set CC_STATUS_NO_COLOR=1 (or NO_COLOR) to emit plain text.
"""

import json
import os
import re
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statusline import (  # noqa: E402  (path shim must run first)
    _infer_provider_billing,
    _normalize_model_name,
    _pricing_key,
    _safe_session_key,
    cache_state,
    detect_cache_ttl,
    last_activity_epoch,
    read_cache_ttl,
)


# ---------------------------------------------------------------------------
# Effort resolution
#
# A subagent only carries its own `effort` when its agent definition pins one.
# Otherwise Claude Code falls back to the session effort, then to the model's
# catalogue default, then to "high". We mirror that chain so every effort-capable
# agent shows a level, marking anything not pinned by the agent as inherited.
# ---------------------------------------------------------------------------

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_FALLBACK = "high"

# Models whose catalogue entry advertises the "effort" capability, mapped to
# their default_effort (None = capable but no catalogue default).
_EFFORT_CAPABLE = {
    "claude-sonnet-4-6": None,
    "claude-sonnet-5": "high",
    "claude-opus-4-6": None,
    "claude-opus-4-7": "xhigh",
    "claude-opus-4-8": "high",
    "claude-opus-5": "high",
    "claude-fable-5": "high",
    "claude-fable-5-1": "high",
    "claude-mythos-5-1": "high",
}

# Claude models that predate effort entirely.
_EFFORT_INCAPABLE = {
    "claude-3-5-haiku",
    "claude-haiku-4-5",
    "claude-3-5-sonnet",
    "claude-3-7-sonnet",
    "claude-sonnet-4-0",
    "claude-sonnet-4-5",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-5",
    "claude-mythos-5",
}


def supports_effort(model_id: str) -> bool:
    """Whether the model has an effort level at all.

    Unknown Claude models are assumed capable — new releases carry effort, and
    showing an inherited level beats silently dropping the column.
    """
    key = _pricing_key(model_id)
    if key in _EFFORT_CAPABLE:
        return True
    if key in _EFFORT_INCAPABLE:
        return False
    return key.startswith("claude-")


def resolve_effort(task_effort: Optional[str],
                   session_effort: Optional[str],
                   model_id: str) -> Optional[tuple]:
    """Return (level, inherited) for a subagent, or None if effort does not apply."""
    if not supports_effort(model_id):
        return None
    if task_effort in _EFFORT_LEVELS:
        return task_effort, False
    if session_effort in _EFFORT_LEVELS:
        return session_effort, True
    catalogue_default = _EFFORT_CAPABLE.get(_pricing_key(model_id))
    if catalogue_default in _EFFORT_LEVELS:
        return catalogue_default, True
    return _EFFORT_FALLBACK, True


def read_session_effort(session_id: Optional[str]) -> Optional[str]:
    """Read the effort level the main status line last resolved for this session."""
    if not session_id:
        return None
    path = os.path.join(
        os.path.expanduser("~/.cache/claude-statusline"),
        f"session-{_safe_session_key(session_id)}.json",
    )
    try:
        with open(path) as f:
            level = (json.load(f) or {}).get("effort")
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return None
    return level if level in _EFFORT_LEVELS else None


# ---------------------------------------------------------------------------
# Agent transcripts
#
# A subagent's own transcript lives beside the session's, and its mtime is the
# only record of when the agent last did anything — the payload carries no
# endTime and no last-activity field. Claude Code writes it as the agent works,
# so the file is current to within ~100 ms.
# ---------------------------------------------------------------------------

# Both ids are Claude Code's own, but they arrive as payload data and are about
# to become path segments, so they are checked rather than trusted.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Workflow-spawned agents sit under subagents/<workflow>/<run>/, so the walk
# goes deep enough for those and no further.
_MAX_NESTED_DEPTH = 3


def _session_dir_name(transcript_path: str, session_id: Optional[str]) -> Optional[str]:
    """The per-session directory beside the transcript: its own id."""
    candidate = str(session_id or "").strip()
    if not _ID_RE.match(candidate):
        # Served/remote sessions report a session_id the filesystem never saw
        # (`served:…`), so fall back to the name the transcript itself carries.
        base = os.path.basename(transcript_path)
        candidate = base[:-6] if base.endswith(".jsonl") else ""
    return candidate if _ID_RE.match(candidate) else None


def agent_transcript_path(transcript_path: Optional[str],
                          session_id: Optional[str],
                          task_id: Optional[str]) -> Optional[str]:
    """Locate <project>/<session>/subagents/[<nested>/]agent-<id>.jsonl."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    if not _ID_RE.match(str(task_id or "")):
        return None
    session = _session_dir_name(transcript_path, session_id)
    if session is None:
        return None
    base = os.path.join(os.path.dirname(transcript_path), session, "subagents")
    name = f"agent-{task_id}.jsonl"

    base_depth = base.rstrip(os.sep).count(os.sep)
    try:
        flat = os.path.join(base, name)
        if os.path.exists(flat):
            return flat
        for root, dirs, files in os.walk(base):
            if name in files:
                return os.path.join(root, name)
            if root.count(os.sep) - base_depth >= _MAX_NESTED_DEPTH:
                dirs[:] = []
    except (OSError, ValueError):
        # A path the OS rejects is one more agent without a transcript, not a
        # reason to drop every row's decoration.
        pass
    # Forks run with skipTranscript, so having no file at all is normal.
    return None


# Enough siblings to find one that has answered, few enough to stay cheap.
_COHORT_SAMPLE = 3


def cohort_ttl(agent_path: str, limit: int = _COHORT_SAMPLE) -> Optional[int]:
    """The TTL this session's *other* subagents are recording, or None.

    A subagent does not run on the session's TTL. Claude Code grants the 1h
    window per query source against an allowlist the main thread is on and the
    subagent sources are not, so a session caches for 1h while every agent it
    spawns caches for 5m (`CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` and the
    `subagentPromptCacheTtl` setting move it). Inheriting the session's value
    reads an hour of life into a cache that has five minutes of it — so until
    an agent has usage of its own, its siblings are the evidence for what it
    will record.
    """
    directory = os.path.dirname(agent_path)
    try:
        siblings = [entry for entry in os.scandir(directory)
                    if entry.name.startswith("agent-") and entry.name.endswith(".jsonl")
                    and entry.path != agent_path and entry.is_file()]
        siblings.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    except (OSError, ValueError):
        return None
    for entry in siblings[:limit]:
        ttl = detect_cache_ttl(entry.path)
        if ttl:
            return ttl
    return None


def format_cache(task: dict, transcript_path: Optional[str], session_id: Optional[str],
                 now: float) -> tuple:
    """Prompt cache cell: ('warm', '4m', colour), or three Nones when unknown."""
    path = agent_transcript_path(transcript_path, session_id, task.get("id"))
    if path is None:
        return None, None, None
    ttl, recorded = read_cache_ttl(path)
    if ttl is None:
        ttl = cohort_ttl(path)
    if ttl is None and not recorded:
        # Spawned, but nothing has answered yet here or in the cohort: there is
        # no cache to describe, and a placeholder would only flip a moment later.
        return None, None, None
    state = cache_state(last_activity_epoch(path), ttl, now)
    return state if state else (None, None, None)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

_DIM = "2"
_EFFORT_COLORS = {"low": "34", "medium": "32", "high": "33", "xhigh": "35", "max": "31"}
_STATUS_COLORS = {"completed": "32", "failed": "31", "killed": "31"}

# Close each attribute specifically rather than with a blanket reset. Claude Code
# wraps the row in a Text that is dimmed unless selected and bold while viewed;
# a \x1b[0m in the middle of our content would drop that styling for everything
# after it. 39 restores the default foreground, 22 the default intensity.
_SGR_CLOSERS = {_DIM: "22"}


def color_enabled(env: dict) -> bool:
    if env.get("CC_STATUS_NO_COLOR", "").strip() == "1":
        return False
    return not env.get("NO_COLOR", "").strip()


def paint(text: str, code: Optional[str], enabled: bool) -> str:
    if not enabled or not code or not text.strip():
        return text
    return f"\x1b[{code}m{text}\x1b[{_SGR_CLOSERS.get(code, '39')}m"


# ---------------------------------------------------------------------------
# Cell formatters
# ---------------------------------------------------------------------------

def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def format_state(task: dict, now: float) -> tuple:
    """Right-hand state cell: elapsed while running, the outcome once settled.

    Terminal rows have no endTime in the payload, so a running clock would keep
    ticking after the agent stopped — show the outcome instead.
    """
    status = task.get("status")
    if status in _STATUS_COLORS:
        return ("done" if status == "completed" else status), _STATUS_COLORS[status]
    start = task.get("startTime")
    if not isinstance(start, (int, float)):
        return "", None
    return format_elapsed(now - start / 1000.0), _DIM


def format_context(task: dict) -> tuple:
    """Return (percentage or None, colour) — the cell is padded once widths are known."""
    window = task.get("contextWindowSize")
    tokens = task.get("tokenCount")
    if not isinstance(window, (int, float)) or window <= 0 or not isinstance(tokens, (int, float)):
        return None, _DIM
    pct = int(max(0, min(100, tokens * 100.0 / window)))
    return pct, ("32" if pct < 50 else ("33" if pct < 80 else "31"))


def format_model(task: dict, provider: str, session_effort: Optional[str]) -> tuple:
    """Model label plus effort, e.g. 'Opus · high' or 'Sonnet · ~max' (inherited)."""
    raw = task.get("model") or ""
    label = _normalize_model_name(raw, provider) if raw else "?"
    resolved = resolve_effort(task.get("effort"), session_effort, raw) if raw else None
    if resolved is None:
        return label, None, None
    level, inherited = resolved
    return label, ("~" if inherited else "") + level, _EFFORT_COLORS.get(level)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_width(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def truncate_visible(text: str, width: int) -> str:
    """Hard-cap a rendered row at `width` visible columns, keeping escapes intact.

    Only bites when the fixed cells alone overrun a very narrow pane — the
    description is already fitted before this. Cutting on raw length would slice
    an escape sequence in half, so step over them instead.
    """
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text
    out, seen, i = [], 0, 0
    while i < len(text) and seen < width:
        match = _ANSI_RE.match(text, i)
        if match:
            out.append(match.group(0))
            i = match.end()
            continue
        out.append(text[i])
        seen += 1
        i += 1
    # The cut may have dropped a cell's closing code; end styling explicitly.
    if any(chunk.startswith("\x1b[") for chunk in out):
        out.append("\x1b[39m\x1b[22m")
    return "".join(out)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_GAP = "   "


def build_rows(payload: dict, env: dict, now: Optional[float] = None) -> list:
    """Return [(task_id, content)] for every task in the payload."""
    now = time.time() if now is None else now
    tasks = payload.get("tasks") or []
    if not isinstance(tasks, list):
        return []

    colored = color_enabled(env)
    provider, _ = _infer_provider_billing(
        env.get("ANTHROPIC_BASE_URL", "").strip() or None,
        env.get("ANTHROPIC_AUTH_TOKEN", "").strip() or None,
    )
    session_effort = read_session_effort(payload.get("session_id"))
    transcript_path = payload.get("transcript_path")
    session_id = payload.get("session_id")

    # Pass 1 — plain cells, so column widths ignore the escape codes.
    cells = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            continue
        model, effort, effort_code = format_model(task, provider, session_effort)
        context_pct, context_code = format_context(task)
        state, state_code = format_state(task, now)
        cache_word, cache_age, cache_code = format_cache(
            task, transcript_path, session_id, now)
        cells.append({
            "id": task["id"],
            "name": str(task.get("name") or ""),
            "model": model + (f" · {effort}" if effort else ""),
            "effort_code": effort_code,
            "context_pct": context_pct,
            "context_code": context_code,
            "state": state,
            "state_code": state_code,
            "cache_word": cache_word,
            "cache_age": cache_age,
            "cache_code": cache_code,
            "description": str(task.get("label") or task.get("description") or ""),
        })
    if not cells:
        return []

    name_width = max(len(c["name"]) for c in cells)
    model_width = max(len(c["model"]) for c in cells)
    state_width = max(len(c["state"]) for c in cells)
    pct_width = max(len(str(c["context_pct"])) if c["context_pct"] is not None else 1
                    for c in cells)
    for c in cells:
        value = "?" if c["context_pct"] is None else str(c["context_pct"])
        c["context"] = f"ctx {value:>{pct_width}}%"

    # The whole cache column is dropped when nothing is known — an agent panel
    # full of forks should not carry an empty column.
    age_width = max((len(c["cache_age"]) for c in cells if c["cache_age"]), default=0)
    for c in cells:
        c["cache"] = (f"{c['cache_word']} {c['cache_age']:>{age_width}}"
                      if c["cache_age"] else "")
    cache_width = max(len(c["cache"]) for c in cells)

    try:
        budget = int(payload.get("columns"))
    except (TypeError, ValueError):
        budget = 80
    budget = budget if budget > 0 else 80

    # Pass 2 — pad, colour, fit the description into what is left.
    rows = []
    for c in cells:
        fixed = []
        if name_width:
            fixed.append(paint(c["name"].ljust(name_width), "36", colored))
        model_cell = c["model"].ljust(model_width)
        fixed.append(paint(model_cell, c["effort_code"], colored) if c["effort_code"] else model_cell)
        fixed.append(paint(c["context"], c["context_code"], colored))
        if state_width:
            fixed.append(paint(c["state"].rjust(state_width), c["state_code"], colored))
        if cache_width:
            fixed.append(paint(c["cache"].ljust(cache_width), c["cache_code"], colored))
        prefix = _GAP.join(fixed)

        remaining = budget - visible_width(prefix) - len(_GAP)
        description = truncate(c["description"], remaining) if remaining >= 4 else ""
        content = prefix + (_GAP + paint(description, _DIM, colored) if description else "")
        rows.append((c["id"], truncate_visible(content, budget)))
    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    debug = os.environ.get("CC_STATUS_DEBUG", "").strip() == "1"
    try:
        raw = sys.stdin.read()
        if debug:
            print(f"[CC_STATUS_DEBUG] stdin: {raw[:300]}", file=sys.stderr)
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        for task_id, content in build_rows(payload, dict(os.environ)):
            print(json.dumps({"id": task_id, "content": content}))
    except Exception as exc:
        # Emitting nothing leaves the default rows in place; emitting a bad line
        # would make Claude Code log a schema error for every tick.
        if debug:
            print(f"[CC_STATUS_DEBUG] error: {exc}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
