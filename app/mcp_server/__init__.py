"""MCP server exposing approved module manager tools."""

from .server import create_server, main
from .tools import MUTATING_TOOLS, READ_ONLY_TOOLS, TOOL_SPECS, ToolDispatcher, validate_arguments

__all__ = [
    "MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_SPECS",
    "ToolDispatcher",
    "create_server",
    "main",
    "validate_arguments",
]
