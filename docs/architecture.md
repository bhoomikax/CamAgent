# CamAgent architecture

## Component overview

| Component | Responsibility |
| --- | --- |
| `app/cli.py` | Reads repeated terminal input, parses simple or JSON requests, handles help and exit, asks for mutation confirmation, and formats structured feedback. |
| `app/orchestrator.py` | Translates CLI command names to structured operations and adapts the shared executor result to CLI feedback. |
| `app/manager/module_manager.py` | Wraps Linux module utilities, parses module metadata and loaded state, and provides load, unload, and reload operations. |
| `app/agent/executor.py` | Runs both CLI and MCP actions through schema validation, the state-aware `SafetyPolicy`, dry-run, confirmation, dispatch, and post-change state verification. |
| `app/llm/` | Translates natural-language requests into structured actions through an LLM provider, rejecting unsupported, ambiguous, or ungrounded requests. |
| `app/mcp_server/` | Exposes approved module tools over MCP, validates tool arguments, and routes calls to the executor or intent parser. See [mcp.md](mcp.md). |
| `tests/` | Exercises parsing, policy decisions, confirmation handling, execution outcomes, verification, intent parsing, and MCP tool calls using mocked system commands and a mocked LLM. |

The CLI and MCP use the same `ActionExecutor`. Their adapters translate CLI
commands or MCP tool arguments into the structured operation schema and
format the shared execution result for their respective interfaces.

## Action flow

1. The CLI parses simple commands or JSON; MCP validates tool names and
   argument schemas. Both adapters create the same structured action.
2. `ActionExecutor` validates the operation and module name, then applies the
   state-aware safety policy. Unsupported operations and unsafe names are
   rejected before a module operation is dispatched.
3. `list_modules`, `get_module_info`, and `check_module` are read-only.
   `load_module`, `unload_module`, and `reload_module` require explicit
   confirmation. The CLI prompts; MCP requires `confirm: true`. A decline or
   missing confirmation blocks dispatch.
4. The executor dispatches only approved schema operations to the module
   manager. The manager invokes Linux utilities with argument lists and no
   shell.
5. After a successful mutation, the executor independently checks loaded
   state. Success requires the observed state to match the requested outcome;
   unavailable state inspection or a mismatch is reported as failure.
6. The CLI and MCP adapt the common result into their interface-specific
   response formats.

## Safety and privilege boundary

Module names must be non-empty and contain only letters, digits, underscores,
periods, or hyphens; path separators, shell metacharacters, and `..` are
rejected. The policy also blocks unloading or reloading `ext4`, `xfs`, `btrfs`,
`overlay`, and `dm_mod` by default, and checks live state before allowing a
mutation. Manager commands are executed without a shell. These checks do not
replace operating-system access controls.

CamAgent does not automatically elevate privileges. The operating system may
deny a module operation; these errors are surfaced as permission errors.
Inspection and mutation require a Linux environment with the respective kernel
utilities installed.

## Testing boundary

Unit tests replace command execution or manager operations with mocks. The
opt-in Linux integration suite performs only real listing, metadata
inspection, and loaded-state checks; it never loads or unloads modules. See
[linux-modules.md](linux-modules.md#15-linux-validation-and-integration-checks).
