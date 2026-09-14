# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0,<3"]
# ///
"""Context usage MCP server — exposes get_context_usage tool.

All logic lives in ``context_mcp_lib``; this file only registers the tool, so
the behaviour is testable with the repo's plain-``python3`` suite while the
``mcp`` dependency stays inside uv's environment (the ``permissions_mcp_lib``
convention).

The server reports the MAIN SESSION's usage and says so. It cannot identify its
caller — one server process serves a whole session, and tool calls carry no
agent identity — so a subagent calling this gets its parent's numbers. Rather
than implying otherwise it marks ``caller_identified: false``, warns whenever
subagents exist, and reports each subagent's own measured usage alongside.
See ``context_mcp_lib`` for the incident that motivated this.
"""

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).parent))

import context_mcp_lib as lib  # noqa: E402

mcp = MCPServer("context-usage", version="2.0.0")


@mcp.tool()
def get_context_usage() -> str:
    """Get the MAIN SESSION's context window usage, plus each subagent's own.

    Returns a JSON object with model name, context window size and where that
    size came from, token breakdown for the latest turn, cumulative output
    tokens, fill percentage, and remaining headroom.

    IMPORTANT: the numbers at the top level are the **main session's**. This
    server cannot identify its caller, so if you are a subagent they are your
    parent's numbers, not yours — read your own row in the `subagents` array
    instead, and never report a top-level `fill_percent` as your own. Check
    `context_window_source`: `default-fallback` means the denominator is a
    guess and `fill_percent` is unreliable.
    """
    return lib.get_context_usage()


if __name__ == "__main__":
    mcp.run()
