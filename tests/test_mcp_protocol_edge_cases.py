"""MCP protocol edge cases: a real in-process MCP client talks to the server.

The server is built with an injected dispatcher over a fake module manager, so
no Linux command runs and no module changes state. These tests check what a
client actually receives over the protocol for invalid, disabled, rejected and
failing calls, and that it matches the dispatcher's documented envelope.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from app.agent import ActionExecutor  # noqa: E402
from app.mcp_server import MUTATING_TOOLS, READ_ONLY_TOOLS, TOOL_SPECS, ToolDispatcher  # noqa: E402
from app.mcp_server.server import (  # noqa: E402
    INSTRUCTIONS,
    MUTATIONS_ENV_VAR,
    SERVER_NAME,
    create_server,
    mutations_enabled_from_env,
    to_call_tool_result,
)
from app.safety import DEFAULT_PROTECTED_MODULES, SafetyPolicy  # noqa: E402

RESPONSE_KEYS = {"ok", "tool", "status", "message", "action", "result", "safety", "errors"}


class FakeModuleManager:
    def __init__(self):
        self.loaded = {"snd", "ext4"}
        self.changes = []
        self.error = None

    def _fail(self):
        if self.error is not None:
            raise self.error

    def list_modules(self):
        self._fail()
        return [{"name": module} for module in sorted(self.loaded)]

    def get_module_info(self, module):
        self._fail()
        return {"success": True, "name": module}

    def is_module_loaded(self, module):
        self._fail()
        return module in self.loaded

    def load_module(self, module):
        self.changes.append(("load", module))
        self.loaded.add(module)
        return {"success": True}

    def unload_module(self, module):
        self.changes.append(("unload", module))
        self.loaded.discard(module)
        return {"success": True}

    def reload_module(self, module):
        self.changes.append(("reload", module))
        return {"success": True}


@pytest.fixture
def manager():
    return FakeModuleManager()


@pytest.fixture
def make_server(manager):
    def make(allow_mutations=True):
        executor = ActionExecutor(manager=manager, policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded))
        return create_server(ToolDispatcher(executor, allow_mutations=allow_mutations))

    return make


@pytest.fixture
def server(make_server):
    return make_server(allow_mutations=True)


@pytest.fixture
def read_only_server(make_server):
    return make_server(allow_mutations=False)


def run_client(server, scenario):
    async def main():
        async with Client(server) as client:
            return await scenario(client)

    return anyio.run(main)


def call(server, tool, arguments):
    async def scenario(client):
        return await client.call_tool(tool, arguments)

    return run_client(server, scenario)


def list_tools(server):
    async def scenario(client):
        return (await client.list_tools()).tools

    return run_client(server, scenario)


def assert_envelope(result, tool, status):
    body = result.structured_content
    assert set(body) == RESPONSE_KEYS
    assert body["tool"] == tool
    assert body["status"] == status
    assert result.is_error is (not body["ok"])
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert json.loads(result.content[0].text) == body
    return body


# --------------------------------------------------------------------------- #
# Tool listing
# --------------------------------------------------------------------------- #


def test_listed_schemas_and_annotations_match_the_catalog(server):
    tools = {tool.name: tool for tool in list_tools(server)}

    assert list(tools) == list(TOOL_SPECS)
    for name, spec in TOOL_SPECS.items():
        tool = tools[name]
        assert tool.description == spec.description
        assert tool.input_schema == spec.input_schema()
        assert tool.annotations.read_only_hint is (not spec.mutating)
        assert tool.annotations.destructive_hint is spec.mutating
        assert tool.annotations.open_world_hint is False


def test_disabled_server_lists_only_read_only_tools(read_only_server):
    tools = list_tools(read_only_server)

    assert [tool.name for tool in tools] == list(READ_ONLY_TOOLS)
    assert all(tool.annotations.read_only_hint for tool in tools)


def test_server_identifies_itself_and_explains_the_tools(server):
    async def scenario(client):
        return client.server_info, client.instructions

    info, instructions = run_client(server, scenario)

    assert info.name == SERVER_NAME
    assert instructions == INSTRUCTIONS
    for tool in TOOL_SPECS:
        assert tool in INSTRUCTIONS


# --------------------------------------------------------------------------- #
# Invalid calls over the protocol
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "arguments", "errors"),
    [
        ("check_module", {}, ["missing required argument 'module'"]),
        ("get_module_info", None, ["missing required argument 'module'"]),
        ("list_modules", {"module": "snd"}, ["unexpected argument(s): ['module']"]),
        ("check_module", {"module": "snd", "force": True}, ["unexpected argument(s): ['force']"]),
        ("check_module", {"module": 5}, ["argument 'module' must be a str, got int"]),
        ("check_module", {"module": None}, ["argument 'module' must be a str, got NoneType"]),
        ("check_module", {"module": ["snd"]}, ["argument 'module' must be a str, got list"]),
        ("load_module", {"module": "uvcvideo", "confirm": "true"}, ["argument 'confirm' must be a bool, got str"]),
        ("load_module", {"module": "uvcvideo", "confirm": "yes"}, ["argument 'confirm' must be a bool, got str"]),
        ("load_module", {"module": "uvcvideo", "confirm": 1}, ["argument 'confirm' must be a bool, got int"]),
        ("load_module", {"module": "uvcvideo", "confirm": None}, ["argument 'confirm' must be a bool, got NoneType"]),
        ("load_module", {"module": "uvcvideo", "dry_run": "false"}, ["argument 'dry_run' must be a bool, got str"]),
        ("load_module", {"module": "uvcvideo", "dry_run": 0}, ["argument 'dry_run' must be a bool, got int"]),
        (
            "load_module",
            {"module": "uvcvideo", "confirm": True, "dry_run": True},
            ["'confirm' and 'dry_run' cannot both be true"],
        ),
        ("check_module", {"module": "snd; reboot"}, ["module name contains forbidden character(s): [' ', ';']"]),
        ("check_module", {"module": "../evil"}, None),
        ("check_module", {"module": "-r"}, ["module name must not start with '-' (looks like a command flag)"]),
        ("parse_request", {"request": ""}, ["argument 'request' must not be empty"]),
        ("parse_request", {"request": 7}, ["argument 'request' must be a str, got int"]),
        ("parse_request", {}, ["missing required argument 'request'"]),
    ],
)
def test_invalid_arguments_are_error_results_not_protocol_failures(server, manager, tool, arguments, errors):
    result = call(server, tool, arguments)

    body = assert_envelope(result, tool, "invalid_arguments")
    assert result.is_error is True
    assert body["message"] == f"Invalid arguments for tool '{tool}'."
    if errors is not None:
        assert body["errors"] == errors
    else:
        assert body["errors"]
    assert body["action"] is None and body["result"] is None and body["safety"] is None
    assert manager.changes == []


def test_unknown_tool_lists_available_tools(read_only_server):
    result = call(read_only_server, "rmmod", {"module": "snd"})

    body = assert_envelope(result, "rmmod", "unknown_tool")
    assert body["message"] == "Unknown tool 'rmmod'."
    assert body["errors"] == [f"available tools: {list(READ_ONLY_TOOLS)}"]


# --------------------------------------------------------------------------- #
# State-changing tools when disabled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
@pytest.mark.parametrize(
    "arguments",
    [{"module": "uvcvideo", "confirm": True}, {"module": "uvcvideo", "dry_run": True}, {"module": "uvcvideo"}, {}],
)
def test_hidden_mutating_tools_are_refused_over_the_protocol(read_only_server, manager, tool, arguments):
    result = call(read_only_server, tool, arguments)

    body = assert_envelope(result, tool, "tool_disabled")
    assert result.is_error is True
    assert body["message"] == f"Tool {tool!r} is disabled; module changes are not enabled on this server."
    assert body["errors"] == []
    assert manager.changes == []
    assert manager.loaded == {"snd", "ext4"}


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("list_modules", {}, [{"name": "ext4"}, {"name": "snd"}]),
        ("get_module_info", {"module": "snd"}, {"success": True, "name": "snd"}),
        ("check_module", {"module": "snd"}, {"loaded": True}),
        ("check_module", {"module": "uvcvideo"}, {"loaded": False}),
    ],
)
def test_read_only_tools_still_work_when_mutations_are_disabled(read_only_server, manager, tool, arguments, expected):
    result = call(read_only_server, tool, arguments)

    body = assert_envelope(result, tool, "success")
    assert result.is_error is False
    assert body["result"] == expected
    assert body["safety"] == {"risk_level": "read_only", "requires_confirmation": False, "confirmed": False}
    assert manager.changes == []


# --------------------------------------------------------------------------- #
# Confirmation, protected modules and policy over the protocol
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "module"),
    [("load_module", "uvcvideo"), ("unload_module", "snd"), ("reload_module", "snd")],
)
def test_missing_confirmation_blocks_every_mutating_tool(server, manager, tool, module):
    result = call(server, tool, {"module": module})

    body = assert_envelope(result, tool, "confirmation_required")
    assert result.is_error is True
    assert body["errors"] == ["explicit confirmation is required"]
    assert body["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": False}
    assert manager.changes == []


def test_dry_run_is_a_successful_preview(server, manager):
    result = call(server, "unload_module", {"module": "snd", "dry_run": True})

    body = assert_envelope(result, "unload_module", "dry_run")
    assert result.is_error is False
    assert body["result"]["would_execute"] is True
    assert manager.changes == []
    assert "snd" in manager.loaded


@pytest.mark.parametrize("module", sorted(DEFAULT_PROTECTED_MODULES))
@pytest.mark.parametrize("tool", ["unload_module", "reload_module"])
def test_protected_modules_are_rejected_even_when_confirmed(server, manager, module, tool):
    manager.loaded.add(module)

    result = call(server, tool, {"module": module, "confirm": True})

    body = assert_envelope(result, tool, "rejected")
    assert result.is_error is True
    assert body["errors"] == [f"module {module!r} is protected from {tool}"]
    assert manager.changes == []


@pytest.mark.parametrize(
    ("tool", "module", "error"),
    [
        ("load_module", "snd", "module 'snd' is already loaded"),
        ("unload_module", "uvcvideo", "module 'uvcvideo' is not loaded"),
    ],
)
def test_state_conflicts_are_rejected_over_the_protocol(server, manager, tool, module, error):
    result = call(server, tool, {"module": module, "confirm": True})

    body = assert_envelope(result, tool, "rejected")
    assert body["errors"] == [error]
    assert manager.changes == []


def test_confirmed_mutation_runs_once_and_is_verified(server, manager):
    result = call(server, "load_module", {"module": "uvcvideo", "confirm": True})

    body = assert_envelope(result, "load_module", "success")
    assert result.is_error is False
    assert body["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": True}
    assert body["result"]["verification"]["passed"] is True
    assert manager.changes == [("load", "uvcvideo")]


# --------------------------------------------------------------------------- #
# Execution failures over the protocol
# --------------------------------------------------------------------------- #


def test_manager_failure_is_an_error_result(server, manager):
    manager.error = RuntimeError("lsmod: command failed")

    result = call(server, "list_modules", {})

    body = assert_envelope(result, "list_modules", "error")
    assert result.is_error is True
    assert body["message"] == "Module manager operation failed."
    assert body["errors"] == ["RuntimeError: lsmod: command failed"]
    assert body["result"] is None


def test_permission_failure_is_a_permission_error_result(server, manager):
    manager.error = PermissionError("lsmod: Permission denied")

    result = call(server, "check_module", {"module": "snd"})

    body = assert_envelope(result, "check_module", "permission_error")
    assert result.is_error is True
    assert body["errors"] == ["lsmod: Permission denied"]


def test_permission_failure_during_state_check_blocks_the_mutation(server, manager):
    manager.error = PermissionError("lsmod: Permission denied")

    result = call(server, "load_module", {"module": "uvcvideo", "confirm": True})

    body = assert_envelope(result, "load_module", "permission_error")
    assert body["message"] == "Permission denied while checking module state."
    assert manager.changes == []


def test_unexpected_exceptions_are_internal_errors_without_details(manager):
    manager.error = ValueError("secret detail")
    executor = ActionExecutor(manager=manager, policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded))
    server = create_server(ToolDispatcher(executor))

    result = call(server, "list_modules", {})

    body = assert_envelope(result, "list_modules", "internal_error")
    assert result.is_error is True
    assert body["errors"] == ["internal error"]
    assert "secret" not in result.content[0].text


# --------------------------------------------------------------------------- #
# Result wrapping and configuration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ok", [True, False])
def test_call_tool_result_mirrors_the_dispatcher_response(ok):
    response = {
        "ok": ok,
        "tool": "check_module",
        "status": "success" if ok else "error",
        "message": "m",
        "action": {"operation": "check_module", "module": "snd"},
        "result": {"loaded": True},
        "safety": {"risk_level": "read_only", "requires_confirmation": False, "confirmed": False},
        "errors": [] if ok else ["boom"],
    }

    result = to_call_tool_result(response)

    assert result.is_error is (not ok)
    assert result.structured_content == response
    assert [content.type for content in result.content] == ["text"]
    assert json.loads(result.content[0].text) == response


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("On", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("2", False),
        ("enabled", False),
        ("t", False),
        ("y", False),
    ],
)
def test_mutation_switch_accepts_only_documented_true_values(monkeypatch, value, expected):
    monkeypatch.setenv(MUTATIONS_ENV_VAR, value)

    assert mutations_enabled_from_env() is expected


def test_mutation_switch_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv(MUTATIONS_ENV_VAR, raising=False)

    assert mutations_enabled_from_env() is False


def test_explicit_allow_mutations_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(MUTATIONS_ENV_VAR, "1")

    names = {tool.name for tool in list_tools(create_server(allow_mutations=False))}

    assert names == set(READ_ONLY_TOOLS)
