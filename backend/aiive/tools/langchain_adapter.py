"""
LangChain 工具适配器：将 ToolRegistry 中的工具转换为 LangChain StructuredTool。
使得已注册的工具可以通过 llm.bind_tools() 供 LangChain Agent 使用，
同时保持执行路径仍经过 ToolRegistry.execute() 以确保一致性和安全性。

RunContext 通过 _make_handler 闭包捕获，经由 registry.execute() 注入 handler，
全程不暴露给 LLM tool schema。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from aiive.context.run_context import RunContext
from aiive.tools.registry import ToolRegistry
from aiive.tools.tool_validation import build_tool_args_model


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

            return json.dumps(
                {
                    "ok": False,
                    "error": err,
                    "error_type": result.get("error_type", "execution_failed"),
                    "execution_status": result.get("execution_status", "failed"),
                    "operation_id": result.get("operation_id", ""),
                },
                ensure_ascii=False,
            )
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
    all_regs = registry.list_all()

    visible = set(visible_tool_ids) if visible_tool_ids else None

    for reg_info in all_regs:
        cap_id = reg_info["capability_id"]
        if visible is not None and cap_id not in visible:
            continue

        reg = registry.get(cap_id)
        if reg is None:
            continue

        desc = reg.description
        safety = reg.safety
        if safety.writes_external_world:
            desc += " (side_effect: true)"
        if safety.risk_level in ("high", "critical"):
            desc += f" (risk: {safety.risk_level})"

        tool = StructuredTool.from_function(
            func=_make_handler(registry, cap_id, run_context),
            name=cap_id,
            description=desc,
            args_schema=build_tool_args_model(
                reg, include_injected_tool_call_id=True,
            ),
            return_direct=False,
        )
        tools.append(tool)

    return tools
