"""一次有界 Worker LLM 决策；不直接执行 capability。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from json_repair import repair_json
from sqlalchemy.orm import Session

from aiive.core.llm_client import LLMClient, default_llm_client
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


class TaskAgentExecutor:
    def __init__(self, db: Session, llm_client: LLMClient | None = None):
        self.db = db
        self.llm = llm_client or default_llm_client()

    def decide(self, task: AgentTask, capabilities: list[dict[str, Any]]) -> AgentDecision:
        task_id = task.id
        messages, snapshot, prompt_refs = TaskContextAssembler(self.db).messages(task, capabilities)
        # Task Context 读取完成后立即结束读事务；远程 LLM 调用期间不占数据库事务。
        self.db.commit()
        response = self.llm.chat(
            messages,
            temperature=0.1,
            trace_id=task_id,
            json_mode=True,
        )
        proposal = self.parse(response.content)
        return AgentDecision(
            proposal=proposal,
            model=response.model,
            token_usage=response.usage,
            input_snapshot=snapshot,
            prompt_refs=prompt_refs,
        )

    @staticmethod
    def parse(raw: str) -> ActionProposal:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines.pop()
            text = "\n".join(lines)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = json.loads(repair_json(text))
        return ActionProposal.model_validate(payload)
