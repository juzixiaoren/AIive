"""Explicit discovery namespaces for the backend's built-in Worker tools."""
from dataclasses import replace

from aiive.tool_discovery.documents import DiscoveryMetadata

_GROUPS = (
    ("attachments", "对话附件", "浏览、搜索、读取对话附件并记录处理进度",
     ("list_thread_attachments", "read_attachment", "record_attachment_progress")),
    ("task-files", "任务成果", "查找和读取当前任务的证据、成果与日志",
     ("list_task_files", "read_task_evidence", "read_task_artifact")),
    ("utilities", "后端实用工具", "获取当前时间、回显消息、运行后端命令或操作文件",
     ("echo", "get_current_time", "run_shell", "shell_session", "read_file", "edit_file", "write_file", "apply_patch", "safe_delete", "read_text_file")),
    ("reminders", "提醒与通知", "创建、确认或取消提醒，管理通知和旧调度任务",
     ("schedule_reminder", "remind_alert", "confirm_reminder", "snooze_reminder",
      "list_reminders", "cancel_reminder", "list_tasks", "cancel_task",
      "dismiss_notifications", "show_notifications")),
    ("memory", "记忆与历史", "查询和维护长期记忆、会话历史及回忆证据",
     ("remember_or_update", "forget", "forget_status", "run_memory_maintenance",
      "memory_search", "memory_timeline", "memory_event_log", "history_search")),
    ("knowledge", "文档与知识库", "读取文件正文，导入和检索持久化知识库",
     ("read_document", "ingest_document", "reindex_document", "search_knowledge", "read_knowledge_chunk")),
    ("web", "公开网页研究", "搜索公开网页并提取正文",
     ("web_search", "fetch_web_page")),
    ("integrations", "MCP 集成管理", "在本地 MCP Server 目录中查找候选、规划并安装集成",
     ("search_mcp", "install_mcp_sandbox", "plan_capability")),
    ("selfdev", "AIive 维护", "维护 AIive 自身的补丁与部署槽位",
     ("create_selfdev_plan", "apply_patch_to_inactive_slot", "promote_slot",
      "get_promotion_status", "get_runtime_identity", "rollback_slot", "run_selfdev_repair")),
    ("runtime-state", "运行状态", "查询节奏和注意力，记录工作状态",
     ("query_rhythm", "query_attention", "update_working_state")),
)

_NAMESPACES = {
    capability_id: DiscoveryMetadata(f"builtin:{namespace_id}", name, description)
    for namespace_id, name, description, capability_ids in _GROUPS
    for capability_id in capability_ids
}
_DETAILS = {
    "read_document": {
        "aliases": ("文档解析", "document reader"),
        "use_cases": ("从 PDF 或 Word 文件中提取纯文本",),
        "input_concepts": ("PDF", "DOCX", "Markdown", "CSV"),
        "output_concepts": ("纯文本", "文件元数据"),
        "tags": ("document extraction",),
    },
    "get_current_time": {"aliases": ("现在几点", "current time"), "tags": ("timezone",)},
    "web_search": {"aliases": ("上网搜索", "internet search"), "tags": ("web research",)},
    "create_selfdev_plan": {
        "aliases": ("补丁计划", "plan a code change", "self improvement"),
        "output_concepts": ("patch operations", "candidate test plan"),
    },
    "apply_patch_to_inactive_slot": {
        "aliases": ("修改 AIive 源码", "应用补丁", "patch worker runtime", "edit AIive code"),
        "output_concepts": ("inactive slot", "candidate tests"),
    },
    "run_selfdev_repair": {
        "aliases": ("AIive 自进化", "自主修复", "self repair", "repair AIive itself"),
        "use_cases": ("读取活跃槽位、修改候选代码、运行测试并按失败反馈自动修复",),
        "output_concepts": ("candidate ready", "evaluation gate"),
    },
    "promote_slot": {"aliases": ("发布候选", "promote candidate", "switch active slot")},
    "get_promotion_status": {"aliases": ("部署进度", "promotion status", "deployment status")},
    "rollback_slot": {"aliases": ("回滚部署", "release rollback", "revert active slot")},
}


def builtin_discovery(capability_id: str) -> DiscoveryMetadata:
    return replace(_NAMESPACES[capability_id], **_DETAILS.get(capability_id, {}))
