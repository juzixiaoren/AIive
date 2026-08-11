"""Main Agent 唯一可见的 Persistent Task 元工具。"""
from __future__ import annotations

from typing import Any

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.router import normalize_task_brief
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry


def _require_context(ctx: RunContext | None) -> RunContext:
    if ctx is None or not ctx.thread_id:
        raise ValueError("task_meta_tool_requires_run_context")
    return ctx


def _delegate_task(
    goal: str,
    task_type: str = "general",
    title: str = "",
    acceptance_criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    allowed_roots: list[str] | None = None,
    allowed_capabilities: list[str] | None = None,
    isolation_mode: str = "shared",
    deterministic_action: dict[str, Any] | None = None,
    target_node_id: str = "",
    budgets: dict[str, Any] | None = None,
    ctx: RunContext | None = None,
) -> dict[str, Any]:
    run_ctx = _require_context(ctx)
    raw_brief = {
        "acceptance_criteria": acceptance_criteria or [],
        "constraints": constraints or [],
        "scope": {
            "allowed_roots": allowed_roots or [],
            "allowed_capabilities": allowed_capabilities or [],
            "isolation_mode": isolation_mode,
        },
        "initial_input": {
            **({"deterministic_action": deterministic_action} if deterministic_action else {}),
        },
    }
    brief = normalize_task_brief(task_type, raw_brief)
    db = SessionLocal()
    try:
        task = TaskRepository(db).create_task(
            thread_id=run_ctx.thread_id,
            source_turn_record_id=run_ctx.turn_record_id or None,
            goal=goal,
            title=title or None,
            task_type=task_type,
            task_brief=brief,
            budgets=budgets,
            target_node_id=target_node_id or None,
        )
        db.commit()
        return {
            "task_id": task.id,
            "status": task.status,
            "title": task.title,
            "summary": "任务已进入持久执行队列；后续状态、审批和产物可在任务中心查看。",
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _get_task_status(task_id: str, ctx: RunContext | None = None) -> dict[str, Any]:
    run_ctx = _require_context(ctx)
    db = SessionLocal()
    try:
        repo = TaskRepository(db)
        task = repo.get_task(task_id)
        if task is None or task.thread_id != run_ctx.thread_id:
            raise ValueError("task_not_found")
        detail = repo.detail(task, include_events=False)
        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "usage": task.usage or {},
            "report": task.report,
            "pending_approvals": [item for item in detail["approvals"] if item["status"] == "pending"],
            "artifact_count": len(detail["artifacts"]),
        }
    finally:
        db.close()


def _cancel_task(task_id: str, ctx: RunContext | None = None) -> dict[str, Any]:
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


def _send_task_input(task_id: str, content: str, ctx: RunContext | None = None) -> dict[str, Any]:
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


def _registration(capability_id: str, description: str, parameters: dict[str, Any], handler) -> ToolRegistration:
    return ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id=capability_id,
            definition_source="task_runtime",
            definition_trust_level="trusted_core",
            risk_level="low",
            allowed_instruction_sources=["trusted_user_command"],
            effect_mode="db_transactional",
        ),
        handler=handler,
        description=description,
        parameters=parameters,
    )


def build_main_agent_registry() -> ToolRegistry:
    """返回 Main Agent 固定能力面；不包含 Desktop、文件或 selfdev 执行工具。"""
    registry = ToolRegistry()
    registry.register(_registration(
        "delegate_task",
        "创建可跨会话、审批和重启持续执行的任务。复杂或需要工具的工作必须委托为任务。",
        {
            "goal": {"type": "str", "required": True, "minLength": 1},
            "task_type": {"type": "str", "default": "general"},
            "title": {"type": "str"},
            "acceptance_criteria": {"type": "list"},
            "constraints": {"type": "list"},
            "allowed_roots": {"type": "list", "description": "任务允许访问的绝对路径根目录"},
            "allowed_capabilities": {"type": "list", "description": "可选；不填则使用任务类型最小 profile"},
            "isolation_mode": {
                "type": "str", "enum": ["shared", "git_worktree"], "default": "shared",
                "description": "代码任务可显式要求独立 Git worktree",
            },
            "deterministic_action": {
                "type": "dict",
                "description": "可选；目标已完全确定时直接给出 capability_id/arguments/preconditions/effects，仍经过 Broker 与审批",
            },
            "target_node_id": {"type": "str"},
            "budgets": {"type": "dict"},
        },
        _delegate_task,
    ))
    registry.register(_registration(
        "get_task_status", "查询当前对话所属持久任务的高层状态和报告。",
        {"task_id": {"type": "str", "required": True, "minLength": 1}},
        _get_task_status,
    ))
    registry.register(_registration(
        "cancel_task", "取消当前对话所属的持久任务；不会重复执行已经在途的未知外部副作用。",
        {"task_id": {"type": "str", "required": True, "minLength": 1}},
        _cancel_task,
    ))
    registry.register(_registration(
        "send_task_input", "向等待用户信息的持久任务追加输入并唤醒它。",
        {
            "task_id": {"type": "str", "required": True, "minLength": 1},
            "content": {"type": "str", "required": True, "minLength": 1},
        },
        _send_task_input,
    ))
    return registry
