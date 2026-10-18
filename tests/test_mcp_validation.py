"""Edge-case tests for MCP tool validation and dispatch (app/mcp_server/tools.py).

These tests have no MCP dependency: they drive `ToolDispatcher` directly with
the raw argument mappings an MCP client would send. Everything external is
mocked: the module manager, the LLM provider and `subprocess`. No module is
ever loaded, unloaded or reloaded.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from app.agent import ActionExecutor
from app.llm import IntentParser, LLMProviderError
from app.manager import module_manager
from app.mcp_server import MUTATING_TOOLS, READ_ONLY_TOOLS, TOOL_SPECS, ToolDispatcher, validate_arguments
from app.mcp_server.tools import ToolCallError
from app.safety import DEFAULT_PROTECTED_MODULES, MAX_MODULE_NAME_LENGTH, SafetyPolicy

RESPONSE_KEYS = {"ok", "tool", "status", "message", "action", "result", "safety", "errors"}
SAFETY_KEYS = {"risk_level", "requires_confirmation", "confirmed"}
MODULE_TOOLS = tuple(name for name, spec in TOOL_SPECS.items() if spec.operation is not None)
TOOLS_WITH_MODULE = tuple(name for name in MODULE_TOOLS if name != "list_modules")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeModuleManager:
    """Scriptable stand-in for app.manager.module_manager.

    * ``errors[method]`` makes that method raise.
    * ``results[method]`` makes that method return a fixed value (state is then
      left untouched, which lets tests simulate a failed post-change check).
    * ``state_errors`` is consumed one entry per ``is_module_loaded`` call so a
      test can let the policy's state check pass but fail the verification.
    """

    def __init__(self, loaded=("snd", "ext4")) -> None:
        self.loaded: set[str] = set(loaded)
        self.calls: list[tuple[str, str | None]] = []
        self.errors: dict[str, Exception] = {}
        self.results: dict[str, Any] = {}
        self.state_errors: list[Exception | None] = []

    def _record(self, name: str, module: str | None = None) -> None:
        self.calls.append((name, module))
        if name in self.errors:
            raise self.errors[name]

    def list_modules(self) -> Any:
        self._record("list_modules")
        if "list_modules" in self.results:
            return self.results["list_modules"]
        return [{"name": module} for module in sorted(self.loaded)]

    def get_module_info(self, module: str) -> Any:
        self._record("get_module_info", module)
        if "get_module_info" in self.results:
            return self.results["get_module_info"]
        return {"success": True, "name": module}

    def is_module_loaded(self, module: str) -> Any:
        self._record("is_module_loaded", module)
        if self.state_errors:
            error = self.state_errors.pop(0)
            if error is not None:
                raise error
        if "is_module_loaded" in self.results:
            return self.results["is_module_loaded"]
        return module in self.loaded

    def load_module(self, module: str) -> Any:
        self._record("load_module", module)
        if "load_module" in self.results:
            return self.results["load_module"]
        self.loaded.add(module)
        return {"success": True, "module": module, "operation": "load", "message": "ok", "error": None}

    def unload_module(self, module: str) -> Any:
        self._record("unload_module", module)
        if "unload_module" in self.results:
            return self.results["unload_module"]
        self.loaded.discard(module)
        return {"success": True, "module": module, "operation": "unload", "message": "ok", "error": None}

    def reload_module(self, module: str) -> Any:
        self._record("reload_module", module)
        if "reload_module" in self.results:
            return self.results["reload_module"]
        return {"success": True, "module": module, "operation": "reload", "message": "ok", "error": None}

    @property
    def mutations(self) -> list[tuple[str, str | None]]:
        return [call for call in self.calls if call[0] in {"load_module", "unload_module", "reload_module"}]


class FakeProvider:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []

    def complete(self, system, prompt, response_schema=None):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def parser_for(reply: Any = None, error: Exception | None = None) -> IntentParser:
    return IntentParser(FakeProvider(reply, error))


@pytest.fixture(autouse=True)
def forbid_real_commands(monkeypatch):
    """Nothing in these tests may reach a real Linux utility."""

    def explode(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError(f"MCP tests must not run commands: {args!r} {kwargs!r}")

    monkeypatch.setattr(module_manager, "_run_command", explode)
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, explode)


@pytest.fixture
def manager():
    return FakeModuleManager()


@pytest.fixture
def make_executor(manager):
    def make(policy: SafetyPolicy | None = None, confirmation_handler=None) -> ActionExecutor:
        return ActionExecutor(
            manager=manager,
            policy=policy or SafetyPolicy(is_module_loaded=manager.is_module_loaded),
            confirmation_handler=confirmation_handler,
        )

    return make


@pytest.fixture
def make_dispatcher(make_executor):
    def make(allow_mutations=True, intent_parser=None, executor=None, policy=None) -> ToolDispatcher:
        return ToolDispatcher(
            executor or make_executor(policy=policy),
            intent_parser=intent_parser,
            allow_mutations=allow_mutations,
        )

    return make


@pytest.fixture
def dispatcher(make_dispatcher):
    return make_dispatcher()


@pytest.fixture
def read_only_dispatcher(make_dispatcher):
    return make_dispatcher(allow_mutations=False)


def assert_response_shape(response: dict[str, Any], tool: str) -> None:
    assert set(response) == RESPONSE_KEYS
    assert isinstance(response["ok"], bool)
    assert response["tool"] == tool
    assert isinstance(response["status"], str) and response["status"]
    assert isinstance(response["message"], str) and response["message"]
    assert response["action"] is None or isinstance(response["action"], dict)
    assert response["safety"] is None or set(response["safety"]) == SAFETY_KEYS
    assert isinstance(response["errors"], list)
    assert all(isinstance(error, str) for error in response["errors"])
    json.dumps(response)


def assert_invalid_arguments(response: dict[str, Any], tool: str, errors: list[str]) -> None:
    assert_response_shape(response, tool)
    assert response["ok"] is False
    assert response["status"] == "invalid_arguments"
    assert response["message"] == f"Invalid arguments for tool '{tool}'."
    assert response["errors"] == errors
    assert response["action"] is None
    assert response["result"] is None
    assert response["safety"] is None


# --------------------------------------------------------------------------- #
# Tool catalog and input schemas
# --------------------------------------------------------------------------- #


def test_catalog_partitions_into_read_only_and_mutating():
    assert set(READ_ONLY_TOOLS) | set(MUTATING_TOOLS) == set(TOOL_SPECS)
    assert not set(READ_ONLY_TOOLS) & set(MUTATING_TOOLS)
    assert set(READ_ONLY_TOOLS) == {"list_modules", "get_module_info", "check_module", "parse_request"}
    assert list(TOOL_SPECS) == list(READ_ONLY_TOOLS) + list(MUTATING_TOOLS)


@pytest.mark.parametrize("spec", TOOL_SPECS.values(), ids=lambda spec: spec.name)
def test_input_schema_mirrors_arg_specs(spec):
    schema = spec.input_schema()

    assert set(schema["properties"]) == {arg.name for arg in spec.args}
    assert schema["required"] == [arg.name for arg in spec.args if arg.required]
    for arg in spec.args:
        prop = schema["properties"][arg.name]
        assert prop["type"] == {str: "string", bool: "boolean"}[arg.type]
        assert prop["description"]
        if arg.required:
            assert "default" not in prop
        else:
            assert prop["default"] == arg.default
    json.dumps(schema)


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
def test_mutating_tool_schemas_default_confirm_and_dry_run_to_false(tool):
    schema = TOOL_SPECS[tool].input_schema()

    assert schema["required"] == ["module"]
    assert schema["properties"]["confirm"] == {
        "type": "boolean",
        "description": TOOL_SPECS[tool].args[1].description,
        "default": False,
    }
    assert schema["properties"]["dry_run"]["default"] is False
    assert TOOL_SPECS[tool].mutating is True
    assert TOOL_SPECS[tool].operation is not None


def test_read_only_tool_schemas_have_no_confirmation_arguments():
    for tool in READ_ONLY_TOOLS:
        assert not {"confirm", "dry_run"} & set(TOOL_SPECS[tool].input_schema()["properties"])
        assert TOOL_SPECS[tool].mutating is False
    assert TOOL_SPECS["parse_request"].operation is None


# --------------------------------------------------------------------------- #
# Missing required arguments
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", TOOLS_WITH_MODULE)
@pytest.mark.parametrize("arguments", [{}, None])
def test_missing_module_argument_is_rejected(dispatcher, manager, tool, arguments):
    response = dispatcher.call(tool, arguments)

    assert_invalid_arguments(response, tool, ["missing required argument 'module'"])
    assert manager.calls == []


@pytest.mark.parametrize("arguments", [{}, None])
def test_missing_request_argument_is_rejected(make_dispatcher, arguments):
    dispatcher = make_dispatcher(intent_parser=parser_for({"status": "ambiguous", "action": None, "reason": "x"}))

    response = dispatcher.call("parse_request", arguments)

    assert_invalid_arguments(response, "parse_request", ["missing required argument 'request'"])


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
def test_optional_arguments_alone_do_not_satisfy_required_ones(dispatcher, manager, tool):
    response = dispatcher.call(tool, {"confirm": True, "dry_run": False})

    assert_invalid_arguments(response, tool, ["missing required argument 'module'"])
    assert manager.calls == []


def test_missing_and_unexpected_arguments_are_both_reported(dispatcher, manager):
    response = dispatcher.call("check_module", {"name": "snd", "Module": "snd"})

    assert_invalid_arguments(
        response,
        "check_module",
        ["unexpected argument(s): ['Module', 'name']", "missing required argument 'module'"],
    )
    assert manager.calls == []


def test_all_argument_problems_are_reported_in_one_response(dispatcher, manager):
    response = dispatcher.call("load_module", {"confirm": "yes", "dry_run": 1, "force": True})

    assert_invalid_arguments(
        response,
        "load_module",
        [
            "unexpected argument(s): ['force']",
            "missing required argument 'module'",
            "argument 'confirm' must be a bool, got str",
            "argument 'dry_run' must be a bool, got int",
        ],
    )
    assert manager.calls == []


# --------------------------------------------------------------------------- #
# Unexpected or extra arguments
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "extra",
    [
        {"force": True},
        {"Module": "snd"},  # keys are case-sensitive
        {"module ": "snd"},
        {"": "snd"},
        {"options": {"force": True}},
        {"args": ["-f"]},
        {"command": "rmmod snd"},
        {"confirm": True},  # only mutating tools take confirm
        {"dry_run": True},
        {"request": "is snd loaded"},
    ],
)
def test_extra_arguments_on_module_tools_are_rejected(dispatcher, manager, extra):
    response = dispatcher.call("check_module", {"module": "snd", **extra})

    assert_invalid_arguments(response, "check_module", [f"unexpected argument(s): {sorted(extra)}"])
    assert manager.calls == []


@pytest.mark.parametrize("arguments", [{"module": "snd"}, {"confirm": True}, {"dry_run": True}, {"all": True}])
def test_list_modules_takes_no_arguments(dispatcher, manager, arguments):
    response = dispatcher.call("list_modules", arguments)

    assert_invalid_arguments(response, "list_modules", [f"unexpected argument(s): {sorted(arguments)}"])
    assert manager.calls == []


def test_extra_arguments_are_reported_sorted_and_deduplicated(dispatcher):
    response = dispatcher.call("check_module", {"module": "snd", "zeta": 1, "alpha": 2, "Mid": 3})

    assert response["errors"] == ["unexpected argument(s): ['Mid', 'alpha', 'zeta']"]


@pytest.mark.parametrize("extra", [{"module_name": "snd"}, {"execute": True}, {"confirm": True}])
def test_parse_request_rejects_extra_arguments(make_dispatcher, extra):
    provider = FakeProvider({"status": "ambiguous", "action": None, "reason": "x"})
    dispatcher = make_dispatcher(intent_parser=IntentParser(provider))

    response = dispatcher.call("parse_request", {"request": "is snd loaded", **extra})

    assert_invalid_arguments(response, "parse_request", [f"unexpected argument(s): {sorted(extra)}"])
    assert provider.prompts == []


# --------------------------------------------------------------------------- #
# Incorrect argument types
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", TOOLS_WITH_MODULE)
@pytest.mark.parametrize(
    ("value", "type_name"),
    [(5, "int"), (1.5, "float"), (True, "bool"), (None, "NoneType"), (["snd"], "list"), ({"name": "snd"}, "dict")],
)
def test_module_must_be_a_string(dispatcher, manager, tool, value, type_name):
    response = dispatcher.call(tool, {"module": value})

    assert_invalid_arguments(response, tool, [f"argument 'module' must be a str, got {type_name}"])
    assert manager.calls == []


@pytest.mark.parametrize(
    ("value", "type_name"),
    [(7, "int"), (None, "NoneType"), (["list modules"], "list"), ({"text": "x"}, "dict"), (True, "bool")],
)
def test_request_must_be_a_string(make_dispatcher, value, type_name):
    provider = FakeProvider({"status": "ambiguous", "action": None, "reason": "x"})
    dispatcher = make_dispatcher(intent_parser=IntentParser(provider))

    response = dispatcher.call("parse_request", {"request": value})

    assert_invalid_arguments(response, "parse_request", [f"argument 'request' must be a str, got {type_name}"])
    assert provider.prompts == []


@pytest.mark.parametrize("arguments", ["module=snd", ["snd"], ("snd",), 42, True, b"{}", "{}"])
def test_non_object_arguments_are_rejected_with_type_name(dispatcher, manager, arguments):
    response = dispatcher.call("check_module", arguments)

    assert_response_shape(response, "check_module")
    assert response["status"] == "invalid_arguments"
    assert response["message"] == "Tool arguments must be a JSON object."
    assert response["errors"] == [f"arguments must be an object, got {type(arguments).__name__}"]
    assert manager.calls == []


# --------------------------------------------------------------------------- #
# Booleans are not coerced
# --------------------------------------------------------------------------- #

NOT_BOOLEANS = [
    ("true", "str"),
    ("True", "str"),
    ("false", "str"),
    ("yes", "str"),
    ("no", "str"),
    ("1", "str"),
    ("", "str"),
    (1, "int"),
    (0, "int"),
    (1.0, "float"),
    (None, "NoneType"),
    ([True], "list"),
    ({"value": True}, "dict"),
]


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
@pytest.mark.parametrize(("value", "type_name"), NOT_BOOLEANS)
def test_confirm_requires_a_real_boolean(dispatcher, manager, tool, value, type_name):
    response = dispatcher.call(tool, {"module": "uvcvideo", "confirm": value})

    assert_invalid_arguments(response, tool, [f"argument 'confirm' must be a bool, got {type_name}"])
    assert manager.calls == []


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
@pytest.mark.parametrize(("value", "type_name"), NOT_BOOLEANS)
def test_dry_run_requires_a_real_boolean(dispatcher, manager, tool, value, type_name):
    response = dispatcher.call(tool, {"module": "uvcvideo", "dry_run": value})

    assert_invalid_arguments(response, tool, [f"argument 'dry_run' must be a bool, got {type_name}"])
    assert manager.calls == []


def test_string_true_is_not_confirmation_even_when_otherwise_valid(dispatcher, manager):
    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": "true", "dry_run": False})

    assert response["status"] == "invalid_arguments"
    assert manager.mutations == []
    assert "uvcvideo" not in manager.loaded


def test_explicit_false_confirm_behaves_like_omitted(dispatcher, manager):
    explicit = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": False})
    omitted = dispatcher.call("load_module", {"module": "uvcvideo"})

    assert explicit == omitted
    assert explicit["status"] == "confirmation_required"
    assert manager.mutations == []


def test_validate_arguments_fills_defaults_and_keeps_raw_module():
    values = validate_arguments(TOOL_SPECS["load_module"], {"module": "snd-hda-intel"})

    assert values == {"module": "snd-hda-intel", "confirm": False, "dry_run": False}
    assert validate_arguments(TOOL_SPECS["list_modules"], None) == {}


def test_validate_arguments_raises_tool_call_error_with_structured_details():
    with pytest.raises(ToolCallError) as excinfo:
        validate_arguments(TOOL_SPECS["check_module"], {"module": 1})

    assert excinfo.value.status == "invalid_arguments"
    assert excinfo.value.message == "Invalid arguments for tool 'check_module'."
    assert excinfo.value.errors == ["argument 'module' must be a str, got int"]


# --------------------------------------------------------------------------- #
# State-changing tools when disabled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", MUTATING_TOOLS)
@pytest.mark.parametrize(
    "arguments",
    [
        {"module": "uvcvideo", "confirm": True},
        {"module": "uvcvideo", "dry_run": True},
        {"module": "uvcvideo"},
        {},  # even invalid arguments: the tool is refused before validation
        {"module": "snd; reboot", "confirm": True},
        None,
    ],
)
def test_disabled_mutating_tools_are_refused_before_argument_validation(read_only_dispatcher, manager, tool, arguments):
    response = read_only_dispatcher.call(tool, arguments)

    assert_response_shape(response, tool)
    assert response["ok"] is False
    assert response["status"] == "tool_disabled"
    assert response["message"] == f"Tool {tool!r} is disabled; module changes are not enabled on this server."
    assert response["errors"] == []
    assert response["action"] is None
    assert response["safety"] is None
    assert manager.calls == []


def test_disabled_dispatcher_lists_only_read_only_tools(read_only_dispatcher):
    names = [spec.name for spec in read_only_dispatcher.available_tools()]

    assert names == list(READ_ONLY_TOOLS)
    assert not any(spec.mutating for spec in read_only_dispatcher.available_tools())


def test_mutations_default_to_disabled(make_executor):
    dispatcher = ToolDispatcher(make_executor())

    assert dispatcher.allow_mutations is False
    assert [spec.name for spec in dispatcher.available_tools()] == list(READ_ONLY_TOOLS)
    assert dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})["status"] == "tool_disabled"


def test_unknown_tool_error_lists_only_available_tools(read_only_dispatcher, dispatcher):
    hidden = read_only_dispatcher.call("rmmod", {})
    full = dispatcher.call("rmmod", {})

    assert hidden["status"] == full["status"] == "unknown_tool"
    assert hidden["message"] == full["message"] == "Unknown tool 'rmmod'."
    assert hidden["errors"] == [f"available tools: {list(READ_ONLY_TOOLS)}"]
    assert full["errors"] == [f"available tools: {list(TOOL_SPECS)}"]


# --------------------------------------------------------------------------- #
# Read-only tools remain available when mutations are disabled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "arguments", "result"),
    [
        ("list_modules", {}, [{"name": "ext4"}, {"name": "snd"}]),
        ("get_module_info", {"module": "snd"}, {"success": True, "name": "snd"}),
        ("check_module", {"module": "snd"}, {"loaded": True}),
        ("check_module", {"module": "uvcvideo"}, {"loaded": False}),
    ],
)
def test_read_only_tools_work_with_mutations_disabled(read_only_dispatcher, manager, tool, arguments, result):
    response = read_only_dispatcher.call(tool, arguments)

    assert_response_shape(response, tool)
    assert response["ok"] is True
    assert response["status"] == "success"
    assert response["result"] == result
    assert response["safety"] == {"risk_level": "read_only", "requires_confirmation": False, "confirmed": False}
    assert manager.mutations == []


def test_parse_request_works_with_mutations_disabled(make_dispatcher, manager):
    reply = {"status": "action", "action": {"operation": "unload_module", "module": "snd"}, "reason": "x"}
    dispatcher = make_dispatcher(allow_mutations=False, intent_parser=parser_for(reply))

    response = dispatcher.call("parse_request", {"request": "unload snd"})

    assert response["ok"] is True
    assert response["status"] == "parsed"
    assert response["action"] == {"operation": "unload_module", "module": "snd"}
    assert response["result"] == {"executed": False}
    assert response["safety"] is None
    # The parsed mutating action cannot be carried out on this server.
    follow_up = dispatcher.call("unload_module", {"module": "snd", "confirm": True})
    assert follow_up["status"] == "tool_disabled"
    assert manager.mutations == []


def test_read_only_results_are_identical_whether_or_not_mutations_are_enabled(make_dispatcher):
    enabled = make_dispatcher(allow_mutations=True)
    disabled = make_dispatcher(allow_mutations=False)

    for tool, arguments in (("list_modules", {}), ("check_module", {"module": "snd"}), ("get_module_info", {"module": "snd"})):
        assert enabled.call(tool, arguments) == disabled.call(tool, arguments)


# --------------------------------------------------------------------------- #
# Missing or invalid confirmation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "module"),
    [("load_module", "uvcvideo"), ("unload_module", "snd"), ("reload_module", "snd")],
)
def test_unconfirmed_mutation_reports_what_is_needed(dispatcher, manager, tool, module):
    response = dispatcher.call(tool, {"module": module})

    assert_response_shape(response, tool)
    assert response["ok"] is False
    assert response["status"] == "confirmation_required"
    assert response["message"] == "Execution blocked because confirmation was not provided."
    assert response["errors"] == ["explicit confirmation is required"]
    assert response["action"] == {"operation": tool, "module": module}
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": False}
    assert response["result"] is None
    assert manager.mutations == []


def test_dry_run_does_not_require_or_grant_confirmation(dispatcher, manager):
    response = dispatcher.call("load_module", {"module": "uvcvideo", "dry_run": True})

    assert response["ok"] is True
    assert response["status"] == "dry_run"
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": False}
    assert response["result"] == {
        "would_execute": True,
        "summary": "Would perform load_module uvcvideo. Risk: disruptive. Confirmation will be required before execution.",
    }
    assert manager.mutations == []
    assert "uvcvideo" not in manager.loaded


def test_confirm_and_dry_run_together_are_rejected_before_any_state_check(dispatcher, manager):
    response = dispatcher.call("unload_module", {"module": "snd", "confirm": True, "dry_run": True})

    assert_invalid_arguments(response, "unload_module", ["'confirm' and 'dry_run' cannot both be true"])
    assert manager.calls == []


def test_confirmation_on_a_read_only_tool_is_an_argument_error(dispatcher, manager):
    response = dispatcher.call("get_module_info", {"module": "snd", "confirm": True})

    assert_invalid_arguments(response, "get_module_info", ["unexpected argument(s): ['confirm']"])
    assert manager.calls == []


def test_confirmation_is_per_call_and_never_remembered(dispatcher, manager):
    confirmed = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})
    assert confirmed["status"] == "success"

    manager.loaded.discard("uvcvideo")
    unconfirmed = dispatcher.call("load_module", {"module": "uvcvideo"})

    assert unconfirmed["status"] == "confirmation_required"
    assert manager.mutations == [("load_module", "uvcvideo")]


def test_confirmed_call_records_confirmation_in_safety(dispatcher, manager):
    response = dispatcher.call("unload_module", {"module": "snd", "confirm": True})

    assert response["ok"] is True
    assert response["status"] == "success"
    assert response["message"] == "unload_module completed."
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": True}
    assert response["result"]["verification"] == {"expected_loaded": False, "actual_loaded": False, "passed": True}
    assert manager.mutations == [("unload_module", "snd")]


# --------------------------------------------------------------------------- #
# Invalid module names and protected modules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("module", "fragment"),
    [
        ("snd; rm -rf /", "forbidden character"),
        ("snd && reboot", "forbidden character"),
        ("$(id)", "forbidden character"),
        ("`id`", "forbidden character"),
        ("snd|tee", "forbidden character"),
        ("snd>out", "forbidden character"),
        ("snd hda", "forbidden character"),
        ("snd\t", "forbidden character"),
        ("snd\n", "forbidden character"),
        ("../../lib/modules", "forbidden character"),
        ("/lib/modules/snd.ko", "forbidden character"),
        ("snd..hda", "must not contain '..'"),
        ("-r", "must not start with '-'"),
        ("--remove", "must not start with '-'"),
        ("_snd", "must start with a letter or digit"),
        (".snd", "must start with a letter or digit"),
        ("sñd", "may only contain letters, digits"),
        ("snd\x00", "may only contain letters, digits"),
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        ("a" * (MAX_MODULE_NAME_LENGTH + 1), "too long"),
    ],
)
@pytest.mark.parametrize("tool", TOOLS_WITH_MODULE)
def test_invalid_module_names_are_rejected_on_every_module_tool(dispatcher, manager, tool, module, fragment):
    arguments = {"module": module, "confirm": True} if tool in MUTATING_TOOLS else {"module": module}

    response = dispatcher.call(tool, arguments)

    assert_response_shape(response, tool)
    assert response["status"] == "invalid_arguments"
    assert any(fragment in error for error in response["errors"])
    assert manager.calls == []


def test_longest_valid_module_name_is_accepted(dispatcher, manager):
    module = "a" * MAX_MODULE_NAME_LENGTH

    response = dispatcher.call("check_module", {"module": module})

    assert response["status"] == "success"
    assert manager.calls == [("is_module_loaded", module)]


@pytest.mark.parametrize("module", sorted(DEFAULT_PROTECTED_MODULES))
@pytest.mark.parametrize("tool", ["unload_module", "reload_module"])
def test_every_default_protected_module_resists_unload_and_reload(make_dispatcher, manager, module, tool):
    manager.loaded.add(module)
    dispatcher = make_dispatcher()

    confirmed = dispatcher.call(tool, {"module": module, "confirm": True})
    preview = dispatcher.call(tool, {"module": module, "dry_run": True})

    for response in (confirmed, preview):
        assert_response_shape(response, tool)
        assert response["ok"] is False
        assert response["status"] == "rejected"
        assert response["errors"] == [f"module {module!r} is protected from {tool}"]
        assert response["action"] == {"operation": tool, "module": module}
        assert response["safety"]["risk_level"] == "disruptive"
    assert confirmed["message"] == "Action was rejected by safety policy."
    assert manager.mutations == []


def test_protected_module_check_uses_canonical_name(dispatcher, manager):
    manager.loaded.add("dm_mod")

    response = dispatcher.call("unload_module", {"module": "dm-mod", "confirm": True})

    assert response["status"] == "rejected"
    assert response["errors"] == ["module 'dm_mod' is protected from unload_module"]
    assert manager.mutations == []


def test_protected_module_is_rejected_before_its_state_is_checked(dispatcher, manager):
    dispatcher.call("unload_module", {"module": "ext4", "confirm": True})

    assert manager.calls == []


def test_protection_does_not_block_loading_or_inspecting(dispatcher, manager):
    manager.loaded.discard("ext4")

    load = dispatcher.call("load_module", {"module": "ext4", "dry_run": True})
    info = dispatcher.call("get_module_info", {"module": "ext4"})
    check = dispatcher.call("check_module", {"module": "ext4"})

    assert load["status"] == "dry_run"
    assert info["status"] == check["status"] == "success"
    assert manager.mutations == []


def test_custom_protected_modules_replace_the_defaults(make_dispatcher, manager):
    policy = SafetyPolicy(is_module_loaded=manager.is_module_loaded, protected_modules={"snd"})
    dispatcher = make_dispatcher(policy=policy)

    protected = dispatcher.call("unload_module", {"module": "snd", "confirm": True})
    default_no_longer_protected = dispatcher.call("unload_module", {"module": "ext4", "confirm": True})

    assert protected["status"] == "rejected"
    assert protected["errors"] == ["module 'snd' is protected from unload_module"]
    assert default_no_longer_protected["status"] == "success"
    assert manager.mutations == [("unload_module", "ext4")]


@pytest.mark.parametrize(
    ("tool", "module", "error"),
    [
        ("load_module", "snd", "module 'snd' is already loaded"),
        ("unload_module", "uvcvideo", "module 'uvcvideo' is not loaded"),
        ("reload_module", "uvcvideo", "module 'uvcvideo' is not loaded"),
    ],
)
def test_state_conflicts_are_rejected_with_exact_reason(dispatcher, manager, tool, module, error):
    response = dispatcher.call(tool, {"module": module, "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "rejected"
    assert response["errors"] == [error]
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": False, "confirmed": False}
    assert manager.mutations == []


# --------------------------------------------------------------------------- #
# Tool execution failures and permission errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "arguments", "method"),
    [
        ("list_modules", {}, "list_modules"),
        ("get_module_info", {"module": "snd"}, "get_module_info"),
        ("check_module", {"module": "snd"}, "is_module_loaded"),
    ],
)
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError("lsmod not found"), "FileNotFoundError: lsmod not found"),
        (RuntimeError("lsmod: command failed"), "RuntimeError: lsmod: command failed"),
        (OSError("disk error"), "OSError: disk error"),
    ],
)
def test_read_only_command_failures_are_structured_errors(dispatcher, manager, tool, arguments, method, error, expected):
    manager.errors[method] = error

    response = dispatcher.call(tool, arguments)

    assert_response_shape(response, tool)
    assert response["ok"] is False
    assert response["status"] == "error"
    assert response["message"] == "Module manager operation failed."
    assert response["errors"] == [expected]
    assert response["result"] is None
    assert response["safety"]["risk_level"] == "read_only"


@pytest.mark.parametrize(
    ("tool", "arguments", "method"),
    [
        ("list_modules", {}, "list_modules"),
        ("get_module_info", {"module": "snd"}, "get_module_info"),
        ("check_module", {"module": "snd"}, "is_module_loaded"),
    ],
)
def test_read_only_permission_errors_are_reported_as_permission_error(dispatcher, manager, tool, arguments, method):
    manager.errors[method] = PermissionError("[Errno 13] Permission denied")

    response = dispatcher.call(tool, arguments)

    assert response["ok"] is False
    assert response["status"] == "permission_error"
    assert response["message"] == "Permission denied while accessing Linux module state."
    assert response["errors"] == ["[Errno 13] Permission denied"]


def test_permission_error_without_message_gets_a_default(dispatcher, manager):
    manager.errors["list_modules"] = PermissionError()

    response = dispatcher.call("list_modules", {})

    assert response["status"] == "permission_error"
    assert response["errors"] == ["permission denied"]


def test_permission_error_during_policy_state_check_is_not_a_rejection(dispatcher, manager):
    manager.state_errors = [PermissionError("lsmod: Permission denied")]

    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "permission_error"
    assert response["message"] == "Permission denied while checking module state."
    assert response["errors"] == ["could not determine whether module 'uvcvideo' is loaded: lsmod: Permission denied"]
    assert manager.mutations == []


def test_unknown_state_during_policy_check_is_an_error_not_a_rejection(dispatcher, manager):
    manager.state_errors = [RuntimeError("lsmod: failed")]

    response = dispatcher.call("unload_module", {"module": "snd", "confirm": True})

    assert response["status"] == "error"
    assert response["message"] == "Module state could not be determined for the safety check."
    assert response["errors"] == ["could not determine whether module 'snd' is loaded: lsmod: failed"]
    assert manager.mutations == []


def test_non_boolean_state_from_manager_is_rejected(dispatcher, manager):
    manager.results["is_module_loaded"] = "yes"

    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["status"] == "rejected"
    assert response["errors"] == ["module state checker must return a bool"]
    assert manager.mutations == []


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (
            {"success": False, "error": "PermissionDenied", "message": "modprobe: ERROR: Operation not permitted"},
            "PermissionDenied: modprobe: ERROR: Operation not permitted",
        ),
        (
            {"success": False, "error": "CommandFailed", "message": "must be root to insert modules"},
            "PermissionDenied: must be root to insert modules",
        ),
        ({"success": False, "error": "PermissionDenied", "message": ""}, "PermissionDenied: permission denied"),
    ],
)
def test_manager_reported_permission_failures_become_permission_error(dispatcher, manager, result, error):
    manager.results["load_module"] = result

    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "permission_error"
    assert response["message"] == "Module manager reported an unsuccessful operation."
    assert response["errors"] == [error]
    assert response["result"] == result
    assert response["safety"]["confirmed"] is True


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (
            {"success": False, "error": "CommandFailed", "message": "modprobe: FATAL: Module nope not found"},
            "modprobe: FATAL: Module nope not found",
        ),
        ({"success": False, "error": "CommandNotFound", "message": ""}, "CommandNotFound"),
        ({"success": False}, "load_module failed"),
    ],
)
def test_manager_reported_failures_become_manager_error(dispatcher, manager, result, error):
    manager.results["load_module"] = result

    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "manager_error"
    assert response["message"] == "Module manager reported an unsuccessful operation."
    assert response["errors"] == [error]
    assert response["result"] == result


@pytest.mark.parametrize(
    ("tool", "arguments", "method", "bad_result", "error"),
    [
        ("list_modules", {}, "list_modules", None, "list_modules returned an invalid result"),
        ("list_modules", {}, "list_modules", {"snd": 1}, "list_modules returned an invalid result"),
        ("check_module", {"module": "snd"}, "is_module_loaded", None, "check_module returned an invalid result"),
        ("get_module_info", {"module": "snd"}, "get_module_info", "snd info", "get_module_info returned an invalid result"),
        ("get_module_info", {"module": "snd"}, "get_module_info", {"name": "snd"}, "get_module_info returned an invalid result"),
        ("load_module", {"module": "uvcvideo", "confirm": True}, "load_module", None, "load_module returned an invalid result"),
        ("load_module", {"module": "uvcvideo", "confirm": True}, "load_module", {"success": "yes"}, "load_module returned an invalid result"),
        ("load_module", {"module": "uvcvideo", "confirm": True}, "load_module", [], "load_module returned an invalid result"),
    ],
)
def test_malformed_manager_results_are_manager_errors(dispatcher, manager, tool, arguments, method, bad_result, error):
    manager.results[method] = bad_result

    response = dispatcher.call(tool, arguments)

    assert response["ok"] is False
    assert response["status"] == "manager_error"
    assert response["errors"] == [error]


@pytest.mark.parametrize(
    ("tool", "module", "expected_loaded"),
    [("load_module", "uvcvideo", True), ("unload_module", "snd", False)],
)
def test_unverified_state_change_is_reported_as_manager_error(dispatcher, manager, tool, module, expected_loaded):
    # The manager claims success but leaves the module state unchanged.
    manager.results[tool] = {"success": True}

    response = dispatcher.call(tool, {"module": module, "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "manager_error"
    assert response["message"] == "The requested module state could not be verified."
    assert response["errors"] == ["module state does not match the requested operation"]
    assert response["result"]["verification"] == {
        "expected_loaded": expected_loaded,
        "actual_loaded": not expected_loaded,
        "passed": False,
    }
    assert response["safety"]["confirmed"] is True


def test_permission_error_during_verification_is_reported(dispatcher, manager):
    manager.state_errors = [None, PermissionError("lsmod: Permission denied")]

    response = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["status"] == "permission_error"
    assert response["message"] == "Permission denied while verifying module state."
    assert response["errors"] == ["lsmod: Permission denied"]
    assert manager.mutations == [("load_module", "uvcvideo")]


def test_command_failure_during_verification_is_reported(dispatcher, manager):
    manager.state_errors = [None, RuntimeError("lsmod: failed")]

    response = dispatcher.call("unload_module", {"module": "snd", "confirm": True})

    assert response["status"] == "error"
    assert response["message"] == "Module state could not be verified after the operation."
    assert response["errors"] == ["RuntimeError: lsmod: failed"]


@pytest.mark.parametrize("method", ["load_module", "unload_module", "reload_module"])
def test_mutation_exceptions_after_confirmation_are_structured(dispatcher, manager, method):
    module = "uvcvideo" if method == "load_module" else "snd"
    manager.errors[method] = RuntimeError("modprobe crashed")

    response = dispatcher.call(method, {"module": module, "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "error"
    assert response["message"] == "Module manager operation failed."
    assert response["errors"] == ["RuntimeError: modprobe crashed"]
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": True}


@pytest.mark.parametrize("error", [ValueError("secret path /etc/shadow"), TypeError("secret"), KeyError("secret")])
def test_unexpected_manager_exceptions_become_internal_error_without_details(dispatcher, manager, error):
    manager.errors["list_modules"] = error

    response = dispatcher.call("list_modules", {})

    assert_response_shape(response, "list_modules")
    assert response["ok"] is False
    assert response["status"] == "internal_error"
    assert response["message"] == "The tool failed unexpectedly."
    assert response["errors"] == ["internal error"]
    assert "secret" not in json.dumps(response)


def test_intent_parser_factory_failure_is_an_internal_error(make_dispatcher):
    def broken_factory():
        raise RuntimeError("secret credentials problem")

    response = make_dispatcher(intent_parser=broken_factory).call("parse_request", {"request": "list modules"})

    assert response["status"] == "internal_error"
    assert "secret" not in json.dumps(response)


def test_provider_failure_is_reported_without_execution(make_dispatcher, manager):
    dispatcher = make_dispatcher(intent_parser=parser_for(error=LLMProviderError("no API key")))

    response = dispatcher.call("parse_request", {"request": "list modules"})

    assert_response_shape(response, "parse_request")
    assert response["ok"] is False
    assert response["status"] == "provider_error"
    assert response["errors"] == ["no API key"]
    assert response["result"] == {"executed": False}
    assert manager.calls == []


def test_default_dispatcher_reports_permission_denied_from_lsmod(monkeypatch):
    def denied(args):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="lsmod: Permission denied")

    monkeypatch.setattr(module_manager, "_run_command", denied)

    response = ToolDispatcher().call("check_module", {"module": "uvcvideo"})

    assert response["ok"] is False
    assert response["status"] == "permission_error"
    assert response["errors"] == ["lsmod: Permission denied"]


# --------------------------------------------------------------------------- #
# Consistent response fields and error structures
# --------------------------------------------------------------------------- #

OK_STATUSES = {"success", "dry_run", "parsed"}


def _scenarios(manager: FakeModuleManager, make_dispatcher) -> list[tuple[str, str, Any, ToolDispatcher]]:
    broken = FakeModuleManager()
    broken.errors["list_modules"] = RuntimeError("lsmod failed")
    broken_executor = ActionExecutor(manager=broken, policy=SafetyPolicy(is_module_loaded=broken.is_module_loaded))
    denied = FakeModuleManager()
    denied.errors["list_modules"] = PermissionError("denied")
    denied_executor = ActionExecutor(manager=denied, policy=SafetyPolicy(is_module_loaded=denied.is_module_loaded))
    failing = FakeModuleManager()
    failing.results["load_module"] = {"success": False, "message": "modprobe failed"}
    failing_executor = ActionExecutor(manager=failing, policy=SafetyPolicy(is_module_loaded=failing.is_module_loaded))

    class BrokenExecutor:
        def execute(self, *args, **kwargs):
            raise KeyError("boom")

    parsed = parser_for({"status": "action", "action": {"operation": "list_modules", "module": None}, "reason": "x"})
    ambiguous = parser_for({"status": "ambiguous", "action": None, "reason": "x"})
    unsupported = parser_for({"status": "unsupported", "action": None, "reason": "x"})
    garbage = parser_for("not json")
    down = parser_for(error=LLMProviderError("down"))

    default = make_dispatcher()
    return [
        ("success", "check_module", {"module": "snd"}, default),
        ("dry_run", "load_module", {"module": "uvcvideo", "dry_run": True}, default),
        ("parsed", "parse_request", {"request": "list modules"}, make_dispatcher(intent_parser=parsed)),
        ("invalid_arguments", "check_module", {"module": 1}, default),
        ("unknown_tool", "rmmod", {}, default),
        ("tool_disabled", "load_module", {"module": "uvcvideo", "confirm": True}, make_dispatcher(allow_mutations=False)),
        ("rejected", "unload_module", {"module": "ext4", "confirm": True}, default),
        ("confirmation_required", "load_module", {"module": "uvcvideo"}, default),
        ("error", "list_modules", {}, ToolDispatcher(broken_executor)),
        ("permission_error", "list_modules", {}, ToolDispatcher(denied_executor)),
        ("manager_error", "load_module", {"module": "uvcvideo", "confirm": True}, ToolDispatcher(failing_executor, allow_mutations=True)),
        ("internal_error", "list_modules", {}, ToolDispatcher(BrokenExecutor())),
        ("ambiguous", "parse_request", {"request": "do it"}, make_dispatcher(intent_parser=ambiguous)),
        ("unsupported", "parse_request", {"request": "rm -rf /"}, make_dispatcher(intent_parser=unsupported)),
        ("invalid_output", "parse_request", {"request": "list modules"}, make_dispatcher(intent_parser=garbage)),
        ("provider_error", "parse_request", {"request": "list modules"}, make_dispatcher(intent_parser=down)),
    ]


def test_every_documented_status_has_the_same_response_shape(manager, make_dispatcher):
    seen = set()
    for expected_status, tool, arguments, dispatcher in _scenarios(manager, make_dispatcher):
        response = dispatcher.call(tool, arguments)

        assert response["status"] == expected_status, (tool, arguments, response)
        assert_response_shape(response, tool)
        assert response["ok"] is (expected_status in OK_STATUSES), response
        if not response["ok"]:
            assert response["status"] not in OK_STATUSES
        if response["ok"]:
            assert response["errors"] == []
        if expected_status in {"invalid_arguments", "unknown_tool", "tool_disabled", "internal_error"}:
            assert response["action"] is None and response["result"] is None and response["safety"] is None
        if tool in MODULE_TOOLS and expected_status not in {"invalid_arguments", "tool_disabled", "internal_error"}:
            assert set(response["safety"]) == SAFETY_KEYS
            assert response["safety"]["risk_level"] in {"read_only", "disruptive"}
            assert isinstance(response["safety"]["requires_confirmation"], bool)
            assert isinstance(response["safety"]["confirmed"], bool)
        if tool == "parse_request":
            assert response["safety"] is None
            assert response["result"] == {"executed": False}
        seen.add(expected_status)
    assert seen >= {
        "success", "dry_run", "parsed", "invalid_arguments", "unknown_tool", "tool_disabled", "rejected",
        "confirmation_required", "error", "permission_error", "manager_error", "internal_error",
        "ambiguous", "unsupported", "invalid_output", "provider_error",
    }


def test_failure_responses_never_carry_a_result_from_a_skipped_operation(dispatcher, manager):
    for tool, arguments in (
        ("load_module", {"module": "uvcvideo"}),
        ("unload_module", {"module": "ext4", "confirm": True}),
        ("check_module", {"module": "bad name"}),
    ):
        response = dispatcher.call(tool, arguments)
        assert response["ok"] is False
        assert response["result"] is None, response
    assert manager.mutations == []


def test_tool_name_is_echoed_even_when_unknown_or_not_a_string(dispatcher):
    assert dispatcher.call("rmmod", {})["tool"] == "rmmod"
    assert dispatcher.call(None, {})["tool"] == "None"
    assert dispatcher.call(42, {})["tool"] == "42"
    assert dispatcher.call(["check_module"], {})["tool"] == "['check_module']"
    assert dispatcher.call("", {})["status"] == "unknown_tool"


def test_errors_list_is_a_fresh_copy_per_response(dispatcher):
    first = dispatcher.call("check_module", {"module": 1})
    first["errors"].append("tampered")
    second = dispatcher.call("check_module", {"module": 1})

    assert second["errors"] == ["argument 'module' must be a str, got int"]


# --------------------------------------------------------------------------- #
# MCP and the shared executor stay consistent
# --------------------------------------------------------------------------- #

EXECUTOR_SCENARIOS = [
    # (tool, arguments, raw_action, execute options, loaded set, manager tweaks)
    ("list_modules", {}, {"operation": "list_modules"}, {}, {"snd"}, {}),
    ("check_module", {"module": "snd-hda-intel"}, {"operation": "check_module", "module": "snd-hda-intel"}, {}, {"snd_hda_intel"}, {}),
    ("get_module_info", {"module": "snd"}, {"operation": "get_module_info", "module": "snd"}, {}, {"snd"}, {}),
    ("load_module", {"module": "uvcvideo"}, {"operation": "load_module", "module": "uvcvideo"}, {"dry_run": False, "confirmation_handler": None}, {"snd"}, {}),
    ("load_module", {"module": "uvcvideo", "dry_run": True}, {"operation": "load_module", "module": "uvcvideo"}, {"dry_run": True, "confirmation_handler": None}, {"snd"}, {}),
    ("load_module", {"module": "uvcvideo", "confirm": True}, {"operation": "load_module", "module": "uvcvideo"}, {"dry_run": False, "confirmation_handler": lambda _p: True}, {"snd"}, {}),
    ("unload_module", {"module": "ext4", "confirm": True}, {"operation": "unload_module", "module": "ext4"}, {"dry_run": False, "confirmation_handler": lambda _p: True}, {"ext4"}, {}),
    ("unload_module", {"module": "uvcvideo", "confirm": True}, {"operation": "unload_module", "module": "uvcvideo"}, {"dry_run": False, "confirmation_handler": lambda _p: True}, {"snd"}, {}),
    ("reload_module", {"module": "snd", "confirm": True}, {"operation": "reload_module", "module": "snd"}, {"dry_run": False, "confirmation_handler": lambda _p: True}, {"snd"}, {}),
    ("list_modules", {}, {"operation": "list_modules"}, {}, {"snd"}, {"errors": {"list_modules": RuntimeError("lsmod failed")}}),
    ("check_module", {"module": "snd"}, {"operation": "check_module", "module": "snd"}, {}, {"snd"}, {"errors": {"is_module_loaded": PermissionError("denied")}}),
    ("load_module", {"module": "uvcvideo", "confirm": True}, {"operation": "load_module", "module": "uvcvideo"}, {"dry_run": False, "confirmation_handler": lambda _p: True}, {"snd"}, {"results": {"load_module": {"success": False, "error": "PermissionDenied", "message": "Operation not permitted"}}}),
    ("load_module", {"module": "uvcvideo", "confirm": True}, {"operation": "load_module", "module": "uvcvideo"}, {"dry_run": False, "confirmation_handler": lambda _p: True}, {"snd"}, {"results": {"load_module": {"success": False, "message": "modprobe failed"}}}),
]


def _fresh(loaded, tweaks) -> tuple[FakeModuleManager, ActionExecutor]:
    manager = FakeModuleManager(loaded)
    manager.errors.update(tweaks.get("errors", {}))
    manager.results.update(tweaks.get("results", {}))
    executor = ActionExecutor(manager=manager, policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded))
    return manager, executor


@pytest.mark.parametrize(
    ("tool", "arguments", "raw_action", "options", "loaded", "tweaks"),
    EXECUTOR_SCENARIOS,
    ids=[f"{s[0]}-{i}" for i, s in enumerate(EXECUTOR_SCENARIOS)],
)
def test_mcp_response_is_a_faithful_view_of_the_executor_result(tool, arguments, raw_action, options, loaded, tweaks):
    direct_manager, direct_executor = _fresh(loaded, tweaks)
    mcp_manager, mcp_executor = _fresh(loaded, tweaks)

    direct = direct_executor.execute(raw_action, **options)
    response = ToolDispatcher(mcp_executor, allow_mutations=True).call(tool, arguments)

    assert response["ok"] is direct.success
    assert response["status"] == direct.status
    assert response["message"] == direct.message
    assert response["action"] == direct.action
    assert response["result"] == direct.result
    assert response["errors"] == direct.errors
    assert response["safety"] == {
        "risk_level": direct.risk_level,
        "requires_confirmation": direct.requires_confirmation,
        "confirmed": direct.confirmed,
    }
    assert mcp_manager.calls == direct_manager.calls
    assert mcp_manager.loaded == direct_manager.loaded


def test_mcp_never_bypasses_the_executor_for_module_tools(manager):
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, raw_action, **options):
            self.calls.append((raw_action, options))
            return ActionExecutor(manager=manager, policy=SafetyPolicy(is_module_loaded=manager.is_module_loaded)).execute(
                raw_action, **options
            )

    executor = RecordingExecutor()
    dispatcher = ToolDispatcher(executor, allow_mutations=True)

    dispatcher.call("list_modules", {})
    dispatcher.call("check_module", {"module": "snd"})
    dispatcher.call("load_module", {"module": "uvcvideo"})
    dispatcher.call("load_module", {"module": "uvcvideo", "dry_run": True})
    dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert [call[0] for call in executor.calls] == [
        {"operation": "list_modules"},
        {"operation": "check_module", "module": "snd"},
        {"operation": "load_module", "module": "uvcvideo"},
        {"operation": "load_module", "module": "uvcvideo"},
        {"operation": "load_module", "module": "uvcvideo"},
    ]
    read_only_options, check_options, unconfirmed, preview, confirmed = (call[1] for call in executor.calls)
    assert read_only_options == check_options == {}
    assert unconfirmed == {"dry_run": False, "confirmation_handler": None}
    assert preview == {"dry_run": True, "confirmation_handler": None}
    assert confirmed["dry_run"] is False
    assert callable(confirmed["confirmation_handler"])
    assert confirmed["confirmation_handler"]("Confirm?") is True


def test_executor_without_handler_and_mcp_without_confirm_agree(manager, make_executor):
    executor = make_executor()

    direct = executor.execute({"operation": "load_module", "module": "uvcvideo"})
    via_mcp = ToolDispatcher(executor, allow_mutations=True).call("load_module", {"module": "uvcvideo"})

    assert direct.status == via_mcp["status"] == "confirmation_required"
    assert manager.mutations == []


def test_executor_dry_run_and_mcp_dry_run_agree_on_protected_module(manager, make_executor):
    executor = make_executor()

    direct = executor.execute({"operation": "reload_module", "module": "ext4"}, dry_run=True)
    via_mcp = ToolDispatcher(executor, allow_mutations=True).call("reload_module", {"module": "ext4", "dry_run": True})

    # The policy rejects before the dry-run branch, so there is no preview on either path.
    assert direct.success is via_mcp["ok"] is False
    assert direct.status == via_mcp["status"] == "rejected"
    assert via_mcp["result"] is direct.result is None
    assert via_mcp["errors"] == direct.errors == ["module 'ext4' is protected from reload_module"]
    assert manager.mutations == []


def test_mcp_call_without_confirm_never_borrows_the_executors_default_handler(manager, make_executor):
    # Even an executor configured with an auto-approving handler must not let an
    # unconfirmed MCP call through: the dispatcher passes an explicit "no handler".
    executor = make_executor(confirmation_handler=lambda _prompt: True)
    dispatcher = ToolDispatcher(executor, allow_mutations=True)

    response = dispatcher.call("load_module", {"module": "uvcvideo"})

    assert response["ok"] is False
    assert response["status"] == "confirmation_required"
    assert manager.mutations == []
    assert "uvcvideo" not in manager.loaded

    confirmed = dispatcher.call("load_module", {"module": "uvcvideo", "confirm": True})
    assert confirmed["status"] == "success"
    assert manager.mutations == [("load_module", "uvcvideo")]
