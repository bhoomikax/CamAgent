"""MCP server exposing the module manager as a fixed set of approved tools.

Only the operations from the structured action schema are exposed, and every
call goes through `ToolDispatcher` (app/mcp_server/tools.py), which validates
the raw arguments and routes to `ActionExecutor`, so the validator, safety
policy and confirmation gate apply exactly as they do elsewhere. There is no
tool that runs arbitrary shell commands, and tool arguments never reach a
shell.

The low-level MCP server is used deliberately: it hands the client's raw
arguments to the dispatcher without coercing types, so a value such as
``"confirm": "yes"`` is rejected rather than silently treated as true.

State-changing tools (load/unload/reload) are available only when
``CAMAGENT_MCP_ALLOW_MUTATIONS`` is set to a true value. Even then, each call
must pass ``confirm=true`` (or ``dry_run=true`` to preview), and the safety
policy can still reject it (for example, protected modules).
"""

from __future__ import annotations

import json
import os
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .tools import ToolDispatcher, ToolSpec

SERVER_NAME = "camagent"
MUTATIONS_ENV_VAR = "CAMAGENT_MCP_ALLOW_MUTATIONS"

INSTRUCTIONS = (
    "CamAgent inspects and manages Linux kernel modules. Use list_modules, "
    "get_module_info and check_module to inspect state. parse_request turns a "
    "natural-language request into a structured action without running it. "
    "If enabled, load_module, unload_module and reload_module change module "
    "state; call them with dry_run=true first, and pass confirm=true only "
    "after the user has explicitly approved the change."
)

_READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_MUTATING = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def mutations_enabled_from_env() -> bool:
    return os.environ.get(MUTATIONS_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def _to_mcp_tool(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema(),
        annotations=_MUTATING if spec.mutating else _READ_ONLY,
    )


def to_call_tool_result(response: dict[str, Any]) -> types.CallToolResult:
    """Wrap a dispatcher response as both JSON text and structured content."""
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(response, indent=2, default=str))],
        structured_content=response,
        is_error=not response["ok"],
    )


def create_server(
    dispatcher: ToolDispatcher | None = None,
    allow_mutations: bool | None = None,
) -> Server:
    """Build the MCP server. The dispatcher is injectable for testing."""
    if dispatcher is None:
        if allow_mutations is None:
            allow_mutations = mutations_enabled_from_env()
        dispatcher = ToolDispatcher(allow_mutations=allow_mutations)

    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_to_mcp_tool(spec) for spec in dispatcher.available_tools()])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # Module commands block on subprocesses; keep the event loop free.
        response = await anyio.to_thread.run_sync(dispatcher.call, params.name, params.arguments)
        return to_call_tool_result(response)

    return Server(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(run_stdio, create_server())
