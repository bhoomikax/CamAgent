"""MCP protocol tests: a real client talks to the server in-process."""

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from app.agent import ActionExecutor  # noqa: E402
from app.mcp_server import ToolDispatcher  # noqa: E402
from app.mcp_server.server import MUTATIONS_ENV_VAR, create_server  # noqa: E402
from app.safety import SafetyPolicy  # noqa: E402


class FakeModuleManager:
    def __init__(self):
        self.loaded = {"snd"}
        self.changes = []

    def list_modules(self):
        return [{"name": module} for module in sorted(self.loaded)]

    def get_module_info(self, module):
        return {"success": True, "name": module}

    def is_module_loaded(self, module):
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
def server(manager):
    executor = ActionExecutor(manager=manager, policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded))
    return create_server(ToolDispatcher(executor, allow_mutations=True))


def run_client(server, scenario):
    async def main():
        async with Client(server) as client:
            return await scenario(client)

    return anyio.run(main)


def test_lists_tools_with_schemas_and_annotations(server):
    async def scenario(client):
        return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in run_client(server, scenario)}

    assert set(tools) >= {"list_modules", "get_module_info", "check_module", "load_module"}
    assert tools["check_module"].annotations.read_only_hint is True
    assert tools["load_module"].annotations.destructive_hint is True
    assert tools["load_module"].input_schema["additionalProperties"] is False


def test_valid_call_returns_structured_result(server):
    async def scenario(client):
        return await client.call_tool("check_module", {"module": "snd"})

    result = run_client(server, scenario)

    assert result.is_error is False
    assert result.structured_content["status"] == "success"
    assert result.structured_content["result"] == {"loaded": True}
    assert '"status": "success"' in result.content[0].text


def test_unknown_tool_is_an_error_result(server):
    async def scenario(client):
        return await client.call_tool("run_shell", {"command": "reboot"})

    result = run_client(server, scenario)

    assert result.is_error is True
    assert result.structured_content["status"] == "unknown_tool"


def test_arguments_are_not_coerced(server, manager):
    async def scenario(client):
        return await client.call_tool("load_module", {"module": "uvcvideo", "confirm": "true"})

    result = run_client(server, scenario)

    assert result.is_error is True
    assert result.structured_content["status"] == "invalid_arguments"
    assert manager.changes == []


def test_mutation_requires_confirmation_over_protocol(server, manager):
    async def scenario(client):
        unconfirmed = await client.call_tool("load_module", {"module": "uvcvideo"})
        confirmed = await client.call_tool("load_module", {"module": "uvcvideo", "confirm": True})
        return unconfirmed, confirmed

    unconfirmed, confirmed = run_client(server, scenario)

    assert unconfirmed.structured_content["status"] == "confirmation_required"
    assert confirmed.structured_content["status"] == "success"
    assert manager.changes == [("load", "uvcvideo")]


@pytest.mark.parametrize(("value", "expected"), [("", False), ("0", False), ("1", True), ("true", True)])
def test_mutating_tools_follow_environment_setting(monkeypatch, value, expected):
    monkeypatch.setenv(MUTATIONS_ENV_VAR, value)

    async def scenario(client):
        return {tool.name for tool in (await client.list_tools()).tools}

    names = run_client(create_server(), scenario)

    assert ("load_module" in names) is expected
    assert "list_modules" in names
