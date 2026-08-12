"""独立 Task Context；从不加载 Conversation raw history 或 Tool Event。"""
from __future__ import annotations

import json
import hashlib
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentAction, AgentTask, AgentTaskEvent, TaskArtifact, TaskEvidence
from aiive.prompts import get_prompt_registry
from aiive.skills import SkillDefinition
from aiive.task_runtime.repository import TaskRepository


class TaskContextAssembler:
    def __init__(self, db: Session):
        self.db: Session = db

    def snapshot(self, task: AgentTask, capabilities: list[dict[str, Any]]) -> dict[str, Any]:
        skill = self._active_skill(task)
        checkpoint = TaskRepository(self.db).latest_checkpoint(task.id)
        actions = self.db.query(AgentAction).filter(
            AgentAction.task_id == task.id,
        ).order_by(AgentAction.sequence.desc()).limit(20).all()
        evidence = self.db.query(TaskEvidence).filter(
            TaskEvidence.task_id == task.id,
        ).order_by(TaskEvidence.created_at.desc()).limit(20).all()
        artifacts = self.db.query(TaskArtifact).filter(
            TaskArtifact.task_id == task.id,
        ).order_by(TaskArtifact.created_at.desc()).limit(20).all()
        events = self.db.query(AgentTaskEvent).filter(
            AgentTaskEvent.task_id == task.id,
            AgentTaskEvent.event_type.in_([
                "user_input_received", "watch_triggered", "approval_denied",
                "task_recovered", "action_invalidated",
            ]),
        ).order_by(AgentTaskEvent.sequence.desc()).limit(10).all()
        return {
            "task": {
                "id": task.id,
                "type": task.task_type,
                "goal": task.goal,
                "status": task.status,
                "brief": _bounded_value(task.task_brief or {}, 20_000),
                "state": _compact_task_state(task.task_state or {}),
                "budgets": task.budgets or {},
                "usage": task.usage or {},
            },
            "checkpoint": {
                "version": checkpoint.version,
                "state": _bounded_value(checkpoint.state, 16_000),
                "plan": _bounded_value(checkpoint.plan, 8000),
                "reason": checkpoint.reason,
            } if checkpoint else None,
            "actions": [
                {
                    "id": item.id,
                    "sequence": item.sequence,
                    "capability_id": item.capability_id,
                    "status": item.status,
                    "arguments": _bounded_value(item.arguments, 3000),
                    "result_summary": _bounded_value(item.result_summary, 2000),
                    "error": item.error_message[:2000] if item.error_message else None,
                }
                for item in reversed(actions)
            ],
            "evidence": [
                {"id": item.id, "action_id": item.action_id, "kind": item.kind, "summary": item.summary[:1600]}
                for item in reversed(evidence)
            ],
            "artifacts": [
                {"id": item.id, "name": item.name, "type": item.artifact_type, "summary": item.summary[:1600]}
                for item in reversed(artifacts)
            ],
            "signals": [
                {"event_type": item.event_type, "payload": _bounded_value(item.payload, 4000)}
                for item in reversed(events)
            ],
            "capability_ids": [item.get("capability_id") for item in capabilities],
            "skill": skill.as_dict() if skill else None,
        }

    def messages(self, task: AgentTask, capabilities: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, Any], list[str]]:
        snapshot = self.snapshot(task, capabilities)
        prompts = get_prompt_registry()
        system = prompts.render("task_runtime.worker_system")
        skill = self._active_skill(task)
        system_content = system.content
        if skill is not None:
            system_content += f"\n\n# Active Skill: {skill.name}\n\n{skill.instructions}"
        user = prompts.render(
            "task_runtime.task_context",
            task_context=json.dumps(snapshot, ensure_ascii=False, default=str),
            capabilities=json.dumps(capabilities, ensure_ascii=False, default=str),
        )
        return (
            [{"role": system.role, "content": system_content}, {"role": user.role, "content": user.content}],
            snapshot,
            [system.audit_ref, user.audit_ref],
        )

    @staticmethod
    def _active_skill(task: AgentTask) -> SkillDefinition | None:
        initial = (task.task_brief or {}).get("initial_input")
        skill_id = initial.get("skill_id") if isinstance(initial, dict) else None
        if not isinstance(skill_id, str) or not skill_id:
            return None
        from aiive.skills import get_skill

        return get_skill(skill_id)


def _bounded_value(value: Any, max_chars: int) -> Any:
    """保留小对象原结构；大对象只进入可验证摘要，避免 Task Prompt 再次膨胀。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw) <= max_chars:
        return value
    return {
        "truncated": True,
        "original_chars": len(raw),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "preview": raw[:max_chars],
    }


def _compact_task_state(state: dict[str, Any]) -> dict[str, Any]:
    compact = dict(state)
    inputs = state.get("inputs")
    if isinstance(inputs, list):
        compact["inputs"] = [
            _bounded_value(item, 1500) for item in inputs[-10:]
        ]
        if len(inputs) > 10:
            compact["older_input_count"] = len(inputs) - 10
    for key in ("plan", "last_decision"):
        if key in compact:
            compact[key] = _bounded_value(compact[key], 6000)
    return _bounded_value(compact, 32_000)
