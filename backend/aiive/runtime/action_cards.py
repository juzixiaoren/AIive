"""
运行时层 - 操作卡片数据模型。

定义 Agent 向前端发送的结构化操作卡片（ActionCard），用于展示工具执行结果、
提醒、通知、审批请求等各类运行时事件。
"""
from typing import Any, Literal

from pydantic import BaseModel

# 操作卡片类型：覆盖记忆、任务、通知、工具、审批、自进化等全部运行时卡片种类
ActionCardType = Literal[
    "memory_created", "memory_revised", "memory_superseded", "task_created",
    "notification", "tool_result", "delete_decision", "knowledge_result",
    "forget_result", "maintenance_report", "mcp_candidates", "mcp_sandbox_result",
    "selfdev_plan", "selfdev_run", "approval_required", "rhythm_summary", "attention_state",
    "reminder_alert", "tool_blocked", "tool_error",
]


class ActionCard(BaseModel):
    """前端操作卡片的结构化数据模型。

    每个卡片携带 trace_id 用于链路追踪，event_ids 用于关联底层事件，
    resource_refs 用于关联受影响的资源。
    """
    card_type: ActionCardType
    title: str
    summary: str = ""
    trace_id: str = ""
    event_ids: list[str] = []
    resource_refs: dict[str, str] = {}
    status: Literal["pending", "completed", "failed", "needs_review", "alerting", "blocked", "error"] = "completed"
    payload_preview: dict[str, Any] = {}
    # 提醒专用字段
    reminder_id: str = ""
    actions: list[dict[str, Any]] = []


class PendingOperation(BaseModel):
    """待处理操作的数据模型，用于审批流等需要用户确认的操作。"""
    operation_id: str
    operation_type: str
    status: str = "pending"
    resource_ref: str = ""
