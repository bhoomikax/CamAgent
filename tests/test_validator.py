import pytest

from app.safety import (
    MAX_MODULE_NAME_LENGTH,
    ValidationResult,
    is_valid_module_name,
    module_name_errors,
    normalise_module_name,
    validate_action,
    validate_raw,
)
from app.schemas import ModuleAction, Operation


# --- module_name_errors / is_valid_module_name ----------------------------


@pytest.mark.parametrize(
    "name",
    ["uvcvideo", "snd_hda_intel", "e1000e", "nvme-core", "8021q", "vboxdrv", "x"],
)
def test_realistic_module_names_are_valid(name):
    assert module_name_errors(name) == []
    assert is_valid_module_name(name) is True


@pytest.mark.parametrize("name", ["", "   ", "\n"])
def test_empty_names_are_invalid(name):
    assert "must not be empty" in module_name_errors(name)[0]


@pytest.mark.parametrize("name", [None, 7, ["uvcvideo"]])
def test_non_string_names_are_invalid(name):
    assert "must be a string" in module_name_errors(name)[0]


@pytest.mark.parametrize(
    "name",
    [
        "uvcvideo; rm -rf /",
        "uvcvideo && reboot",
        "uvcvideo | cat",
        "$(whoami)",
        "`id`",
        "uvcvideo video",
        "uvc*",
    ],
)
def test_shell_metacharacters_are_rejected(name):
    errors = module_name_errors(name)
    assert any("forbidden character" in error for error in errors)
    assert is_valid_module_name(name) is False


@pytest.mark.parametrize("name", ["../../etc/passwd", "/lib/modules/x.ko", "a/b"])
def test_path_like_names_are_rejected(name):
    assert is_valid_module_name(name) is False


def test_double_dot_is_rejected():
    errors = module_name_errors("a..b")
    assert any("must not contain '..'" in error for error in errors)
    assert is_valid_module_name("a..b") is False


@pytest.mark.parametrize("name", ["-r", "--help", "-uvcvideo"])
def test_flag_like_names_are_rejected(name):
    errors = module_name_errors(name)
    assert any("command flag" in error for error in errors)


def test_overlong_name_is_rejected():
    name = "a" * (MAX_MODULE_NAME_LENGTH + 1)
    assert any("too long" in error for error in module_name_errors(name))


def test_name_at_max_length_is_accepted():
    assert is_valid_module_name("a" * MAX_MODULE_NAME_LENGTH)


@pytest.mark.parametrize("name", ["uvcvidéo", "モジュール", "uvc\x00video"])
def test_non_ascii_or_control_characters_are_rejected(name):
    errors = module_name_errors(name)
    assert errors
    assert is_valid_module_name(name) is False


# --- normalise_module_name ------------------------------------------------


def test_normalise_maps_hyphen_to_underscore():
    assert normalise_module_name("nvme-core") == "nvme_core"
    assert normalise_module_name("  uvcvideo ") == "uvcvideo"


# --- validate_action ------------------------------------------------------


def test_validate_action_accepts_valid_module_action():
    action = ModuleAction(Operation.LOAD_MODULE, "uvcvideo")

    result = validate_action(action)

    assert result.ok is True
    assert result.errors == []
    assert result.action == action


def test_validate_action_returns_canonical_module_name():
    result = validate_action(ModuleAction(Operation.UNLOAD_MODULE, "nvme-core"))

    assert result.ok is True
    assert result.action.module == "nvme_core"
    assert result.action.operation is Operation.UNLOAD_MODULE


def test_validate_action_accepts_list_modules():
    result = validate_action(ModuleAction(Operation.LIST_MODULES))

    assert result.ok is True
    assert result.action.module is None


def test_validate_action_rejects_bad_module_name():
    result = validate_action(ModuleAction(Operation.LOAD_MODULE, "uvcvideo; reboot"))

    assert result.ok is False
    assert result.action is None
    assert any("forbidden character" in error for error in result.errors)


def test_validate_action_rejects_non_action():
    result = validate_action({"operation": "load_module", "module": "uvcvideo"})  # type: ignore[arg-type]

    assert result.ok is False
    assert "expected a ModuleAction" in result.errors[0]


# --- validate_raw ---------------------------------------------------------


def test_validate_raw_accepts_dict():
    result = validate_raw({"operation": "reload_module", "module": "uvcvideo"})

    assert result.ok is True
    assert result.action == ModuleAction(Operation.RELOAD_MODULE, "uvcvideo")


def test_validate_raw_accepts_json_string():
    result = validate_raw('{"operation": "check_module", "module": "snd"}')

    assert result.ok is True
    assert result.action.module == "snd"


def test_validate_raw_reports_schema_errors_as_rejection():
    result = validate_raw({"operation": "run_shell", "module": "uvcvideo"})

    assert result.ok is False
    assert "Unsupported operation" in result.errors[0]


def test_validate_raw_reports_content_errors_as_rejection():
    result = validate_raw({"operation": "load_module", "module": "../evil"})

    assert result.ok is False
    assert result.action is None


def test_validate_raw_never_raises_on_garbage():
    for garbage in [None, 42, "not json", "[1, 2]", {"module": "x"}]:
        result = validate_raw(garbage)
        assert isinstance(result, ValidationResult)
        assert result.ok is False
