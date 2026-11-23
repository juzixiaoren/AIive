"""Worker 工具注入策略：常驻核心能力 + 按需发现，不注入全量 Schema。"""
from __future__ import annotations

import json
import unicodedata
from typing import Any

from aiive.tool_discovery import DEFAULT_LIMIT, MAX_LIMIT, MAX_QUERY_CHARS
from aiive.tools.registry import ToolRegistry
from aiive.task_runtime.router import SELFDEV_CAPABILITIES, SELFDEV_TASK_TYPES

# L0 常驻入口：长期记忆读取、基础搜索、Bash（桌面节点在线时才有该能力）、
# 任务管理。常驻只代表入口始终可见，不代表执行授权——网络、路径、密钥、
# 审批与策略仍由 CapabilityBroker / TaskPolicyEngine 判定。
WORKER_RESIDENT_CAPABILITIES: tuple[str, ...] = (
    "list_thread_attachments",
    "read_attachment",
    "record_attachment_progress",
    "list_task_files",
    "read_task_evidence",
    "read_task_artifact",
    "memory_search",
    "memory_timeline",
    "memory_event_log",
    "web_search",
    "search_knowledge",
    "read_knowledge_chunk",
    "run_shell",
    "desktop_exec",
    "list_reminders",
    "cancel_reminder",
)

# 目录规模较小时直接注入完整 Schema（少一次模型调用）；超过阈值改用按需发现。
FULL_INJECTION_MAX_TOOLS = 12
FULL_INJECTION_MAX_CHARS = 24_000
MAX_DISCOVERY_QUERIES = 3
MAX_LOADED_DEFINITIONS = 6

STATE_KEY = "tool_discovery"


def worker_resident_ids(task_type: str) -> tuple[str, ...]:
    if task_type in SELFDEV_TASK_TYPES:
        return (*SELFDEV_CAPABILITIES, "read_text_file", *WORKER_RESIDENT_CAPABILITIES)
    return WORKER_RESIDENT_CAPABILITIES


def normalize_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())[:MAX_QUERY_CHARS]


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
    *, namespace_ids: list[str] | None = None, matched_namespace_ids: list[str] | None = None,
    loaded: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """记录一次目录检索结果（只存 id，Schema 每次重新解析，避免陈旧缓存）。"""
    state = discovery_state(task_state)
    entry = {
        "query": normalize_query(query),
        "capability_ids": list(dict.fromkeys(capability_ids))[:MAX_LIMIT],
        "namespace_ids": sorted(set(namespace_ids or []))[:MAX_LIMIT],
        "matched_namespace_ids": list(dict.fromkeys(matched_namespace_ids or []))[:MAX_LIMIT],
    }
    queries = [
        item for item in state["queries"]
        if (item.get("query"), item.get("namespace_ids") or []) != (entry["query"], entry["namespace_ids"])
    ]
    queries.append(entry)
    state["queries"] = queries[-MAX_DISCOVERY_QUERIES:]
    if loaded:
        _update_loaded(state, loaded)
    return state


def record_definition(
    task_state: dict[str, Any] | None, capability_id: str, descriptor_hash: str,
) -> dict[str, Any]:
    """记录已加载定义，只保存 id 与指纹；Schema 在注入时按当前注册表重建。"""
    state = discovery_state(task_state)
    _update_loaded(state, [{"capability_id": capability_id, "descriptor_hash": descriptor_hash}])
    return state


def _update_loaded(state: dict[str, Any], entries: list[dict[str, str]]) -> None:
    updated_ids = {item["capability_id"] for item in entries}
    loaded = [item for item in state["loaded"] if item.get("capability_id") not in updated_ids]
    state["loaded"] = (loaded + entries)[-MAX_LOADED_DEFINITIONS:]


def merge_state(task_state: dict[str, Any] | None, discovery: dict[str, Any]) -> dict[str, Any]:
    """把发现状态写回 task_state，保留其他键不变。"""
    merged = dict(task_state or {})
    merged[STATE_KEY] = discovery
    return merged


def _definition_size(definition: dict[str, Any]) -> int:
    return len(json.dumps({
        "capability_id": definition["capability_id"],
        "description": definition.get("description", ""),
        "input_schema": definition["input_schema"],
    }, ensure_ascii=False))


def select_search_definitions(
    execution: ToolRegistry, candidates: list[dict[str, Any]],
    *, resident_ids: tuple[str, ...] = WORKER_RESIDENT_CAPABILITIES,
) -> list[dict[str, str]]:
    """按检索顺序选择能与常驻定义一同绑定的 Schema 引用。"""
    remaining = FULL_INJECTION_MAX_CHARS
    bound_resident = set()
    for definition in resident_definitions(execution, resident_ids):
        size = _definition_size(definition)
        if size <= remaining:
            remaining -= size
            bound_resident.add(definition["capability_id"])
    selected = []
    for candidate in candidates:
        if candidate.get("in_scope") is not True:
            continue
        capability_id = candidate["capability_id"]
        definition = execution.definition(capability_id)
        if definition is None or definition["descriptor_hash"] != candidate["descriptor_hash"]:
            continue
        if capability_id in bound_resident:
            continue
        size = _definition_size(definition)
        if size <= remaining:
            remaining -= size
            selected.append({"capability_id": capability_id, "descriptor_hash": definition["descriptor_hash"]})
            if len(selected) == min(DEFAULT_LIMIT, MAX_LOADED_DEFINITIONS):
                break
    return selected


def build_tool_view(
    execution: ToolRegistry,
    capabilities: list[dict[str, Any]],
    discovery: dict[str, Any],
    *,
    discovery_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """从执行视图重建所有 Schema；发现视图只提供摘要。"""
    catalog = execution.catalog()
    executable_ids = frozenset(entry["capability_id"] for entry in catalog)
    mode = _injection_mode(execution, catalog)
    discovery_registry = discovery_registry or execution
    resident = []
    for item in capabilities:
        capability_id = str(item.get("capability_id", ""))
        definition = execution.definition(capability_id)
        if definition is not None:
            resident.append({**definition, "in_scope": True})
        elif (summary := discovery_registry.summary(capability_id)) is not None:
            resident.append({**summary, "description": summary["description"][:500], "in_scope": False})
    resident_ids = {str(item.get("capability_id")) for item in resident}
    available: list[dict[str, Any]] = []
    if mode == "full":
        for entry in catalog:
            if entry["capability_id"] in resident_ids:
                continue
            definition = execution.definition(entry["capability_id"])
            if definition is not None:
                available.append({**definition, "in_scope": True})
    loaded = _resolve_loaded(discovery, execution, discovery_registry)
    view: dict[str, Any] = {
        "mode": mode,
        "resident": resident,
        "available": available,
        "discovered": _resolve_queries(discovery, discovery_registry, executable_ids),
        "loaded": loaded,
    }
    _fit_definitions(view)
    return view


def _fit_definitions(view: dict[str, Any]) -> None:
    omitted = []
    latest = view["loaded"][-1] if view["loaded"] else {}
    # A definition this large can reach loaded only through an explicit describe
    # (or if the task predates batch loading). Give it a dedicated binding round.
    explicit_large = latest if "input_schema" in latest and _definition_size(latest) > FULL_INJECTION_MAX_CHARS else None
    remaining = FULL_INJECTION_MAX_CHARS
    selected = set()
    for group in ("resident", "loaded", "available"):
        entries = list(reversed(view[group])) if group == "loaded" else view[group]
        kept = []
        for entry in entries:
            key = entry["capability_id"]
            if "input_schema" not in entry:
                kept.append(entry)
                continue
            size = 0 if key in selected else _definition_size(entry)
            required = group == "resident" and key in SELFDEV_CAPABILITIES
            fits = required or (key == explicit_large["capability_id"] if explicit_large else size <= remaining)
            if fits:
                remaining -= size
                selected.add(key)
                kept.append(entry)
                continue
            omitted.append(key)
            if group == "loaded":
                kept.append({"capability_id": key, "status": "budget_exceeded", "in_scope": True})
        view[group] = list(reversed(kept)) if group == "loaded" else kept
    if omitted:
        view["mode"] = "discovery"
    view["omitted_capability_ids"] = list(dict.fromkeys(omitted))


def _injection_mode(registry: ToolRegistry, catalog: list[dict[str, Any]]) -> str:
    """小目录直接全量注入；超出工具数或字符预算后转为按需发现。"""
    if len(catalog) > FULL_INJECTION_MAX_TOOLS:
        return "discovery"
    total = 0
    for entry in catalog:
        definition = registry.definition(entry["capability_id"])
        if definition is None:
            continue
        total += _definition_size(definition)
        if total > FULL_INJECTION_MAX_CHARS:
            return "discovery"
    return "full"


def _resolve_queries(
    discovery: dict[str, Any],
    registry: ToolRegistry,
    executable_ids: frozenset[str],
) -> list[dict[str, Any]]:
    """把历史检索结果对账到当前目录，已卸载的工具显式列为 removed。"""
    resolved: list[dict[str, Any]] = []
    groups = registry.discovery_index().groups if discovery.get("queries") else {}
    for item in discovery.get("queries") or []:
        results: list[dict[str, Any]] = []
        removed: list[str] = []
        for capability_id in item.get("capability_ids") or []:
            entry = registry.summary(str(capability_id))
            if entry is None:
                removed.append(str(capability_id))
            else:
                results.append({
                    **entry, "description": entry["description"][:500],
                    "in_scope": str(capability_id) in executable_ids,
                })
        resolved.append({
            "query": item.get("query", ""),
            "namespace_ids": item.get("namespace_ids") or [],
            "namespaces": [
                groups[key].summary(key) for key in (item.get("matched_namespace_ids") or []) if key in groups
            ],
            "results": results,
            "removed": removed,
        })
    return resolved


def _resolve_loaded(
    discovery: dict[str, Any],
    execution: ToolRegistry,
    discovery_registry: ToolRegistry,
) -> list[dict[str, Any]]:
    """按当前注册表重建已加载定义；指纹不一致时不返回旧 Schema。"""
    resolved: list[dict[str, Any]] = []
    for item in discovery.get("loaded") or []:
        capability_id = str(item.get("capability_id") or "")
        expected = str(item.get("descriptor_hash") or "")
        definition = execution.definition(capability_id) if capability_id else None
        if definition is None:
            summary = discovery_registry.summary(capability_id)
            if summary is not None:
                resolved.append({
                    **summary, "description": summary["description"][:500],
                    "in_scope": False, "status": "out_of_scope",
                })
            else:
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
            "in_scope": True,
            "status": "ready",
        })
    return resolved
