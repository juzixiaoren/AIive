"""
LangChain 工具适配器：将 ToolRegistry 中的工具转换为 LangChain StructuredTool。
使得已注册的工具可以通过 llm.bind_tools() 供 LangChain Agent 使用，
同时保持执行路径仍经过 ToolRegistry.execute() 以确保一致性和安全性。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from aiive.tools.builtin_tools import set_thread_context
from aiive.tools.registry import ToolRegistry


def _make_handler(registry: ToolRegistry, capability_id: str):
    """创建包装 ToolRegistry.execute() 的可调用对象。

    参数:
        registry: ToolRegistry 实例
        capability_id: 工具能力标识符

    返回:
        一个可调用函数，签名为 (**params) -> str
    """

    def handler(**params: Any) -> str:
        result = registry.execute(capability_id, params, "trusted_user_command")
        if not result.get("ok"):
            err = result.get("error", "Unknown error")
            if result.get("approval_required"):
                err = f"Approval required for {capability_id}"
            import json

            return json.dumps(
                {"ok": False, "error": err, "error_type": result.get("error_type", "execution_failed")},
                ensure_ascii=False,
            )
        # 提取内部结果并转为字符串
        inner = result.get("result", result)
        if isinstance(inner, dict):
            import json

            return json.dumps(inner, ensure_ascii=False)
        return str(inner)

    return handler


def build_langchain_tools(
    registry: ToolRegistry,
    thread_id: str | None = None,
    visible_tool_ids: list[str] | None = None,
) -> list[StructuredTool]:
    """将 ToolRegistry 中的所有（或过滤后的）工具转换为 LangChain StructuredTool 列表。

    转换过程：
    1. 设置线程上下文（用于工具执行时的 thread 关联）
    2. 按 visible_tool_ids 过滤（可选）
    3. 将参数定义从 {name: type_str} 转换为 args_schema 格式
    4. 在描述中附加元数据提示（副作用、风险等级）

    参数:
        registry: ToolRegistry 单例
        thread_id: 当前线程 ID，用于设置线程上下文
        visible_tool_ids: 可选的要包含的工具 ID 列表，为 None 则包含全部

    返回:
        StructuredTool 列表，可直接用于 llm.bind_tools()
    """
    if thread_id:
        set_thread_context(thread_id)

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

        # 将参数定义从 {name: type_str} 转换为 args_schema 格式
        params = reg.parameters or {}
        args_schema = {}
        for pname, ptype in params.items():
            args_schema[pname] = _type_str_to_python(ptype)

        # 在描述中附加元数据提示，供 LLM 理解工具的副作用和风险
        desc = reg.description
        safety = reg.safety
        if safety.writes_external_world:
            desc += " (side_effect: true)"
        if safety.risk_level in ("high", "critical"):
            desc += f" (risk: {safety.risk_level})"

        tool = StructuredTool.from_function(
            func=_make_handler(registry, cap_id),
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


def _build_args_schema(name: str, fields: dict[str, type]) -> type:
    """为工具参数构建 Pydantic 模型（用于 LangChain args_schema）。

    参数:
        name: 模型名称前缀
        fields: {字段名: Python 类型} 的字典

    返回:
        动态创建的 Pydantic BaseModel 子类

    注意:
        对 int 字段启用 strict 模式，拒绝 float → int 的隐式截断（如 0.5 → 0）。
    """
    from pydantic import BaseModel, ConfigDict, Field, create_model

    field_defs: dict[str, Any] = {}
    for fname, ftype in fields.items():
        if ftype is int:
            field_defs[fname] = (int, Field(default=None, strict=True))
        elif ftype is float:
            field_defs[fname] = (float, Field(default=None))
        elif ftype is str:
            field_defs[fname] = (str, Field(default=""))
        else:
            field_defs[fname] = (ftype, Field(default=None))

    return create_model(f"{name}_args", **field_defs)
