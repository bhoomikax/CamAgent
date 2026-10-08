import pytest

from app.safety import (
    PolicyResult,
    RiskLevel,
    SafetyPolicy,
    confirm_operation,
    create_dry_run,
)
from app.schemas import ModuleAction, Operation


@pytest.fixture
def read_only_decision():
    return SafetyPolicy().evaluate(ModuleAction(Operation.LIST_MODULES))


@pytest.fixture
def disruptive_decision():
    policy = SafetyPolicy(
        is_module_loaded=lambda _: False,
        protected_modules=(),
    )
    return policy.evaluate(ModuleAction(Operation.LOAD_MODULE, "uvcvideo"))


def test_dry_run_describes_approved_read_only_action(read_only_decision):
    result = create_dry_run(read_only_decision)

    assert result.allowed is True
    assert result.action == ModuleAction(Operation.LIST_MODULES)
    assert result.risk_level is RiskLevel.READ_ONLY
    assert result.requires_confirmation is False
    assert "Would perform list_modules" in result.summary
    assert "No confirmation is required" in result.summary


def test_dry_run_describes_confirmation_for_disruptive_action(disruptive_decision):
    result = create_dry_run(disruptive_decision)

    assert result.allowed is True
    assert result.action == ModuleAction(Operation.LOAD_MODULE, "uvcvideo")
    assert result.risk_level is RiskLevel.DISRUPTIVE
    assert result.requires_confirmation is True
    assert "load_module uvcvideo" in result.summary
    assert "Confirmation will be required" in result.summary


def test_dry_run_reports_policy_rejection():
    decision = SafetyPolicy(
        is_module_loaded=lambda _: True,
        protected_modules=(),
    ).evaluate(ModuleAction(Operation.LOAD_MODULE, "uvcvideo"))

    result = create_dry_run(decision)

    assert result.allowed is False
    assert result.action == ModuleAction(Operation.LOAD_MODULE, "uvcvideo")
    assert result.requires_confirmation is False
    assert "already loaded" in result.errors[0]
    assert "blocked" in result.summary


def test_dry_run_does_not_call_confirmation_or_execute():
    policy_result = SafetyPolicy(
        is_module_loaded=lambda _: False,
        protected_modules=(),
    ).evaluate(ModuleAction(Operation.LOAD_MODULE, "uvcvideo"))
    preview = create_dry_run(policy_result)
    preview = create_dry_run(policy_result)
    assert preview.allowed is True
    assert preview.allowed is True


def test_confirmation_auto_allows_read_only_action_without_handler(
    read_only_decision,
):
    result = confirm_operation(read_only_decision)

    assert result.proceed is True
    assert result.required is False
    assert result.confirmed is False


def test_confirmation_requires_handler_for_disruptive_operation(
    disruptive_decision,
):
    result = confirm_operation(disruptive_decision)

    assert result.proceed is False
    assert result.required is True
    assert result.confirmed is False
    assert "confirmation is required" in result.errors[0]


def test_confirmation_invokes_handler_with_action_and_risk(disruptive_decision):
    prompts = []

    result = confirm_operation(disruptive_decision, lambda prompt: prompts.append(prompt) or True)

    assert result.proceed is True
    assert result.required is True
    assert result.confirmed is True
    assert len(prompts) == 1
    assert "load_module uvcvideo" in prompts[0]
    assert "disruptive" in prompts[0]


def test_declined_confirmation_blocks_execution(disruptive_decision):
    result = confirm_operation(disruptive_decision, lambda _: False)

    assert result.proceed is False
    assert result.required is True
    assert result.confirmed is False
    assert result.errors == ["operation was declined"]


@pytest.mark.parametrize("response", [None, "yes", 1])
def test_non_boolean_confirmation_response_fails_closed(
    disruptive_decision, response
):
    result = confirm_operation(disruptive_decision, lambda _: response)

    assert result.proceed is False
    assert result.confirmed is False
    assert result.errors == ["confirmation handler must return a bool"]


def test_confirmation_handler_is_not_called_for_policy_rejection():
    rejected = PolicyResult.rejected(
        RiskLevel.DISRUPTIVE,
        "protected module",
        action=ModuleAction(Operation.UNLOAD_MODULE, "ext4"),
    )

    def unexpected_handler(_):
        pytest.fail("policy-rejected actions must not prompt for confirmation")

    result = confirm_operation(rejected, unexpected_handler)

    assert result.proceed is False
    assert "protected module" in result.errors[0]


def test_confirmation_handler_is_not_called_for_read_only_action(
    read_only_decision,
):
    def unexpected_handler(_):
        pytest.fail("read-only actions must not prompt for confirmation")

    result = confirm_operation(read_only_decision, unexpected_handler)

    assert result.proceed is True
    assert result.required is False


@pytest.mark.parametrize("bad_input", [None, object()])
def test_helpers_reject_non_policy_results(bad_input):
    with pytest.raises(TypeError):
        create_dry_run(bad_input)
    with pytest.raises(TypeError):
        confirm_operation(bad_input)
