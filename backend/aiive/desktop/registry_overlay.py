"""为单个 Turn 构建包含在线 Desktop Node 本地能力的工具注册表快照。"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aiive.desktop.connection_manager import desktop_connection_manager
from aiive.desktop.node_service import desktop_node_service
from aiive.desktop.protocol import definition_for
from aiive.desktop.schemas import DesktopCapability
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    get_tool_registry,
)

logger = logging.getLogger(__name__)

def _json_type_to_parameter(value: Any) -> str:
    mapping = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }
    if isinstance(value, list):
        value = next((item for item in value if item != "null"), "string")
    return mapping.get(str(value), "str")


def _schema_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = set(schema.get("required", []) or [])
    result: dict[str, Any] = {}
    for name, raw in properties.items():
        definition = raw if isinstance(raw, dict) else {}
        result[str(name)] = {
            "type": _json_type_to_parameter(definition.get("type", "string")),
            "description": str(definition.get("description", "")),
            "required": name in required,
            **({"enum": definition["enum"]} if isinstance(definition.get("enum"), list) else {}),
            **({"default": definition["default"]} if "default" in definition else {}),
        }
    return result


def _proxy_handler(node_id: str, capability: DesktopCapability):
    def handler(**params: Any) -> Any:
        return desktop_connection_manager.dispatch_sync(
            node_id,
            capability.name,
            params,
            capability.timeout_seconds,
        )

    return handler


def build_registry_for_thread(
    db: Session, thread_id: str, target_node_id: str | None = None,
) -> ToolRegistry:
    """复制基础注册表，并叠加线程或 Task 显式指定的在线节点能力。"""
    registry = get_tool_registry().snapshot()
    try:
        node = desktop_node_service.resolve_task_node(db, thread_id, target_node_id)
    except Exception:
        logger.exception("解析 Desktop Node 失败，本轮不注入桌面工具: thread_id=%s", thread_id)
        return registry
    if node is None or not desktop_connection_manager.is_connected(node.id):
        return registry

    for raw_capability in node.capabilities or []:
        try:
            capability = DesktopCapability.model_validate(raw_capability)
        except Exception:
            logger.warning("忽略无效 Desktop capability: node_id=%s", node.id, exc_info=True)
            continue
        definition = definition_for(capability.name)
        if definition is None:
            logger.warning(
                "忽略服务端未定义的 Desktop capability: node_id=%s capability=%s",
                node.id, capability.name,
            )
            continue
        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema(
                capability_id=capability.name,
                definition_source="desktop_node",
                definition_trust_level="server_defined",
                risk_level=definition.risk_level,
                requires_confirmation=definition.requires_confirmation,
                writes_external_world=definition.writes_external_world,
                can_access_secret=definition.can_access_secret,
                can_delete=definition.can_delete,
                allowed_instruction_sources=["task_runtime"],
                # WebSocket dispatcher 先收敛为确定结果或 execution_unknown；外层
                # Registry 多留 5 秒，避免两个相同截止时间互相竞态。
                timeout_seconds=definition.timeout_seconds,
                effect_mode=definition.effect_mode,
            ),
            handler=_proxy_handler(node.id, capability),
            description=f"[桌面:{node.name}] {definition.description}",
            parameters=_schema_parameters(definition.input_schema or {}),
            input_schema=definition.input_schema or {},
            executor_kind="desktop_node",
            executor_node_id=node.id,
        ))
    return registry
