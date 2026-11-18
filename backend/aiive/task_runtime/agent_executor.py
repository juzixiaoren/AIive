"""一次有界 Worker LLM 决策；不直接执行 capability。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from langchain_core.messages import AIMessage, BaseMessage, convert_to_messages, messages_from_dict
from langchain_core.runnables import Runnable
from aiive.core.function_calling import build_chat_model, normalize_calls, validate_calls, response_usage
from aiive.task_runtime.function_decisions import WorkerFunctions
from aiive.task_runtime.errors import AgentOutputValidationError
from sqlalchemy.orm import Session

from aiive.core.llm_client import LLMClient, default_llm_client, normalize_llm_error
from aiive.db.models import AgentTask
from aiive.task_runtime.context_assembler import TaskContextAssembler
from aiive.task_runtime.schemas import ActionProposal


@dataclass(frozen=True)
class AgentDecision:
    proposal: ActionProposal
    model: str
    token_usage: dict[str, int]
    input_snapshot: dict[str, Any]
    prompt_refs: list[str]
    call_message: AIMessage | None = None


class TaskAgentExecutor:
    def __init__(self, db: Session, llm_client: LLMClient | None = None, *, chat_model: Runnable[Any, AIMessage] | None = None):
        self.db: Session = db
        self.llm: LLMClient = llm_client or default_llm_client()
        self.chat_model: Any = chat_model

    def decide(self, task: AgentTask, capabilities: list[dict[str, Any]]) -> AgentDecision:
        from aiive.forget.concurrency import forget_revision
        from aiive.task_runtime.read_repository import TaskReadRepository, TaskContentUnavailable, inherit_sources
        task_id = task.id
        revision = forget_revision(self.db)
        messages, snapshot, prompt_refs = TaskContextAssembler(self.db).messages(task, capabilities)
        parents = [(kind, item["id"]) for key, kind in (
            ("actions", "agent_action"), ("evidence", "task_evidence"),
            ("artifacts", "task_artifact"), ("signals", "agent_task_event")) for item in snapshot.get(key, [])]
        # Subsequent model decisions/state are derived from this exact input.
        inherit_sources(self.db, "agent_task", task_id, parents + [("goal_plan", task_id)])
        if task.current_run_id:
            inherit_sources(self.db, "agent_run", task.current_run_id, [("agent_task", task_id)])
        self.db.flush()
        # Task Context 读取完成后立即结束读事务；远程 LLM 调用期间不占数据库事务。
        self.db.commit()
        if forget_revision(self.db) != revision or not TaskReadRepository(self.db).task_visible(task):
            raise TaskContentUnavailable("task_context_source_changed")
        self.db.commit()
        functions = WorkerFunctions(snapshot["tools"])
        call_messages: list[BaseMessage] = convert_to_messages(messages)
        feedback = (task.task_state or {}).get("validation_messages")
        if feedback:
            call_messages.extend(messages_from_dict(feedback))
        model = self.chat_model if self.chat_model is not None else build_chat_model(self.llm)
        try:
            # Thinking providers may reject forced tool_choice. Exactly one call is
            # still enforced locally by validate_calls(require_one=True), with bounded repair.
            response = model.bind_tools(functions.definitions(), tool_choice="auto", parallel_tool_calls=False).invoke(
                call_messages, config={"metadata": {"task_id": task_id}, "run_name": "task_decision"},
            )
        except TaskContentUnavailable:
            raise
        except Exception as error:
            raise normalize_llm_error(error, trace_id=task_id) from error
        if forget_revision(self.db) != revision or not TaskReadRepository(self.db).task_visible(task):
            raise TaskContentUnavailable("task_context_source_changed")
        response = normalize_calls(response)
        usage = response_usage(response)
        actual_model = str(response.response_metadata.get("model_name") or self.llm.default_model)
        validation = validate_calls(response, functions.registry, require_one=True)
        if not validation.ok:
            for call in response.tool_calls:
                payload = validation.errors.get(str(call["id"]))
                if payload is not None and call["name"] in functions.capabilities:
                    payload["capability_id"] = functions.capabilities[call["name"]]
            payload = next(iter(validation.errors.values()))
            raise AgentOutputValidationError(payload.get("issues", []), token_usage=usage, model=actual_model,
                                             payload=payload, feedback=validation.retry_turn(response))
        call = response.tool_calls[0]
        try:
            proposal = functions.proposal(call["name"], call["args"])
        except ValidationError as error:
            # Domain DTO invariants (e.g. whitespace-only question) remain strict.
            from aiive.core.function_calling import CallValidation, output_error
            issues = [{"path": ".".join(map(str, item["loc"])) or "$", "code": item["type"],
                       "message": item["msg"][:240], "expected": "valid decision fields",
                       "received": type(item.get("input")).__name__} for item in error.errors(include_url=False)]
            payload = output_error("decision_payload")
            payload["issues"] = issues
            validation = CallValidation({str(call["id"]): payload})
            raise AgentOutputValidationError(issues, token_usage=usage, model=actual_model,
                                             payload=payload, feedback=validation.retry_turn(response)) from error
        snapshot["model_tool_call"] = {"id": call["id"], "name": call["name"], "capability_id": proposal.capability_id}
        return AgentDecision(proposal=proposal, model=actual_model, token_usage=usage,
                             input_snapshot=attachment_reference_snapshot(snapshot), prompt_refs=prompt_refs, call_message=response)



def attachment_reference_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Audit snapshots keep evidence IDs; transient Worker messages keep the page."""
    from copy import deepcopy
    result = deepcopy(snapshot)
    reads = {a["id"] for a in result.get("actions", []) if a.get("capability_id") == "read_attachment"}
    for action in result.get("actions", []):
        if action.get("id") in reads:
            action["result_summary"] = {"body_omitted": True}
    for evidence in result.get("evidence", []):
        if evidence.get("action_id") in reads:
            evidence["summary"] = "[附件读取结果仅按 evidence id 引用；正文未复制到快照]"
    return result
