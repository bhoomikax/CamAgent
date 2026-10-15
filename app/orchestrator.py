from typing import Any, Callable

from app import safety
from app.manager import module_manager


def execute_action(
    command: str,
    module: str | None = None,
    confirm_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    decision = safety.evaluate_action(command, module)
    if not decision.approved:
        return {
            "success": False,
            "status": "rejected",
            "operation": decision.command,
            "module": module,
            "message": decision.reason,
            "error": "SafetyPolicyRejected",
        }

    if decision.requires_confirmation:
        prompt = f"Confirm {decision.command} for module {decision.module}?"
        if confirm_fn is None or not confirm_fn(prompt):
            return {
                "success": False,
                "status": "declined",
                "operation": decision.command,
                "module": decision.module,
                "message": "Action was not confirmed.",
                "error": "ConfirmationDeclined",
            }

    try:
        if decision.command == "list":
            return {"success": True, "operation": "list", "modules": module_manager.list_modules()}
        if decision.command == "info":
            return module_manager.get_module_info(decision.module)
        if decision.command == "loaded":
            return {
                "success": True,
                "operation": "loaded",
                "module": decision.module,
                "loaded": module_manager.is_module_loaded(decision.module),
            }
        if decision.command == "load":
            return module_manager.load_module(decision.module)
        if decision.command == "unload":
            return module_manager.unload_module(decision.module)
        return module_manager.reload_module(decision.module)
    except PermissionError as exc:
        return {
            "success": False,
            "status": "permission_error",
            "operation": decision.command,
            "module": decision.module,
            "message": str(exc) or "Permission denied.",
            "error": "PermissionDenied",
        }
    except OSError as exc:
        return {
            "success": False,
            "status": "failed",
            "operation": decision.command,
            "module": decision.module,
            "message": str(exc),
            "error": "SystemError",
        }
