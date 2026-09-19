import logging
import re
import subprocess
from typing import Any, Dict, List

logger = logging.getLogger("module_manager")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_module_name(module: Any) -> str:
    if module is None:
        raise ValueError("Module name cannot be empty.")

    name = str(module).strip()
    if not name:
        raise ValueError("Module name cannot be empty.")

    blocked_tokens = ("/", "..", ";", "|", "&", "$", "\n", "\r")
    if any(token in name for token in blocked_tokens):
        raise ValueError(f"Invalid module name: {module!r}")

    if not MODULE_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid module name: {module!r}")

    return name


def _run_command(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _log_operation(operation: str, module: str, success: bool, message: str, error: str | None = None) -> None:
    logger.info(
        "operation=%s module=%s result=%s message=%s error=%s",
        operation,
        module,
        "success" if success else "failed",
        message,
        error,
    )


def list_modules() -> List[Dict[str, Any]]:
    try:
        result = _run_command(["lsmod"])
    except FileNotFoundError as exc:
        logger.error("lsmod is not available: %s", exc)
        return []

    if result.returncode != 0:
        logger.error("lsmod failed: %s", result.stderr.strip())
        return []

    lines = result.stdout.strip().splitlines()
    if len(lines) < 2:
        return []

    modules: List[Dict[str, Any]] = []
    for line in lines[1:]:
        if not line.strip():
            continue

        parts = line.split()
        if len(parts) < 3:
            continue

        name = parts[0]
        size = parts[1]
        used_by_value = parts[2] if len(parts) >= 3 else ""
        used_by: List[int] = []

        if used_by_value.isdigit():
            used_by = [int(used_by_value)]
        else:
            used_by_raw = " ".join(parts[2:])
            for token in re.split(r"[\s,]+", used_by_raw.strip()):
                if not token:
                    continue
                try:
                    used_by.append(int(token))
                except ValueError:
                    continue

        modules.append({
            "name": name,
            "size": size,
            "used_by": used_by,
        })

    return modules


def get_module_info(module: str) -> Dict[str, Any]:
    try:
        safe_module = _validate_module_name(module)
    except ValueError as exc:
        return {
            "success": False,
            "module": str(module),
            "operation": "get_module_info",
            "message": str(exc),
            "error": "InvalidModuleName",
        }

    try:
        result = _run_command(["modinfo", safe_module])
    except FileNotFoundError as exc:
        logger.error("modinfo is not available: %s", exc)
        return {
            "success": False,
            "module": safe_module,
            "operation": "get_module_info",
            "message": "modinfo command is unavailable",
            "error": "CommandNotFound",
        }

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "module not found"
        logger.warning("modinfo failed for %s: %s", safe_module, message)
        return {
            "success": False,
            "module": safe_module,
            "operation": "get_module_info",
            "message": message,
            "error": "ModuleNotFound",
        }

    info: Dict[str, Any] = {
        "name": safe_module,
        "filename": "",
        "description": "",
        "author": "",
        "version": "",
        "depends": "",
        "parameters": "",
    }

    for line in result.stdout.splitlines():
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "filename":
            info["filename"] = value
        elif key == "description":
            info["description"] = value
        elif key == "author":
            info["author"] = value
        elif key == "version":
            info["version"] = value
        elif key == "depends":
            info["depends"] = value
        elif key == "parm":
            info["parameters"] = value if not info["parameters"] else f"{info['parameters']}; {value}"
        elif key == "name":
            info["name"] = value

    return {**info, "success": True, "module": safe_module, "operation": "get_module_info"}


def is_module_loaded(module: str) -> bool:
    try:
        safe_module = _validate_module_name(module)
    except ValueError:
        return False

    for item in list_modules():
        if item.get("name") == safe_module:
            return True

    return False


def load_module(module: str) -> Dict[str, Any]:
    try:
        safe_module = _validate_module_name(module)
    except ValueError as exc:
        return {
            "success": False,
            "module": str(module),
            "operation": "load",
            "message": str(exc),
            "error": "InvalidModuleName",
        }

    if is_module_loaded(safe_module):
        _log_operation("load", safe_module, False, "module is already loaded", "ModuleAlreadyLoaded")
        return {
            "success": False,
            "module": safe_module,
            "operation": "load",
            "message": "module is already loaded",
            "error": "ModuleAlreadyLoaded",
        }

    try:
        result = _run_command(["modprobe", safe_module])
    except FileNotFoundError as exc:
        logger.error("modprobe is not available: %s", exc)
        return {
            "success": False,
            "module": safe_module,
            "operation": "load",
            "message": "modprobe command is unavailable",
            "error": "CommandNotFound",
        }

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "load failed"
        logger.warning("modprobe failed for %s: %s", safe_module, message)
        _log_operation("load", safe_module, False, message, "CommandFailed")
        return {
            "success": False,
            "module": safe_module,
            "operation": "load",
            "message": message,
            "error": "CommandFailed",
        }

    _log_operation("load", safe_module, True, "module loaded successfully", None)
    return {
        "success": True,
        "module": safe_module,
        "operation": "load",
        "message": "module loaded successfully",
        "error": None,
    }


def unload_module(module: str) -> Dict[str, Any]:
    try:
        safe_module = _validate_module_name(module)
    except ValueError as exc:
        return {
            "success": False,
            "module": str(module),
            "operation": "unload",
            "message": str(exc),
            "error": "InvalidModuleName",
        }

    if not is_module_loaded(safe_module):
        _log_operation("unload", safe_module, False, "module is not loaded", "ModuleNotLoaded")
        return {
            "success": False,
            "module": safe_module,
            "operation": "unload",
            "message": "module is not loaded",
            "error": "ModuleNotLoaded",
        }

    try:
        result = _run_command(["modprobe", "-r", safe_module])
    except FileNotFoundError as exc:
        logger.error("modprobe is not available: %s", exc)
        return {
            "success": False,
            "module": safe_module,
            "operation": "unload",
            "message": "modprobe command is unavailable",
            "error": "CommandNotFound",
        }

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unload failed"
        logger.warning("modprobe -r failed for %s: %s", safe_module, message)
        _log_operation("unload", safe_module, False, message, "CommandFailed")
        return {
            "success": False,
            "module": safe_module,
            "operation": "unload",
            "message": message,
            "error": "CommandFailed",
        }

    _log_operation("unload", safe_module, True, "module unloaded successfully", None)
    return {
        "success": True,
        "module": safe_module,
        "operation": "unload",
        "message": "module unloaded successfully",
        "error": None,
    }


def reload_module(module: str) -> Dict[str, Any]:
    try:
        safe_module = _validate_module_name(module)
    except ValueError as exc:
        return {
            "success": False,
            "module": str(module),
            "operation": "reload",
            "message": str(exc),
            "error": "InvalidModuleName",
        }

    if not is_module_loaded(safe_module):
        load_result = load_module(safe_module)
        verified_loaded = is_module_loaded(safe_module)
        _log_operation(
            "reload",
            safe_module,
            load_result["success"] and verified_loaded,
            "module reloaded successfully" if load_result["success"] and verified_loaded else load_result["message"],
            None if load_result["success"] and verified_loaded else load_result["error"],
        )
        return {
            "success": load_result["success"] and verified_loaded,
            "module": safe_module,
            "operation": "reload",
            "message": "module reloaded successfully" if load_result["success"] and verified_loaded else load_result["message"],
            "error": None if load_result["success"] and verified_loaded else load_result["error"],
            "verified_loaded": verified_loaded,
        }

    unload_result = unload_module(safe_module)
    if not unload_result["success"]:
        _log_operation("reload", safe_module, False, unload_result["message"], unload_result["error"])
        return {
            "success": False,
            "module": safe_module,
            "operation": "reload",
            "message": unload_result["message"],
            "error": unload_result["error"],
            "verified_loaded": is_module_loaded(safe_module),
        }

    load_result = load_module(safe_module)
    verified_loaded = is_module_loaded(safe_module)

    if load_result["success"] and verified_loaded:
        _log_operation("reload", safe_module, True, "module reloaded successfully", None)
        return {
            "success": True,
            "module": safe_module,
            "operation": "reload",
            "message": "module reloaded successfully",
            "error": None,
            "verified_loaded": True,
        }

    _log_operation("reload", safe_module, False, load_result["message"], load_result["error"])
    return {
        "success": False,
        "module": safe_module,
        "operation": "reload",
        "message": load_result["message"],
        "error": load_result["error"],
        "verified_loaded": verified_loaded,
    }
