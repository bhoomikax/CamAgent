import json
import subprocess
from typing import Any

import pytest

from app.agent import ActionExecutor
from app.llm import IntentParser
from app.manager import module_manager
from app.mcp_server import MUTATING_TOOLS, READ_ONLY_TOOLS, TOOL_SPECS, ToolDispatcher
from app.mcp_server.tools import CheckedModuleManager
from app.safety import SafetyPolicy

RESPONSE_KEYS = {"ok", "tool", "status", "message", "action", "result", "safety", "errors"}


class FakeModuleManager:
    def __init__(self) -> None:
        self.loaded = {"snd", "ext4"}
        self.calls: list[tuple[str, str | None]] = []
        self.error: Exception | None = None

    def _record(self, name: str, module: str | None = None) -> None:
        self.calls.append((name, module))
        if self.error is not None:
            raise self.error

    def list_modules(self) -> list[dict[str, Any]]:
        self._record("list_modules")
        return [{"name": module} for module in sorted(self.loaded)]

    def get_module_info(self, module: str) -> dict[str, Any]:
        self._record("get_module_info", module)
        return {"success": True, "name": module}

    def is_module_loaded(self, module: str) -> bool:
        self._record("is_module_loaded", module)
        return module in self.loaded

    def load_module(self, module: str) -> dict[str, Any]:
        self._record("load_module", module)
        self.loaded.add(module)
        return {"success": True, "operation": "load"}

    def unload_module(self, module: str) -> dict[str, Any]:
        self._record("unload_module", module)
        self.loaded.discard(module)
        return {"success": True, "operation": "unload"}

    def reload_module(self, module: str) -> dict[str, Any]:
        self._record("reload_module", module)
        return {"success": True, "operation": "reload"}

    def state_changes(self) -> list[tuple[str, str | None]]:
        return [call for call in self.calls if call[0] in {"load_module", "unload_module", "reload_module"}]


class FakeProvider:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, system, prompt, response_schema=None):
        self.prompts.append(prompt)
        return json.dumps(self.reply)


@pytest.fixture
def manager():
    return FakeModuleManager()


@pytest.fixture
def make_dispatcher(manager):
    def make(allow_mutations=True, intent_parser=None):
        executor = ActionExecutor(
            manager=manager,
            policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded),
        )
        return ToolDispatcher(executor, intent_parser=intent_parser, allow_mutations=allow_mutations)

    return make


@pytest.fixture
def dispatcher(make_dispatcher):
    return make_dispatcher()


def test_tool_catalog_exposes_only_approved_tools():
    assert set(TOOL_SPECS) == {
        "list_modules",
        "get_module_info",
        "check_module",
        "parse_request",
        "load_module",
        "unload_module",
        "reload_module",
    }
    assert set(MUTATING_TOOLS) == {"load_module", "unload_module", "reload_module"}
    assert not any("shell" in name or "exec" in name for name in TOOL_SPECS)


@pytest.mark.parametrize("spec", TOOL_SPECS.values(), ids=lambda spec: spec.name)
def test_input_schemas_are_closed(spec):
    schema = spec.input_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])


def test_mutating_tools_are_hidden_unless_enabled(make_dispatcher):
    assert [spec.name for spec in make_dispatcher(allow_mutations=False).available_tools()] == list(READ_ONLY_TOOLS)
    assert {spec.name for spec in make_dispatcher(allow_mutations=True).available_tools()} == set(TOOL_SPECS)


@pytest.mark.parametrize(
    ("tool", "arguments", "result"),
    [
        ("list_modules", {}, [{"name": "ext4"}, {"name": "snd"}]),
        ("list_modules", None, [{"name": "ext4"}, {"name": "snd"}]),
        ("get_module_info", {"module": "snd"}, {"success": True, "name": "snd"}),
        ("check_module", {"module": "snd"}, {"loaded": True}),
        ("check_module", {"module": "uvcvideo"}, {"loaded": False}),
    ],
)
def test_read_only_tools_return_structured_results(dispatcher, tool, arguments, result):
    response = dispatcher.call(tool, arguments)

    assert set(response) == RESPONSE_KEYS
    assert response["ok"] is True
    assert response["tool"] == tool
    assert response["status"] == "success"
    assert response["result"] == result
    assert response["safety"]["risk_level"] == "read_only"
    assert response["errors"] == []
    json.dumps(response)


def test_module_names_are_canonicalised(dispatcher, manager):
    response = dispatcher.call("check_module", {"module": "snd-hda-intel"})

    assert response["action"] == {"operation": "check_module", "module": "snd_hda_intel"}
    assert ("is_module_loaded", "snd_hda_intel") in manager.calls


@pytest.mark.parametrize("name", ["run_shell", "exec", "", "LIST_MODULES", None, 42])
def test_unknown_tools_are_rejected(dispatcher, manager, name):
    response = dispatcher.call(name, {})

    assert set(response) == RESPONSE_KEYS
    assert response["ok"] is False
    assert response["status"] == "unknown_tool"
    assert manager.calls == []


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
def test_disabled_mutating_tools_are_rejected(make_dispatcher, manager, tool):
    response = make_dispatcher(allow_mutations=False).call(tool, {"module": "snd", "confirm": True})

    assert response["status"] == "tool_disabled"
    assert manager.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments", "error"),
    [
        ("check_module", {}, "missing required argument 'module'"),
        ("check_module", {"module": "snd", "force": True}, "unexpected argument(s): ['force']"),
        ("list_modules", {"module": "snd"}, "unexpected argument(s): ['module']"),
        ("check_module", {"module": 5}, "argument 'module' must be a str, got int"),
        ("check_module", {"module": None}, "argument 'module' must be a str, got NoneType"),
        ("check_module", {"module": ["snd"]}, "argument 'module' must be a str, got list"),
        ("load_module", {"module": "snd", "confirm": "yes"}, "argument 'confirm' must be a bool, got str"),
        ("load_module", {"module": "snd", "confirm": 1}, "argument 'confirm' must be a bool, got int"),
        ("load_module", {"module": "snd", "dry_run": "false"}, "argument 'dry_run' must be a bool, got str"),
        ("load_module", {"module": "snd", "confirm": True, "dry_run": True}, "'confirm' and 'dry_run' cannot both be true"),
        ("parse_request", {"request": "  "}, "argument 'request' must not be empty"),
        ("parse_request", {"request": "x" * 1000}, "argument 'request' must be at most 500 characters"),
        ("check_module", {"module": ""}, "module name must not be empty"),
    ],
)
def test_invalid_arguments_are_rejected(dispatcher, manager, tool, arguments, error):
    response = dispatcher.call(tool, arguments)

    assert set(response) == RESPONSE_KEYS
    assert response["ok"] is False
    assert response["status"] == "invalid_arguments"
    assert error in response["errors"]
    assert manager.calls == []


@pytest.mark.parametrize("arguments", ["module=snd", ["snd"], 42])
def test_non_object_arguments_are_rejected(dispatcher, arguments):
    response = dispatcher.call("check_module", arguments)

    assert response["status"] == "invalid_arguments"


@pytest.mark.parametrize(
    "module",
    ["snd; rm -rf /", "$(reboot)", "snd|cat", "../../lib/evil", "-r", "--help", "snd\nevil", "a" * 80],
)
def test_unsafe_module_names_are_rejected(dispatcher, manager, module):
    for tool, extra in (("get_module_info", {}), ("load_module", {"confirm": True})):
        response = dispatcher.call(tool, {"module": module, **extra})

        assert response["status"] == "invalid_arguments"
    assert manager.calls == []


@pytest.mark.parametrize(
    ("tool", "module"),
    [("load_module", "uvcvideo"), ("unload_module", "snd"), ("reload_module", "snd")],
)
def test_mutations_without_confirmation_are_not_executed(dispatcher, manager, tool, module):
    response = dispatcher.call(tool, {"module": module})

    assert response["ok"] is False
    assert response["status"] == "confirmation_required"
    assert response["safety"]["requires_confirmation"] is True
    assert manager.state_changes() == []


def test_dry_run_previews_without_executing(dispatcher, manager):
    response = dispatcher.call("unload_module", {"module": "snd", "dry_run": True})

    assert response["ok"] is True
    assert response["status"] == "dry_run"
    assert response["result"]["would_execute"] is True
    assert manager.state_changes() == []


def test_confirmed_mutation_is_executed(dispatcher, manager):
    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["ok"] is True
    assert response["status"] == "success"
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": True}
    assert manager.state_changes() == [("load_module", "uvcvideo")]


@pytest.mark.parametrize(
    ("tool", "module", "reason"),
    [
        ("unload_module", "ext4", "protected"),
        ("reload_module", "ext4", "protected"),
        ("load_module", "snd", "already loaded"),
        ("unload_module", "uvcvideo", "not loaded"),
    ],
)
def test_safety_policy_rejections_apply_even_when_confirmed(dispatcher, manager, tool, module, reason):
    response = dispatcher.call(tool, {"module": module, "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "rejected"
    assert any(reason in error for error in response["errors"])
    assert manager.state_changes() == []


def test_manager_failures_are_reported_as_errors(dispatcher, manager):
    manager.error = RuntimeError("lsmod failed")

    response = dispatcher.call("list_modules", {})

    assert response["ok"] is False
    assert response["status"] == "error"
    assert response["errors"] == ["RuntimeError: lsmod failed"]


def test_unexpected_exceptions_do_not_leak_details(manager):
    class BrokenExecutor:
        def execute(self, *args, **kwargs):
            raise KeyError("secret internal state")

    response = ToolDispatcher(BrokenExecutor()).call("list_modules", {})

    assert response["status"] == "internal_error"
    assert "secret" not in json.dumps(response)


def test_parse_request_returns_action_without_executing(make_dispatcher, manager):
    provider = FakeProvider(
        {"status": "action", "action": {"operation": "unload_module", "module": "uvcvideo"}, "reason": "x"}
    )
    dispatcher = make_dispatcher(intent_parser=IntentParser(provider))

    response = dispatcher.call("parse_request", {"request": "unload the webcam driver"})

    assert set(response) == RESPONSE_KEYS
    assert response["ok"] is True
    assert response["status"] == "parsed"
    assert response["action"] == {"operation": "unload_module", "module": "uvcvideo"}
    assert response["result"] == {"executed": False}
    assert provider.prompts == ["unload the webcam driver"]
    assert manager.calls == []


def test_parse_request_reports_unsafe_requests(make_dispatcher, manager):
    provider = FakeProvider({"status": "unsupported", "action": None, "reason": "shell commands are not allowed"})
    dispatcher = make_dispatcher(intent_parser=IntentParser(provider))

    response = dispatcher.call("parse_request", {"request": "run rm -rf / as root"})

    assert response["ok"] is False
    assert response["status"] == "unsupported"
    assert response["action"] is None
    assert manager.calls == []


def test_intent_parser_factory_is_created_lazily(make_dispatcher):
    created = []

    def factory():
        created.append(True)
        return IntentParser(FakeProvider({"status": "ambiguous", "action": None, "reason": "unclear"}))

    dispatcher = make_dispatcher(intent_parser=factory)
    assert created == []

    dispatcher.call("parse_request", {"request": "do the thing"})
    dispatcher.call("parse_request", {"request": "do the other thing"})

    assert created == [True]


def test_default_dispatcher_reports_lsmod_failures(monkeypatch):
    def missing_lsmod(args):
        raise FileNotFoundError("lsmod not found")

    monkeypatch.setattr(module_manager, "_run_command", missing_lsmod)

    response = ToolDispatcher().call("check_module", {"module": "uvcvideo"})

    assert response["ok"] is False
    assert response["status"] == "error"
    assert response["result"] is None


def test_checked_manager_raises_when_lsmod_fails(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="lsmod: failed"),
    )

    with pytest.raises(RuntimeError, match="lsmod: failed"):
        CheckedModuleManager.is_module_loaded("uvcvideo")
