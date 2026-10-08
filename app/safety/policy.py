"""State-aware safety decisions for validated module actions.

This layer decides whether an operation is allowed and whether it needs
confirmation. It never performs Linux commands; state is supplied by the
module manager through an injected checker.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from app.schemas import ModuleAction, Operation

from .validator import module_name_errors, normalise_module_name, validate_action

DEFAULT_PROTECTED_MODULES = frozenset({"btrfs", "dm_mod", "ext4", "overlay", "xfs"})

_MUTATING_OPERATIONS = frozenset(
    {Operation.LOAD_MODULE, Operation.UNLOAD_MODULE, Operation.RELOAD_MODULE}
)
_REMOVING_OPERATIONS = frozenset({Operation.UNLOAD_MODULE, Operation.RELOAD_MODULE})


class RiskLevel(str, Enum):
    """Broad impact category used by the executor and confirmation layer."""

    READ_ONLY = "read_only"
    DISRUPTIVE = "disruptive"


@dataclass
class PolicyResult:
    """Structured allow/reject decision from the safety policy."""

    allowed: bool
    risk_level: RiskLevel
    requires_confirmation: bool = False
    errors: list[str] = field(default_factory=list)
    action: ModuleAction | None = None

    @classmethod
    def rejected(
        cls,
        risk_level: RiskLevel,
        *errors: str,
        action: ModuleAction | None = None,
    ) -> PolicyResult:
        return cls(
            allowed=False,
            risk_level=risk_level,
            errors=list(errors),
            action=action,
        )


class SafetyPolicy:
    """Evaluate actions against protected-module and live-state rules.

    By default, a small set of common filesystem/storage modules cannot be
    unloaded or reloaded. Supply ``protected_modules`` to replace this set;
    an empty iterable disables the defaults. Mutating operations require a
    state checker, which should be the module manager's ``is_module_loaded``.
    """

    def __init__(
        self,
        is_module_loaded: Callable[[str], bool] | None = None,
        protected_modules: Iterable[str] | None = None,
    ) -> None:
        self._is_module_loaded = is_module_loaded
        configured_modules = (
            DEFAULT_PROTECTED_MODULES
            if protected_modules is None
            else protected_modules
        )
        if isinstance(configured_modules, str):
            raise ValueError("protected_modules must be an iterable of module names")

        self._protected_modules = frozenset(
            self._normalise_protected_module(module)
            for module in configured_modules
        )

    @staticmethod
    def _normalise_protected_module(module: str) -> str:
        errors = module_name_errors(module)
        if errors:
            raise ValueError(
                f"invalid protected module {module!r}: {'; '.join(errors)}"
            )
        return normalise_module_name(module)

    @property
    def protected_modules(self) -> frozenset[str]:
        """Canonical module names protected from unload and reload."""
        return self._protected_modules

    def evaluate(self, action: ModuleAction) -> PolicyResult:
        """Return a policy decision without executing the requested action."""
        validation = validate_action(action)
        if not validation.ok or validation.action is None:
            return PolicyResult.rejected(
                RiskLevel.READ_ONLY,
                *validation.errors,
            )

        action = validation.action
        if action.operation not in _MUTATING_OPERATIONS:
            return PolicyResult(
                allowed=True,
                risk_level=RiskLevel.READ_ONLY,
                action=action,
            )

        if self._is_module_loaded is None:
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                "module state checker is required for mutating operations",
                action=action,
            )

        module = action.module
        if module is None:
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                "mutating operation requires a module name",
                action=action,
            )

        if (
            action.operation in _REMOVING_OPERATIONS
            and module in self._protected_modules
        ):
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                f"module {module!r} is protected from "
                f"{action.operation.value}",
                action=action,
            )

        try:
            loaded = self._is_module_loaded(module)
        except (OSError, RuntimeError) as exc:
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                f"could not determine whether module {module!r} is loaded: {exc}",
                action=action,
            )

        if not isinstance(loaded, bool):
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                "module state checker must return a bool",
                action=action,
            )

        if action.operation is Operation.LOAD_MODULE and loaded:
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                f"module {module!r} is already loaded",
                action=action,
            )

        if action.operation in _REMOVING_OPERATIONS and not loaded:
            return PolicyResult.rejected(
                RiskLevel.DISRUPTIVE,
                f"module {module!r} is not loaded",
                action=action,
            )

        return PolicyResult(
            allowed=True,
            risk_level=RiskLevel.DISRUPTIVE,
            requires_confirmation=True,
            action=action,
        )
