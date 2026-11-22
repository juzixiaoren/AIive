"""Main Agent 唯一可见的 Persistent Task 元工具。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import override, Any

from fastapi.encoders import jsonable_encoder

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.router import normalize_task_brief
from aiive.task_runtime.schemas import TaskBudget, ActionProposal
from aiive.task_runtime.goal_plan import GoalPlan
from aiive.tools.base import BaseTool
from aiive.tools.quick_web_search import QUICK_WEB_SEARCH_TIMEOUT_SECONDS, QuickWebSearchTool
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition, ToolRegistry


TASK_STATUS_INLINE_CHARS = 4000
TASK_DETAILS_PAGE_CHARS = 1800


def _require_context(ctx: RunContext | None) -> RunContext:
    if ctx is None or not ctx.thread_id:
        raise ValueError("task_meta_tool_requires_run_context")
    return ctx


class DelegateTaskTool(BaseTool):
    @override
    def _run(self,
        goal: str,
        task_type: str = "general",
        skill_id: str = "",
        title: str = "",
        acceptance_criteria: list[str] | None = None,
        constraints: list[str] | None = None,
        allowed_roots: list[str] | None = None,
        allowed_capabilities: list[str] | None = None,
        allowed_hosts: list[str] | None = None,
        allow_network: bool | None = None,
        allow_secrets: bool = False,
        isolation_mode: str = "shared",
        deterministic_action: dict[str, Any] | None = None,
        target_node_id: str = "",
        budgets: dict[str, Any] | None = None,
        goal_plan: dict[str, Any] | None = None,
        attachment_ids: list[str] | None = None,
        ctx: RunContext | None = None,
    ) -> dict[str, Any]:
        run_ctx = _require_context(ctx)
        if goal_plan is not None:
            if attachment_ids:
                raise ValueError("goal_plan_attachment_authorization_not_supported")
            return _delegate_goal(
                run_ctx,
                goal=goal,
                title=title,
                raw_plan=goal_plan,
                acceptance_criteria=acceptance_criteria or [],
                constraints=constraints or [],
                budgets=budgets,
                scope={
                    "allowed_roots": allowed_roots or [], "allowed_capabilities": allowed_capabilities or [],
                    "allowed_hosts": allowed_hosts or [], "allow_network": bool(allow_network),
                    "allow_secrets": allow_secrets, "isolation_mode": isolation_mode,
                },
            )
        selected_skill = None
        if skill_id:
            from aiive.skills import get_skill

            selected_skill = get_skill(skill_id)
            if selected_skill is None:
                raise ValueError("skill_not_found")
        effective_task_type = (
            selected_skill.default_task_type
            if selected_skill is not None and task_type == "general"
            else task_type
        )
        if allowed_capabilities:
            # 调用方显式给出的能力列表是真实授权约束，保持 restricted。
            effective_capabilities = list(allowed_capabilities)
            capability_policy = "restricted"
        else:
            # Skill 只提供指令与网络需求，不再用技能 profile 缩小可发现能力面。
            effective_capabilities = []
            capability_policy = "unified"
        raw_brief = {
            "acceptance_criteria": acceptance_criteria or [],
            "constraints": constraints or [],
            "scope": {
                "allowed_roots": allowed_roots or [],
                "allowed_capabilities": effective_capabilities,
                "capability_policy": capability_policy,
                "allowed_hosts": allowed_hosts or [],
                "allow_network": allow_network if allow_network is not None else bool(
                    selected_skill and selected_skill.requires_network
                ),
                "allow_secrets": allow_secrets,
                "isolation_mode": isolation_mode,
            },
            "initial_input": {
                "attachment_ids": attachment_ids if attachment_ids is not None else [],
                **({"user_timezone": run_ctx.user_timezone} if run_ctx.user_timezone else {}),
                **({"skill_id": selected_skill.skill_id} if selected_skill else {}),
                **({"deterministic_action": deterministic_action} if deterministic_action else {}),
            },
        }
        brief = normalize_task_brief(effective_task_type, raw_brief)
        db = SessionLocal()
        try:
            task = TaskRepository(db).create_task(
                thread_id=run_ctx.thread_id,
                source_turn_record_id=run_ctx.turn_record_id or None,
                goal=goal,
                title=title or None,
                task_type=effective_task_type,
                task_brief=brief,
                budgets=budgets,
                target_node_id=target_node_id or None,
            )
            db.commit()
            return {
                "task_id": task.id,
                "status": task.status,
                "title": task.title,
                "skill_id": selected_skill.skill_id if selected_skill else None,
                "summary": "任务已进入持久执行队列；结束后对话会跟进，状态、审批和产物也可在任务中心查看。",
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()



def _delegate_goal(
    run_ctx: RunContext,
    *,
    goal: str,
    title: str,
    raw_plan: dict[str, Any],
    acceptance_criteria: list[str],
    constraints: list[str],
    budgets: dict[str, Any] | None,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """把结构化计划落成"待确认"的长期目标任务（不入执行队列）。"""
    from aiive.task_runtime.goal_plan import GOAL_TASK_TYPE, GoalPlan, summarize
    from aiive.task_runtime.goal_service import GoalService

    payload = dict(raw_plan)
    if run_ctx.user_timezone:
        payload.setdefault("timezone", run_ctx.user_timezone)
    if acceptance_criteria and not payload.get("acceptance"):
        payload["acceptance"] = acceptance_criteria
    # 先做纯模型校验：计划非法时不落库，直接把可读错误返回给主 Agent。
    GoalPlan.model_validate({**payload, "status": "proposed", "created_at": "1970-01-01T00:00:00+00:00"})
    db = SessionLocal()
    try:
        brief = normalize_task_brief(GOAL_TASK_TYPE, {
            "acceptance_criteria": list(payload.get("acceptance") or []),
            "constraints": constraints,
            "scope": scope,
            "initial_input": {"goal": True, **({"user_timezone": run_ctx.user_timezone} if run_ctx.user_timezone else {})},
        })
        task = TaskRepository(db).create_task(
            thread_id=run_ctx.thread_id,
            source_turn_record_id=run_ctx.turn_record_id or None,
            goal=goal,
            title=title or None,
            task_type=GOAL_TASK_TYPE,
            task_brief=brief,
            budgets=budgets,
            enqueue=False,
        )
        plan = GoalService(db).attach_plan(task, payload)
        db.commit()
        return {
            "task_id": task.id,
            "status": "proposed",
            "title": task.title,
            "summary": "长期目标计划已生成，等待用户确认后开始执行；确认前不会启动任何执行。",
            "plan": summarize(plan),
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

class GetTaskStatusTool(BaseTool):
    @override
    def _run(
        self,
        task_id: str,
        details_cursor: str = "",
        evidence_id: str = "",
        evidence_offset: int = 0,
        ctx: RunContext | None = None,
    ) -> dict[str, Any]:
        run_ctx = _require_context(ctx)
        db = SessionLocal()
        try:
            from aiive.forget.concurrency import acquire_forget_shared_lock
            acquire_forget_shared_lock(db)
            repo = TaskRepository(db)
            task = repo.get_task(task_id)
            if task is None or task.thread_id != run_ctx.thread_id:
                raise ValueError("task_not_found")
            if evidence_id:
                from aiive.task_runtime.evidence import EvidenceStore
                from aiive.task_runtime.read_repository import TaskReadRepository
                from aiive.tools.task_file_tools import read_text_task_file

                evidence = TaskReadRepository(db).get_evidence(task.id, evidence_id)
                return {"task_id": task.id, "evidence": read_text_task_file(
                    evidence, EvidenceStore.read, db, evidence_offset, TASK_DETAILS_PAGE_CHARS,
                )}
            detail = repo.detail(task, include_events=False)
            result: dict[str, Any] = {
                "task_id": task.id,
                "title": task.title,
                "task_type": task.task_type,
                "status": task.status,
                "created_at": task.created_at.isoformat(),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                "usage": task.usage or {},
                "budgets": task.budgets or {},
                "artifact_count": len(detail["artifacts"]),
            }
            report = task.report or {}
            for key in ("budget_exhausted", "stop_reason"):
                if key in report:
                    result[key] = report[key]
            approvals = jsonable_encoder([
                item for item in detail["approvals"] if item["status"] == "pending"
            ], sqlalchemy_safe=False)
            details = {
                "report": task.report,
                "pending_approvals": approvals,
            }
            if task.last_error:
                details["last_error"] = task.last_error
            request = {"goal": task.goal, "acceptance_criteria": (task.task_brief or {}).get("acceptance_criteria") or []}
            if len(json.dumps(request, ensure_ascii=False)) > TASK_STATUS_INLINE_CHARS // 2:
                details.update(request)
            else:
                result.update(request)
            inline = {**result, **details}
            if not details_cursor and len(json.dumps(inline, ensure_ascii=False)) <= TASK_STATUS_INLINE_CHARS:
                return inline

            # 页游标绑定本次详情内容，防止任务或审批更新时把两份快照拼在一起。
            serialized = json.dumps(details, ensure_ascii=False, sort_keys=True)
            version = hashlib.sha256(f"{task.id}:{serialized}".encode()).hexdigest()[:16]
            offset = 0
            if details_cursor:
                cursor_version, _, cursor_offset = details_cursor.partition(":")
                if cursor_version != version:
                    raise ValueError("task_details_changed")
                offset = int(cursor_offset)
                if not 0 < offset < len(serialized):
                    raise ValueError("invalid_task_details_cursor")
            end = min(offset + TASK_DETAILS_PAGE_CHARS, len(serialized))
            return {
                **result,
                "pending_approval_count": len(approvals),
                "details_page": {
                    "content": serialized[offset:end],
                    "offset": offset,
                    "total_chars": len(serialized),
                    "next_cursor": f"{version}:{end}" if end < len(serialized) else None,
                },
            }
        finally:
            db.close()



class CancelTaskTool(BaseTool):
    @override
    def _run(self, task_id: str, ctx: RunContext | None = None) -> dict[str, Any]:
        run_ctx = _require_context(ctx)
        db = SessionLocal()
        try:
            repo = TaskRepository(db)
            task = repo.get_task(task_id)
            if task is None or task.thread_id != run_ctx.thread_id:
                raise ValueError("task_not_found")
            task = repo.cancel_task(task_id)
            db.commit()
            return {"task_id": task.id, "status": task.status}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()



class SendTaskInputTool(BaseTool):
    @override
    def _run(self, task_id: str, content: str, ctx: RunContext | None = None) -> dict[str, Any]:
        run_ctx = _require_context(ctx)
        db = SessionLocal()
        try:
            repo = TaskRepository(db)
            task = repo.get_task(task_id)
            if task is None or task.thread_id != run_ctx.thread_id:
                raise ValueError("task_not_found")
            task = repo.send_input(task_id, content)
            db.commit()
            return {"task_id": task.id, "status": task.status, "input_accepted": True}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()



def _registration(
    capability_id: str,
    description: str,
    parameters: dict[str, Any],
    tool_type: type[BaseTool],
) -> BaseTool:
    return tool_type(ToolDefinition(
        safety=CapabilitySafetySchema(
            capability_id=capability_id,
            definition_source="task_runtime",
            definition_trust_level="trusted_core",
            risk_level="low",
            allowed_instruction_sources=["trusted_user_command"],
            effect_mode="db_transactional",
        ),
        description=description,
        parameters=parameters,
    ))


class ListSkillsTool(BaseTool):
    @override
    def _run(self) -> dict[str, Any]:
        from aiive.skills import list_skills

        return {
            "skills": [skill.as_dict() for skill in list_skills()],
            "hint": "将选中的 skill_id 传给 delegate_task 以加载工作指引和联网需求。实际执行受任务授权、服务端策略和审批约束。",
        }



def build_main_agent_registry() -> ToolRegistry:
    """User-facing task, attachment, reminder, memory and privacy controls."""
    from aiive.skills import list_skills

    registry = ToolRegistry()
    deterministic_schema = ActionProposal.model_json_schema()
    deterministic_schema["properties"] = {key: value for key, value in deterministic_schema["properties"].items()
                                          if key in {"capability_id", "arguments", "preconditions", "effects", "summary"}}
    deterministic_schema["required"] = ["capability_id"]
    registry.register(_registration(
        "list_skills",
        "列出可用于持久任务的内置 Skill、相关能力和联网要求。",
        {},
        ListSkillsTool,
    ))
    registry.register(_registration(
        "delegate_task",
        "创建可跨会话、审批和重启持续执行的任务。复杂、全文文档或跨轮工作委托为任务；普通聊天附件问题可直接使用 read_attachment。",
        {
            "goal": {"type": "str", "required": True, "minLength": 1},
            "task_type": {"type": "str", "default": "general"},
            "skill_id": {
                "type": "str",
                "description": "可选内置 Skill ID；先用 list_skills 查询。",
                "enum": ["", *(skill.skill_id for skill in list_skills())],
            },
            "attachment_ids": {"type": "list", "description": "聊天附件任务的明确授权 ID 列表，仅用于 document_processing；目标文本不授予附件权限。"},
            "title": {"type": "str", "description": "简短任务标题，完整目标和约束请写入 goal 和 constraints。"},
            "acceptance_criteria": {"type": "list"},
            "constraints": {"type": "list"},
            "allowed_roots": {
                "type": "list",
                "description": "本机或桌面文件、Shell 工作目录、文件等待和知识摄取需要用户授权的最小绝对路径根。聊天附件用 attachment_ids。",
            },
            "allowed_capabilities": {
                "type": "list",
                "description": "非空时为任务的能力授权白名单；留空时由 Worker 在统一工具目录按需发现，仍受任务范围和审批约束。",
            },
            "allowed_hosts": {"type": "list", "description": "可选联网域名白名单"},
            "allow_network": {"type": "bool", "default": False},
            "allow_secrets": {"type": "bool", "default": False},
            "isolation_mode": {
                "type": "str", "enum": ["shared", "git_worktree"], "default": "shared",
                "description": "代码任务可显式要求独立 Git worktree",
            },
            "deterministic_action": {
                "schema": deterministic_schema,
                "type": "dict",
                "description": "可选；目标已完全确定时直接给出 capability_id/arguments/preconditions/effects，仍经过 Broker 与审批",
            },
            "target_node_id": {"type": "str"},
            "budgets": {"type": "dict", "schema": TaskBudget.model_json_schema()},
            "goal_plan": {
                "schema": GoalPlan.model_json_schema(),
                "type": "dict",
                "description": (
                    "长期目标计划（自然语言里的跨周/跨月目标用这个）："
                    "{summary, deadline, acceptance, timezone, steps:[{id, title, "
                    "kind(autonomous|interactive|approval_required), depends_on, acceptance, "
                    "schedule{kind:once|weekly, at|weekday+time, until}}]}。"
                    "提供后按提案处理，必须先经用户确认才会执行。"
                ),
            },
        },
        DelegateTaskTool,
    ))
    registry.register(_registration(
        "get_task_status",
        "查询当前对话任务的原始目标、验收标准、状态、预算、用量、报告和待审批项。详情较长时返回 details_page，"
        + "按顺序读取 JSON 文本 content；长目标和验收标准也可能在分页中。用同一 task_id 和 next_cursor 续读。"
        + "如返回 task_details_changed，去掉游标重新查询。budget_exhausted 为 true 时，报告仅基于已完成的行动。"
        + "传报告中的 evidence_id 可读取原始证据文本，按 evidence.next_offset 续读。",
        {
            "task_id": {"type": "str", "required": True, "minLength": 1},
            "details_cursor": {
                "type": "str", "minLength": 18, "maxLength": 27,
                "pattern": "^[0-9a-f]{16}:[0-9]{1,10}$",
                "description": "上一页 details_page.next_cursor 的原值；首次查询省略。",
            },
            "evidence_id": {"type": "str", "minLength": 1, "maxLength": 64},
            "evidence_offset": {"type": "int", "minimum": 0},
        },
        GetTaskStatusTool,
    ))
    registry.register(_registration(
        "cancel_task", "取消当前对话所属的持久任务；不会重复执行已经在途的未知外部副作用。",
        {"task_id": {"type": "str", "required": True, "minLength": 1}},
        CancelTaskTool,
    ))
    registry.register(_registration(
        "send_task_input", "向等待用户信息的持久任务追加输入并唤醒它。",
        {
            "task_id": {"type": "str", "required": True, "minLength": 1},
            "content": {"type": "str", "required": True, "minLength": 1},
        },
        SendTaskInputTool,
    ))
    from aiive.tools.attachment_tools import register_attachment_tools
    from aiive.tools.operation_executor import GetToolOperationStatusTool
    from aiive.tools.registry import get_tool_registry

    register_attachment_tools(registry)
    registry.register(_registration(
        "get_tool_operation_status",
        "按 operation_id 查询当前对话的工具操作。仅 committed 返回确认结果。",
        {"operation_id": {"type": "str", "required": True, "minLength": 1, "maxLength": 36}},
        GetToolOperationStatusTool,
    ))
    builtin = get_tool_registry()
    search = builtin.get("web_search")
    assert search is not None
    registry.register(QuickWebSearchTool(ToolDefinition(
        safety=replace(search.safety, timeout_seconds=QUICK_WEB_SEARCH_TIMEOUT_SECONDS),
        description="即时搜索公开网页，最多返回 3 条带来源链接的外部证据。",
        parameters={"query": {"type": "str", "minLength": 1, "pattern": r"\S", "required": True}},
    )))
    for name in (
        "get_current_time", "schedule_reminder", "list_reminders", "cancel_reminder",
        "confirm_reminder", "snooze_reminder", "memory_search", "memory_timeline", "remember_or_update",
        "history_search", "forget", "forget_status",
    ):
        tool = builtin.get(name)
        if tool is not None:
            registry.register(tool)
    return registry
