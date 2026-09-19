from types import SimpleNamespace

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
    monkeypatch.setattr(
        module_manager,
        "list_modules",
        lambda: [{"name": "uvcvideo", "size": "1234", "used_by": []}],
    )

    assert module_manager.is_module_loaded("uvcvideo") is True
    assert module_manager.is_module_loaded("nonexistent_module") is False


def test_invalid_module_name_is_rejected():
    result = module_manager.load_module("../bad; rm -rf /")
    assert result["success"] is False
    assert result["error"] == "InvalidModuleName"


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


def test_get_module_info_missing_module_returns_error(monkeypatch):
    monkeypatch.setattr(
        module_manager,
        "_run_command",
        lambda args: SimpleNamespace(returncode=1, stdout="", stderr="modinfo: ERROR: Module not found"),
    )

    result = module_manager.get_module_info("missing_module")

    assert result["success"] is False
    assert result["error"] == "ModuleNotFound"
