"""运行时工具消息字段规范化。"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def stable_tool_call_id(data: dict[str, Any]) -> str:
    """从新旧消息格式中提取稳定 tool_call_id。

    回退键必须对 tool_call / tool_result 两侧一致，否则历史重建配对断裂
    （旧实现用各自的 event_id 派生，两事件必然不同 → LLM API 400）。
    回退使用双方共享的 (turn_id, batch_index)；仍无法确定时返回空串，
    由 ContextAssembler._repair_tool_pairing 丢弃整对。
    """
    value = data.get("tool_call_id") or data.get("id")
    if value:
        return str(value)
    turn_id = str(data.get("turn_id", "") or "")
    batch_index = data.get("batch_index", None)
    if turn_id and batch_index is not None:
        return f"tc_{turn_id}_{batch_index}"
    logger.warning(
        "工具事件缺少 tool_call_id 且无 (turn_id, batch_index) 回退键，"
        "该事件将无法配对并被丢弃: event_id=%s", data.get("event_id"),
    )
    return ""


def normalize_tool_call(data: dict[str, Any]) -> dict[str, Any]:
    """将事件格式或 OpenAI tool_call 格式统一为 id/name/args。"""
    function = data.get("function")
    function_data = function if isinstance(function, dict) else {}
    name = str(
        function_data.get("name")
        or data.get("tool_name")
        or data.get("name")
        or ""
    )
    raw_args = function_data.get(
        "arguments",
        data.get("tool_params", data.get("params", data.get("args", {}))),
    )
    args = _normalize_args(raw_args)
    return {
        "id": stable_tool_call_id(data),
        "name": name,
        "args": args,
    }


def normalize_tool_result(data: dict[str, Any]) -> dict[str, Any]:
    """将新旧工具结果格式统一为 id/name/result。"""
    result = data.get("tool_result", data.get("result", {}))
    return {
        "id": stable_tool_call_id(data),
        "name": str(data.get("tool_name") or data.get("name") or ""),
        "result": result,
    }


def _normalize_args(raw_args: Any) -> dict[str, Any]:
    """安全解析工具参数；畸形字符串保留原文而不使历史重建失败。"""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"raw_arguments": raw_args}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if raw_args is None:
        return {}
    return {"value": raw_args}
