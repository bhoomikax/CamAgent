import json

import pytest

from app import cli


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


def test_cli_ignores_blank_input_and_exits(monkeypatch):
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


def test_cli_dispatches_commands_and_prints_json(monkeypatch):
    monkeypatch.setattr(cli.module_manager, "list_modules", lambda: [{"name": "uvcvideo"}])
    responses = iter(["list", '{"command": "loaded", "module": "uvcvideo"}', "quit"])
    output = []

    cli.run_cli(input_fn=lambda prompt: next(responses), output_fn=output.append)

    assert json.loads(output[1]) == [{"name": "uvcvideo"}]
    assert json.loads(output[2]) == {"module": "uvcvideo", "loaded": True}
    assert output[-1] == "Goodbye."


def test_cli_exits_cleanly_at_end_of_input():
    output = []

    def end_input(prompt):
        raise EOFError

    cli.run_cli(input_fn=end_input, output_fn=output.append)

    assert output[-1] == "\nGoodbye."
