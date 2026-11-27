"""独立 Task Context；从不加载 Conversation raw history 或 Tool Event。"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.core.message_time import as_utc, timestamp
from aiive.core.scheduling_time import user_timezone
from aiive.db.models import AgentRun, AgentTask
from aiive.prompts import get_prompt_registry
from aiive.skills import SkillDefinition
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.evidence import FULL_OBSERVATION_TOOLS
from aiive.task_runtime.tool_injection import build_tool_view, discovery_state
from aiive.tools.registry import ToolRegistry


class TaskContextAssembler:
    def __init__(self, db: Session):
        self.db: Session = db

    def snapshot(
        self,
        task: AgentTask,
        capabilities: list[dict[str, Any]],
        *,
        registry: ToolRegistry | None = None,
        discovery_registry: ToolRegistry | None = None,
        reporting: bool = False,
    ) -> dict[str, Any]:
        """构造 Task Context 快照。

        capabilities 是常驻能力定义（不是全量清单）。可选工具通过
        tools.discovered / tools.loaded 暴露，且每次都按当前注册表重建，
        因此注册、卸载或定义变化不会被旧缓存掩盖。
        """
        from aiive.task_runtime.read_repository import TaskReadRepository, TaskContentUnavailable
        reader = TaskReadRepository(self.db)
        if not reader.task_visible(task):
            raise TaskContentUnavailable("task_unavailable")
        initial = (task.task_brief or {}).get("initial_input") or {}
        device_zone = initial.get("user_timezone") if isinstance(initial, dict) else None
        zone = user_timezone(device_zone if isinstance(device_zone, str) else None) or "UTC"
        current_run = self.db.get(AgentRun, task.current_run_id) if task.current_run_id else None
        if current_run is not None and (current_run.task_id != task.id or current_run.completed_at is not None):
            current_run = None
        previous_runs = self.db.query(AgentRun).filter(
            AgentRun.task_id == task.id, AgentRun.completed_at.is_not(None),
        )
        if current_run is not None:
            previous_runs = previous_runs.filter(AgentRun.run_index < current_run.run_index)
        runs = reader.filter_children(
            "agent_run", previous_runs.order_by(AgentRun.run_index.desc()).limit(20).all(),
        )[:5]
        now = datetime.now(timezone.utc)
        skill = self._active_skill(task)
        tool_view = {}
        experience = []
        if not reporting:
            execution, available = self._registries(task, registry, discovery_registry)
            from aiive.tool_discovery.service import get_discovery_service

            get_discovery_service().warm(available)
            from aiive.control.scope import TaskScope
            from aiive.experience.families import family_for
            from aiive.experience.retrieval import ExperienceInsightRetriever

            experience = ExperienceInsightRetriever(self.db).context(
                task.goal, family_for(task.goal, task.task_type), execution,
                allow_network=TaskScope.from_brief(task.task_brief or {}).allow_network,
                skill_id=initial.get("skill_id") if isinstance(initial, dict) else None,
            )
            tool_view = build_tool_view(
                execution,
                capabilities,
                discovery_state(task.task_state or {}),
                discovery_registry=available,
            )
        checkpoint = TaskRepository(self.db).latest_checkpoint(task.id)
        actions = reader.actions(task.id)
        evidence = list(reversed(reader.evidence(task.id)[-20:]))
        page_actions = {a.id for a in actions if a.capability_id in FULL_OBSERVATION_TOOLS}
        artifacts = reader.artifacts(task.id)
        event_types = ["user_input_received", "watch_triggered", "watch_failed", "approval_denied",
                       "task_recovered", "action_invalidated"]
        if not reporting:
            event_types.extend(("tool_search", "tool_describe"))
        events = reader.signals(task.id, event_types)
        workspace = None
        if (task.task_state or {}).get("worktree_path"):
            from aiive.project_paths import runtime_identity
            from aiive.supervisor.slot_manager import SlotManager

            manager = SlotManager()
            identity = runtime_identity()
            active = identity["active_slot_pointer"]
            inactive = "B" if active == "A" else "A" if active == "B" else None
            workspace = {
                **identity,
                "source_repository": task.task_state["source_repository"],
                "task_workspace": task.task_state["worktree_path"],
                "inactive_slot": str(manager.base_dir / inactive / "app") if inactive else None,
            }
        return {
            "workspace": workspace,
            "temporal": {
                "current_time": timestamp(now, zone),
                "timezone": zone,
                "task_created_at": timestamp(task.created_at, zone),
                "task_age_seconds": max(0, int((now - as_utc(task.created_at)).total_seconds())) if task.created_at else None,
                "current_run": _run_time(current_run, zone) if current_run is not None else None,
                "completed_runs": [_run_time(run, zone) for run in runs],
            },
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
                    "created_at": timestamp(item.created_at, zone),
                    "capability_id": item.capability_id,
                    "status": item.status,
                    "arguments": _bounded_value(item.arguments, 3000),
                    "result_summary": _bounded_value(item.result_summary, 2000),
                    "error": item.error_message[:2000] if item.error_message else None,
                }
                for item in reversed(actions)
            ],
            "evidence": [
                {"id": item.id, "action_id": item.action_id, "kind": item.kind, "summary": item.summary if item.action_id in page_actions and len(item.summary) <= 32000 else item.summary[:1600]}
                for item in reversed(evidence)
            ],
            "artifacts": [
                {"id": item.id, "name": item.name, "type": item.artifact_type, "summary": item.summary[:1600]}
                for item in reversed(artifacts)
            ],
            "signals": [
                {"id": item.id, "sequence": item.sequence, "created_at": timestamp(item.created_at, zone),
                 "event_type": item.event_type, "payload": _bounded_value(item.payload, 4000)}
                for item in reversed(events)
            ],
            "tools": tool_view,
            "skill": skill.as_dict() if skill else None,
            "experience": experience,
            # 长期目标：只注入当前步骤与进度，避免整份计划挤占工具定义预算。
            "goal": self._goal_context(task),
        }

    def messages(
        self,
        task: AgentTask,
        capabilities: list[dict[str, Any]],
        *,
        registry: ToolRegistry | None = None,
        discovery_registry: ToolRegistry | None = None,
    ) -> tuple[list[dict[str, str]], dict[str, Any], list[str]]:
        snapshot = self.snapshot(
            task, capabilities, registry=registry, discovery_registry=discovery_registry,
        )
        messages, prompt_refs = self.render(task, snapshot)
        return messages, snapshot, prompt_refs

    def render(
        self, task: AgentTask, snapshot: dict[str, Any],
    ) -> tuple[list[dict[str, str]], list[str]]:
        prompts = get_prompt_registry()
        system = prompts.render("task_runtime.worker_system")
        skill = self._active_skill(task)
        system_content = system.content
        if skill is not None:
            system_content += f"\n\n# Active Skill: {skill.name}\n\n{skill.instructions}"
        goal = snapshot.get("goal")
        if isinstance(goal, dict) and goal.get("current_step"):
            step = goal["current_step"]
            system_content += (
                "\n\n# 长期目标模式\n"
                f"当前执行步骤：{step.get('title')}（{step.get('kind')}，id={step.get('id')}）。\n"
                "- 这里的 finish 只表示当前步骤完成，由系统结算并推进下一步；目标整体完成由系统校验。\n"
                "- 自主步骤完成前必须留下可验证 Evidence，不能只凭口头结论宣布完成。\n"
                "- 不要替其他步骤执行 Action，也不要修改目标计划。\n"
            )
        from copy import deepcopy
        from aiive.experience.retrieval import render_experience

        prompt_snapshot = deepcopy(snapshot)
        experience = render_experience(prompt_snapshot.pop("experience", []))
        if experience:
            system_content += "\n\n" + experience
        evidence_ids = {item["id"] for item in prompt_snapshot["evidence"]}
        for action in prompt_snapshot["actions"]:
            result = action["result_summary"]
            if isinstance(result, dict) and result.get("evidence_id") in evidence_ids:
                result.pop("summary", None)
        for group in ("resident", "available", "loaded"):
            for definition in prompt_snapshot["tools"].get(group, []):
                definition.pop("parameters", None)
                if definition.pop("input_schema", None) is not None and definition.get("status", "ready") == "ready":
                    definition.pop("description", None)
        # Binding and prompt metadata share the same exact selection snapshot.
        user = prompts.render(
            "task_runtime.task_context",
            task_context=json.dumps(prompt_snapshot, ensure_ascii=False, default=str),
        )
        return (
            [{"role": system.role, "content": system_content}, {"role": user.role, "content": user.content}],
            [system.audit_ref, user.audit_ref],
        )

    def _registries(
        self,
        task: AgentTask,
        registry: ToolRegistry | None,
        discovery_registry: ToolRegistry | None,
    ) -> tuple[ToolRegistry, ToolRegistry]:
        """返回（执行视图，发现视图）；调用方已提供的一侧直接复用。"""
        if registry is not None and discovery_registry is not None:
            return registry, discovery_registry
        from aiive.control.capability_broker import CapabilityBroker

        broker = CapabilityBroker(self.db)
        available = discovery_registry if discovery_registry is not None else broker.available_registry(task)
        return (
            registry if registry is not None else broker.registry_for(task, available=available),
            available,
        )

    @staticmethod
    def _goal_context(task: AgentTask) -> dict[str, Any] | None:
        """长期目标的聚焦视图：当前步骤 + 步骤状态清单（不含完整计划正文）。"""
        raw = (task.task_state or {}).get("goal")
        if not isinstance(raw, dict):
            return None
        steps = [item for item in (raw.get("steps") or []) if isinstance(item, dict)]
        current_id = raw.get("current_step_id")
        current = next((item for item in steps if item.get("id") == current_id), None)
        return {
            "status": raw.get("status"),
            "deadline": raw.get("deadline"),
            "acceptance": list(raw.get("acceptance") or []),
            "current_step": (
                {
                    "id": current.get("id"),
                    "title": current.get("title"),
                    "kind": current.get("kind"),
                    "acceptance": list(current.get("acceptance") or []),
                }
                if current
                else None
            ),
            "steps": [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "kind": item.get("kind"),
                    "status": item.get("status"),
                }
                for item in steps
            ],
        }

    @staticmethod
    def _active_skill(task: AgentTask) -> SkillDefinition | None:
        initial = (task.task_brief or {}).get("initial_input")
        skill_id = initial.get("skill_id") if isinstance(initial, dict) else None
        if not isinstance(skill_id, str) or not skill_id:
            return None
        from aiive.skills import get_skill

        return get_skill(skill_id)


def _run_time(run: AgentRun, zone: str) -> dict[str, Any]:
    start, end = run.started_at, run.completed_at
    return {
        "run_index": run.run_index,
        "status": run.status,
        "created_at": timestamp(run.created_at, zone),
        "started_at": timestamp(start, zone),
        "completed_at": timestamp(end, zone),
        "duration_seconds": (as_utc(end) - as_utc(start)).total_seconds() if start and end else None,
    }


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
    # 工具发现状态已在 snapshot.tools 中按当前注册表重建，这里不重复注入原始记录。
    compact.pop("tool_discovery", None)
    compact.pop("searched_tool_queries", None)
    compact.pop("seen_tool_definitions", None)
    compact.pop("workspace_observations", None)
    compact.pop("workspace_delivery", None)
    compact.pop("validation_feedback", None)
    compact.pop("validation_messages", None)
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
    # Progress cursors are recovery state, never hide them inside a JSON preview.
    progress = compact.pop("attachment_processing", None)
    required = compact.pop("required_attachment_ids", None)
    result = _bounded_value(compact, 32_000)
    if progress is not None:
        result["attachment_processing"] = progress
    if required is not None:
        result["required_attachment_ids"] = required
    return result
