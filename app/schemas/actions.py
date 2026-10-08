"""Structured action schema.

This is the ONLY thing the (future) LLM is allowed to produce. Instead of
free-form shell commands, the LLM emits a small JSON object such as:

    {"operation": "reload_module", "module": "uvcvideo"}

This module turns that raw JSON/dict into a `ModuleAction` object and rejects
anything that is structurally wrong (unknown operation, missing/extra fields,
wrong types). It does NOT check whether the module name is a well-formed
kernel module name - that is the validator's job (app/safety/validator.py) -
and it never touches the Linux system.

Layer order:  Structured Action -> Validator -> Policy -> Dry run /
Confirmation -> Module Manager.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional


class ActionSchemaError(ValueError):
    """Raised when a raw action does not match the structured action schema."""


class Operation(str, Enum):
    """The closed set of operations the agent can perform.

    Anything not listed here is rejected before it reaches any other layer.
    """

    LIST_MODULES = "list_modules"
    GET_MODULE_INFO = "get_module_info"
    CHECK_MODULE = "check_module"
    LOAD_MODULE = "load_module"
    UNLOAD_MODULE = "unload_module"
    RELOAD_MODULE = "reload_module"

    @property
    def requires_module(self) -> bool:
        """True when the operation acts on a single, named module."""
        return self is not Operation.LIST_MODULES

    @classmethod
    def parse(cls, value: Any) -> "Operation":
        """Convert a raw value (normally a string from JSON) into an Operation."""
        if isinstance(value, Operation):
            return value
        if not isinstance(value, str):
            raise ActionSchemaError(
                f"'operation' must be a string, got {type(value).__name__}"
            )
        normalised = value.strip().lower()
        for operation in cls:
            if operation.value == normalised:
                return operation
        raise ActionSchemaError(
            f"Unsupported operation {value!r}. Allowed operations: {ALLOWED_OPERATIONS}"
        )


ALLOWED_OPERATIONS = tuple(operation.value for operation in Operation)

# The only keys a structured action may contain.
ALLOWED_FIELDS = frozenset({"operation", "module"})


@dataclass(frozen=True)
class ModuleAction:
    """A validated, immutable structured action.

    `frozen=True` means later layers cannot accidentally mutate the action
    (for example, swapping the module name after validation).
    """

    operation: Operation
    module: Optional[str] = None

    def __post_init__(self) -> None:
        # Enforce the schema even when constructed directly in Python, not
        # only via from_dict(), so there is no way to build a malformed action.
        if not isinstance(self.operation, Operation):
            raise ActionSchemaError("operation must be an Operation value")

        if self.operation.requires_module:
            if self.module is None:
                raise ActionSchemaError(
                    f"Operation '{self.operation.value}' requires a 'module' name"
                )
            if not isinstance(self.module, str):
                raise ActionSchemaError(
                    f"'module' must be a string, got {type(self.module).__name__}"
                )
            if not self.module.strip():
                raise ActionSchemaError("'module' must not be empty")
            # Store the stripped name so later layers see exactly one form.
            object.__setattr__(self, "module", self.module.strip())
        else:
            if self.module is not None:
                raise ActionSchemaError(
                    f"Operation '{self.operation.value}' does not take a 'module'"
                )

    @classmethod
    def from_dict(cls, raw: Any) -> "ModuleAction":
        """Build a ModuleAction from a raw mapping (e.g. parsed LLM JSON)."""
        if not isinstance(raw, Mapping):
            raise ActionSchemaError(
                f"Action must be a JSON object/dict, got {type(raw).__name__}"
            )

        unknown = set(raw.keys()) - ALLOWED_FIELDS
        if unknown:
            raise ActionSchemaError(
                f"Unexpected field(s) in action: {sorted(unknown)}. "
                f"Allowed fields: {sorted(ALLOWED_FIELDS)}"
            )

        if "operation" not in raw:
            raise ActionSchemaError("Action is missing required field 'operation'")

        operation = Operation.parse(raw["operation"])
        module = raw.get("module")

        # Treat an explicit `"module": null` the same as an absent module.
        return cls(operation=operation, module=module)

    @classmethod
    def from_json(cls, text: str) -> "ModuleAction":
        """Build a ModuleAction from a JSON string (the raw LLM output)."""
        if not isinstance(text, str):
            raise ActionSchemaError("JSON action must be a string")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ActionSchemaError(f"Action is not valid JSON: {exc.msg}") from exc
        return cls.from_dict(raw)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise back to the plain JSON-compatible form."""
        data: Dict[str, Any] = {"operation": self.operation.value}
        if self.module is not None:
            data["module"] = self.module
        return data

    def describe(self) -> str:
        """Short human-readable form, used in dry-run and confirmation messages."""
        if self.module is None:
            return self.operation.value
        return f"{self.operation.value} {self.module}"
