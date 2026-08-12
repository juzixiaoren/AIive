"""
MCP 能力 → ToolRegistry 注册桥。

职责：
- 把冒烟通过（state=active）的 MCP 能力的真实工具注册进 ToolRegistry，
  使 Agent 可以像内置工具一样调用（definition_source="remote_mcp"）。
- 服务重启后的恢复：restore_active_capabilities(registry) 从 DB 读取所有
  active 的 MCP 能力并重新注册。本模块导入时【不】自动执行任何注册；
  需要主线在 main.py 的 lifespan 中显式调用。

安全：
- 工具 handler 的返回值统一包一层 {"ok", "result"/"error", "untrusted": True}，
  下游据此把 MCP 输出当作不信任内容处理。
- 启动命令只从 Capability.definition["launch"]（installer 写入）拼装。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from aiive.mcp.runtime_client import (
    MCPToolResult,
    build_launch_spec,
    get_runtime_client,
)

logger = logging.getLogger(__name__)

# 注册进 ToolRegistry 的 MCP 工具的单次调用超时（秒）
MCP_TOOL_TIMEOUT_SECONDS = 60.0

# 工具名中暗示外部写副作用 / 删除的关键词（保守启发式）
_WRITE_HINTS = (
    "write", "create", "update", "move", "put", "post", "send", "insert",
    "set", "add", "fill", "click", "navigate", "execute", "apply",
)
_DELETE_HINTS = ("delete", "remove", "drop", "clear")
# `search` 本身并不代表联网：memory/search_nodes、filesystem/search_files 都是
# 典型的本地检索。这里只匹配能明确表达网络 I/O 的词，避免错误要求
# TaskScope.allow_network。
_NETWORK_HINTS = ("web", "fetch", "url", "http", "browser", "navigate")

_RISK_ORDER = ("low", "medium", "high", "critical")


def _sanitize_identifier(name: str) -> str:
    """把能力名转成工具 id 片段（@scope/server-xxx → server_xxx）。"""
    short = name.split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", short).strip("_").lower()
    return cleaned or "unnamed"


def mcp_tool_capability_id(capability_name: str, tool_name: str) -> str:
    """MCP 工具在 ToolRegistry 中的 capability_id（加前缀避免与内置冲突）。"""
    return f"mcp_{_sanitize_identifier(capability_name)}_{_sanitize_identifier(tool_name)}"


def _max_risk(a: str, b: str) -> str:
    """取两个风险等级中的较高者。"""
    ia = _RISK_ORDER.index(a) if a in _RISK_ORDER else 1
    ib = _RISK_ORDER.index(b) if b in _RISK_ORDER else 1
    return _RISK_ORDER[max(ia, ib)]


def _json_type_to_param_type(json_type: Any) -> str:
    """JSON Schema 类型 → registry parameters 的简单类型字符串。"""
    mapping = {
        "string": "str", "integer": "int", "number": "float",
        "boolean": "bool", "array": "list", "object": "dict",
    }
    if isinstance(json_type, list) and json_type:
        json_type = json_type[0]
    return mapping.get(str(json_type), "str")


def _input_schema_to_parameters(input_schema: dict[str, Any]) -> dict[str, Any]:
    """把 MCP inputSchema 转成 registry 的 parameters 声明格式。"""
    parameters: dict[str, Any] = {}
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return parameters
    required = set(input_schema.get("required", []) or [])
    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            prop_def = {}
        desc = str(prop_def.get("description", ""))
        if prop_name in required:
            desc = (desc + " (required)").strip()
        parameters[str(prop_name)] = {
            "type": _json_type_to_param_type(prop_def.get("type", "string")),
            "description": desc,
            "required": prop_name in required,
            **({"enum": prop_def["enum"]} if isinstance(prop_def.get("enum"), list) else {}),
            **({"default": prop_def["default"]} if "default" in prop_def else {}),
        }
    return parameters


def _make_tool_handler(db_capability_id: str, tool_name: str):
    """构造调用真实 MCP 运行时的工具 handler 闭包。

    返回值统一包一层 untrusted 标记；失败时不抛异常，如实返回 ok=False。
    """

    def _handler(**params: Any) -> dict[str, Any]:
        client = get_runtime_client()
        result: MCPToolResult = client.call_tool(
            db_capability_id, tool_name, params, timeout=MCP_TOOL_TIMEOUT_SECONDS,
        )
        return {
            "ok": result.ok,
            "result": result.result,
            "error": result.error,
            "untrusted": True,
        }

    return _handler


def register_capability_tools(
    registry: Any,
    capability_name: str,
    db_capability_id: str,
    tools: list[dict[str, Any]],
    *,
    base_risk_level: str = "medium",
    trust_level: str = "untrusted",
) -> list[str]:
    """把一个 MCP 能力的真实工具列表注册进 ToolRegistry。

    参数:
        registry: ToolRegistry 实例（只使用其公开 register API）
        capability_name: 能力名（如 @modelcontextprotocol/server-filesystem）
        db_capability_id: DB 中的能力标识（mcp:{name}），也是运行时会话键
        tools: 真实 tools/list 结果 [{"name", "description", "input_schema"}]
        base_risk_level: 能力级风险评估（planner verdict），工具级按副作用上调
        trust_level: 信任级别（discovery 的 source_trust 映射）

    返回:
        注册的 capability_id 列表
    """
    from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration

    registered: list[str] = []
    for tool in tools:
        tool_name = str(tool.get("name", "")).strip()
        if not tool_name:
            continue
        name_lower = tool_name.lower()
        can_delete = any(w in name_lower for w in _DELETE_HINTS)
        writes = can_delete or any(w in name_lower for w in _WRITE_HINTS)
        uses_network = any(w in name_lower for w in _NETWORK_HINTS)
        risk = base_risk_level
        if writes:
            risk = _max_risk(risk, "medium")
        if can_delete:
            risk = _max_risk(risk, "high")

        cap_id = mcp_tool_capability_id(capability_name, tool_name)
        safety = CapabilitySafetySchema(
            capability_id=cap_id,
            definition_source="remote_mcp",
            definition_trust_level=trust_level,
            risk_level=risk,
            requires_confirmation=risk in ("high", "critical"),
            writes_external_world=writes,
            can_access_secret=False,
            can_delete=can_delete,
            uses_network=uses_network,
            allowed_instruction_sources=["trusted_user_command"],
            tool_description_is_instruction=False,
            timeout_seconds=MCP_TOOL_TIMEOUT_SECONDS,
            effect_mode="non_repeatable_external" if writes else "db_transactional",
        )
        description = (
            f"[MCP:{capability_name}] {str(tool.get('description', '')).strip()}"
        ).strip()
        registry.register(ToolRegistration(
            safety=safety,
            handler=_make_tool_handler(db_capability_id, tool_name),
            description=description,
            parameters=_input_schema_to_parameters(
                tool.get("input_schema") or {},
            ),
            input_schema=tool.get("input_schema") or {},
        ))
        registered.append(cap_id)
    logger.info(
        "MCP 能力已注册进 ToolRegistry: %s tools=%d", capability_name, len(registered),
    )
    return registered


def activate_capability_tools(registry: Any, cap: Any) -> dict[str, Any]:
    """为一个 state=active 的 Capability（DB 行）完成运行时配置 + 注册。

    从 cap.definition 读取 launch 与冒烟缓存的 tools 列表；tools 缺失时
    现场做一次真实 tools/list。

    返回:
        {"ok", "registered": [...], "error"}
    """
    definition: dict[str, Any] = dict(cap.definition or {})
    launch = definition.get("launch")
    if not isinstance(launch, dict):
        return {"ok": False, "registered": [],
                "error": f"capability {cap.capability_id} has no launch config"}
    try:
        spec = build_launch_spec(launch)
    except ValueError as e:
        return {"ok": False, "registered": [], "error": str(e)}

    client = get_runtime_client()
    client.configure(cap.capability_id, spec)

    tools = definition.get("tools")
    if not isinstance(tools, list) or not tools:
        try:
            tools = client.list_tools(cap.capability_id)
        except Exception as e:
            return {"ok": False, "registered": [],
                    "error": f"tools/list failed: {e}"}
    registered = register_capability_tools(
        registry,
        capability_name=cap.name,
        db_capability_id=cap.capability_id,
        tools=tools,
        base_risk_level=str(definition.get("risk_verdict", "medium")),
        trust_level=str(definition.get("trust_level", "untrusted")),
    )
    return {"ok": True, "registered": registered, "error": None}


def restore_active_capabilities(registry: Any) -> dict[str, Any]:
    """服务重启后恢复：把 DB 中所有 active 的 MCP 能力重新注册进 registry。

    本函数不会在模块导入时自动执行；需要主线在 main.py 的 lifespan 中调用：

        from aiive.mcp.bootstrap import restore_active_capabilities
        from aiive.tools.registry import get_tool_registry
        restore_active_capabilities(get_tool_registry())

    返回:
        {"restored": 数量, "capabilities": [...], "failures": [{name, error}]}
    """
    from aiive.db.base import SessionLocal
    from aiive.db.models import Capability

    restored: list[str] = []
    failures: list[dict[str, str]] = []
    db = SessionLocal()
    try:
        caps = (
            db.query(Capability)
            .filter(
                Capability.state == "active",
                Capability.capability_id.like("mcp:%"),
            )
            .all()
        )
        for cap in caps:
            try:
                result = activate_capability_tools(registry, cap)
            except Exception as e:  # 单个能力失败不阻断其他能力
                result = {"ok": False, "error": str(e)}
            if result.get("ok"):
                restored.append(cap.capability_id)
            else:
                failures.append({
                    "capability_id": cap.capability_id,
                    "error": str(result.get("error", "unknown")),
                })
                logger.warning(
                    "MCP 能力恢复注册失败: %s error=%s",
                    cap.capability_id, result.get("error"),
                )
    finally:
        db.close()
    return {
        "restored": len(restored),
        "capabilities": restored,
        "failures": failures,
    }
