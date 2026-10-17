"""CLI compatibility adapter over the shared structured action executor."""

from typing import Any, Callable

from app.agent import ActionExecutor
from app.schemas import Operation

_COMMAND_OPERATIONS = {
    "list": Operation.LIST_MODULES,
    "info": Operation.GET_MODULE_INFO,
    "loaded": Operation.CHECK_MODULE,
    "load": Operation.LOAD_MODULE,
    "unload": Operation.UNLOAD_MODULE,
    "reload": Operation.RELOAD_MODULE,
}


def execute_action(
    command: str,
    module: str | None = None,
    confirm_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Translate the CLI vocabulary and execute through ActionExecutor."""
    operation = _COMMAND_OPERATIONS.get(command.strip().lower()) if isinstance(command, str) else None
    if operation is None:
        result = ActionExecutor().execute({"operation": command, "module": module})
        return _cli_result(result.to_dict(), command, module)

    raw_action: dict[str, Any] = {"operation": operation.value}
    if module is not None:
        raw_action["module"] = module
    result = ActionExecutor().execute(raw_action, confirmation_handler=confirm_fn)
    return _cli_result(result.to_dict(), command, module)


def _cli_result(result: dict[str, Any], command: Any, module: Any) -> dict[str, Any]:
    status = result["status"]
    if status in {"confirmation_required", "confirmation_declined", "confirmation_invalid"}:
        status = "declined"
    elif status in {"manager_error", "error", "internal_error"}:
        status = "failure"

    action = result.get("action") or {}
    operation = action.get("operation")
    cli_command = {
        "list_modules": "list",
        "get_module_info": "info",
        "check_module": "loaded",
        "load_module": "load",
        "unload_module": "unload",
        "reload_module": "reload",
    }.get(operation, command)
    payload = result.get("result")

    feedback: dict[str, Any] = {
        "success": result["success"],
        "status": status,
        "operation": cli_command,
        "module": action.get("module", module),
        "message": result["message"],
    }
    if isinstance(payload, dict):
        if operation == "check_module":
            feedback["loaded"] = payload.get("loaded")
        else:
            feedback.update({key: value for key, value in payload.items() if key != "verification"})
            if "verification" in payload:
                feedback["verification"] = payload["verification"]
    elif operation == "list_modules":
        feedback["modules"] = payload
    if result.get("errors"):
        feedback["error"] = result["errors"][0]
    return feedback
