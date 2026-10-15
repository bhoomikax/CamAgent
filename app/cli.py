import json
import shlex
from typing import Any, Callable

from app.orchestrator import execute_action

PROMPT = "camagent> "
HELP_TEXT = """Supported commands:
  list
  info <module>
  loaded <module>
  load <module>
  unload <module>
  reload <module>
  help
  exit | quit

Commands can also be supplied as JSON, for example:
  {"command": "info", "module": "uvcvideo"}"""

MODULE_COMMANDS = {"info", "loaded", "load", "unload", "reload"}


def parse_request(text: str) -> tuple[str, str | None]:
    request = text.strip()
    if request.startswith("{"):
        try:
            payload = json.loads(request)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON request: {exc.msg}") from exc

        if not isinstance(payload, dict):
            raise ValueError("A structured request must be a JSON object.")
        if set(payload) - {"command", "module"}:
            raise ValueError("Structured requests accept only 'command' and 'module'.")
        command = payload.get("command")
        module = payload.get("module")
        if not isinstance(command, str):
            raise ValueError("Structured requests require a string 'command'.")
        if module is not None and not isinstance(module, str):
            raise ValueError("'module' must be a string.")
    else:
        try:
            parts = shlex.split(request)
        except ValueError as exc:
            raise ValueError(f"Invalid command: {exc}") from exc
        if not parts:
            raise ValueError("Enter a command or type 'help'.")
        if len(parts) > 2:
            raise ValueError("Commands accept at most one module name.")
        command = parts[0]
        module = parts[1] if len(parts) == 2 else None

    command = command.lower()
    if command in MODULE_COMMANDS and not module:
        raise ValueError(f"'{command}' requires a module name.")
    if command not in MODULE_COMMANDS | {"list", "help", "exit", "quit"}:
        raise ValueError(f"Unknown command: {command}")
    if command not in MODULE_COMMANDS and module is not None:
        raise ValueError(f"'{command}' does not accept a module name.")

    return command, module


def run_cli(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> None:
    output_fn("CamAgent interactive CLI. Type 'help' for commands.")
    while True:
        try:
            text = input_fn(PROMPT)
        except (EOFError, KeyboardInterrupt):
            output_fn("\nGoodbye.")
            return

        if not text.strip():
            continue

        try:
            command, module = parse_request(text)
        except ValueError as exc:
            output_fn(f"Error: {exc}")
            continue

        if command in {"exit", "quit"}:
            output_fn("Goodbye.")
            return
        if command == "help":
            output_fn(HELP_TEXT)
            continue

        def confirm(prompt: str) -> bool:
            try:
                answer = input_fn(f"{prompt} [y/N] ")
            except (EOFError, KeyboardInterrupt):
                return False
            return answer.strip().lower() in {"y", "yes"}

        result = execute_action(command, module, confirm)
        output_fn(json.dumps(result, indent=2))


def main() -> None:
    run_cli()


if __name__ == "__main__":
    main()
