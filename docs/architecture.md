# CamAgent architecture

## Component overview

| Component | Responsibility |
| --- | --- |
| `app/cli.py` | Reads repeated terminal input, parses simple or JSON requests, handles help and exit, asks for mutation confirmation, and formats structured feedback. |
| `app/orchestrator.py` | Sends each module action through policy evaluation, gates execution on confirmation, dispatches approved actions to the manager, verifies post-operation state, and builds the result envelope. |
| `app/safety.py` | Defines supported actions, validates module names, and marks module-changing actions as requiring confirmation. |
| `app/manager/module_manager.py` | Wraps Linux module utilities, parses module metadata and loaded state, and provides load, unload, and reload operations. |
| `tests/` | Exercises parsing, policy decisions, confirmation handling, execution outcomes, and verification using mocked system commands. |

## Action flow

1. The CLI parses either a simple command (`info uvcvideo`) or a JSON object
   (`{"command": "info", "module": "uvcvideo"}`). Help and exit are CLI controls;
   module actions are passed to the orchestrator.
2. The safety policy checks the action against the supported set. Module names
   are validated against the same shared validator used by the module manager.
   Unknown actions, missing module names, and unsafe module names are rejected
   before manager execution.
3. `list`, `info`, and `loaded` are read-only. `load`, `unload`, and `reload`
   require an affirmative CLI confirmation. If confirmation is declined or
   unavailable, the manager is not called.
4. The orchestrator dispatches approved actions to the manager. The manager
   invokes `lsmod`, `modinfo`, or `modprobe` with argument lists rather than
   constructing shell commands.
5. After a module-changing action, the orchestrator independently reads the
   module state with `lsmod`. Success requires both a successful operation and
   a successful state check matching the requested postcondition. If the state
   cannot be inspected, the response reports a verification failure.
6. The CLI presents a structured result with a clear status (`success`,
   `failure`, `permission_error`, `rejected`, or `declined`), a message, and
   verification details when applicable.

## Safety and privilege boundary

Module names must be non-empty and contain only letters, digits, underscores,
periods, or hyphens; path separators, shell metacharacters, and `..` are
rejected. Manager commands are executed without a shell. These checks reduce
input and command-injection risk but do not replace operating-system access
controls.

CamAgent does not automatically elevate privileges. The operating system may
deny a module operation; these errors are surfaced as permission errors.
Inspection and mutation require a Linux environment with the respective kernel
utilities installed.

## Testing boundary

Unit tests replace command execution or manager operations with mocks. In
particular, CLI and orchestration tests do not unload, reload, or load real
kernel modules.
