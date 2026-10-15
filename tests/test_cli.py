import json

import pytest

from app import cli, orchestrator
from app.manager import module_manager


def test_parse_simple_and_structured_requests():
    assert cli.parse_request("  info uvcvideo  ") == ("info", "uvcvideo")
    assert cli.parse_request('{"command": "list"}') == ("list", None)
    assert cli.parse_request('{"command": "reload", "module": "uvcvideo"}') == (
        "reload",
        "uvcvideo",
    )


def test_parse_request_reports_invalid_commands():
    for request in ("unknown", "load", "list extra", '{"command": "info"}', "{bad json"):
        with pytest.raises(ValueError):
            cli.parse_request(request)


def test_cli_ignores_blank_input_and_exits():
    responses = iter(["", "exit"])
    prompts = []
    output = []

    def fake_input(prompt):
        prompts.append(prompt)
        return next(responses)

    cli.run_cli(input_fn=fake_input, output_fn=output.append)

    assert prompts == [cli.PROMPT, cli.PROMPT]
    assert output[-1] == "Goodbye."
    assert len(output) == 2


def test_cli_confirms_actions_and_formats_success(monkeypatch):
    executed = []

    def execute(command, module, confirm_fn):
        executed.append((command, module, confirm_fn("confirmation")))
        return {
            "success": True,
            "status": "success",
            "operation": command,
            "module": module,
            "message": "Module loaded successfully.",
            "verification": {"expected_loaded": True, "actual_loaded": True, "passed": True},
        }

    monkeypatch.setattr(cli, "execute_action", execute)
    responses = iter(["load uvcvideo", "yes", "quit"])
    output = []

    cli.run_cli(input_fn=lambda prompt: next(responses), output_fn=output.append)

    assert executed == [("load", "uvcvideo", True)]
    assert output[1].startswith("SUCCESS: Module loaded successfully.")
    assert json.loads(output[1].split("\n", 1)[1])["verification"]["passed"] is True


def test_cli_displays_validation_rejections(monkeypatch):
    monkeypatch.setattr(
        cli,
        "execute_action",
        lambda command, module, confirm_fn: orchestrator.execute_action(command, module, confirm_fn),
    )
    responses = iter(["load ../invalid", "quit"])
    output = []

    cli.run_cli(input_fn=lambda prompt: next(responses), output_fn=output.append)

    assert output[1].startswith("VALIDATION REJECTED:")


def test_cli_exits_cleanly_at_end_of_input():
    output = []

    def end_input(prompt):
        raise EOFError

    cli.run_cli(input_fn=end_input, output_fn=output.append)

    assert output[-1] == "\nGoodbye."


def test_orchestrator_approves_and_verifies_confirmed_load(monkeypatch):
    calls = []
    monkeypatch.setattr(
        module_manager,
        "load_module",
        lambda module: calls.append(("load", module)) or {
            "success": True,
            "message": "loaded",
            "error": None,
        },
    )
    monkeypatch.setattr(module_manager, "verify_module_loaded", lambda module: True)

    result = orchestrator.execute_action("load", "uvcvideo", lambda prompt: True)

    assert calls == [("load", "uvcvideo")]
    assert result["status"] == "success"
    assert result["verification"] == {
        "expected_loaded": True,
        "actual_loaded": True,
        "passed": True,
    }


def test_orchestrator_rejects_invalid_action_before_manager(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "load_module",
        lambda module: pytest.fail("rejected action must not reach the manager"),
    )

    result = orchestrator.execute_action("load", "../invalid", lambda prompt: True)

    assert result["status"] == "rejected"
    assert result["error"] == "SafetyPolicyRejected"


def test_orchestrator_declined_confirmation_does_not_execute(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "unload_module",
        lambda module: pytest.fail("declined action must not reach the manager"),
    )

    result = orchestrator.execute_action("unload", "uvcvideo", lambda prompt: False)

    assert result["status"] == "declined"
    assert result["error"] == "ConfirmationDeclined"


def test_orchestrator_fails_if_expected_state_is_not_verified(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "load_module",
        lambda module: {"success": True, "message": "loaded"},
    )
    monkeypatch.setattr(module_manager, "verify_module_loaded", lambda module: False)

    result = orchestrator.execute_action("load", "uvcvideo", lambda prompt: True)

    assert result["success"] is False
    assert result["status"] == "failure"
    assert result["verification"]["passed"] is False


def test_orchestrator_reports_permission_and_execution_failures(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "load_module",
        lambda module: {
            "success": False,
            "message": "Operation not permitted",
            "error": "CommandFailed",
        },
    )
    monkeypatch.setattr(module_manager, "verify_module_loaded", lambda module: False)

    result = orchestrator.execute_action("load", "uvcvideo", lambda prompt: True)
    assert result["status"] == "permission_error"
    assert result["success"] is False

    monkeypatch.setattr(
        module_manager,
        "get_module_info",
        lambda module: {"success": False, "message": "module not found", "error": "ModuleNotFound"},
    )
    failure = orchestrator.execute_action("info", "missing_module")
    assert failure["status"] == "failure"
    assert failure["message"] == "module not found"


def test_orchestrator_reports_permission_exception(monkeypatch):
    def denied(module):
        raise PermissionError("permission denied")

    monkeypatch.setattr(module_manager, "load_module", denied)

    result = orchestrator.execute_action("load", "uvcvideo", lambda prompt: True)

    assert result["status"] == "permission_error"
    assert result["error"] == "PermissionDenied"
