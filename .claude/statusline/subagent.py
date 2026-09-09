#!/usr/bin/env python3
"""
Claude Code per-subagent status line.

Reads the agent-panel row context as JSON on stdin and prints one JSON object
per line — {"id": ..., "content": ...} — decorating each subagent row in the
agent panel (ctrl-t) with model, effort, context usage and elapsed time.

Claude Code replaces the *whole* row with this content apart from the leading
status glyph, so the agent name and description are rendered here too.

Layout (metadata first, description last so only the description truncates):

    Explore-2      Opus · high     ctx 38%    1m12s   searching for callers

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

    # Pass 1 — plain cells, so column widths ignore the escape codes.
    cells = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            continue
        model, effort, effort_code = format_model(task, provider, session_effort)
        context_pct, context_code = format_context(task)
        state, state_code = format_state(task, now)
        cells.append({
            "id": task["id"],
            "name": str(task.get("name") or ""),
            "model": model + (f" · {effort}" if effort else ""),
            "effort_code": effort_code,
            "context_pct": context_pct,
            "context_code": context_code,
            "state": state,
            "state_code": state_code,
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
