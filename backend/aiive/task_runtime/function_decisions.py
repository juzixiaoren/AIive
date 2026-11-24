"""Worker-only Function Calling definitions. Never dispatch or invoke a handler."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any

from aiive.core.function_calling import function_definition
from aiive.task_runtime.schemas import ActionProposal, FilePreconditions
from aiive.task_runtime.watch_contract import WATCH_PROPOSAL
from aiive.tools.registry import CapabilitySafetySchema
from aiive.core.function_spec import FunctionSpec, FunctionSpecs


CONTROLS = {
    "task_finish": ("finish", ("summary", "report_status", "risks", "unresolved")),
    "task_ask_user": ("ask_user", ("question",)),
    "task_watch": ("watch", ("watch",)),
    "task_search_capabilities": ("search_capabilities", ("query", "namespace_ids", "limit")),
    "task_describe_capability": ("describe_capability", ("capability_id",)),
}


def proposal_function_name(capability_id: str) -> str:
    # Prefix separates domain controls and capabilities, including MCP long names.
    digest = hashlib.sha256(capability_id.encode()).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", capability_id)[:36]
    return f"propose_{slug}_{digest}"


class WorkerFunctions:
    def __init__(self, tool_view: dict[str, Any]):
        self.registry: FunctionSpecs = FunctionSpecs()
        self.capabilities: dict[str, str] = {}
        self.descriptor_hashes: dict[str, str] = {}
        fields = ActionProposal.model_json_schema()["properties"]
        for name, (_, keys) in CONTROLS.items():
            properties = {key: deepcopy(fields[key]) for key in keys}
            required = [key for key in keys if key not in {"report_status", "risks", "unresolved", "namespace_ids", "limit"}]
            definitions = {}
            description = f"Task control: {CONTROLS[name][0]}. Produce a decision only."
            for key in ("question", "query", "capability_id"):
                if key in properties:
                    properties[key]["minLength"] = 1
            if name == "task_watch":
                properties["watch"] = WATCH_PROPOSAL.json_schema()
                definitions = properties["watch"].pop("$defs", {})
                description = "Wait for one match, then resume this task. File watches run on the backend host."
            elif name == "task_search_capabilities":
                description = "Search for tools by goal or exact name and load relevant authorized schemas. Omit namespace_ids to search globally."
            elif name == "task_describe_capability":
                description = "Load or refresh the schema for one authorized capability by its exact ID."
            schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
            if definitions:
                schema["$defs"] = definitions
            self._register(name, description, schema)
        selected = {}
        for group in ("resident", "available", "loaded"):
            for definition in tool_view.get(group, []):
                if (definition.get("in_scope") is True and definition.get("input_schema") is not None
                        and definition.get("status", "ready") == "ready"):
                    selected[definition["capability_id"]] = definition
        for capability_id, definition in selected.items():
            name = proposal_function_name(capability_id)
            if name in self.capabilities and self.capabilities[name] != capability_id:
                raise RuntimeError("proposal_function_name_collision")
            self.capabilities[name] = capability_id
            self.descriptor_hashes[name] = definition["descriptor_hash"]
            arguments = deepcopy(definition["input_schema"])
            definitions = arguments.pop("$defs", {})
            properties = {"arguments": arguments, **{
                key: deepcopy(fields[key]) for key in ("effects", "summary", "progress_draft")
            }}
            if (definition.get("writes_external_world") and capability_id not in {"edit_file", "write_file"}
                    and {"path", "source", "file_path"}.intersection(arguments.get("properties", {}))):
                properties["preconditions"] = FilePreconditions.model_json_schema()
            schema = {"type": "object", "additionalProperties": False, "required": ["arguments"],
                      "properties": properties}
            if definitions:
                schema["$defs"] = definitions
            self._register(name, f"Propose {capability_id}: {definition.get('description', '')}. Does not execute the tool.", schema)

    def _register(self, name: str, description: str, schema: dict[str, Any]) -> None:
        self.registry.register(FunctionSpec(
            CapabilitySafetySchema(name, "local_builtin", "trusted", "low"),
            description=description, input_schema=schema,
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
            return ActionProposal.model_validate({
                **arguments, "decision": "action", "capability_id": self.capabilities[name],
                "descriptor_hash": self.descriptor_hashes[name],
            }, strict=True)
        return ActionProposal.model_validate({"decision": CONTROLS[name][0], **arguments}, strict=True)
