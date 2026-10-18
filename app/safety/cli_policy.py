"""Command-level policy used by the interactive CLI orchestrator.

Checks CLI command names (``list``, ``info``, ``load`` ...) and module names
before the orchestrator calls the module manager.
"""

from dataclasses import dataclass
from typing import Any

from .validator import module_name_errors

READ_ONLY_ACTIONS = {"list", "info", "loaded"}
MUTATING_ACTIONS = {"load", "unload", "reload"}


def validate_module_name(module: Any) -> str:
    """Return the stripped module name or raise ValueError.

    Delegates to the same rules as app/safety/validator.py (the executor's
    validator) so the module manager can never accept a name the executor
    would reject: leading '-', over-length names, '..', shell characters, and
    names that do not start with a letter or digit are all refused here too.
    """
    if module is None:
        raise ValueError("Module name cannot be empty.")

    name = str(module).strip()
    if not name:
        raise ValueError("Module name cannot be empty.")

    errors = module_name_errors(name)
    if errors:
        raise ValueError(f"Invalid module name {module!r}: {'; '.join(errors)}")

    return name


@dataclass(frozen=True)
class SafetyDecision:
    approved: bool
    command: str
    module: str | None = None
    reason: str = ""
    requires_confirmation: bool = False


def evaluate_action(command: str, module: Any = None) -> SafetyDecision:
    if not isinstance(command, str):
        return SafetyDecision(False, "", reason="Action command must be a string.")

    normalized_command = command.strip().lower()
    if normalized_command not in READ_ONLY_ACTIONS | MUTATING_ACTIONS:
        return SafetyDecision(
            False,
            normalized_command,
            reason=f"Unsupported action: {normalized_command or '(empty)'}",
        )

    if normalized_command == "list":
        if module is not None:
            return SafetyDecision(False, normalized_command, reason="'list' does not accept a module name.")
        return SafetyDecision(True, normalized_command)

    try:
        safe_module = validate_module_name(module)
    except ValueError as exc:
        return SafetyDecision(False, normalized_command, reason=str(exc))

    return SafetyDecision(
        True,
        normalized_command,
        safe_module,
        requires_confirmation=normalized_command in MUTATING_ACTIONS,
    )
