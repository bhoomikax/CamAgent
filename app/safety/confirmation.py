"""Dry-run planning and explicit confirmation for policy-approved actions.

These helpers do not execute module operations. The executor is responsible
for honoring a dry-run plan and calling the module manager only after a
successful confirmation when one is required.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.schemas import ModuleAction

from .policy import PolicyResult, RiskLevel


@dataclass(frozen=True)
class DryRunResult:
    """A structured description of what an action would do."""

    allowed: bool
    action: ModuleAction | None
    risk_level: RiskLevel
    requires_confirmation: bool
    summary: str
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfirmationResult:
    """Whether execution may proceed after applying confirmation policy."""

    proceed: bool
    required: bool
    confirmed: bool
    message: str
    errors: list[str] = field(default_factory=list)


ConfirmationHandler = Callable[[str], bool]


def create_dry_run(policy_result: PolicyResult) -> DryRunResult:
    """Build a preview from a policy decision without prompting or executing."""
    if not isinstance(policy_result, PolicyResult):
        raise TypeError("policy_result must be a PolicyResult")

    if not policy_result.allowed or policy_result.action is None:
        errors = list(policy_result.errors)
        if not errors:
            errors.append("policy did not approve an executable action")
        return DryRunResult(
            allowed=False,
            action=policy_result.action,
            risk_level=policy_result.risk_level,
            requires_confirmation=False,
            summary="Action is blocked by safety policy.",
            errors=errors,
        )

    confirmation = (
        "Confirmation will be required before execution."
        if policy_result.requires_confirmation
        else "No confirmation is required."
    )
    return DryRunResult(
        allowed=True,
        action=policy_result.action,
        risk_level=policy_result.risk_level,
        requires_confirmation=policy_result.requires_confirmation,
        summary=(
            f"Would perform {policy_result.action.describe()}. "
            f"Risk: {policy_result.risk_level.value}. {confirmation}"
        ),
    )


def confirm_operation(
    policy_result: PolicyResult,
    confirmation_handler: ConfirmationHandler | None = None,
) -> ConfirmationResult:
    """Require an explicit boolean approval for policy-marked risky actions.

    A missing handler, declined response, or malformed response fails closed.
    The handler is never called for rejected actions or actions that do not
    require confirmation.
    """
    if not isinstance(policy_result, PolicyResult):
        raise TypeError("policy_result must be a PolicyResult")

    if not policy_result.allowed or policy_result.action is None:
        errors = list(policy_result.errors)
        if not errors:
            errors.append("policy did not approve an executable action")
        return ConfirmationResult(
            proceed=False,
            required=False,
            confirmed=False,
            message="Action is blocked by safety policy.",
            errors=errors,
        )

    if not policy_result.requires_confirmation:
        return ConfirmationResult(
            proceed=True,
            required=False,
            confirmed=False,
            message="Action does not require confirmation.",
        )

    if confirmation_handler is None:
        return ConfirmationResult(
            proceed=False,
            required=True,
            confirmed=False,
            message="Execution blocked because confirmation was not provided.",
            errors=["explicit confirmation is required"],
        )

    prompt = (
        f"Confirm {policy_result.action.describe()}? "
        f"Risk: {policy_result.risk_level.value}."
    )
    decision = confirmation_handler(prompt)
    if not isinstance(decision, bool):
        return ConfirmationResult(
            proceed=False,
            required=True,
            confirmed=False,
            message="Execution blocked because the confirmation response was invalid.",
            errors=["confirmation handler must return a bool"],
        )
    if not decision:
        return ConfirmationResult(
            proceed=False,
            required=True,
            confirmed=False,
            message="Execution was not confirmed.",
            errors=["operation was declined"],
        )

    return ConfirmationResult(
        proceed=True,
        required=True,
        confirmed=True,
        message="Operation was confirmed.",
    )
