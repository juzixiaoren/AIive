"""工具流事件关联与执行回执契约；不依赖图编排、数据库或模型客户端。"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamToolCallMatcher:
    """将 LangGraph 流式工具事件关联到模型生成的真实工具调用标识。"""

    pending: list[dict[str, Any]] = field(default_factory=list)
    run_ids: dict[str, str] = field(default_factory=dict)

    def register_batch(self, tool_calls: list[Any]) -> None:
        """登记一个 tools 节点即将执行的模型工具调用批次。"""
        self.pending.extend(
            {
                "tool_call_id": str(call.get("id", "") or ""),
                "name": str(call.get("name", "") or ""),
                "params": call.get("args", {}) if isinstance(call.get("args"), dict) else {},
            }
            for call in tool_calls
            if isinstance(call, dict)
        )

    def start(
        self,
        run_id: str,
        name: str,
        params: dict[str, Any],
        event_tool_call_id: str = "",
    ) -> str:
        """优先按事件调用标识匹配，缺失时按名称、参数和登记顺序回退。"""
        if event_tool_call_id:
            for index, call in enumerate(self.pending):
                if call["tool_call_id"] == event_tool_call_id:
                    self.pending.pop(index)
                    self.run_ids[run_id] = event_tool_call_id
                    return event_tool_call_id
        for index, call in enumerate(self.pending):
            if call["name"] == name and call["params"] == params:
                matched = self.pending.pop(index)
                self.run_ids[run_id] = matched["tool_call_id"]
                return matched["tool_call_id"]
        return ""

    def finish(self, run_id: str, fallback_id: str = "") -> str:
        """返回工具结束事件对应的真实工具调用标识。"""
        return self.run_ids.pop(run_id, "") or fallback_id


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolExecutionReceipt:
    """工具返回内容解析后的统一执行回执。"""

    content: str
    ok: bool
    status: str
    execution_status: str = ""
    operation_id: str = ""
    error_type: str = ""


def parse_tool_execution_receipt(content: str) -> ToolExecutionReceipt:
    """将 ToolMessage 内容解析为唯一的工具执行状态契约。"""
    is_error = content.startswith("Error:") if content else False
    execution_status = ""
    operation_id = ""
    error_type = ""
    if not is_error and content.startswith("{"):
        try:
            parsed = _json.loads(content)
            if isinstance(parsed, dict):
                execution_status = str(parsed.get("execution_status", "") or "")
                operation_id = str(parsed.get("operation_id", "") or "")
                error_type = str(parsed.get("error_type", "") or "")
                if not parsed.get("ok", True):
                    is_error = True
        except (ValueError, TypeError):
            pass
    status = (
        "execution_unknown"
        if execution_status in ("queued", "running", "execution_unknown")
        else ("failed" if is_error else "completed")
    )
    return ToolExecutionReceipt(
        content=content,
        ok=not is_error,
        status=status,
        execution_status=execution_status,
        operation_id=operation_id,
        error_type=error_type,
    )
