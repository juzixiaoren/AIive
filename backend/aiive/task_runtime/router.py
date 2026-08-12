"""任务类型到最小 capability profile 的确定性路由。"""
from __future__ import annotations

from typing import Any


READ_ONLY_PROFILE = {
    "get_current_time", "memory_search", "memory_timeline", "memory_event_log",
    "history_search", "search_knowledge", "desktop_system_info", "desktop_fs_stat",
    "desktop_fs_list", "desktop_fs_read_text", "desktop_fs_read_binary",
}
DOCUMENT_PROFILE = READ_ONLY_PROFILE | {
    "read_document", "ingest_document", "reindex_document",
}
WEB_RESEARCH_PROFILE = READ_ONLY_PROFILE | {"web_search", "fetch_web_page"}
MCP_PROFILE = {"search_mcp", "plan_capability", "install_mcp_sandbox"}
FILE_PROFILE = READ_ONLY_PROFILE | {
    "desktop_fs_write_text", "desktop_fs_write_binary", "desktop_fs_edit_text",
    "desktop_fs_mkdir", "desktop_fs_copy", "desktop_fs_move", "desktop_fs_delete",
}
SELF_IMPROVEMENT_PROFILE = {
    "create_selfdev_plan", "apply_patch_to_inactive_slot", "promote_slot", "rollback_slot",
}


def default_capabilities(task_type: str) -> set[str]:
    normalized = task_type.strip().lower()
    if normalized in {"self_improvement", "selfdev", "lifecycle"}:
        return set(SELF_IMPROVEMENT_PROFILE)
    if normalized in {"file_operation", "desktop", "invoice_organization", "workspace"}:
        return set(FILE_PROFILE)
    if normalized in {"document", "documents", "knowledge", "knowledge_base"}:
        return set(DOCUMENT_PROFILE)
    if normalized in {"research", "web_research", "internet", "web"}:
        return set(WEB_RESEARCH_PROFILE)
    if normalized in {"mcp", "capability", "capability_management"}:
        return set(MCP_PROFILE)
    if normalized in {"shell", "development"}:
        return set(FILE_PROFILE | {"desktop_exec"})
    return set(READ_ONLY_PROFILE)


def normalize_task_brief(task_type: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    brief = dict(raw or {})
    scope = dict(brief.get("scope") or {})
    if not scope.get("allowed_capabilities"):
        scope["allowed_capabilities"] = sorted(default_capabilities(task_type))
    brief["scope"] = scope
    return brief


def select_executor_type(task_brief: dict[str, Any]) -> str:
    initial = task_brief.get("initial_input")
    if isinstance(initial, dict) and isinstance(initial.get("deterministic_action"), dict):
        return "deterministic"
    return "agent"
