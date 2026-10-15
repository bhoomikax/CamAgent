"""MCP server exposing the module manager as a fixed set of approved tools.

Only the operations from the structured action schema are exposed, and every
call goes through `ActionExecutor`, so the validator, safety policy and
confirmation gate apply exactly as they do elsewhere. There is no tool that
runs arbitrary shell commands, and tool arguments never reach a shell.

State-changing tools (load/unload/reload) are registered only when
``CAMAGENT_MCP_ALLOW_MUTATIONS`` is set to a true value. Even then, each call
must pass ``confirm=true`` (or ``dry_run=true`` to preview), and the safety
policy can still reject it (for example, protected modules).
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from app.agent import ActionExecutor
from app.llm import AnthropicProvider, IntentParser
from app.schemas import Operation

SERVER_NAME = "camagent"
MUTATIONS_ENV_VAR = "CAMAGENT_MCP_ALLOW_MUTATIONS"

READ_ONLY_TOOLS = ("list_modules", "get_module_info", "check_module", "parse_request")
MUTATING_TOOLS = ("load_module", "unload_module", "reload_module")

INSTRUCTIONS = (
    "CamAgent inspects and manages Linux kernel modules. Use list_modules, "
    "get_module_info and check_module to inspect state. parse_request turns a "
    "natural-language request into a structured action without running it. "
    "If enabled, load_module, unload_module and reload_module change module "
    "state; call them with dry_run=true first, and pass confirm=true only "
    "after the user has explicitly approved the change."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def mutations_enabled_from_env() -> bool:
    return os.environ.get(MUTATIONS_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def create_server(
    executor: ActionExecutor | None = None,
    intent_parser: IntentParser | None = None,
    allow_mutations: bool | None = None,
) -> MCPServer:
    """Build the MCP server. Dependencies are injectable for testing."""
    executor = executor or ActionExecutor()
    if allow_mutations is None:
        allow_mutations = mutations_enabled_from_env()

    parser_holder: dict[str, IntentParser] = {}
    if intent_parser is not None:
        parser_holder["parser"] = intent_parser

    def get_parser() -> IntentParser:
        # Created on first use so the server starts without LLM credentials.
        if "parser" not in parser_holder:
            parser_holder["parser"] = IntentParser(AnthropicProvider())
        return parser_holder["parser"]

    def run(operation: Operation, module: str | None = None, **options: Any) -> dict[str, Any]:
        # Pass the raw form so the executor runs full schema validation.
        raw_action = {"operation": operation.value}
        if module is not None:
            raw_action["module"] = module
        return executor.execute(raw_action, **options).to_dict()

    def run_mutation(operation: Operation, module: str, confirm: bool, dry_run: bool) -> dict[str, Any]:
        # A missing confirmation leaves the handler unset, so the executor
        # reports "confirmation_required" and never calls the manager.
        handler = (lambda _prompt: True) if confirm else None
        return run(operation, module, dry_run=dry_run, confirmation_handler=handler)

    server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS)

    @server.tool(annotations=_READ_ONLY)
    def list_modules() -> dict[str, Any]:
        """List the kernel modules that are currently loaded."""
        return run(Operation.LIST_MODULES)

    @server.tool(annotations=_READ_ONLY)
    def get_module_info(module: str) -> dict[str, Any]:
        """Show modinfo details (description, author, dependencies, parameters) for one module."""
        return run(Operation.GET_MODULE_INFO, module)

    @server.tool(annotations=_READ_ONLY)
    def check_module(module: str) -> dict[str, Any]:
        """Check whether one kernel module is currently loaded."""
        return run(Operation.CHECK_MODULE, module)

    @server.tool(annotations=_READ_ONLY)
    def parse_request(request: str) -> dict[str, Any]:
        """Translate a natural-language request into a structured module action without executing it."""
        return get_parser().parse(request).to_dict()

    if allow_mutations:

        @server.tool(annotations=_MUTATING)
        def load_module(module: str, confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
            """Load a kernel module. Requires confirm=true; use dry_run=true to preview."""
            return run_mutation(Operation.LOAD_MODULE, module, confirm, dry_run)

        @server.tool(annotations=_MUTATING)
        def unload_module(module: str, confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
            """Unload a kernel module. Requires confirm=true; use dry_run=true to preview."""
            return run_mutation(Operation.UNLOAD_MODULE, module, confirm, dry_run)

        @server.tool(annotations=_MUTATING)
        def reload_module(module: str, confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
            """Unload and reload a kernel module. Requires confirm=true; use dry_run=true to preview."""
            return run_mutation(Operation.RELOAD_MODULE, module, confirm, dry_run)

    return server


def main() -> None:
    create_server().run("stdio")
