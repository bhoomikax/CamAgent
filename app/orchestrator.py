import re
from typing import Any, Callable

from app import safety
from app.manager import module_manager


PERMISSION_ERROR_RE = re.compile(
    r"permission denied|operation not permitted|must be root|requires root",
    re.IGNORECASE,
)


def _feedback(
    command: str,
    module: str | None,
    result: dict[str, Any],
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = str(result.get("message") or "")
    error = result.get("error")
    permission_error = error == "PermissionDenied" or bool(
        PERMISSION_ERROR_RE.search(
            f"{error or ''} {message} {(verification or {}).get('error', '')}"
        )
    )
    success = bool(result.get("success", True))
    if verification is not None and not verification["passed"]:
        success = False
        if not message:
            message = "The requested module state could not be verified."

    if permission_error:
        status = "permission_error"
    elif result.get("status") in {"rejected", "declined"}:
        status = result["status"]
    else:
        status = "success" if success else "failure"

    feedback = {
        "success": success and status == "success",
        "status": status,
        "operation": command,
        "module": module,
        "message": message or ("Action completed successfully." if success else "Action failed."),
    }
    if error:
        feedback["error"] = error
    if verification is not None:
        feedback["verification"] = verification
    for key, value in result.items():
        if key not in {"success", "status", "operation", "module", "message", "error"}:
            feedback[key] = value
    return feedback


def execute_action(
    command: str,
    module: str | None = None,
    confirm_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    decision = safety.evaluate_action(command, module)
    if not decision.approved:
        return _feedback(
            decision.command,
            module if isinstance(module, str) else None,
            {
                "success": False,
                "status": "rejected",
                "message": decision.reason,
                "error": "SafetyPolicyRejected",
            },
        )

    if decision.requires_confirmation:
        prompt = f"Confirm {decision.command} for module {decision.module}?"
        if confirm_fn is None or not confirm_fn(prompt):
            return _feedback(
                decision.command,
                decision.module,
                {
                    "success": False,
                    "status": "declined",
                    "message": "Action was not confirmed.",
                    "error": "ConfirmationDeclined",
                },
            )

    try:
        if decision.command == "list":
            result = {"success": True, "modules": module_manager.list_modules()}
        elif decision.command == "info":
            result = module_manager.get_module_info(decision.module)
        elif decision.command == "loaded":
            result = {
                "success": True,
                "loaded": module_manager.is_module_loaded(decision.module),
            }
        elif decision.command == "load":
            result = module_manager.load_module(decision.module)
        elif decision.command == "unload":
            result = module_manager.unload_module(decision.module)
        else:
            result = module_manager.reload_module(decision.module)

        verification = None
        if decision.command in safety.MUTATING_ACTIONS:
            expected_loaded = decision.command != "unload"
            try:
                actual_loaded = module_manager.verify_module_loaded(decision.module)
                verification = {
                    "expected_loaded": expected_loaded,
                    "actual_loaded": actual_loaded,
                    "passed": actual_loaded == expected_loaded,
                }
            except (OSError, RuntimeError) as exc:
                verification = {
                    "expected_loaded": expected_loaded,
                    "actual_loaded": None,
                    "passed": False,
                    "error": str(exc) or "Unable to inspect module state.",
                }
        return _feedback(decision.command, decision.module, result, verification)
    except PermissionError as exc:
        return _feedback(
            decision.command,
            decision.module,
            {"success": False, "message": str(exc) or "Permission denied.", "error": "PermissionDenied"},
        )
    except OSError as exc:
        return _feedback(
            decision.command,
            decision.module,
            {"success": False, "message": str(exc), "error": "SystemError"},
        )
