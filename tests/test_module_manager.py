from types import SimpleNamespace

import pytest

from app import orchestrator
from app.manager import module_manager


def test_list_modules_parses_lsmod_output(monkeypatch):
    sample = "Module Size Used by\nusbcore 245760 8\nsnd 409600 1 snd_hda_intel\n"
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=0, stdout=sample, stderr=""),
    )

    modules = module_manager.list_modules()

    assert modules[0]["name"] == "usbcore"
    assert modules[0]["size"] == "245760"
    assert modules[1]["used_by"] == [1]


def test_verify_module_loaded_reports_actual_state(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(
            returncode=0,
            stdout="Module Size Used by\nuvcvideo 1234 0\n",
            stderr="",
        ),
    )

    assert module_manager.verify_module_loaded("uvcvideo") is True
    assert module_manager.verify_module_loaded("missing_module") is False


def test_verify_module_loaded_does_not_assume_failed_lsmod_means_unloaded(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=1, stdout="", stderr="lsmod failed"),
    )

    try:
        module_manager.verify_module_loaded("uvcvideo")
    except RuntimeError as exc:
        assert str(exc) == "lsmod failed"
    else:
        raise AssertionError("Expected verification to fail when lsmod fails")


def test_list_modules_propagates_missing_utility(monkeypatch):
    def missing(args):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(module_manager, "_run_command", missing)

    try:
        module_manager.list_modules()
    except FileNotFoundError as exc:
        assert str(exc) == "lsmod"
    else:
        raise AssertionError("Expected a missing lsmod utility to be reported")


def test_list_modules_reports_permission_failure(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted"),
    )

    try:
        module_manager.list_modules()
    except PermissionError as exc:
        assert str(exc) == "Operation not permitted"
    else:
        raise AssertionError("Expected lsmod permission failure to be reported")


@pytest.mark.parametrize("output", ["", "unexpected header\n", "Module Size Used by\nmalformed\n"])
def test_list_modules_rejects_malformed_success_output(monkeypatch, output):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )

    try:
        module_manager.list_modules_checked()
    except RuntimeError as exc:
        assert "malformed output" in str(exc)
    else:
        raise AssertionError("Expected malformed lsmod output to be rejected")


def test_load_module_classifies_permission_and_command_failures(monkeypatch):
    monkeypatch.setattr(module_manager, "is_module_loaded", lambda module: False)
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted"),
    )

    denied = module_manager.load_module("uvcvideo")
    assert denied["error"] == "PermissionDenied"

    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=1, stdout="", stderr="unknown module"),
    )
    failed = module_manager.load_module("uvcvideo")
    assert failed["error"] == "CommandFailed"


def test_inspection_reports_missing_utility(monkeypatch):
    def missing(args):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(module_manager, "_run_command", missing)

    result = module_manager.get_module_info("uvcvideo")

    assert result["success"] is False
    assert result["error"] == "CommandNotFound"


def test_orchestrator_reports_failed_module_listing(monkeypatch):
    def fail_listing():
        raise RuntimeError("lsmod unavailable")

    monkeypatch.setattr(
        module_manager,
        "list_modules_checked",
        fail_listing,
    )

    result = orchestrator.execute_action("list")

    assert result["success"] is False
    assert result["status"] == "failure"
    # The CLI summary is the executor's message; the utility's own text is in `error`.
    assert result["message"] == "Module manager operation failed."
    assert result["error"] == "RuntimeError: lsmod unavailable"
    assert result["modules"] is None


def test_get_module_info_parses_modinfo(monkeypatch):
    sample = "\n".join([
        "filename: /lib/modules/6.8.0-xx-generic/kernel/drivers/media/usb/uvc/uvcvideo.ko",
        "name: uvcvideo",
        "description: USB Video Class driver",
        "author: Linux UVC project",
        "version: 1.0",
        "depends: videobuf2_v4l2,videobuf2_common,media",
        "parm: nobright:Enable brightness control",
    ])
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=0, stdout=sample, stderr=""),
    )

    info = module_manager.get_module_info("uvcvideo")

    assert info["success"] is True
    assert info["name"] == "uvcvideo"
    assert info["description"] == "USB Video Class driver"
    assert info["depends"] == "videobuf2_v4l2,videobuf2_common,media"


def test_is_module_loaded_detects_loaded_module(monkeypatch):
    # verify_module_loaded reads list_modules_checked, so mock that dependency.
    monkeypatch.setattr(
        module_manager,
        "list_modules_checked",
        lambda: [{"name": "uvcvideo", "size": "1234", "used_by": []}],
    )

    assert module_manager.is_module_loaded("uvcvideo") is True
    assert module_manager.is_module_loaded("nonexistent_module") is False


def test_invalid_module_name_is_rejected():
    result = module_manager.load_module("../bad; rm -rf /")
    assert result["success"] is False
    assert result["error"] == "InvalidModuleName"


def test_load_module_returns_already_loaded(monkeypatch):
    monkeypatch.setattr(module_manager, "is_module_loaded", lambda module: True)

    result = module_manager.load_module("uvcvideo")

    assert result["success"] is False
    assert result["error"] == "ModuleAlreadyLoaded"


def test_unload_module_returns_not_loaded(monkeypatch):
    monkeypatch.setattr(module_manager, "is_module_loaded", lambda module: False)

    result = module_manager.unload_module("uvcvideo")

    assert result["success"] is False
    assert result["error"] == "ModuleNotLoaded"


def test_reload_module_verifies_state(monkeypatch):
    calls = {"unload": False, "load": False}

    def fake_unload(module):
        calls["unload"] = True
        return {"success": True, "module": module, "operation": "unload", "message": "ok", "error": None}

    def fake_load(module):
        calls["load"] = True
        return {"success": True, "module": module, "operation": "load", "message": "ok", "error": None}

    monkeypatch.setattr(module_manager, "is_module_loaded", lambda module: True)
    monkeypatch.setattr(module_manager, "unload_module", fake_unload)
    monkeypatch.setattr(module_manager, "load_module", fake_load)

    result = module_manager.reload_module("uvcvideo")

    assert result["success"] is True
    assert calls["unload"] is True
    assert calls["load"] is True
    assert result["verified_loaded"] is True


def test_reload_module_loads_when_not_loaded(monkeypatch):
    monkeypatch.setattr(module_manager, "is_module_loaded", lambda module: False)
    monkeypatch.setattr(module_manager, "load_module", lambda module: {"success": True, "module": module, "operation": "load", "message": "ok", "error": None})

    result = module_manager.reload_module("uvcvideo")

    assert result["success"] is False
    assert result["verified_loaded"] is False


def test_get_module_info_missing_module_returns_error(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=1, stdout="", stderr="modinfo: ERROR: Module not found"),
    )

    result = module_manager.get_module_info("missing_module")

    assert result["success"] is False
    assert result["error"] == "ModuleNotFound"


def test_get_module_info_rejects_success_with_unrecognized_output(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=0, stdout="unexpected output\n", stderr=""),
    )

    result = module_manager.get_module_info("uvcvideo")

    assert result["success"] is False
    assert result["error"] == "MalformedOutput"


# --- loaded-state errors are never reported as "not loaded" ------------------


def _lsmod(monkeypatch, returncode=0, stdout="Module Size Used by\n", stderr=""):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr),
    )


def test_is_module_loaded_false_only_when_lsmod_answered(monkeypatch):
    _lsmod(monkeypatch, stdout="Module Size Used by\nsnd 16384 0\n")

    assert module_manager.is_module_loaded("snd") is True
    assert module_manager.is_module_loaded("uvcvideo") is False


@pytest.mark.parametrize("module", ["../x", "-r", "snd; reboot", "_hidden", "a" * 80, ""])
def test_is_module_loaded_raises_for_invalid_names(monkeypatch, module):
    _lsmod(monkeypatch)

    with pytest.raises(ValueError):
        module_manager.is_module_loaded(module)


def test_is_module_loaded_raises_when_lsmod_fails(monkeypatch):
    _lsmod(monkeypatch, returncode=1, stderr="lsmod: command failed")

    with pytest.raises(RuntimeError, match="lsmod: command failed"):
        module_manager.is_module_loaded("snd")


def test_is_module_loaded_raises_permission_error(monkeypatch):
    _lsmod(monkeypatch, returncode=1, stderr="lsmod: Permission denied")

    with pytest.raises(PermissionError, match="Permission denied"):
        module_manager.is_module_loaded("snd")


def test_is_module_loaded_raises_on_malformed_lsmod_output(monkeypatch):
    _lsmod(monkeypatch, stdout="unexpected\n")

    with pytest.raises(RuntimeError, match="malformed"):
        module_manager.is_module_loaded("snd")


# --- the manager uses the same strict validator as the executor ---------------


@pytest.mark.parametrize("module", ["-r", "--force", "a" * 56, "_hidden", ".snd", "snd..hda", "snd hda", "snd/x", "uvcvidéo"])
def test_manager_validator_rejects_what_the_executor_rejects(monkeypatch, module):
    calls = []
    monkeypatch.setattr(module_manager, "_run_command", lambda args: calls.append(args))

    with pytest.raises(ValueError, match="Invalid module name"):
        module_manager.validate_module_name(module)
    for operation in (module_manager.load_module, module_manager.unload_module, module_manager.reload_module):
        result = operation(module)
        assert result["success"] is False
        assert result["error"] == "InvalidModuleName"
    assert module_manager.get_module_info(module)["error"] == "InvalidModuleName"
    assert calls == [], "no utility may run for an invalid name"


@pytest.mark.parametrize("module", ["uvcvideo", "snd-hda-intel", "snd_hda_intel", "a" * 55, "v4l2.core"])
def test_manager_validator_accepts_valid_names(module):
    assert module_manager.validate_module_name(f"  {module} ") == module
