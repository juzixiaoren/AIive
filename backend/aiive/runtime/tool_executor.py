"""
运行时层 - 工具调用记录和操作卡片构建。

结果展示工具：从 ToolNode 返回的工具执行记录构建前端 action_cards。
不再包含自定义 <tool_call> XML 解析逻辑（已由 LangGraph ToolNode 替代）。
"""
import json as _json
from dataclasses import dataclass
from typing import Any

from aiive.runtime.action_cards import ActionCard, ActionCardAction, PendingOperation


@dataclass
class ToolCallRecord:
    """一次已执行（或被阻止/出错）工具调用的记录。"""
    name: str
    params: dict[str, Any]
    result: dict[str, Any]
    status: str  # "completed" | "failed" | "blocked" | "duplicate" | "parse_error"
    trace_id: str
    reason: str = ""


def build_action_cards(
    records: list[ToolCallRecord],
    pending_approvals: list[dict[str, Any]] | None = None,
) -> list[ActionCard]:
    """从真实工具记录构建并校验操作卡片。"""
    cards: list[ActionCard] = []
    pa_by_name: dict[str, dict[str, Any]] = {
        str(pa.get("name", "") or ""): pa for pa in (pending_approvals or [])
    }

    for record in records:
        inner_raw = record.result.get("result", {})
        if isinstance(inner_raw, str):
            try:
                inner_raw = _json.loads(inner_raw)
            except (ValueError, TypeError):
                inner_raw = {}
        if not isinstance(inner_raw, dict):
            inner_raw = {}

        common = {"trace_id": record.trace_id}
        if record.status == "pending_approval":
            approval = pa_by_name.get(record.name, {})
            approval_id = str(approval.get("approval_id", "") or "")
            tool_call_id = str(approval.get("id", "") or "")
            cards.append(ActionCard(
                card_type="approval_required",
                title=f"需要确认: {record.name}",
                summary=f"工具 {record.name} 需要你的确认后才能执行",
                status="needs_review",
                resource_refs={
                    key: value for key, value in {
                        "approval_id": approval_id,
                        "tool_call_id": tool_call_id,
                    }.items() if value
                },
                payload_preview={
                    "tool_name": record.name,
                    "tool_params": record.params,
                    "approval_id": approval_id,
                    "tool_call_id": tool_call_id,
                },
                actions=[
                    ActionCardAction(action="approve", approval_id=approval_id, label="确认执行"),
                    ActionCardAction(action="deny", approval_id=approval_id, label="拒绝"),
                ],
                **common,
            ))
        elif record.name == "remind_alert" and record.status == "completed":
            reminder_id = str(inner_raw.get("reminder_id", "") or "")
            cards.append(ActionCard(
                card_type="reminder_alert",
                title=f"提醒: {inner_raw.get('content', '')}",
                summary=str(inner_raw.get("content", "") or ""),
                status="alerting",
                reminder_id=reminder_id,
                event_ids=[reminder_id] if reminder_id else [],
                resource_refs={"reminder_id": reminder_id} if reminder_id else {},
                payload_preview={
                    "content": inner_raw.get("content", ""),
                    "status": inner_raw.get("status", "alerting"),
                },
                trace_id=record.trace_id,
            ))
        elif record.name == "schedule_reminder" and record.status == "completed" and record.result.get("ok"):
            task_id = str(inner_raw.get("task_id", "") or "")
            cards.append(ActionCard(
                card_type="task_created",
                title=f"已创建提醒: {inner_raw.get('content', '')}",
                summary=str(inner_raw.get("content", "") or ""),
                status="completed",
                resource_refs={"task_id": task_id} if task_id else {},
                payload_preview={
                    "content": inner_raw.get("content", ""),
                    "delay_minutes": inner_raw.get("delay_minutes"),
                },
                trace_id=record.trace_id,
            ))
        elif record.status == "blocked":
            cards.append(ActionCard(
                card_type="tool_blocked", title=f"已阻止: {record.name}",
                summary=record.reason, status="blocked",
                payload_preview={"tool_name": record.name, "reason": record.reason},
                **common,
            ))
        elif record.status == "parse_error":
            cards.append(ActionCard(
                card_type="tool_error", title="工具调用解析失败",
                summary=record.reason, status="error",
                payload_preview={"tool_name": record.name, "reason": record.reason},
                **common,
            ))
        elif record.status in ("completed", "failed", "execution_unknown"):
            operation_id = str(
                record.result.get("operation_id", "")
                or inner_raw.get("operation_id", "")
                or ""
            )
            cards.append(ActionCard(
                card_type="tool_result", title=f"执行: {record.name}",
                summary=(
                    "执行结果尚未确认，请勿重复提交"
                    if record.status == "execution_unknown"
                    else _json.dumps(record.params, ensure_ascii=False)[:80]
                ),
                status=record.status,
                resource_refs={"operation_id": operation_id} if operation_id else {},
                payload_preview={
                    "tool_name": record.name,
                    "tool_params": record.params,
                    "result": inner_raw,
                    "operation_id": operation_id,
                },
                **common,
            ))
    return cards


def build_pending_operations(
    records: list[ToolCallRecord],
    pending_approvals: list[dict[str, Any]] | None = None,
) -> list[PendingOperation]:
    """从真实待审批记录构建机器可读的待处理操作。"""
    pa_by_name = {
        str(pa.get("name", "") or ""): pa for pa in (pending_approvals or [])
    }
    operations: list[PendingOperation] = []
    for record in records:
        if record.status == "execution_unknown":
            inner = record.result.get("result", {})
            if isinstance(inner, str):
                try:
                    inner = _json.loads(inner)
                except (ValueError, TypeError):
                    inner = {}
            operation_id = str(
                record.result.get("operation_id", "")
                or (inner.get("operation_id", "") if isinstance(inner, dict) else "")
                or ""
            )
            if operation_id:
                operations.append(PendingOperation(
                    operation_id=operation_id,
                    operation_type=f"tool_operation:{record.name}",
                    trace_id=record.trace_id,
                    resource_refs={"operation_id": operation_id},
                    payload_preview={"tool_name": record.name, "tool_params": record.params},
                ))
            continue
        if record.status != "pending_approval":
            continue
        approval = pa_by_name.get(record.name, {})
        approval_id = str(approval.get("approval_id", "") or "")
        tool_call_id = str(approval.get("id", "") or "")
        if not approval_id:
            continue
        operations.append(PendingOperation(
            operation_id=approval_id,
            operation_type=f"tool_approval:{record.name}",
            trace_id=record.trace_id,
            resource_refs={
                key: value for key, value in {
                    "approval_id": approval_id,
                    "tool_call_id": tool_call_id,
                }.items() if value
            },
            payload_preview={"tool_name": record.name, "tool_params": record.params},
        ))
    return operations
