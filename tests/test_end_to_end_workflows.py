"""Mocked end-to-end workflow tests.

Each test traces a realistic request through the real components:

    CLI text / MCP tool call / natural language
      -> cli.parse_request / ToolDispatcher / IntentParser (fake LLM)
      -> structured action + schema and module-name validation
      -> SafetyPolicy (protected modules, live state via lsmod)
      -> confirmation (CLI prompt answers / MCP ``confirm`` argument)
      -> ActionExecutor dispatch to the real module manager
      -> post-operation verification via lsmod
      -> final CLI output / MCP response envelope

Only the process boundary is faked: ``module_manager._run_command`` is replaced
by ``FakeLinux``, which keeps a set of loaded modules and answers ``lsmod``,
``modinfo`` and ``modprobe`` like the real utilities would. No real command
runs and no module changes state on the host.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest

from app import cli
from app.llm import IntentParser, LLMProviderError
from app.manager import module_manager
from app.mcp_server import ToolDispatcher
from app.safety import DEFAULT_PROTECTED_MODULES

try:
    import anyio
    from mcp import Client

    from app.mcp_server.server import create_server

    HAS_MCP = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_MCP = False

requires_mcp = pytest.mark.skipif(not HAS_MCP, reason="the mcp package is not installed")

LSMOD_HEADER = "Module                  Size  Used by"
CLI_STATUSES = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "PERMISSION ERROR": "permission_error",
    "VALIDATION REJECTED": "rejected",
    "DECLINED": "declined",
}
CLI_LABELS = tuple(f"{label}:" for label in CLI_STATUSES)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeLinux:
    """Scripted stand-in for lsmod, modinfo and modprobe.

    * ``failures[utility] = (returncode, stderr)`` makes that utility fail.
    * ``missing`` lists utilities that are not installed (FileNotFoundError).
    * ``silent_modprobe`` makes modprobe exit 0 without changing module state.
    * ``lsmod_calls_before_failure`` lets the first N lsmod calls succeed and
      then fails lsmod with ``late_lsmod_stderr`` (to break verification only).
    * ``lsmod_stdout`` overrides lsmod output (for malformed output).
    """

    def __init__(self, loaded=("snd", "ext4", "usbcore")) -> None:
        self.loaded: set[str] = set(loaded)
        self.known: set[str] = set(self.loaded) | {"uvcvideo", "snd_hda_intel"}
        self.commands: list[list[str]] = []
        self.failures: dict[str, tuple[int, str]] = {}
        self.missing: set[str] = set()
        self.silent_modprobe = False
        self.lsmod_calls_before_failure: int | None = None
        self.late_lsmod_stderr = "lsmod: command failed"
        self.lsmod_stdout: str | None = None
        self._lsmod_calls = 0

    def run(self, args: list[str]) -> subprocess.CompletedProcess:
        args = list(args)
        self.commands.append(args)
        utility = args[0]
        if utility in self.missing:
            raise FileNotFoundError(f"[Errno 2] No such file or directory: {utility!r}")
        if utility in self.failures:
            code, stderr = self.failures[utility]
            return subprocess.CompletedProcess(args, code, stdout="", stderr=stderr)
        if utility == "lsmod":
            return self._lsmod(args)
        if utility == "modinfo":
            return self._modinfo(args)
        if utility == "modprobe":
            return self._modprobe(args)
        raise AssertionError(f"unexpected utility invoked: {args!r}")

    def _lsmod(self, args):
        self._lsmod_calls += 1
        if self.lsmod_calls_before_failure is not None and self._lsmod_calls > self.lsmod_calls_before_failure:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=self.late_lsmod_stderr)
        if self.lsmod_stdout is not None:
            return subprocess.CompletedProcess(args, 0, stdout=self.lsmod_stdout, stderr="")
        lines = [LSMOD_HEADER] + [f"{module:<24}16384  0" for module in sorted(self.loaded)]
        return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr="")

    def _modinfo(self, args):
        module = args[1]
        if module not in self.known:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"modinfo: ERROR: Module {module} not found.")
        text = (
            f"filename:       /lib/modules/6.1.0/kernel/drivers/{module}.ko\n"
            f"description:    Fake driver for {module}\n"
            "author:         CamAgent tests\n"
            "version:        1.0\n"
            "depends:        videodev\n"
            "parm:           quirks:Forced device quirks (uint)\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")

    def _modprobe(self, args):
        if args[1] == "-r":
            module = args[2]
            if not self.silent_modprobe:
                self.loaded.discard(module)
        else:
            module = args[1]
            if module not in self.known:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr=f"modprobe: FATAL: Module {module} not found in directory /lib/modules/6.1.0"
                )
            if not self.silent_modprobe:
                self.loaded.add(module)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    @property
    def modprobe_calls(self) -> list[list[str]]:
        return [command for command in self.commands if command[0] == "modprobe"]

    @property
    def utilities(self) -> list[str]:
        return [command[0] for command in self.commands]


class FakeProvider:
    """Fake LLM: returns one canned envelope (dict -> JSON, str as-is) or raises."""

    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []

    def complete(self, system, prompt, response_schema=None):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def action_reply(operation: str, module: str | None = None) -> dict[str, Any]:
    return {"status": "action", "action": {"operation": operation, "module": module}, "reason": "classified"}


def rejection_reply(status: str, reason: str = "unclear") -> dict[str, Any]:
    return {"status": status, "action": None, "reason": reason}


@dataclass
class CliRun:
    outputs: list[str]
    prompts: list[str]
    results: list[tuple[str, dict[str, Any]]]

    @property
    def confirmations(self) -> list[str]:
        return [prompt for prompt in self.prompts if prompt != cli.PROMPT]

    @property
    def result(self) -> tuple[str, dict[str, Any]]:
        assert len(self.results) == 1, self.outputs
        return self.results[0]

    @property
    def errors(self) -> list[str]:
        return [line for line in self.outputs if line.startswith("Error:")]


def cli_session(*inputs: str | BaseException) -> CliRun:
    """Run the interactive CLI with scripted answers; exceptions are raised at that prompt."""
    script = iter([*inputs, "exit"])
    prompts: list[str] = []
    outputs: list[str] = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        item = next(script)
        if isinstance(item, BaseException):
            raise item
        return item

    cli.run_cli(input_fn=input_fn, output_fn=outputs.append)

    results = []
    for line in outputs:
        if line.startswith(CLI_LABELS):
            summary, details = line.split("\n", 1)
            label, message = summary.split(": ", 1)
            # The CLI prints status and message on the summary line only; put
            # them back so assertions can read one dict.
            results.append((label, {**json.loads(details), "status": CLI_STATUSES[label], "message": message}))
    return CliRun(outputs, prompts, results)


def mcp_dispatcher(reply: Any = None, error: Exception | None = None, allow_mutations: bool = True) -> ToolDispatcher:
    """Default dispatcher: real executor and real module manager over FakeLinux."""
    parser = IntentParser(FakeProvider(reply, error)) if reply is not None or error is not None else None
    return ToolDispatcher(intent_parser=parser, allow_mutations=allow_mutations)


def json_command(command: str, module: str) -> str:
    return json.dumps({"command": command, "module": module})


@pytest.fixture
def make_linux(monkeypatch):
    """Install a fresh FakeLinux behind the module manager and forbid real subprocesses."""

    def explode(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError(f"a real subprocess call was attempted: {args!r} {kwargs!r}")

    # The module manager only ever uses subprocess.run.
    monkeypatch.setattr(subprocess, "run", explode)

    def make(**kwargs) -> FakeLinux:
        fake = FakeLinux(**kwargs)
        monkeypatch.setattr(module_manager, "_run_command", fake.run)
        return fake

    return make


@pytest.fixture
def linux(make_linux) -> FakeLinux:
    return make_linux()


# --------------------------------------------------------------------------- #
# 1. Successful read-only requests
# --------------------------------------------------------------------------- #


def test_cli_list_reads_lsmod_and_reports_modules(linux):
    run = cli_session("list")

    label, result = run.result
    assert label == "SUCCESS"
    assert result["success"] is True
    assert result["operation"] == "list"
    assert [module["name"] for module in result["modules"]] == sorted(linux.loaded)
    assert linux.commands == [["lsmod"]]
    assert run.confirmations == []


def test_cli_info_reads_modinfo_fields(linux):
    label, result = cli_session("info uvcvideo").result

    assert label == "SUCCESS"
    assert result["operation"] == "info", "CLI vocabulary, not the manager's internal name"
    assert result["module"] == "uvcvideo"
    assert result["description"] == "Fake driver for uvcvideo"
    assert result["parameters"] == "quirks:Forced device quirks (uint)"
    assert result["depends"] == "videodev"
    assert linux.commands == [["modinfo", "uvcvideo"]]


@pytest.mark.parametrize(("module", "loaded"), [("snd", True), ("uvcvideo", False), ("snd-hda-intel", False)])
def test_cli_loaded_reports_live_state(linux, module, loaded):
    label, result = cli_session(f"loaded {module}").result

    assert label == "SUCCESS"
    assert result["loaded"] is loaded
    assert result["module"] == module.replace("-", "_")
    assert linux.commands == [["lsmod"]]


def test_mcp_read_only_tools_read_the_system(linux):
    mcp = mcp_dispatcher()

    listing = mcp.call("list_modules", {})
    check = mcp.call("check_module", {"module": "snd"})
    info = mcp.call("get_module_info", {"module": "uvcvideo"})

    assert listing["status"] == check["status"] == info["status"] == "success"
    assert [module["name"] for module in listing["result"]] == sorted(linux.loaded)
    assert check["result"] == {"loaded": True}
    assert info["result"]["description"] == "Fake driver for uvcvideo"
    assert all(response["safety"]["risk_level"] == "read_only" for response in (listing, check, info))
    assert linux.commands == [["lsmod"], ["lsmod"], ["modinfo", "uvcvideo"]]


def test_natural_language_check_flows_from_parse_to_tool_call(linux):
    mcp = mcp_dispatcher(action_reply("check_module", "uvcvideo"))

    parsed = mcp.call("parse_request", {"request": "is the webcam driver loaded?"})
    assert parsed["status"] == "parsed"
    assert parsed["action"] == {"operation": "check_module", "module": "uvcvideo"}
    assert parsed["result"] == {"executed": False}
    assert linux.commands == [], "parsing must not touch the system"

    # The client acts on the parsed action by calling the matching tool.
    executed = mcp.call(parsed["action"]["operation"], {"module": parsed["action"]["module"]})

    assert executed["status"] == "success"
    assert executed["result"] == {"loaded": False}
    assert linux.commands == [["lsmod"]]


# --------------------------------------------------------------------------- #
# 2. Valid state-changing requests with explicit confirmation
# --------------------------------------------------------------------------- #


def test_cli_confirmed_load_checks_state_prompts_runs_modprobe_and_verifies(linux):
    run = cli_session("load uvcvideo", "yes")

    label, result = run.result
    assert label == "SUCCESS"
    assert result["success"] is True
    assert result["message"] == "module loaded successfully"
    assert result["verification"] == {"expected_loaded": True, "actual_loaded": True, "passed": True}
    assert "uvcvideo" in linux.loaded
    # Exactly one confirmation, phrased by the confirmation layer.
    assert run.confirmations == ["Confirm load_module uvcvideo? Risk: disruptive. [y/N] "]
    # State is checked before the prompt and re-checked after the change.
    assert linux.utilities == ["lsmod", "lsmod", "modprobe", "lsmod"]
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"]]


@pytest.mark.parametrize(
    ("command", "module", "modprobe", "expected_loaded"),
    [
        ("unload", "snd", [["modprobe", "-r", "snd"]], False),
        ("reload", "snd", [["modprobe", "-r", "snd"], ["modprobe", "snd"]], True),
        ("load", "snd-hda-intel", [["modprobe", "snd_hda_intel"]], True),
    ],
)
def test_cli_confirmed_unload_reload_and_hyphenated_names(linux, command, module, modprobe, expected_loaded):
    label, result = cli_session(f"{command} {module}", "y").result

    assert label == "SUCCESS"
    assert result["verification"]["passed"] is True
    assert result["verification"]["expected_loaded"] is expected_loaded
    assert linux.modprobe_calls == modprobe
    assert (module.replace("-", "_") in linux.loaded) is expected_loaded


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "  yes  "])
def test_cli_accepts_any_affirmative_spelling(linux, answer):
    label, _ = cli_session("load uvcvideo", answer).result

    assert label == "SUCCESS"
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"]]


def test_mcp_confirmed_mutation_runs_and_verifies(linux):
    response = mcp_dispatcher().call("unload_module", {"module": "snd", "confirm": True})

    assert response["ok"] is True
    assert response["status"] == "success"
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": True}
    assert response["result"]["verification"] == {"expected_loaded": False, "actual_loaded": False, "passed": True}
    assert response["result"]["message"] == "module unloaded successfully"
    assert "snd" not in linux.loaded
    assert linux.modprobe_calls == [["modprobe", "-r", "snd"]]


def test_agent_flow_parse_preview_then_confirm(linux):
    linux.loaded.add("uvcvideo")
    mcp = mcp_dispatcher(action_reply("reload_module", "uvcvideo"))

    parsed = mcp.call("parse_request", {"request": "please reload the webcam driver"})
    assert parsed["status"] == "parsed"
    action = parsed["action"]
    assert action == {"operation": "reload_module", "module": "uvcvideo"}

    preview = mcp.call(action["operation"], {"module": action["module"], "dry_run": True})
    assert preview["status"] == "dry_run"
    assert preview["result"]["would_execute"] is True
    assert linux.modprobe_calls == [], "a preview must not change state"

    confirmed = mcp.call(action["operation"], {"module": action["module"], "confirm": True})
    assert confirmed["status"] == "success"
    assert confirmed["result"]["verification"]["passed"] is True
    assert linux.modprobe_calls == [["modprobe", "-r", "uvcvideo"], ["modprobe", "uvcvideo"]]
    assert "uvcvideo" in linux.loaded


def test_cli_session_carries_state_across_commands(linux):
    run = cli_session(
        "loaded uvcvideo",
        "load uvcvideo", "yes",
        "loaded uvcvideo",
        "unload uvcvideo", "yes",
        "loaded uvcvideo",
    )

    labels = [label for label, _ in run.results]
    assert labels == ["SUCCESS"] * 5
    assert [result.get("loaded") for _, result in run.results] == [False, None, True, None, False]
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"], ["modprobe", "-r", "uvcvideo"]]
    assert len(run.confirmations) == 2
    assert run.outputs[-1] == "Goodbye."


# --------------------------------------------------------------------------- #
# 3. Rejected requests without confirmation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("answer", ["n", "N", "no", "", "maybe", "yes please", "1"])
def test_cli_declines_anything_that_is_not_a_clear_yes(linux, answer):
    run = cli_session("unload snd", answer)

    label, result = run.result
    assert label == "DECLINED"
    assert result["success"] is False
    assert result["status"] == "declined"
    assert result["message"] == "Execution was not confirmed."
    assert result["error"] == "operation was declined"
    assert "snd" in linux.loaded
    assert linux.commands == [["lsmod"]], "only the policy state check may run"


@pytest.mark.parametrize("interrupt", [EOFError(), KeyboardInterrupt()])
def test_cli_treats_interrupted_confirmation_as_declined(linux, interrupt):
    run = cli_session("unload snd", interrupt)

    label, _ = run.result
    assert label == "DECLINED"
    assert linux.modprobe_calls == []
    assert run.outputs[-1] == "Goodbye."


@pytest.mark.parametrize(
    ("tool", "module", "arguments"),
    [
        ("load_module", "uvcvideo", {}),
        ("load_module", "uvcvideo", {"confirm": False}),
        ("unload_module", "snd", {}),
        ("reload_module", "snd", {"dry_run": False}),
    ],
)
def test_mcp_mutation_without_confirm_stops_after_the_state_check(linux, tool, module, arguments):
    response = mcp_dispatcher().call(tool, {"module": module, **arguments})

    assert response["ok"] is False
    assert response["status"] == "confirmation_required"
    assert response["safety"] == {"risk_level": "disruptive", "requires_confirmation": True, "confirmed": False}
    assert linux.commands == [["lsmod"]]
    assert linux.loaded == {"snd", "ext4", "usbcore"}


def test_mcp_dry_run_reports_the_plan_without_changing_state(linux):
    response = mcp_dispatcher().call("load_module", {"module": "uvcvideo", "dry_run": True})

    assert response["status"] == "dry_run"
    assert response["result"]["summary"].startswith("Would perform load_module uvcvideo.")
    assert linux.commands == [["lsmod"]]
    assert "uvcvideo" not in linux.loaded


# --------------------------------------------------------------------------- #
# 4. Invalid and protected module names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("request_text", "fragment"),
    [
        (json_command("load", "snd; reboot"), "forbidden character"),
        (json_command("load", "$(reboot)"), "forbidden character"),
        (json_command("unload", "../../etc"), "forbidden character"),
        ("load -r", "must not start with '-'"),
        ("load --remove-all", "must not start with '-'"),
        ("load _hidden", "must start with a letter or digit"),
        (json_command("info", "a" * 80), "too long"),
    ],
)
def test_cli_rejects_unsafe_module_names_before_any_command_or_prompt(linux, request_text, fragment):
    run = cli_session(request_text, "yes")

    label, result = run.result
    assert label == "VALIDATION REJECTED"
    assert result["success"] is False
    assert result["status"] == "rejected"
    assert result["message"] == "Action failed validation."
    assert fragment in result["error"]
    assert linux.commands == []
    assert run.confirmations == []


def test_cli_plain_text_cannot_smuggle_a_second_word_into_a_module_name(linux):
    run = cli_session("load snd; reboot", "load snd && reboot", "load 'snd; reboot'", "yes")

    assert run.errors[:2] == ["Error: Commands accept at most one module name."] * 2
    # The quoted form reaches the validator and is rejected there.
    label, result = run.result
    assert label == "VALIDATION REJECTED"
    assert "forbidden character" in result["error"]
    assert linux.commands == []


@pytest.mark.parametrize("module", ["snd; reboot", "$(reboot)", "../../etc", "-r", "_hidden", "a" * 80, ""])
@pytest.mark.parametrize("tool", ["get_module_info", "check_module", "load_module", "unload_module"])
def test_mcp_rejects_unsafe_module_names_at_the_argument_boundary(linux, tool, module):
    arguments = {"module": module, "confirm": True} if tool in ("load_module", "unload_module") else {"module": module}

    response = mcp_dispatcher().call(tool, arguments)

    assert response["ok"] is False
    assert response["status"] == "invalid_arguments"
    assert response["errors"]
    assert linux.commands == []


@pytest.mark.parametrize("module", sorted(DEFAULT_PROTECTED_MODULES))
@pytest.mark.parametrize("command", ["unload", "reload"])
def test_cli_protected_modules_are_refused_before_state_check_and_prompt(linux, command, module):
    linux.loaded.add(module)

    run = cli_session(f"{command} {module}", "yes")

    label, result = run.result
    assert label == "VALIDATION REJECTED"
    assert result["message"] == "Action was rejected by safety policy."
    assert result["error"] == f"module {module!r} is protected from {command}_module"
    assert run.confirmations == []
    assert linux.commands == []
    assert module in linux.loaded


@pytest.mark.parametrize("module", sorted(DEFAULT_PROTECTED_MODULES))
def test_mcp_protected_modules_are_refused_even_with_confirm(linux, module):
    linux.loaded.add(module)

    response = mcp_dispatcher().call("unload_module", {"module": module, "confirm": True})

    assert response["status"] == "rejected"
    assert response["errors"] == [f"module {module!r} is protected from unload_module"]
    assert linux.commands == []
    assert module in linux.loaded


def test_protected_module_passes_parsing_but_not_policy(linux):
    # The intent parser only classifies; protection is enforced when acting.
    mcp = mcp_dispatcher(action_reply("unload_module", "ext4"))

    parsed = mcp.call("parse_request", {"request": "unload ext4"})
    assert parsed["status"] == "parsed"
    assert parsed["action"] == {"operation": "unload_module", "module": "ext4"}

    acted = mcp.call("unload_module", {"module": "ext4", "confirm": True})

    assert acted["status"] == "rejected"
    assert acted["errors"] == ["module 'ext4' is protected from unload_module"]
    assert linux.commands == []


# --------------------------------------------------------------------------- #
# 5. Policy rejections based on live state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "module", "error"),
    [
        ("load", "snd", "module 'snd' is already loaded"),
        ("unload", "uvcvideo", "module 'uvcvideo' is not loaded"),
        ("reload", "uvcvideo", "module 'uvcvideo' is not loaded"),
    ],
)
def test_cli_state_conflicts_are_rejected_without_prompting(linux, command, module, error):
    run = cli_session(f"{command} {module}", "yes")

    label, result = run.result
    assert label == "VALIDATION REJECTED"
    assert result["error"] == error
    assert run.confirmations == []
    assert linux.commands == [["lsmod"]]
    assert linux.modprobe_calls == []


@pytest.mark.parametrize(
    ("tool", "module", "error"),
    [
        ("load_module", "snd", "module 'snd' is already loaded"),
        ("unload_module", "uvcvideo", "module 'uvcvideo' is not loaded"),
        ("reload_module", "uvcvideo", "module 'uvcvideo' is not loaded"),
    ],
)
def test_mcp_state_conflicts_are_rejected_even_with_confirm(linux, tool, module, error):
    response = mcp_dispatcher().call(tool, {"module": module, "confirm": True})

    assert response["status"] == "rejected"
    assert response["errors"] == [error]
    assert response["safety"]["requires_confirmation"] is False
    assert linux.commands == [["lsmod"]]


def test_state_is_rechecked_on_every_request(linux):
    mcp = mcp_dispatcher()

    first = mcp.call("load_module", {"module": "uvcvideo", "confirm": True})
    second = mcp.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert first["status"] == "success"
    assert second["status"] == "rejected"
    assert second["errors"] == ["module 'uvcvideo' is already loaded"]
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"]]


# --------------------------------------------------------------------------- #
# 6. Permission errors and utility failures
# --------------------------------------------------------------------------- #


def test_cli_lsmod_permission_error_on_read(linux):
    linux.failures["lsmod"] = (1, "lsmod: Permission denied")

    label, result = cli_session("list").result

    assert label == "PERMISSION ERROR"
    assert result["status"] == "permission_error"
    assert result["message"] == "Permission denied while accessing Linux module state."
    assert result["error"] == "lsmod: Permission denied"


def test_cli_lsmod_permission_error_blocks_mutation_before_prompt(linux):
    linux.failures["lsmod"] = (1, "lsmod: Permission denied")

    run = cli_session("load uvcvideo", "yes")

    label, result = run.result
    assert label == "PERMISSION ERROR"
    assert result["message"] == "Permission denied while checking module state."
    assert result["error"] == "could not determine whether module 'uvcvideo' is loaded: lsmod: Permission denied"
    assert run.confirmations == []
    assert linux.modprobe_calls == []


def test_mcp_lsmod_permission_error(linux):
    linux.failures["lsmod"] = (1, "lsmod: Permission denied")
    mcp = mcp_dispatcher()

    check = mcp.call("check_module", {"module": "snd"})
    load = mcp.call("load_module", {"module": "uvcvideo", "confirm": True})

    assert check["status"] == load["status"] == "permission_error"
    assert check["errors"] == ["lsmod: Permission denied"]
    assert load["message"] == "Permission denied while checking module state."
    assert linux.modprobe_calls == []


def test_missing_lsmod_is_a_failure_on_both_interfaces(linux):
    linux.missing.add("lsmod")

    label, result = cli_session("list").result
    response = mcp_dispatcher().call("list_modules", {})

    assert label == "FAILURE"
    assert result["status"] == "failure"
    assert result["error"] == "FileNotFoundError: [Errno 2] No such file or directory: 'lsmod'"
    assert response["status"] == "error"
    assert response["errors"] == [result["error"]]


def test_malformed_lsmod_output_is_not_mistaken_for_an_empty_system(linux):
    linux.lsmod_stdout = "totally unexpected\n"

    label, result = cli_session("loaded snd").result
    response = mcp_dispatcher().call("check_module", {"module": "snd"})

    assert label == "FAILURE"
    assert result["error"] == "RuntimeError: lsmod returned malformed output: invalid header"
    assert response["status"] == "error"
    assert response["result"] is None


def test_unknown_module_info_is_a_manager_failure(linux):
    label, result = cli_session("info nope").result
    response = mcp_dispatcher().call("get_module_info", {"module": "nope"})

    assert label == "FAILURE"
    assert result["operation"] == "info"
    assert result["error"] == "modinfo: ERROR: Module nope not found."
    assert response["status"] == "manager_error"
    assert response["result"]["error"] == "ModuleNotFound"
    assert response["errors"] == ["modinfo: ERROR: Module nope not found."]


def test_missing_modinfo_utility_is_reported(linux):
    linux.missing.add("modinfo")

    label, result = cli_session("info uvcvideo").result

    assert label == "FAILURE"
    assert result["error"] == "modinfo command is unavailable"


def test_cli_modprobe_permission_error_after_confirmation(linux):
    linux.failures["modprobe"] = (1, "modprobe: ERROR: could not insert 'uvcvideo': Operation not permitted")

    run = cli_session("load uvcvideo", "yes")

    label, result = run.result
    assert label == "PERMISSION ERROR"
    assert result["status"] == "permission_error"
    assert result["error"] == "PermissionDenied: modprobe: ERROR: could not insert 'uvcvideo': Operation not permitted"
    assert len(run.confirmations) == 1
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"]]
    assert "uvcvideo" not in linux.loaded


def test_mcp_modprobe_permission_error_after_confirmation(linux):
    linux.failures["modprobe"] = (1, "modprobe: ERROR: could not remove 'snd': Operation not permitted")

    response = mcp_dispatcher().call("unload_module", {"module": "snd", "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "permission_error"
    assert response["message"] == "Module manager reported an unsuccessful operation."
    assert response["result"]["error"] == "PermissionDenied"
    assert response["safety"]["confirmed"] is True
    assert "snd" in linux.loaded


def test_missing_modprobe_is_a_manager_failure(linux):
    linux.missing.add("modprobe")

    label, result = cli_session("load uvcvideo", "yes").result
    response = mcp_dispatcher().call("load_module", {"module": "uvcvideo", "confirm": True})

    assert label == "FAILURE"
    assert result["error"] == "modprobe command is unavailable"
    assert response["status"] == "manager_error"
    assert response["result"]["error"] == "CommandNotFound"


def test_modprobe_generic_failure_is_a_manager_failure(linux):
    linux.failures["modprobe"] = (1, "modprobe: FATAL: Module snd is in use.")

    label, result = cli_session("unload snd", "yes").result

    assert label == "FAILURE"
    assert result["error"] == "modprobe: FATAL: Module snd is in use."
    assert "snd" in linux.loaded


def test_loading_a_module_the_kernel_does_not_know(linux):
    linux.known.discard("ghostmod")

    response = mcp_dispatcher().call("load_module", {"module": "ghostmod", "confirm": True})

    assert response["status"] == "manager_error"
    assert response["errors"] == ["modprobe: FATAL: Module ghostmod not found in directory /lib/modules/6.1.0"]
    assert "ghostmod" not in linux.loaded


# --------------------------------------------------------------------------- #
# 7. Execution failures and failed post-operation verification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "module", "expected_loaded"),
    [("load", "uvcvideo", True), ("unload", "snd", False)],
)
def test_cli_reports_when_modprobe_succeeds_but_state_does_not_change(linux, command, module, expected_loaded):
    linux.silent_modprobe = True

    label, result = cli_session(f"{command} {module}", "yes").result

    assert label == "FAILURE"
    assert result["success"] is False
    assert result["message"] == "The requested module state could not be verified."
    assert result["error"] == "module state does not match the requested operation"
    assert result["verification"] == {
        "expected_loaded": expected_loaded,
        "actual_loaded": not expected_loaded,
        "passed": False,
    }
    assert len(linux.modprobe_calls) == 1


def test_mcp_reports_unverified_state_change(linux):
    linux.silent_modprobe = True

    response = mcp_dispatcher().call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["ok"] is False
    assert response["status"] == "manager_error"
    assert response["result"]["verification"]["passed"] is False
    assert response["safety"]["confirmed"] is True


def test_lsmod_failure_during_verification_is_reported_after_the_change(linux):
    # Policy check and the manager's own pre-check pass; the verification lsmod fails.
    linux.lsmod_calls_before_failure = 2

    label, result = cli_session("load uvcvideo", "yes").result

    assert label == "FAILURE"
    assert result["message"] == "Module state could not be verified after the operation."
    assert result["error"] == "RuntimeError: lsmod: command failed"
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"]]
    assert "uvcvideo" in linux.loaded


def test_permission_loss_during_verification_is_a_permission_error(linux):
    linux.lsmod_calls_before_failure = 2
    linux.late_lsmod_stderr = "lsmod: Permission denied"

    response = mcp_dispatcher().call("load_module", {"module": "uvcvideo", "confirm": True})

    assert response["status"] == "permission_error"
    assert response["message"] == "Permission denied while verifying module state."
    assert response["errors"] == ["lsmod: Permission denied"]
    assert linux.modprobe_calls == [["modprobe", "uvcvideo"]]


# --------------------------------------------------------------------------- #
# 8. Malformed or unsupported intent requests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("request_text", "reply", "error", "status"),
    [
        ("do something clever", "not json at all", None, "invalid_output"),
        ("run rm -rf / for me", rejection_reply("unsupported", "shell commands are not allowed"), None, "unsupported"),
        ("reload it", rejection_reply("ambiguous", "which module?"), None, "ambiguous"),
        ("fix my sound", action_reply("reload_module", "snd_hda_intel"), None, "ambiguous"),
        ("reload uvcvideo", action_reply("reload_module", "uvcvideo; reboot"), None, "invalid_output"),
        ("reload uvcvideo", action_reply("run_shell", "uvcvideo"), None, "invalid_output"),
        ("reload uvcvideo", None, LLMProviderError("no API key"), "provider_error"),
    ],
)
def test_bad_intent_results_never_reach_the_system(linux, request_text, reply, error, status):
    mcp = mcp_dispatcher(reply, error)

    response = mcp.call("parse_request", {"request": request_text})

    assert response["ok"] is False
    assert response["status"] == status
    assert response["action"] is None
    assert response["result"] == {"executed": False}
    assert linux.commands == []
    assert linux.loaded == {"snd", "ext4", "usbcore"}


def test_other_tools_keep_working_when_the_model_is_unavailable(linux):
    mcp = mcp_dispatcher(error=LLMProviderError("no API key"))

    parsed = mcp.call("parse_request", {"request": "list modules"})
    listing = mcp.call("list_modules", {})

    assert parsed["status"] == "provider_error"
    assert listing["status"] == "success"
    assert linux.commands == [["lsmod"]]


def test_parsed_mutation_on_a_read_only_server_cannot_be_executed(linux):
    mcp = mcp_dispatcher(action_reply("unload_module", "snd"), allow_mutations=False)

    parsed = mcp.call("parse_request", {"request": "unload snd"})
    attempt = mcp.call("unload_module", {"module": "snd", "confirm": True})

    assert parsed["status"] == "parsed"
    assert attempt["status"] == "tool_disabled"
    assert linux.commands == []
    assert "snd" in linux.loaded


def test_cli_does_not_accept_natural_language(linux):
    run = cli_session("reload the webcam driver", "is uvcvideo loaded?", "please list")

    assert run.errors == [
        "Error: Commands accept at most one module name.",
        "Error: Commands accept at most one module name.",
        "Error: Unknown command: please",
    ]
    assert run.results == []
    assert linux.commands == []


# --------------------------------------------------------------------------- #
# 9. CLI and MCP apply the same safety behaviour
# --------------------------------------------------------------------------- #


def _lsmod_denied(linux: FakeLinux) -> None:
    linux.failures["lsmod"] = (1, "lsmod: Permission denied")


def _modprobe_denied(linux: FakeLinux) -> None:
    linux.failures["modprobe"] = (1, "modprobe: ERROR: Operation not permitted")


def _silent_modprobe(linux: FakeLinux) -> None:
    linux.silent_modprobe = True


def _no_lsmod(linux: FakeLinux) -> None:
    linux.missing.add("lsmod")


CONSISTENCY_CASES = [
    # id, setup, CLI inputs, MCP tool, MCP arguments, CLI label, MCP status, modprobe runs?
    ("read-list", None, ("list",), "list_modules", {}, "SUCCESS", "success", False),
    ("read-info", None, ("info uvcvideo",), "get_module_info", {"module": "uvcvideo"}, "SUCCESS", "success", False),
    ("confirmed-load", None, ("load uvcvideo", "yes"), "load_module", {"module": "uvcvideo", "confirm": True}, "SUCCESS", "success", True),
    ("confirmed-unload", None, ("unload snd", "yes"), "unload_module", {"module": "snd", "confirm": True}, "SUCCESS", "success", True),
    ("unconfirmed", None, ("load uvcvideo", "n"), "load_module", {"module": "uvcvideo"}, "DECLINED", "confirmation_required", False),
    ("protected", None, ("unload ext4", "yes"), "unload_module", {"module": "ext4", "confirm": True}, "VALIDATION REJECTED", "rejected", False),
    ("already-loaded", None, ("load snd", "yes"), "load_module", {"module": "snd", "confirm": True}, "VALIDATION REJECTED", "rejected", False),
    ("not-loaded", None, ("unload uvcvideo", "yes"), "unload_module", {"module": "uvcvideo", "confirm": True}, "VALIDATION REJECTED", "rejected", False),
    ("unsafe-name", None, (json_command("load", "snd; reboot"), "yes"), "load_module", {"module": "snd; reboot", "confirm": True}, "VALIDATION REJECTED", "invalid_arguments", False),
    ("lsmod-denied", _lsmod_denied, ("load uvcvideo", "yes"), "load_module", {"module": "uvcvideo", "confirm": True}, "PERMISSION ERROR", "permission_error", False),
    ("modprobe-denied", _modprobe_denied, ("load uvcvideo", "yes"), "load_module", {"module": "uvcvideo", "confirm": True}, "PERMISSION ERROR", "permission_error", True),
    ("unverified", _silent_modprobe, ("load uvcvideo", "yes"), "load_module", {"module": "uvcvideo", "confirm": True}, "FAILURE", "manager_error", True),
    ("no-lsmod", _no_lsmod, ("list",), "list_modules", {}, "FAILURE", "error", False),
]


@pytest.mark.parametrize(
    ("setup", "cli_inputs", "tool", "arguments", "cli_label", "mcp_status", "modprobe_runs"),
    [case[1:] for case in CONSISTENCY_CASES],
    ids=[case[0] for case in CONSISTENCY_CASES],
)
def test_cli_and_mcp_reach_the_same_outcome_from_the_same_system_state(
    make_linux, setup, cli_inputs, tool, arguments, cli_label, mcp_status, modprobe_runs
):
    cli_linux = make_linux()
    if setup:
        setup(cli_linux)
    cli_result_label, cli_result = cli_session(*cli_inputs).result

    mcp_linux = make_linux()
    if setup:
        setup(mcp_linux)
    response = mcp_dispatcher().call(tool, arguments)

    assert cli_result_label == cli_label
    assert response["status"] == mcp_status
    assert cli_result["success"] is response["ok"]
    assert bool(cli_linux.modprobe_calls) is bool(mcp_linux.modprobe_calls) is modprobe_runs
    assert cli_linux.loaded == mcp_linux.loaded
    # Same utilities, in the same order, on both paths.
    assert cli_linux.utilities == mcp_linux.utilities
    # Wording differs only where the CLI user declined interactively while the
    # MCP client simply omitted the confirm argument.
    if mcp_status not in {"invalid_arguments", "confirmation_required"} and not response["ok"]:
        assert cli_result["error"] == response["errors"][0]


# --------------------------------------------------------------------------- #
# 10. Over the real MCP protocol
# --------------------------------------------------------------------------- #


@requires_mcp
def test_agent_flow_over_the_mcp_protocol(linux):
    linux.loaded.add("uvcvideo")
    server = create_server(mcp_dispatcher(action_reply("reload_module", "uvcvideo")))

    async def scenario(client):
        tools = {tool.name for tool in (await client.list_tools()).tools}
        parsed = await client.call_tool("parse_request", {"request": "reload the webcam driver"})
        action = parsed.structured_content["action"]
        preview = await client.call_tool(action["operation"], {"module": action["module"], "dry_run": True})
        unconfirmed = await client.call_tool(action["operation"], {"module": action["module"]})
        confirmed = await client.call_tool(action["operation"], {"module": action["module"], "confirm": True})
        return tools, parsed, preview, unconfirmed, confirmed

    async def main():
        async with Client(server) as client:
            return await scenario(client)

    tools, parsed, preview, unconfirmed, confirmed = anyio.run(main)

    assert {"parse_request", "reload_module", "check_module"} <= tools
    assert parsed.is_error is False
    assert parsed.structured_content["action"] == {"operation": "reload_module", "module": "uvcvideo"}
    assert preview.is_error is False
    assert preview.structured_content["status"] == "dry_run"
    assert unconfirmed.is_error is True
    assert unconfirmed.structured_content["status"] == "confirmation_required"
    assert confirmed.is_error is False
    assert confirmed.structured_content["status"] == "success"
    assert confirmed.structured_content["result"]["verification"]["passed"] is True
    assert linux.modprobe_calls == [["modprobe", "-r", "uvcvideo"], ["modprobe", "uvcvideo"]]


@requires_mcp
def test_protected_and_unsafe_requests_over_the_mcp_protocol(linux):
    server = create_server(mcp_dispatcher())

    async def main():
        async with Client(server) as client:
            protected = await client.call_tool("unload_module", {"module": "ext4", "confirm": True})
            unsafe = await client.call_tool("load_module", {"module": "snd; reboot", "confirm": True})
            coerced = await client.call_tool("load_module", {"module": "uvcvideo", "confirm": "true"})
            return protected, unsafe, coerced

    protected, unsafe, coerced = anyio.run(main)

    assert protected.is_error is True
    assert protected.structured_content["status"] == "rejected"
    assert unsafe.is_error is True
    assert unsafe.structured_content["status"] == "invalid_arguments"
    assert coerced.is_error is True
    assert coerced.structured_content["status"] == "invalid_arguments"
    assert linux.commands == []
    assert linux.loaded == {"snd", "ext4", "usbcore"}
