import pytest

from app.schemas import (
    ALLOWED_OPERATIONS,
    ActionSchemaError,
    ModuleAction,
    Operation,
)


# --- Operation enum -------------------------------------------------------


def test_allowed_operations_is_the_expected_closed_set():
    assert set(ALLOWED_OPERATIONS) == {
        "list_modules",
        "get_module_info",
        "check_module",
        "load_module",
        "unload_module",
        "reload_module",
    }


def test_only_list_modules_does_not_require_a_module():
    assert Operation.LIST_MODULES.requires_module is False
    for operation in Operation:
        if operation is not Operation.LIST_MODULES:
            assert operation.requires_module is True


def test_operation_parse_accepts_case_and_whitespace_variants():
    assert Operation.parse("  Reload_Module ") is Operation.RELOAD_MODULE


def test_operation_parse_rejects_unknown_operation():
    with pytest.raises(ActionSchemaError, match="Unsupported operation"):
        Operation.parse("rm -rf /")


def test_operation_parse_rejects_non_string():
    with pytest.raises(ActionSchemaError, match="must be a string"):
        Operation.parse(42)


# --- Valid actions --------------------------------------------------------


def test_from_dict_builds_module_action():
    action = ModuleAction.from_dict({"operation": "reload_module", "module": "uvcvideo"})

    assert action.operation is Operation.RELOAD_MODULE
    assert action.module == "uvcvideo"


def test_from_dict_strips_whitespace_from_module():
    action = ModuleAction.from_dict({"operation": "load_module", "module": "  uvcvideo  "})

    assert action.module == "uvcvideo"


def test_list_modules_without_module_is_valid():
    action = ModuleAction.from_dict({"operation": "list_modules"})

    assert action.operation is Operation.LIST_MODULES
    assert action.module is None


def test_explicit_null_module_is_treated_as_absent():
    action = ModuleAction.from_dict({"operation": "list_modules", "module": None})

    assert action.module is None


def test_from_json_parses_llm_output():
    action = ModuleAction.from_json('{"operation": "check_module", "module": "snd"}')

    assert action.operation is Operation.CHECK_MODULE
    assert action.module == "snd"


def test_to_dict_round_trips():
    raw = {"operation": "unload_module", "module": "uvcvideo"}

    assert ModuleAction.from_dict(raw).to_dict() == raw
    assert ModuleAction.from_dict({"operation": "list_modules"}).to_dict() == {
        "operation": "list_modules"
    }


def test_describe_is_human_readable():
    assert ModuleAction(Operation.LIST_MODULES).describe() == "list_modules"
    assert ModuleAction(Operation.LOAD_MODULE, "uvcvideo").describe() == "load_module uvcvideo"


def test_action_is_immutable():
    action = ModuleAction(Operation.LOAD_MODULE, "uvcvideo")

    with pytest.raises(AttributeError):
        action.module = "something_else"  # type: ignore[misc]


# --- Malformed actions ----------------------------------------------------


@pytest.mark.parametrize("raw", [None, "load_module uvcvideo", ["load_module"], 123])
def test_from_dict_rejects_non_mapping(raw):
    with pytest.raises(ActionSchemaError, match="must be a JSON object"):
        ModuleAction.from_dict(raw)


def test_from_dict_rejects_missing_operation():
    with pytest.raises(ActionSchemaError, match="missing required field 'operation'"):
        ModuleAction.from_dict({"module": "uvcvideo"})


def test_from_dict_rejects_unsupported_operation():
    with pytest.raises(ActionSchemaError, match="Unsupported operation"):
        ModuleAction.from_dict({"operation": "run_shell", "module": "uvcvideo"})


def test_from_dict_rejects_unknown_fields():
    with pytest.raises(ActionSchemaError, match="Unexpected field"):
        ModuleAction.from_dict(
            {"operation": "load_module", "module": "uvcvideo", "command": "rm -rf /"}
        )


@pytest.mark.parametrize(
    "operation",
    ["get_module_info", "check_module", "load_module", "unload_module", "reload_module"],
)
def test_module_is_required_for_module_operations(operation):
    with pytest.raises(ActionSchemaError, match="requires a 'module'"):
        ModuleAction.from_dict({"operation": operation})


@pytest.mark.parametrize("module", ["", "   ", "\t\n"])
def test_empty_module_name_is_rejected(module):
    with pytest.raises(ActionSchemaError, match="must not be empty"):
        ModuleAction.from_dict({"operation": "load_module", "module": module})


@pytest.mark.parametrize("module", [123, ["uvcvideo"], {"name": "uvcvideo"}, True])
def test_non_string_module_is_rejected(module):
    with pytest.raises(ActionSchemaError, match="'module' must be a string"):
        ModuleAction.from_dict({"operation": "load_module", "module": module})


def test_list_modules_with_module_is_rejected():
    with pytest.raises(ActionSchemaError, match="does not take a 'module'"):
        ModuleAction.from_dict({"operation": "list_modules", "module": "uvcvideo"})


def test_from_json_rejects_invalid_json():
    with pytest.raises(ActionSchemaError, match="not valid JSON"):
        ModuleAction.from_json("{operation: reload_module}")


def test_direct_construction_is_also_validated():
    with pytest.raises(ActionSchemaError):
        ModuleAction(operation="load_module", module="uvcvideo")  # type: ignore[arg-type]
    with pytest.raises(ActionSchemaError):
        ModuleAction(operation=Operation.LOAD_MODULE)
