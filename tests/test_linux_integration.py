"""Opt-in, read-only integration checks against a real Linux host."""

import os
import shutil
import sys

import pytest

from app.manager import module_manager


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("CAMAGENT_RUN_LINUX_INTEGRATION") != "1",
    reason="requires Linux and CAMAGENT_RUN_LINUX_INTEGRATION=1",
)


def _require_utilities(*utilities: str) -> None:
    missing = [utility for utility in utilities if shutil.which(utility) is None]
    if missing:
        pytest.skip(f"Linux module utilities unavailable: {', '.join(missing)}")


def _integration_module() -> str:
    return os.environ.get("CAMAGENT_TEST_MODULE", "loop")


def test_linux_lists_loaded_modules():
    _require_utilities("lsmod")

    modules = module_manager.list_modules_checked()

    assert isinstance(modules, list)
    assert all({"name", "size", "used_by"} <= module.keys() for module in modules)


def test_linux_inspects_module_metadata():
    _require_utilities("modinfo")
    module = _integration_module()

    info = module_manager.get_module_info(module)

    assert info["success"] is True, info
    assert info["name"] == module
    assert info["module"] == module
    assert info["filename"], info


def test_linux_checks_module_load_state_against_lsmod():
    _require_utilities("lsmod")
    module = _integration_module()
    loaded_names = {item["name"] for item in module_manager.list_modules_checked()}

    assert module_manager.verify_module_loaded(module) is (module in loaded_names)
