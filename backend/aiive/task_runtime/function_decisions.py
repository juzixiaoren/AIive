"""Worker-only Function Calling definitions. Never dispatch or invoke a handler."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any

from aiive.core.function_calling import function_definition
from aiive.task_runtime.schemas import ActionProposal
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry


CONTROLS = {
    "task_finish": ("finish", ("summary", "report_status")),
    "task_ask_user": ("ask_user", ("question",)),
    "task_watch": ("watch", ("watch",)),
    "task_search_capabilities": ("search_capabilities", ("query",)),
    "task_describe_capability": ("describe_capability", ("capability_id",)),
}


def proposal_function_name(capability_id: str) -> str:
    # Prefix separates domain controls and capabilities, including MCP long names.
    digest = hashlib.sha256(capability_id.encode()).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", capability_id)[:36]
    return f"propose_{slug}_{digest}"


def _never_execute(**_kwargs: Any) -> Any:
    raise RuntimeError("worker_decisions_are_not_executable")


class WorkerFunctions:
    def __init__(self, tool_view: dict[str, Any]):
        self.registry: ToolRegistry = ToolRegistry()
        self.capabilities: dict[str, str] = {}
        fields = ActionProposal.model_json_schema()["properties"]
        for name, (_, keys) in CONTROLS.items():
            properties = {key: deepcopy(fields[key]) for key in keys}
            required = [key for key in keys if key not in {"report_status"}]
            for key in ("question", "query", "capability_id"):
                if key in properties:
                    properties[key]["minLength"] = 1
            if name == "task_watch":
                properties["watch"] = {
                    "type": "object", "required": ["watch_type"], "additionalProperties": False,
                    "properties": {"watch_type": {"type": "string", "enum": ["time", "file", "node"]},
                                   "config": {"type": "object", "additionalProperties": True, "default": {}},
                                   "next_check_at": {"type": ["string", "null"], "default": None}},
                }
            self._register(name, f"Task control: {CONTROLS[name][0]}. Produce a decision only.",
                           {"type": "object", "properties": properties, "required": required, "additionalProperties": False})
        selected = {}
        for group in ("resident", "available", "loaded"):
            for definition in tool_view.get(group, []):
                if definition.get("input_schema") is not None and definition.get("status", "ready") == "ready":
                    selected[definition["capability_id"]] = definition
        for capability_id, definition in selected.items():
            name = proposal_function_name(capability_id)
            if name in self.capabilities and self.capabilities[name] != capability_id:
                raise RuntimeError("proposal_function_name_collision")
            self.capabilities[name] = capability_id
            arguments = deepcopy(definition["input_schema"])
            definitions = arguments.pop("$defs", {})
            schema = {"type": "object", "additionalProperties": False, "required": ["arguments"],
                      "properties": {"arguments": arguments, **{key: deepcopy(fields[key]) for key in ("preconditions", "effects", "summary")}}}
            if definitions:
                schema["$defs"] = definitions
            self._register(name, f"Propose {capability_id}: {definition.get('description', '')}. Does not execute the tool.", schema)

    def _register(self, name: str, description: str, schema: dict[str, Any]) -> None:
        self.registry.register(ToolRegistration(
            CapabilitySafetySchema(name, "local_builtin", "trusted", "low"),
            _never_execute, description=description, input_schema=schema,
        ))

    def definitions(self) -> list[dict[str, Any]]:
        definitions = []
        for item in self.registry.catalog():
            registration = self.registry.get(item["capability_id"])
            assert registration is not None
            definitions.append({"type": "function", "function": function_definition(registration)})
        return definitions

    def proposal(self, name: str, arguments: dict[str, Any]) -> ActionProposal:
        if name in self.capabilities:
            return ActionProposal.model_validate({"decision": "action", "capability_id": self.capabilities[name], **arguments}, strict=True)
        return ActionProposal.model_validate({"decision": CONTROLS[name][0], **arguments}, strict=True)
