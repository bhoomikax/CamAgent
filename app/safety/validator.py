"""Input / module-name validation.

The schema layer (app/schemas/actions.py) guarantees an action has the right
*shape*. This layer checks the *content* of the module name:

* only characters that can appear in a real kernel module name,
* no shell metacharacters or path separators (defence in depth, even though
  the module manager never uses a shell),
* must not start with '-' so it can never be mistaken for a modprobe flag
  (e.g. a module called "-r" or "--help"),
* a sane length limit (the kernel caps module names at MODULE_NAME_LEN).

It also canonicalises '-' to '_' because modprobe treats them as equivalent
and `lsmod` always reports underscores, so later state checks compare like
with like.

This module NEVER runs Linux commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.schemas import ActionSchemaError, ModuleAction

# Kernel module names: letters, digits, '_', '-', and '.'; must start with a
# letter or digit. (See include/linux/module.h: MODULE_NAME_LEN.)
MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# MODULE_NAME_LEN is 64 - sizeof(unsigned long) == 56 on 64-bit kernels;
# one byte is reserved for the terminating NUL.
MAX_MODULE_NAME_LENGTH = 55

# Characters that would be dangerous if a shell were ever involved, or that
# would let a name escape the modules directory. Listed explicitly so the
# rejection message can name the exact offending character.
FORBIDDEN_CHARACTERS = (
    " ", "\t", "\n", "\r", ";", "|", "&", "$", "`", "<", ">",
    "(", ")", "{", "}", "[", "]", "'", '"', "\\", "/", "*", "?", "!", "#",
)


@dataclass
class ValidationResult:
    """Outcome of validating an action. `action` is set only when `ok`."""

    ok: bool
    errors: List[str] = field(default_factory=list)
    action: Optional[ModuleAction] = None

    @classmethod
    def rejected(cls, *errors: str) -> "ValidationResult":
        return cls(ok=False, errors=list(errors))

    @classmethod
    def accepted(cls, action: ModuleAction) -> "ValidationResult":
        return cls(ok=True, action=action)


def module_name_errors(name: Any) -> List[str]:
    """Return a list of problems with a module name (empty list == valid)."""
    if not isinstance(name, str):
        return [f"module name must be a string, got {type(name).__name__}"]

    errors: List[str] = []

    if not name.strip():
        errors.append("module name must not be empty")
        return errors

    if len(name) > MAX_MODULE_NAME_LENGTH:
        errors.append(
            f"module name is too long ({len(name)} > {MAX_MODULE_NAME_LENGTH} characters)"
        )

    bad_chars = sorted({ch for ch in name if ch in FORBIDDEN_CHARACTERS})
    if bad_chars:
        errors.append(f"module name contains forbidden character(s): {bad_chars}")

    if ".." in name:
        errors.append("module name must not contain '..'")

    if name.startswith("-"):
        errors.append("module name must not start with '-' (looks like a command flag)")

    # Catch-all for anything the specific checks above did not name
    # (e.g. non-ASCII or control characters).
    if not errors and not MODULE_NAME_RE.fullmatch(name):
        errors.append(
            "module name may only contain letters, digits, '_', '-' and '.' "
            "and must start with a letter or digit"
        )

    return errors


def is_valid_module_name(name: Any) -> bool:
    return not module_name_errors(name)


def normalise_module_name(name: str) -> str:
    """Canonical form used by the kernel: modprobe maps '-' to '_'."""
    return name.strip().replace("-", "_")


def validate_action(action: ModuleAction) -> ValidationResult:
    """Validate the content of an already schema-checked action."""
    if not isinstance(action, ModuleAction):
        return ValidationResult.rejected(
            f"expected a ModuleAction, got {type(action).__name__}"
        )

    if not action.operation.requires_module:
        return ValidationResult.accepted(action)

    errors = module_name_errors(action.module)
    if errors:
        return ValidationResult.rejected(*errors)

    canonical = normalise_module_name(action.module)  # type: ignore[arg-type]
    if canonical != action.module:
        # Actions are frozen, so build a new one with the canonical name.
        action = ModuleAction(operation=action.operation, module=canonical)

    return ValidationResult.accepted(action)


def validate_raw(raw: Any) -> ValidationResult:
    """Schema check + content check in one call (raw dict or JSON string)."""
    try:
        if isinstance(raw, str):
            action = ModuleAction.from_json(raw)
        else:
            action = ModuleAction.from_dict(raw)
    except ActionSchemaError as exc:
        return ValidationResult.rejected(str(exc))

    return validate_action(action)
