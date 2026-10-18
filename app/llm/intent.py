"""Translate natural-language requests into structured module actions.

The LLM is only asked to classify a request. Its reply must be a JSON
envelope whose ``action`` field is exactly the structured action schema
(app/schemas/actions.py); that action is then run through the same schema and
module-name validation as every other input. Nothing here executes an action.

Requests are rejected rather than guessed when:

* the model reports the request as unsupported or ambiguous,
* the model output is not well-formed JSON matching the envelope,
* the action fails schema or module-name validation, or
* the module name does not appear in the request and is not the documented
  alias of a device mentioned in it (so the model cannot invent a module).

Layer order:  Natural language -> Intent parser -> Structured Action ->
Validator -> Policy -> Confirmation -> Module Manager.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.safety import normalise_module_name, validate_raw
from app.schemas import ALLOWED_OPERATIONS, ModuleAction

from .provider import LLMProvider, LLMProviderError

MAX_REQUEST_LENGTH = 500

# Device phrases a user may use instead of a module name. Only modules listed
# here can be chosen without being named in the request.
DEVICE_ALIASES: Mapping[str, tuple[str, ...]] = {
    "uvcvideo": ("webcam", "web cam", "usb camera", "uvc camera", "uvc driver"),
}

ENVELOPE_FIELDS = frozenset({"status", "action", "reason"})
ENVELOPE_STATUSES = frozenset({"action", "unsupported", "ambiguous"})

RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": sorted(ENVELOPE_STATUSES)},
        "action": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": list(ALLOWED_OPERATIONS)},
                        "module": {"type": ["string", "null"]},
                    },
                    "required": ["operation", "module"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "reason": {"type": "string"},
    },
    "required": ["status", "action", "reason"],
    "additionalProperties": False,
}


def _alias_lines() -> str:
    return "\n".join(
        f"- {', '.join(repr(p) for p in phrases)} -> {module}"
        for module, phrases in DEVICE_ALIASES.items()
    )


SYSTEM_PROMPT = f"""You translate a user's request about Linux kernel modules into one structured action for CamAgent. You only classify the request; you never run anything.

Reply with a single JSON object and nothing else:
{{"status": "action" | "unsupported" | "ambiguous", "action": {{"operation": ..., "module": ...}} | null, "reason": "<one short sentence>"}}

Operations:
- list_modules: list loaded modules. "module" must be null.
- get_module_info: show details about one module.
- check_module: check whether one module is currently loaded.
- load_module, unload_module, reload_module: change one module's state.

Rules:
- Use "action" only when the request clearly maps to exactly one operation and, where needed, exactly one module.
- The module must be named in the request, or be the module for a device phrase listed below. Never guess a module name from a vague description.
- Use "ambiguous" (with "action": null) when the operation or module is unclear, or the request mentions several modules or several operations.
- Use "unsupported" (with "action": null) for anything outside these operations, such as shell commands, editing files, installing packages or changing module parameters.

Device phrases:
{_alias_lines()}"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class IntentResult:
    """Outcome of parsing one natural-language request.

    ``status`` is one of: ``parsed``, ``unsupported``, ``ambiguous``,
    ``invalid_request``, ``invalid_output`` or ``provider_error``.
    ``action`` is set only when ``status == "parsed"``.
    """

    status: str
    message: str
    action: ModuleAction | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "parsed" and self.action is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "action": self.action.to_dict() if self.action is not None else None,
            "errors": list(self.errors),
        }


def _mentions(text: str, phrase: str) -> bool:
    """Whole-word, case-insensitive match that treats '-' and '_' alike."""
    canonical_text = text.lower().replace("-", "_")
    canonical_phrase = phrase.lower().replace("-", "_")
    pattern = rf"(?<![a-z0-9_.]){re.escape(canonical_phrase)}(?![a-z0-9_])"
    return re.search(pattern, canonical_text) is not None


def module_is_grounded(request: str, module: str) -> bool:
    """True when the request names the module or a device alias for it."""
    canonical = normalise_module_name(module)
    if _mentions(request, canonical):
        return True
    return any(_mentions(request, phrase) for phrase in DEVICE_ALIASES.get(canonical, ()))


def _decode_envelope(raw_output: str) -> dict[str, Any]:
    """Parse and shape-check the model's reply. Raises ValueError on mismatch."""
    if not isinstance(raw_output, str):
        raise ValueError(f"model output must be text, got {type(raw_output).__name__}")

    text = raw_output.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model output is not valid JSON: {exc.msg}") from exc

    if not isinstance(envelope, dict):
        raise ValueError("model output must be a JSON object")

    unknown = set(envelope) - ENVELOPE_FIELDS
    if unknown:
        raise ValueError(f"model output has unexpected field(s): {sorted(unknown)}")
    missing = ENVELOPE_FIELDS - set(envelope)
    if missing:
        raise ValueError(f"model output is missing field(s): {sorted(missing)}")

    status = envelope["status"]
    # Check the type first: an unhashable value (list, object) would otherwise
    # raise TypeError from the set lookup and escape the ValueError contract.
    if not isinstance(status, str) or status not in ENVELOPE_STATUSES:
        raise ValueError(f"model output has unknown status {status!r}")
    if not isinstance(envelope["reason"], str):
        raise ValueError("model output 'reason' must be a string")

    if envelope["status"] == "action":
        if not isinstance(envelope["action"], dict):
            raise ValueError("model output with status 'action' must include an action object")
    elif envelope["action"] is not None:
        raise ValueError(f"model output with status {envelope['status']!r} must not include an action")

    return envelope


class IntentParser:
    """Turn user text into a validated `ModuleAction` using an LLM provider."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def parse(self, request: Any) -> IntentResult:
        if not isinstance(request, str) or not request.strip():
            return IntentResult("invalid_request", "Request must be non-empty text.")
        request = request.strip()
        if len(request) > MAX_REQUEST_LENGTH:
            return IntentResult(
                "invalid_request",
                f"Request is too long (over {MAX_REQUEST_LENGTH} characters).",
            )

        try:
            raw_output = self._provider.complete(SYSTEM_PROMPT, request, RESPONSE_SCHEMA)
        except LLMProviderError as exc:
            return IntentResult("provider_error", "The language model was unavailable.", errors=[str(exc)])

        try:
            envelope = _decode_envelope(raw_output)
        except ValueError as exc:
            return IntentResult(
                "invalid_output",
                "The language model returned output that does not match the action schema.",
                errors=[str(exc)],
            )

        reason = envelope["reason"].strip()
        if envelope["status"] != "action":
            label = "not supported" if envelope["status"] == "unsupported" else "ambiguous"
            return IntentResult(
                envelope["status"],
                f"Request is {label}: {reason or 'no reason given'}",
            )

        validation = validate_raw(envelope["action"])
        if not validation.ok or validation.action is None:
            return IntentResult(
                "invalid_output",
                "The language model proposed an action that failed validation.",
                errors=list(validation.errors),
            )

        action = validation.action
        if action.module is not None and not module_is_grounded(request, action.module):
            return IntentResult(
                "ambiguous",
                f"Request does not name module {action.module!r}; "
                "please name the module explicitly.",
                errors=[f"module {action.module!r} is not mentioned in the request"],
            )

        return IntentResult("parsed", f"Parsed request as {action.describe()}.", action=action)
