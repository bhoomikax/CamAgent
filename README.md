# CamAgent

CamAgent inspects and manages Linux kernel modules through a guarded interactive
command-line interface. Module changes require explicit confirmation and are
reported only after checking the resulting module state.

## Requirements and setup

- Python 3.10 or newer
- A Linux host with `lsmod`, `modinfo`, and `modprobe` available
- Appropriate system permissions for module changes (typically root)

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Start the CLI:

```bash
python -m app.cli
```

The CLI can be started on other platforms, but Linux module inspection and
management commands require the Linux utilities and kernel interfaces listed
above. The CLI does not elevate privileges automatically.

## CLI usage

Type `help` at the prompt to see available actions:

```text
list
info uvcvideo
loaded uvcvideo
load uvcvideo
unload uvcvideo
reload uvcvideo
exit
```

Read-only actions run directly after policy validation. `load`, `unload`, and
`reload` ask for explicit confirmation; answer `y` or `yes` to proceed.
Responses report success, failures, permission errors, validation rejections,
and confirmation declines. Module-changing actions also include the expected
and observed loaded state. A failed state check is reported as a failure rather
than assumed successful.

Actions can also be entered as structured JSON:

```json
{"command": "info", "module": "uvcvideo"}
{"command": "load", "module": "uvcvideo"}
```

Blank input is ignored. Type `exit` or `quit`, or send end-of-input, to leave
cleanly.

## Architecture

```text
Terminal input
    ↓
CLI parser (syntax and built-in help/exit)
    ↓
Orchestrator
    ├── Safety policy (allowed action, safe module name, confirmation gate)
    └── Module manager (Linux command execution and state verification)
             ↓
       Linux kernel module interfaces
```

Every module action is routed from the CLI through the orchestrator and safety
policy; the CLI does not invoke the module manager directly. See
[docs/architecture.md](docs/architecture.md) for component responsibilities,
execution flow, and feedback semantics.

## Tests

Run the unit tests with:

```bash
python -m pytest
```

Tests mock Linux command interactions and never load or unload real drivers.
