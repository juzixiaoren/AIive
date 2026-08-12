"""
运行时层 - 操作卡片数据模型。

定义 Agent 向前端发送的结构化操作卡片（ActionCard），用于展示工具执行结果、
提醒、通知、审批请求等各类运行时事件。
"""
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 操作卡片类型：覆盖记忆、任务、通知、工具、审批、自进化等全部运行时卡片种类
ActionCardType = Literal[
    "memory_created", "memory_revised", "memory_superseded", "task_created",
    "notification", "tool_result", "delete_decision", "knowledge_result",
    "forget_result", "maintenance_report", "mcp_candidates", "mcp_sandbox_result",
    "selfdev_plan", "selfdev_run", "approval_required", "rhythm_summary", "attention_state",
    "reminder_alert", "tool_blocked", "tool_error",
]
ActionCardStatus = Literal[
    "pending", "completed", "failed", "execution_unknown", "needs_review", "alerting", "blocked", "error",
]


class ActionCardAction(BaseModel):
    """操作卡片上可由用户触发的结构化动作。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    approval_id: str = ""
    label: str = ""


class ActionCard(BaseModel):
    """前端操作卡片的结构化数据模型。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    card_type: ActionCardType
    title: str
    summary: str = ""
    trace_id: str = Field(min_length=1)
    event_ids: list[str] = Field(default_factory=list)
    resource_refs: dict[str, str] = Field(default_factory=dict)
    status: ActionCardStatus = "completed"
    payload_preview: dict[str, Any] = Field(default_factory=dict)
    reminder_id: str = ""
    actions: list[ActionCardAction] = Field(default_factory=list)

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        """拒绝只包含空白字符的追踪标识。"""
        if not value.strip():
            raise ValueError("trace_id 不能为空")
        return value


class PendingOperation(BaseModel):
    """从真实审批或副作用 operation 状态投影出的待处理操作。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1)
    operation_type: str = Field(min_length=1)
    status: Literal["pending"] = "pending"
    trace_id: str = Field(min_length=1)
    resource_refs: dict[str, str] = Field(default_factory=dict)
    payload_preview: dict[str, Any] = Field(default_factory=dict)

    @field_validator("operation_id", "operation_type", "trace_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """拒绝待处理操作中的空白标识。"""
        if not value.strip():
            raise ValueError("待处理操作标识不能为空")
        return value


class ChatResponse(BaseModel):
    """同步聊天和 SSE 完成事件共用的响应契约。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    reply: str
    event_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    action_cards: list[ActionCard] = Field(default_factory=list)
    pending_operations: list[PendingOperation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    parse_errors: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
