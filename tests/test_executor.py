from typing import Any

import pytest

from app.agent import ActionExecutor
from app.safety import SafetyPolicy


class FakeModuleManager:
    def __init__(self) -> None:
        self.loaded = {"snd"}
        self.calls: list[tuple[str, str | None]] = []
        self.load_result: dict[str, Any] = {
            "success": True,
            "operation": "load",
            "message": "loaded",
        }

    def list_modules(self) -> list[dict[str, Any]]:
        self.calls.append(("list_modules", None))
        return [{"name": module} for module in sorted(self.loaded)]

    def get_module_info(self, module: str) -> dict[str, Any]:
        self.calls.append(("get_module_info", module))
        return {"success": True, "name": module}

    def is_module_loaded(self, module: str) -> bool:
        self.calls.append(("is_module_loaded", module))
        return module in self.loaded

    def load_module(self, module: str) -> dict[str, Any]:
        self.calls.append(("load_module", module))
        self.loaded.add(module)
        return dict(self.load_result)

    def unload_module(self, module: str) -> dict[str, Any]:
        self.calls.append(("unload_module", module))
        self.loaded.discard(module)
        return {"success": True, "operation": "unload"}

    def reload_module(self, module: str) -> dict[str, Any]:
        self.calls.append(("reload_module", module))
        return {"success": True, "operation": "reload"}


@pytest.fixture
def manager():
    return FakeModuleManager()


@pytest.fixture
def executor(manager):
    policy = SafetyPolicy(
        is_module_loaded=manager.is_module_loaded,
        protected_modules=(),
    )
    return ActionExecutor(manager=manager, policy=policy)


def test_invalid_input_is_rejected_before_manager_calls(executor, manager):
    result = executor.execute({"operation": "run_shell"})

    assert result.success is False
    assert result.status == "rejected"
    assert "Unsupported operation" in result.errors[0]
    assert manager.calls == []


def test_valid_read_operation_is_dispatched(executor, manager):
    result = executor.execute({"operation": "list_modules"})

    assert result.success is True
    assert result.status == "success"
    assert result.result == [{"name": "snd"}]
    assert manager.calls == [("list_modules", None)]


@pytest.mark.parametrize(
    "action,expected",
    [
        (
            {"operation": "get_module_info", "module": "uvcvideo"},
            ("get_module_info", "uvcvideo"),
        ),
        (
            {"operation": "check_module", "module": "snd"},
            ("is_module_loaded", "snd"),
        ),
    ],
)
def test_module_read_operations_dispatch_to_manager(executor, manager, action, expected):
    result = executor.execute(action)

    assert result.success is True
    assert expected in manager.calls
    if action["operation"] == "check_module":
        assert result.result == {"loaded": True}


def test_dry_run_returns_preview_without_mutating_manager(executor, manager):
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"},
        dry_run=True,
    )

    assert result.success is True
    assert result.status == "dry_run"
    assert result.requires_confirmation is True
    assert result.result["would_execute"] is True
    assert "load_module uvcvideo" in result.result["summary"]
    assert ("load_module", "uvcvideo") not in manager.calls
    assert "uvcvideo" not in manager.loaded


def test_dry_run_rejects_protected_action_without_dispatch(manager):
    executor = ActionExecutor(manager=manager)
    result = executor.execute(
        {"operation": "unload_module", "module": "ext4"},
        dry_run=True,
    )

    assert result.success is False
    assert result.status == "rejected"
    assert "protected" in result.errors[0]
    assert ("unload_module", "ext4") not in manager.calls


def test_disruptive_action_without_confirmation_does_not_dispatch(
    executor, manager
):
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"}
    )

    assert result.success is False
    assert result.status == "confirmation_required"
    assert result.requires_confirmation is True
    assert ("load_module", "uvcvideo") not in manager.calls
    assert "uvcvideo" not in manager.loaded


def test_declined_confirmation_does_not_dispatch(executor, manager):
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"},
        confirmation_handler=lambda _: False,
    )

    assert result.success is False
    assert result.status == "confirmation_declined"
    assert result.confirmed is False
    assert ("load_module", "uvcvideo") not in manager.calls


def test_approved_disruptive_action_dispatches(manager):
    executor = ActionExecutor(
        manager=manager,
        policy=SafetyPolicy(manager.is_module_loaded, protected_modules=()),
        confirmation_handler=lambda _: True,
    )
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"}
    )

    assert result.success is True
    assert result.status == "success"
    assert result.confirmed is True
    assert result.result["success"] is True
    assert ("load_module", "uvcvideo") in manager.calls
    assert "uvcvideo" in manager.loaded


def test_load_manager_failure_is_returned_as_structured_error(executor, manager):
    manager.load_result = {
        "success": False,
        "error": "CommandFailed",
        "message": "permission denied",
    }
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"},
        confirmation_handler=lambda _: True,
    )

    assert result.success is False
    assert result.status == "manager_error"
    assert result.errors == ["permission denied"]


def test_state_conflict_is_rejected_before_confirmation(executor, manager):
    prompted = False

    def confirm(_):
        nonlocal prompted
        prompted = True
        return True

    result = executor.execute(
        {"operation": "load_module", "module": "snd"},
        confirmation_handler=confirm,
    )

    assert result.success is False
    assert result.status == "rejected"
    assert "already loaded" in result.errors[0]
    assert prompted is False
    assert ("load_module", "snd") not in manager.calls


@pytest.mark.parametrize("response", [None, "yes", 1])
def test_invalid_confirmation_is_reported_without_manager_call(
    executor, manager, response
):
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"},
        confirmation_handler=lambda _: response,
    )

    assert result.success is False
    assert result.status == "confirmation_invalid"
    assert result.errors == ["confirmation handler must return a bool"]
    assert ("load_module", "uvcvideo") not in manager.calls


def test_manager_exception_is_a_structured_error_after_confirmation():
    class FailingManager(FakeModuleManager):
            def load_module(self, module: str) -> dict[str, Any]:
                raise OSError(f"modprobe unavailable for {module}")

    failing_manager = FailingManager()
    executor = ActionExecutor(
        manager=failing_manager,
        policy=SafetyPolicy(failing_manager.is_module_loaded, protected_modules=()),
    )
    result = executor.execute(
        {"operation": "load_module", "module": "uvcvideo"},
        confirmation_handler=lambda _: True,
    )

    assert result.success is False
    assert result.status == "error"
    assert result.errors == ["OSError: modprobe unavailable for uvcvideo"]


def test_result_serializes_to_json_compatible_mapping(executor):
    result = executor.execute({"operation": "list_modules"})

    serialized = result.to_dict()

    assert serialized["success"] is True
    assert serialized["status"] == "success"
    assert serialized["action"] == {"operation": "list_modules"}
    assert isinstance(serialized["errors"], list)
