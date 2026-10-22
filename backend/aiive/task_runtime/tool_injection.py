"""Worker 工具注入策略：常驻核心能力 + 按需发现，不注入全量 Schema。"""
from __future__ import annotations

import json
from typing import Any

from aiive.tools.registry import ToolRegistry

# L0 常驻入口：长期记忆读取、基础搜索、Bash（桌面节点在线时才有该能力）、
# 任务管理。常驻只代表入口始终可见，不代表执行授权——网络、路径、密钥、
# 审批与策略仍由 CapabilityBroker / TaskPolicyEngine 判定。
WORKER_RESIDENT_CAPABILITIES: tuple[str, ...] = (
    "memory_search",
    "memory_timeline",
    "memory_event_log",
    "web_search",
    "search_knowledge",
    "desktop_exec",
    "list_tasks",
    "cancel_task",
)

# 目录规模较小时直接注入完整 Schema（少一次模型调用）；超过阈值改用按需发现。
FULL_INJECTION_MAX_TOOLS = 12
FULL_INJECTION_MAX_CHARS = 24_000
MAX_SEARCH_RESULTS = 5
MAX_SEARCH_IDS = 10
MAX_DISCOVERY_QUERIES = 3
MAX_LOADED_DEFINITIONS = 6
MAX_QUERY_CHARS = 200

STATE_KEY = "tool_discovery"


def resident_definitions(
    registry: ToolRegistry,
    capability_ids: tuple[str, ...] = WORKER_RESIDENT_CAPABILITIES,
) -> list[dict[str, Any]]:
    """从给定（授权）视图中取出实际存在的常驻能力完整定义。"""
    definitions: list[dict[str, Any]] = []
    for capability_id in capability_ids:
        definition = registry.definition(capability_id)
        if definition is not None:
            definitions.append(definition)
    return definitions


def discovery_state(task_state: dict[str, Any] | None) -> dict[str, Any]:
    """读取并规范化工具发现状态；结构损坏时退回空状态。"""
    raw = (task_state or {}).get(STATE_KEY)
    raw = raw if isinstance(raw, dict) else {}
    queries = raw.get("queries")
    loaded = raw.get("loaded")
    return {
        "queries": [item for item in queries if isinstance(item, dict)] if isinstance(queries, list) else [],
        "loaded": [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else [],
    }


def record_search(
    task_state: dict[str, Any] | None, query: str, capability_ids: list[str],
) -> dict[str, Any]:
    """记录一次目录检索结果（只存 id，Schema 每次重新解析，避免陈旧缓存）。"""
    state = discovery_state(task_state)
    entry = {
        "query": query[:MAX_QUERY_CHARS],
        "capability_ids": list(dict.fromkeys(capability_ids))[:MAX_SEARCH_IDS],
    }
    queries = [item for item in state["queries"] if item.get("query") != entry["query"]]
    queries.append(entry)
    state["queries"] = queries[-MAX_DISCOVERY_QUERIES:]
    return state


def record_definition(
    task_state: dict[str, Any] | None, capability_id: str, descriptor_hash: str,
) -> dict[str, Any]:
    """记录已加载定义，只保存 id 与指纹；Schema 在注入时按当前注册表重建。"""
    state = discovery_state(task_state)
    entry = {"capability_id": capability_id, "descriptor_hash": descriptor_hash}
    loaded = [item for item in state["loaded"] if item.get("capability_id") != capability_id]
    loaded.append(entry)
    state["loaded"] = loaded[-MAX_LOADED_DEFINITIONS:]
    return state


def merge_state(task_state: dict[str, Any] | None, discovery: dict[str, Any]) -> dict[str, Any]:
    """把发现状态写回 task_state，保留其他键不变。"""
    merged = dict(task_state or {})
    merged[STATE_KEY] = discovery
    return merged


def build_tool_view(
    execution: ToolRegistry,
    capabilities: list[dict[str, Any]],
    discovery: dict[str, Any],
    *,
    discovery_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """构造注入给模型的工具视图。

    execution：当前任务可执行的能力视图（restricted 时为授权白名单）。
    discovery：完整可用能力视图（内置 + 在线桌面 + MCP），用于解析检索/定义结果，
    因此"看得到"与"能执行"分开表达：条目附带 in_scope 标记。
    """
    catalog = execution.catalog()
    executable_ids = frozenset(entry["capability_id"] for entry in catalog)
    mode = _injection_mode(execution, catalog)
    resident = [
        {**item, "in_scope": str(item.get("capability_id")) in executable_ids}
        for item in capabilities
    ]
    resident_ids = {str(item.get("capability_id")) for item in resident}
    available: list[dict[str, Any]] = []
    if mode == "full":
        for entry in catalog:
            if entry["capability_id"] in resident_ids:
                continue
            definition = execution.definition(entry["capability_id"])
            if definition is not None:
                available.append(definition)
    discovery_registry = discovery_registry or execution
    discovery_catalog = {entry["capability_id"]: entry for entry in discovery_registry.catalog()}
    return {
        "mode": mode,
        "resident": resident,
        "available": available,
        "discovered": _resolve_queries(discovery, discovery_catalog, executable_ids),
        "loaded": _resolve_loaded(discovery, discovery_registry, executable_ids),
    }


def _injection_mode(registry: ToolRegistry, catalog: list[dict[str, Any]]) -> str:
    """小目录直接全量注入；超出工具数或字符预算后转为按需发现。"""
    if len(catalog) > FULL_INJECTION_MAX_TOOLS:
        return "discovery"
    total = 0
    for entry in catalog:
        definition = registry.definition(entry["capability_id"])
        if definition is None:
            continue
        total += len(json.dumps(definition, ensure_ascii=False, default=str))
        if total > FULL_INJECTION_MAX_CHARS:
            return "discovery"
    return "full"


def _resolve_queries(
    discovery: dict[str, Any],
    catalog_by_id: dict[str, dict[str, Any]],
    executable_ids: frozenset[str],
) -> list[dict[str, Any]]:
    """把历史检索结果对账到当前目录，已卸载的工具显式列为 removed。"""
    resolved: list[dict[str, Any]] = []
    for item in discovery.get("queries") or []:
        results: list[dict[str, Any]] = []
        removed: list[str] = []
        for capability_id in item.get("capability_ids") or []:
            entry = catalog_by_id.get(str(capability_id))
            if entry is None:
                removed.append(str(capability_id))
            else:
                results.append({**entry, "in_scope": str(capability_id) in executable_ids})
        resolved.append({
            "query": item.get("query", ""),
            "results": results,
            "removed": removed,
        })
    return resolved


def _resolve_loaded(
    discovery: dict[str, Any],
    registry: ToolRegistry,
    executable_ids: frozenset[str],
) -> list[dict[str, Any]]:
    """按当前注册表重建已加载定义；指纹不一致时不返回旧 Schema。"""
    resolved: list[dict[str, Any]] = []
    for item in discovery.get("loaded") or []:
        capability_id = str(item.get("capability_id") or "")
        expected = str(item.get("descriptor_hash") or "")
        definition = registry.definition(capability_id) if capability_id else None
        if definition is None:
            resolved.append({"capability_id": capability_id, "status": "unavailable"})
            continue
        if definition["descriptor_hash"] != expected:
            resolved.append({
                "capability_id": capability_id,
                "status": "definition_changed",
                "hint": "定义已变化，请重新 describe_capability 获取最新 Schema",
            })
            continue
        resolved.append({
            **definition,
            "in_scope": capability_id in executable_ids,
            "status": "ready",
        })
    return resolved
