"""MCP server exposing approved module manager tools.

The protocol server lives in `app.mcp_server.server` and needs the `mcp`
package; the dispatch layer exported here does not.
"""

from .tools import MUTATING_TOOLS, READ_ONLY_TOOLS, TOOL_SPECS, ToolDispatcher, validate_arguments

__all__ = [
    "MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_SPECS",
    "ToolDispatcher",
    "validate_arguments",
]
