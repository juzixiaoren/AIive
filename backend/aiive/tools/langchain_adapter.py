"""
LangChain 工具适配器：将 ToolRegistry 中的工具转换为 LangChain StructuredTool。
使得已注册的工具可以通过 llm.bind_tools() 供 LangChain Agent 使用，
同时保持执行路径仍经过 ToolRegistry.execute() 以确保一致性和安全性。

RunContext 通过 _make_handler 闭包捕获，经由 registry.execute() 注入 handler，
全程不暴露给 LLM tool schema。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import Field

from aiive.context.run_context import RunContext
from aiive.tools.registry import ToolRegistry


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

    def handler(**params: object) -> str:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("[TRACE:langchain] CALL tool=%s params=%s", capability_id, params)
        result = registry.execute(capability_id, params, "trusted_user_command", run_context)
        _log.info("[TRACE:langchain] RESULT tool=%s ok=%s result_type=%s", capability_id, result.get("ok"), type(result).__name__)
        if not result.get("ok"):
            err = result.get("error", "Any error")
            if result.get("approval_required"):
                err = f"Approval required for {capability_id}"
            import json

            return json.dumps(
                {"ok": False, "error": err, "error_type": result.get("error_type", "execution_failed")},
                ensure_ascii=False,
            )
        inner = result.get("result", result)
        if isinstance(inner, dict):
            import json

            return json.dumps(inner, ensure_ascii=False)
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

        params = reg.parameters or {}
        args_schema: dict[str, Any] = {}
        for pname, pdef in params.items():
            # 支持两种写法：纯类型字符串（"str"）或带描述的字典（{"type": "str", "description": "..."}）
            if isinstance(pdef, dict):
                ptype = pdef.get("type", "str")
                pdesc = pdef.get("description", "")
            else:
                ptype = pdef
                pdesc = ""
            args_schema[pname] = (_type_str_to_python(ptype), pdesc)

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
            args_schema=_build_empty_args_schema(cap_id) if not args_schema else _build_args_schema(cap_id, args_schema),
            return_direct=False,
        )
        tools.append(tool)

    return tools


def _type_str_to_python(type_str: str) -> type:
    """将参数类型字符串映射为 Python 类型。

    参数:
        type_str: 类型字符串（"str"、"int"、"float"、"bool"、"list"、"dict"）

    返回:
        对应的 Python type
    """
    mapping = {
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "list": list,
        "dict": dict,
    }
    return mapping.get(type_str, str)


def _build_empty_args_schema(name: str) -> type:
    """为无参数工具创建一个空的 Pydantic 模型，明确告知 LLM 无需传参。"""
    from pydantic import BaseModel, create_model
    return create_model(f"{name}_args", __base__=BaseModel)


def _build_args_schema(name: str, fields: dict[str, tuple[type, str]]) -> type:
    """为工具参数构建 Pydantic 模型（用于 LangChain args_schema）。

    参数:
        name: 模型名称前缀
        fields: {字段名: (Python 类型, 字段描述)} 的字典；字段描述会出现在工具 schema 中，
                是约束 LLM 传参的主要手段（尤其对 content 这类需要语义约束的参数）。

    返回:
        动态创建的 Pydantic BaseModel 子类

    注意:
        对 int 字段启用 strict 模式，拒绝 float → int 的隐式截断（如 0.5 → 0）。
    """
    from pydantic import create_model

    field_defs: dict[str, Any] = {}
    for fname, (ftype, fdesc) in fields.items():
        if ftype is int:
            field_defs[fname] = (int, Field(default=None, strict=True, description=fdesc))
        elif ftype is float:
            field_defs[fname] = (float, Field(default=None, description=fdesc))
        elif ftype is str:
            field_defs[fname] = (str, Field(default="", description=fdesc))
        else:
            field_defs[fname] = (ftype, Field(default=None, description=fdesc))

    return create_model(f"{name}_args", **field_defs)
