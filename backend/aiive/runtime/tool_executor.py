"""
运行时层 - 工具调用记录和操作卡片构建。

结果展示工具：从 ToolNode 返回的工具执行记录构建前端 action_cards。
不再包含自定义 <tool_call> XML 解析逻辑（已由 LangGraph ToolNode 替代）。
"""
import json as _json
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCallRecord:
    """一次已执行（或被阻止/出错）工具调用的记录。"""
    name: str
    params: dict[str, Any]
    result: dict[str, Any]
    status: str  # "completed" | "failed" | "blocked" | "duplicate" | "parse_error"
    trace_id: str
    reason: str = ""


def build_action_cards(records: list[ToolCallRecord]) -> list[dict[str, Any]]:
    """从工具记录构建前端展示用的 action_cards。

    特殊处理：
    - remind_alert：提取提醒操作用于前端按钮
    - schedule_reminder：展示已创建提醒卡片
    - blocked/parse_error/completed/failed：根据状态生成对应卡片类型

    Args:
        records: 工具调用记录列表

    Returns:
        action_cards 字典列表
    """
    cards: list[dict[str, Any]] = []
    for r in records:
        inner_raw = r.result.get("result", {})
        if isinstance(inner_raw, str):
            try:
                inner_raw = _json.loads(inner_raw)
            except (ValueError, TypeError):
                inner_raw = {}

        if r.name == "remind_alert" and r.status == "completed":
            cards.append({
                "card_type": "reminder_alert",
                "title": f"提醒: {inner_raw.get('content', '')}",
                "summary": inner_raw.get("content", ""),
                "trace_id": r.trace_id,
                "status": "alerting",
                "reminder_id": inner_raw.get("reminder_id", ""),
            })
        elif r.name == "schedule_reminder" and r.status == "completed" and r.result.get("ok"):
            cards.append({
                "card_type": "task_created",
                "title": f"已创建提醒: {inner_raw.get('content', '')}",
                "summary": inner_raw.get("content", ""),
                "trace_id": r.trace_id,
                "status": "completed",
                "task_id": inner_raw.get("task_id", ""),
                "reminder_id": inner_raw.get("event_id", ""),
            })
        elif r.status == "blocked":
            cards.append({
                "card_type": "tool_blocked",
                "title": f"已阻止: {r.name}",
                "summary": r.reason,
                "trace_id": r.trace_id,
                "status": "blocked",
            })
        elif r.status == "parse_error":
            cards.append({
                "card_type": "tool_error",
                "title": "工具调用解析失败",
                "summary": r.reason,
                "trace_id": r.trace_id,
                "status": "error",
            })
        elif r.status in ("completed", "failed"):
            cards.append({
                "card_type": "tool_result",
                "title": f"执行: {r.name}",
                "summary": _json.dumps(r.params, ensure_ascii=False)[:80],
                "trace_id": r.trace_id,
                "status": r.status,
            })
    return cards
