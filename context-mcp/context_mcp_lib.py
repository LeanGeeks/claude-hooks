"""Context usage MCP — all logic, no ``mcp`` dependency.

Split out of ``server.py`` so the behaviour is testable with the repo's plain
suite while the ``mcp`` dependency stays inside uv's environment (the
``permissions_mcp_lib`` convention).

WHY THIS SPLIT EXISTS — the 2026-09-14 unit-034 incident
--------------------------------------------------------
An MCP server is one process per *session*. Every tool call from that session
arrives on the same stdio pipe, whether it came from the main agent or from one
of its subagents, and **the call carries no caller identity**. The old version
resolved the transcript from ``CLAUDE_CODE_SESSION_ID`` + ``CLAUDE_PROJECT_DIR``
— both inherited by subagents — and returned the result with no indication of
whose numbers they were. So a subagent asking "how full am I?" got its parent's
answer, confidently and silently.

That is not hypothetical. On 2026-09-14 two manager subagents in leads-platform
unit 034 read ``fill_percent`` 10.5 and 12.9 and reported themselves "well
within budget" while actually sitting at 165.2 K and 112.3 K. One of them got
byte-identical readings four hours apart, because it was reading a parent that
had not taken a turn in between. Their chain auto-compacted four times (166 K,
167 K, 167 K, 173 K) against a reported window of 1,000,000 — a number that was
never measured, only defaulted (see ``models.json``).

The permissions MCP states the rule this server was missing: *a guard that
cannot identify its caller cannot enforce the self-decision rule*. It fails
closed. This one cannot fail closed — a read tool that refuses is useless — so
it fails **loud** instead:

* ``scope`` always says whose numbers these are: ``"main-session"``.
* ``caller_identified`` is always ``false`` — the server genuinely cannot tell,
  and says so rather than implying it can.
* ``warning`` appears whenever the session has subagents, i.e. whenever the
  reading could plausibly be someone else's.
* ``subagents`` reports each subagent's *own* measured usage, so a supervisor
  can see its chain even though a subagent cannot see itself.
* ``context_window_source`` distinguishes a real table entry from the fallback
  constant, so a 1,000,000 denominator can never again be mistaken for a
  measurement.
* ``session_resolution`` marks the most-recent-``*.jsonl`` guess as a guess.
"""

import json
import os
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_CONTEXT_WINDOW = 1_000_000
MODELS_JSON = Path(__file__).parent / "models.json"


# --------------------------------------------------------------------------
# context window resolution
# --------------------------------------------------------------------------

def _load_bundled_models() -> dict[str, int]:
    if MODELS_JSON.exists():
        with open(MODELS_JSON) as f:
            return json.load(f)
    return {}


def resolve_context_window(model: str) -> tuple[int, str]:
    """Return ``(tokens, source)`` for ``model``.

    ``source`` is ``"env"``, ``"models.json"`` or ``"default-fallback"``. The
    caller MUST surface it: an unmatched model silently taking
    ``DEFAULT_CONTEXT_WINDOW`` is exactly how "W = 1M" became doctrine in a
    downstream project without anyone measuring anything.
    """
    env_map = os.environ.get("CONTEXT_WINDOW_MAP", "")
    if env_map:
        for pair in env_map.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            pattern, tokens_str = pair.split("=", 1)
            if fnmatch(model, pattern.strip()):
                return int(tokens_str.strip()), "env"

    bundled = _load_bundled_models()
    # dict order is insertion order: models.json lists the most specific
    # patterns first (the explicit long-context variants before the families).
    for pattern, tokens in bundled.items():
        if fnmatch(model, pattern):
            return tokens, "models.json"

    return DEFAULT_CONTEXT_WINDOW, "default-fallback"


# --------------------------------------------------------------------------
# transcript location
# --------------------------------------------------------------------------

def _project_dir() -> str | None:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD")


def _projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECTS_ROOT") or (Path.home() / ".claude" / "projects"))


def find_session_jsonl() -> tuple[Path | None, str]:
    """Return ``(path, resolution)`` for the MAIN session's transcript.

    ``resolution`` is ``"session-id"`` (exact, from ``CLAUDE_CODE_SESSION_ID``)
    or ``"fallback-most-recent"`` — a guess that picks whichever session in this
    project wrote last, which in a fleet may be a completely different unit.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    project_dir = _project_dir()

    if session_id and project_dir:
        encoded = project_dir.replace("/", "-")
        jsonl_path = _projects_root() / encoded / f"{session_id}.jsonl"
        if jsonl_path.exists():
            return jsonl_path, "session-id"

    if project_dir:
        encoded = project_dir.replace("/", "-")
        project_jsonl_dir = _projects_root() / encoded
        if project_jsonl_dir.is_dir():
            jsonl_files = sorted(
                project_jsonl_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if jsonl_files:
                return jsonl_files[0], "fallback-most-recent"

    return None, "not-found"


# --------------------------------------------------------------------------
# transcript parsing
# --------------------------------------------------------------------------

def parse_session(jsonl_path: Path) -> dict:
    """Fold a transcript down to its latest usage record.

    Deduplicates by message id: a streamed assistant message can appear more
    than once, and counting it twice inflates ``cumulative_output_tokens``.
    """
    seen_ids: set[str] = set()
    latest_usage: dict | None = None
    latest_model: str = "unknown"
    cumulative_output: int = 0
    turns_count: int = 0

    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "assistant":
                continue

            message = entry.get("message")
            if not message:
                continue

            msg_id = message.get("id")
            if not msg_id:
                continue

            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            usage = message.get("usage")
            if not usage:
                continue

            turns_count += 1
            cumulative_output += usage.get("output_tokens", 0)
            latest_usage = usage
            latest_model = message.get("model", latest_model)

    return {
        "latest_usage": latest_usage,
        "model": latest_model,
        "cumulative_output": cumulative_output,
        "turns_count": turns_count,
    }


def _measure(parsed: dict) -> dict | None:
    """Turn a parsed transcript into the public token/fill block."""
    latest_usage = parsed["latest_usage"]
    if not latest_usage:
        return None

    model = parsed["model"]
    context_window, window_source = resolve_context_window(model)

    input_tokens = latest_usage.get("input_tokens", 0)
    cache_creation = latest_usage.get("cache_creation_input_tokens", 0)
    cache_read = latest_usage.get("cache_read_input_tokens", 0)
    output_tokens = latest_usage.get("output_tokens", 0)
    # Prompt-side tokens only: this is what occupies the window on the NEXT
    # turn's request. ``output_tokens`` is reported separately, not added.
    effective = input_tokens + cache_creation + cache_read

    fill_percent = round((effective / context_window) * 100, 1) if context_window else 0
    remaining = max(0, context_window - effective)

    return {
        "model": model,
        "context_window": context_window,
        "context_window_source": window_source,
        "latest_turn": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "effective_context_tokens": effective,
            "output_tokens": output_tokens,
        },
        "cumulative_output_tokens": parsed["cumulative_output"],
        "fill_percent": fill_percent,
        "remaining_tokens": remaining,
        "turns_count": parsed["turns_count"],
    }


# --------------------------------------------------------------------------
# subagents
# --------------------------------------------------------------------------

def subagent_dir_for(jsonl_path: Path) -> Path:
    """``…/<session-id>.jsonl`` → ``…/<session-id>/subagents``."""
    return jsonl_path.parent / jsonl_path.stem / "subagents"


def collect_subagents(jsonl_path: Path) -> list[dict]:
    """Measure every subagent transcript belonging to this session.

    This is the half a subagent cannot do for itself. A supervisor reading its
    own usage also gets its chain's, which is what makes the "your subagent is
    at 173 K" case visible at all.
    """
    sub_dir = subagent_dir_for(jsonl_path)
    if not sub_dir.is_dir():
        return []

    out: list[dict] = []
    for agent_jsonl in sorted(sub_dir.glob("agent-*.jsonl")):
        try:
            parsed = parse_session(agent_jsonl)
        except OSError:
            continue
        measured = _measure(parsed)
        if measured is None:
            continue

        agent_id = agent_jsonl.stem
        row = {
            "agent_id": agent_id,
            "model": measured["model"],
            "context_window": measured["context_window"],
            "context_window_source": measured["context_window_source"],
            "effective_context_tokens": measured["latest_turn"]["effective_context_tokens"],
            "fill_percent": measured["fill_percent"],
            "turns_count": measured["turns_count"],
        }

        meta_path = agent_jsonl.parent / f"{agent_id}.meta.json"
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                row["agent_type"] = meta.get("agentType")
                row["description"] = meta.get("description")
                if meta.get("name"):
                    row["name"] = meta["name"]
            except (OSError, json.JSONDecodeError):
                pass

        out.append(row)

    out.sort(key=lambda r: r["effective_context_tokens"], reverse=True)
    return out


# --------------------------------------------------------------------------
# the tool body
# --------------------------------------------------------------------------

WARNING_SUBAGENTS = (
    "These are the MAIN SESSION's numbers. This server is one process per "
    "session and tool calls carry no caller identity, so if you are a subagent "
    "these are your PARENT's numbers, not yours — a subagent cannot measure "
    "itself here. This session has {n} subagent transcript(s); see the "
    "`subagents` array for each one's own measured usage."
)

WARNING_FALLBACK_WINDOW = (
    "`context_window` for model '{model}' is the fallback constant "
    "({window}), not a measured or configured value — no pattern in "
    "models.json or CONTEXT_WINDOW_MAP matched. Treat `fill_percent` as "
    "unreliable and add the model to models.json."
)

WARNING_FALLBACK_SESSION = (
    "Session could not be resolved from CLAUDE_CODE_SESSION_ID; these numbers "
    "come from the most recently modified transcript in this project, which "
    "may belong to a different session entirely."
)


def get_context_usage() -> str:
    """Build the JSON payload for the ``get_context_usage`` tool."""
    jsonl_path, resolution = find_session_jsonl()
    if not jsonl_path:
        return json.dumps({"error": "Cannot find session JSONL file"})

    parsed = parse_session(jsonl_path)
    measured = _measure(parsed)
    if measured is None:
        return json.dumps({"error": "No assistant turns found in session"})

    subagents = collect_subagents(jsonl_path)

    warnings: list[str] = []
    if subagents:
        warnings.append(WARNING_SUBAGENTS.format(n=len(subagents)))
    if measured["context_window_source"] == "default-fallback":
        warnings.append(
            WARNING_FALLBACK_WINDOW.format(
                model=measured["model"], window=measured["context_window"]
            )
        )
    if resolution == "fallback-most-recent":
        warnings.append(WARNING_FALLBACK_SESSION)

    result = {
        "scope": "main-session",
        "caller_identified": False,
        "session_resolution": resolution,
        **measured,
        "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", jsonl_path.stem),
    }
    if subagents:
        result["subagents"] = subagents
    if warnings:
        result["warnings"] = warnings

    return json.dumps(result)
