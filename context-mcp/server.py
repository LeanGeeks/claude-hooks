# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0"]
# ///
"""Context usage MCP server — exposes get_context_usage tool."""

import json
import os
from fnmatch import fnmatch
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("context-usage")

DEFAULT_CONTEXT_WINDOW = 1_000_000
MODELS_JSON = Path(__file__).parent / "models.json"


def _load_bundled_models() -> dict[str, int]:
    if MODELS_JSON.exists():
        with open(MODELS_JSON) as f:
            return json.load(f)
    return {}


def _resolve_context_window(model: str) -> int:
    env_map = os.environ.get("CONTEXT_WINDOW_MAP", "")
    if env_map:
        for pair in env_map.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            pattern, tokens_str = pair.split("=", 1)
            if fnmatch(model, pattern.strip()):
                return int(tokens_str.strip())

    bundled = _load_bundled_models()
    for pattern, tokens in bundled.items():
        if fnmatch(model, pattern):
            return tokens

    return DEFAULT_CONTEXT_WINDOW


def _find_session_jsonl() -> Path | None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD")

    if session_id and project_dir:
        encoded = project_dir.replace("/", "-")
        jsonl_path = Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
        if jsonl_path.exists():
            return jsonl_path

    if project_dir:
        encoded = project_dir.replace("/", "-")
        project_jsonl_dir = Path.home() / ".claude" / "projects" / encoded
        if project_jsonl_dir.is_dir():
            jsonl_files = sorted(
                project_jsonl_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if jsonl_files:
                return jsonl_files[0]

    return None


def _parse_session(jsonl_path: Path) -> dict:
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


@mcp.tool()
def get_context_usage() -> str:
    """Get current session's context window usage (tokens consumed, fill percentage, remaining capacity).

    Returns a JSON object with model name, context window size, token breakdown
    for the latest turn, cumulative output tokens, fill percentage, and remaining
    headroom. Use this to pace work, decide whether to summarize, or avoid
    exceeding context limits.
    """
    jsonl_path = _find_session_jsonl()
    if not jsonl_path:
        return json.dumps({"error": "Cannot find session JSONL file"})

    parsed = _parse_session(jsonl_path)
    latest_usage = parsed["latest_usage"]

    if not latest_usage:
        return json.dumps({"error": "No assistant turns found in session"})

    model = parsed["model"]
    context_window = _resolve_context_window(model)

    input_tokens = latest_usage.get("input_tokens", 0)
    cache_creation = latest_usage.get("cache_creation_input_tokens", 0)
    cache_read = latest_usage.get("cache_read_input_tokens", 0)
    output_tokens = latest_usage.get("output_tokens", 0)
    effective = input_tokens + cache_creation + cache_read

    fill_percent = round((effective / context_window) * 100, 1) if context_window else 0
    remaining = max(0, context_window - effective)

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", jsonl_path.stem)

    result = {
        "model": model,
        "context_window": context_window,
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
        "session_id": session_id,
    }
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run()
