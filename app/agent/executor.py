"""Coordinate validation, safety policy, confirmation, and module operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.manager import module_manager
from app.safety import (
    ConfirmationHandler,
    SafetyPolicy,
    confirm_operation,
    create_dry_run,
    validate_action,
    validate_raw,
)
from app.schemas import ModuleAction, Operation


class ModuleManagerProtocol(Protocol):
    """Operations required from the low-level module manager."""

    def list_modules(self) -> list[dict[str, Any]]: ...

    def get_module_info(self, module: str) -> dict[str, Any]: ...

    def is_module_loaded(self, module: str) -> bool: ...

    def load_module(self, module: str) -> dict[str, Any]: ...

    def unload_module(self, module: str) -> dict[str, Any]: ...

    def reload_module(self, module: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutionResult:
    """Consistent structured result returned for every handled action."""

    success: bool
    status: str
    message: str
    action: dict[str, str] | None = None
    risk_level: str | None = None
    requires_confirmation: bool = False
    confirmed: bool = False
    result: Any = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible result mapping."""
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "action": self.action,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "confirmed": self.confirmed,
            "result": self.result,
            "errors": list(self.errors),
        }


class ActionExecutor:
    """Enforce the full safety sequence before calling a module manager.

    The manager and confirmation handler are injectable for testing and
    application integration. Dry-run mode never dispatches an operation.
    """

    def __init__(
        self,
        manager: ModuleManagerProtocol | None = None,
        policy: SafetyPolicy | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        self._manager = module_manager if manager is None else manager
        self._policy = (
            SafetyPolicy(is_module_loaded=self._manager.is_module_loaded)
            if policy is None
            else policy
        )
        self._confirmation_handler = confirmation_handler

    def execute(
        self,
        raw_action: Any,
        *,
        dry_run: bool = False,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> ExecutionResult:
        """Validate and, if approved, perform one structured action."""
        validation = (
            validate_action(raw_action)
            if isinstance(raw_action, ModuleAction)
            else validate_raw(raw_action)
        )
        if not validation.ok or validation.action is None:
            return ExecutionResult(
                success=False,
                status="rejected",
                message="Action failed validation.",
                errors=list(validation.errors),
            )

        action = validation.action
        policy_result = self._policy.evaluate(action)
        action_data = action.to_dict()
        risk_level = policy_result.risk_level.value
        if not policy_result.allowed or policy_result.action is None:
            return ExecutionResult(
                success=False,
                status="rejected",
                message="Action was rejected by safety policy.",
                action=action_data,
                risk_level=risk_level,
                errors=list(policy_result.errors),
            )

        action = policy_result.action
        action_data = action.to_dict()
        if dry_run:
            preview = create_dry_run(policy_result)
            return ExecutionResult(
                success=preview.allowed,
                status="dry_run" if preview.allowed else "rejected",
                message=preview.summary,
                action=action_data,
                risk_level=preview.risk_level.value,
                requires_confirmation=preview.requires_confirmation,
                result={
                    "would_execute": preview.allowed,
                    "summary": preview.summary,
                },
                errors=list(preview.errors),
            )

        handler = (
            confirmation_handler
            if confirmation_handler is not None
            else self._confirmation_handler
        )
        try:
            confirmation = confirm_operation(policy_result, handler)
        except (OSError, RuntimeError) as exc:
            return ExecutionResult(
                success=False,
                status="error",
                message="Confirmation could not be completed.",
                action=action_data,
                risk_level=risk_level,
                requires_confirmation=policy_result.requires_confirmation,
                errors=[f"{type(exc).__name__}: {exc}"],
            )

        if not confirmation.proceed:
            status = (
                "rejected"
                if not confirmation.required
                else "confirmation_required"
                if confirmation.errors == ["explicit confirmation is required"]
                else "confirmation_declined"
                if confirmation.errors == ["operation was declined"]
                else "confirmation_invalid"
            )
            return ExecutionResult(
                success=False,
                status=status,
                message=confirmation.message,
                action=action_data,
                risk_level=risk_level,
                requires_confirmation=confirmation.required,
                confirmed=confirmation.confirmed,
                errors=list(confirmation.errors),
            )

        try:
            manager_result = self._dispatch(action)
        except (OSError, RuntimeError) as exc:
            return ExecutionResult(
                success=False,
                status="error",
                message="Module manager operation failed.",
                action=action_data,
                risk_level=risk_level,
                requires_confirmation=confirmation.required,
                confirmed=confirmation.confirmed,
                errors=[f"{type(exc).__name__}: {exc}"],
            )

        result_error = self._manager_result_error(action, manager_result)
        if result_error is not None:
            return ExecutionResult(
                success=False,
                status="manager_error",
                message="Module manager reported an unsuccessful operation.",
                action=action_data,
                risk_level=risk_level,
                requires_confirmation=confirmation.required,
                confirmed=confirmation.confirmed,
                result=manager_result,
                errors=[result_error],
            )

        return ExecutionResult(
            success=True,
            status="success",
            message=f"{action.operation.value} completed.",
            action=action_data,
            risk_level=risk_level,
            requires_confirmation=confirmation.required,
            confirmed=confirmation.confirmed,
            result=manager_result,
        )

    def _dispatch(self, action: ModuleAction) -> Any:
        """Dispatch only schema-defined operations to manager methods."""
        operation = action.operation
        if operation is Operation.LIST_MODULES:
            return self._manager.list_modules()

        module = action.module
        if module is None:
            raise RuntimeError("validated module action has no module name")

        if operation is Operation.GET_MODULE_INFO:
            return self._manager.get_module_info(module)
        if operation is Operation.CHECK_MODULE:
            return {"loaded": self._manager.is_module_loaded(module)}
        if operation is Operation.LOAD_MODULE:
            return self._manager.load_module(module)
        if operation is Operation.UNLOAD_MODULE:
            return self._manager.unload_module(module)
        if operation is Operation.RELOAD_MODULE:
            return self._manager.reload_module(module)

        raise RuntimeError(f"unsupported validated operation: {operation.value}")

    @staticmethod
    def _manager_result_error(action: ModuleAction, result: Any) -> str | None:
        """Validate manager result shapes and translate reported failures."""
        if action.operation is Operation.LIST_MODULES:
            return None if isinstance(result, list) else "list_modules returned an invalid result"
        if action.operation is Operation.CHECK_MODULE:
            loaded = result.get("loaded") if isinstance(result, dict) else None
            return None if isinstance(loaded, bool) else "check_module returned an invalid result"

        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            return f"{action.operation.value} returned an invalid result"
        if result["success"]:
            return None

        detail = result.get("message") or result.get("error")
        return str(detail or f"{action.operation.value} failed")
