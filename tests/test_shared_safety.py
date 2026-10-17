"""Cross-interface checks for the CLI and MCP shared safety executor."""

import json
from typing import Any

import pytest

from app import cli, orchestrator
from app.agent import ActionExecutor
from app.mcp_server import ToolDispatcher
from app.safety import SafetyPolicy


class FakeModuleManager:
    def __init__(self) -> None:
        self.loaded = {"ext4", "snd"}
        self.calls: list[tuple[str, str | None]] = []

    def list_modules(self) -> list[dict[str, Any]]:
        self.calls.append(("list_modules", None))
        return [{"name": name} for name in sorted(self.loaded)]

    def get_module_info(self, module: str) -> dict[str, Any]:
        self.calls.append(("get_module_info", module))
        return {"success": True, "name": module}

    def is_module_loaded(self, module: str) -> bool:
        self.calls.append(("check_state", module))
        return module in self.loaded

    def load_module(self, module: str) -> dict[str, Any]:
        self.calls.append(("load_module", module))
        self.loaded.add(module)
        return {"success": True, "operation": "load_module", "module": module}

    def unload_module(self, module: str) -> dict[str, Any]:
        self.calls.append(("unload_module", module))
        self.loaded.discard(module)
        return {"success": True, "operation": "unload_module", "module": module}

    def reload_module(self, module: str) -> dict[str, Any]:
        self.calls.append(("reload_module", module))
        return {"success": True, "operation": "reload_module", "module": module}

    @property
    def mutations(self) -> list[tuple[str, str | None]]:
        return [call for call in self.calls if call[0] in {"load_module", "unload_module", "reload_module"}]


def _run_cli(request: dict[str, Any], *answers: str) -> tuple[str, dict[str, Any]]:
    output: list[str] = []
    responses = iter([json.dumps(request), *answers, "exit"])
    cli.run_cli(input_fn=lambda _prompt: next(responses), output_fn=output.append)
    labels = ("SUCCESS:", "FAILURE:", "PERMISSION ERROR:", "VALIDATION REJECTED:", "DECLINED:")
    rendered = next(item for item in output if item.startswith(labels))
    summary, details = rendered.split("\n", 1)
    return summary, json.loads(details)


@pytest.fixture
def interfaces(monkeypatch):
    manager = FakeModuleManager()
    executor = ActionExecutor(
        manager=manager,
        policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded),
    )
    monkeypatch.setattr(orchestrator, "ActionExecutor", lambda: executor)
    mcp = ToolDispatcher(executor=executor, allow_mutations=True)
    return manager, mcp


@pytest.mark.parametrize("module", ["../unsafe", "snd;reboot", "$(id)"])
def test_cli_and_mcp_both_reject_unsafe_module_names(interfaces, module):
    manager, mcp = interfaces

    cli_summary, cli_result = _run_cli({"command": "load", "module": module})
    mcp_result = mcp.call("load_module", {"module": module, "confirm": True})

    assert cli_summary.startswith("VALIDATION REJECTED:")
    assert cli_result["success"] is False
    assert mcp_result["ok"] is False
    assert mcp_result["status"] == "invalid_arguments"
    assert manager.mutations == []


@pytest.mark.parametrize(
    ("command", "tool", "module", "reason"),
    [
        ("unload", "unload_module", "ext4", "protected"),
        ("load", "load_module", "snd", "already loaded"),
        ("unload", "unload_module", "uvcvideo", "not loaded"),
    ],
)
def test_cli_and_mcp_apply_the_same_state_policy(interfaces, command, tool, module, reason):
    manager, mcp = interfaces

    cli_summary, cli_result = _run_cli({"command": command, "module": module})
    mcp_result = mcp.call(tool, {"module": module, "confirm": True})

    assert cli_summary.startswith("VALIDATION REJECTED:")
    assert mcp_result["status"] == "rejected"
    assert reason in cli_result["error"]
    assert any(reason in error for error in mcp_result["errors"])
    assert manager.mutations == []


@pytest.mark.parametrize(
    ("command", "tool", "module"),
    [
        ("list", "list_modules", None),
        ("info", "get_module_info", "snd"),
        ("loaded", "check_module", "snd"),
    ],
)
def test_read_only_operations_work_through_cli_and_mcp(interfaces, command, tool, module):
    manager, mcp = interfaces
    cli_request = {"command": command}
    mcp_arguments = {}
    if module is not None:
        cli_request["module"] = module
        mcp_arguments["module"] = module

    cli_summary, cli_result = _run_cli(cli_request)
    mcp_result = mcp.call(tool, mcp_arguments)

    assert cli_summary.startswith("SUCCESS:")
    assert cli_result["success"] is True
    assert mcp_result["ok"] is True
    assert mcp_result["status"] == "success"
    assert manager.mutations == []


def test_cli_decline_and_mcp_missing_confirmation_both_block_mutation(interfaces):
    manager, mcp = interfaces

    cli_summary, cli_result = _run_cli({"command": "load", "module": "uvcvideo"}, "n")
    mcp_result = mcp.call("load_module", {"module": "uvcvideo"})

    assert cli_result["success"] is False
    assert cli_summary.startswith("DECLINED:")
    assert mcp_result["ok"] is False
    assert mcp_result["status"] == "confirmation_required"
    assert manager.mutations == []


@pytest.mark.parametrize(
    ("command", "tool", "module", "initially_loaded", "expected_loaded"),
    [
        ("load", "load_module", "uvcvideo", False, True),
        ("unload", "unload_module", "snd", True, False),
        ("reload", "reload_module", "snd", True, True),
    ],
)
def test_both_interfaces_execute_a_confirmed_state_change(
    interfaces, command, tool, module, initially_loaded, expected_loaded
):
    manager, mcp = interfaces

    cli_summary, cli_result = _run_cli({"command": command, "module": module}, "yes")
    assert cli_summary.startswith("SUCCESS:")
    assert cli_result["verification"]["passed"] is True
    assert (module in manager.loaded) is expected_loaded
    assert manager.mutations == [(tool, module)]

    if initially_loaded:
        manager.loaded.add(module)
    else:
        manager.loaded.discard(module)
    manager.calls.clear()
    mcp_result = mcp.call(tool, {"module": module, "confirm": True})
    assert mcp_result["status"] == "success"
    assert mcp_result["result"]["verification"]["passed"] is True
    assert (module in manager.loaded) is expected_loaded
    assert manager.mutations == [(tool, module)]
