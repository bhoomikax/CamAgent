import pytest

from app.safety import (
    DEFAULT_PROTECTED_MODULES,
    RiskLevel,
    SafetyPolicy,
)
from app.schemas import ModuleAction, Operation


@pytest.fixture
def state():
    return {"uvcvideo": False, "snd": True}


@pytest.fixture
def policy(state):
    return SafetyPolicy(
        is_module_loaded=lambda module: state.get(module, False),
        protected_modules=(),
    )


@pytest.mark.parametrize(
    "action",
    [
        ModuleAction(Operation.LIST_MODULES),
        ModuleAction(Operation.GET_MODULE_INFO, "uvcvideo"),
        ModuleAction(Operation.CHECK_MODULE, "uvcvideo"),
    ],
)
def test_read_only_operations_are_allowed_without_confirmation(action):
    policy = SafetyPolicy()

    result = policy.evaluate(action)

    assert result.allowed is True
    assert result.risk_level is RiskLevel.READ_ONLY
    assert result.requires_confirmation is False
    assert result.action == action


@pytest.mark.parametrize(
    "operation,module",
    [
        (Operation.LOAD_MODULE, "uvcvideo"),
        (Operation.UNLOAD_MODULE, "snd"),
        (Operation.RELOAD_MODULE, "snd"),
    ],
)
def test_valid_mutations_are_disruptive_and_require_confirmation(policy, operation, module):
    result = policy.evaluate(ModuleAction(operation, module))

    assert result.allowed is True
    assert result.risk_level is RiskLevel.DISRUPTIVE
    assert result.requires_confirmation is True


@pytest.mark.parametrize(
    "operation,module,loaded,expected_error",
    [
        (
            Operation.LOAD_MODULE,
            "snd",
            True,
            "already loaded",
        ),
        (
            Operation.UNLOAD_MODULE,
            "uvcvideo",
            False,
            "not loaded",
        ),
        (
            Operation.RELOAD_MODULE,
            "uvcvideo",
            False,
            "not loaded",
        ),
    ],
)
def test_mutations_with_conflicting_module_state_are_rejected(
    operation, module, loaded, expected_error
):
    policy = SafetyPolicy(
        is_module_loaded=lambda _: loaded,
        protected_modules=(),
    )

    result = policy.evaluate(ModuleAction(operation, module))

    assert result.allowed is False
    assert result.requires_confirmation is False
    assert expected_error in result.errors[0]


def test_read_only_operation_does_not_query_module_state():
    def unexpected_state_check(_):
        pytest.fail("read-only operations must not query module state")

    result = SafetyPolicy(unexpected_state_check).evaluate(
        ModuleAction(Operation.LIST_MODULES)
    )

    assert result.allowed is True


def test_mutation_is_rejected_when_state_checker_is_not_configured():
    result = SafetyPolicy(protected_modules=()).evaluate(
        ModuleAction(Operation.LOAD_MODULE, "uvcvideo")
    )

    assert result.allowed is False
    assert "state checker is required" in result.errors[0]


def test_protected_modules_are_blocked_from_unload_and_reload():
    policy = SafetyPolicy(is_module_loaded=lambda _: True)

    for operation in (Operation.UNLOAD_MODULE, Operation.RELOAD_MODULE):
        result = policy.evaluate(ModuleAction(operation, "ext4"))

        assert result.allowed is False
        assert "protected" in result.errors[0]
        assert result.requires_confirmation is False


def test_protected_modules_can_still_be_loaded():
    result = SafetyPolicy(is_module_loaded=lambda _: False).evaluate(
        ModuleAction(Operation.LOAD_MODULE, "ext4")
    )

    assert result.allowed is True
    assert result.requires_confirmation is True


def test_protected_module_names_are_canonicalised():
    policy = SafetyPolicy(protected_modules=["nvme-core"])

    assert policy.protected_modules == frozenset({"nvme_core"})


def test_custom_protected_modules_replace_the_defaults():
    policy = SafetyPolicy(
        is_module_loaded=lambda _: True,
        protected_modules=["custom_driver"],
    )

    assert policy.protected_modules == frozenset({"custom_driver"})
    assert "ext4" not in policy.protected_modules


def test_defaults_are_a_small_documented_protection_set():
    assert DEFAULT_PROTECTED_MODULES == {
        "btrfs",
        "dm_mod",
        "ext4",
        "overlay",
        "xfs",
    }


@pytest.mark.parametrize("protected_modules", ["ext4", ["../bad"]])
def test_invalid_protected_module_configuration_is_rejected(protected_modules):
    with pytest.raises(ValueError):
        SafetyPolicy(protected_modules=protected_modules)


def test_state_checker_errors_are_returned_as_policy_rejections():
    def failed_state_check(_):
        raise OSError("lsmod unavailable")

    result = SafetyPolicy(failed_state_check, protected_modules=()).evaluate(
        ModuleAction(Operation.UNLOAD_MODULE, "uvcvideo")
    )

    assert result.allowed is False
    assert "could not determine" in result.errors[0]
    assert "lsmod unavailable" in result.errors[0]


def test_state_checker_must_return_bool():
    result = SafetyPolicy(
        lambda _: "loaded",  # type: ignore[arg-type]
        protected_modules=(),
    ).evaluate(ModuleAction(Operation.LOAD_MODULE, "uvcvideo"))

    assert result.allowed is False
    assert result.errors == ["module state checker must return a bool"]


def test_policy_revalidates_action_content():
    action = ModuleAction(Operation.LOAD_MODULE, "uvcvideo; reboot")
    result = SafetyPolicy(lambda _: False, protected_modules=()).evaluate(action)

    assert result.allowed is False
    assert any("forbidden character" in error for error in result.errors)
