"""
LangChain 工具适配器：将 ToolRegistry 中的工具转换为 LangChain StructuredTool。
使得已注册的工具可以通过 llm.bind_tools() 供 LangChain Agent 使用，
同时保持执行路径仍经过 ToolRegistry.execute() 以确保一致性和安全性。

RunContext 通过 _make_handler 闭包捕获，经由 registry.execute() 注入 handler，
全程不暴露给 LLM tool schema。
"""

from __future__ import annotations

from typing import Any, override

from langchain_core.tools import StructuredTool

from aiive.context.run_context import RunContext
from aiive.tools.registry import ToolRegistry
from aiive.core.function_calling import function_definition


class CanonicalStructuredTool(StructuredTool):
    @override
    def _parse_input(self, tool_input: str | dict[str, Any], tool_call_id: str | None) -> str | dict[str, Any]:
        parsed = super()._parse_input(tool_input, tool_call_id)
        if isinstance(parsed, dict) and tool_call_id:
            # Transport identity stays outside the business contract. Registry owns
            # strict validation; no competing Pydantic model or coercion is used.
            return {**parsed, "tool_call_id": tool_call_id}
        return parsed


def _make_handler(registry: ToolRegistry, capability_id: str, run_context: RunContext | None):
    """创建包装 ToolRegistry.execute() 的可调用对象。

    闭包捕获 run_context，由 registry.execute() 注入 handler 的 ctx 参数，
    不与 LLM 交互，不出现于 tool schema。

    Args:
        registry: ToolRegistry 实例
        capability_id: 工具能力标识符
        run_context: RunContext 对象，由 build_langchain_tools 传入

    Returns:
        一个可调用函数，签名为 (**params) -> str
    """

    def handler(tool_call_id: str = "", **params: object) -> str:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("[TRACE:langchain] CALL tool=%s params=%s", capability_id, params)
        result = registry.execute(
            capability_id, params, "trusted_user_command", run_context,
            tool_call_id=tool_call_id,
        )
        _log.info("[TRACE:langchain] RESULT tool=%s ok=%s result_type=%s", capability_id, result.get("ok"), type(result).__name__)
        if not result.get("ok"):
            err = result.get("error", "Any error")
            import json

            # 失败时把业务 details 平铺回内容（exit_code / stdout / path 等），
            # 模型看到的字段与改造前一致；执行状态字段仍然以封套为准。
            details = result.get("details")
            payload: dict[str, object] = dict(details) if isinstance(details, dict) else {}
            payload.update({
                "ok": False,
                "error": err,
                "error_type": result.get("error_type", "execution_failed"),
                "execution_status": result.get("execution_status", "failed"),
                "outcome_known": result.get("outcome_known", True),
                "side_effect_started": result.get("side_effect_started", False),
                "operation_id": result.get("operation_id", ""),
                "retryable": result.get("retryable", False),
                "issues": result.get("issues", []),
            })
            # default=str：业务 details 里可能含 Path/datetime，不能让序列化
            # 异常把一次明确的失败变成调用崩溃。
            return json.dumps(payload, ensure_ascii=False, default=str)
        inner = result.get("result", result)
        if isinstance(inner, dict):
            import json

            payload = dict(inner)
            if result.get("operation_id"):
                payload["operation_id"] = result["operation_id"]
                payload["execution_status"] = result.get("execution_status", "committed")
            return json.dumps(payload, ensure_ascii=False)
        return str(inner)

    return handler


def build_langchain_tools(
    registry: ToolRegistry,
    run_context: RunContext | None = None,
    visible_tool_ids: list[str] | None = None,
) -> list[StructuredTool]:
    """将 ToolRegistry 中的所有（或过滤后的）工具转换为 LangChain StructuredTool 列表。

    run_context 通过闭包捕获注入，thread_id/trace_id 不进入 LLM schema。

    Args:
        registry: ToolRegistry 单例
        run_context: RunContext 对象（包含 thread_id、trace_id 等）
        visible_tool_ids: 可选的要包含的工具 ID 列表，为 None 则包含全部

    Returns:
        StructuredTool 列表，可直接用于 llm.bind_tools()
    """
    tools: list[StructuredTool] = []
    visible = set(visible_tool_ids) if visible_tool_ids else None

    for reg in registry.registrations():
        cap_id = reg.safety.capability_id
        if visible is not None and cap_id not in visible:
            continue

        definition = function_definition(reg)
        tool = CanonicalStructuredTool.from_function(
            func=_make_handler(registry, cap_id, run_context),
            name=cap_id,
            description=definition["description"],
            args_schema=definition["parameters"],
            return_direct=False,
        )
        tools.append(tool)

    return tools
