"""
内置工具注册模块：定义所有系统内置工具的类并将其注册到 ToolRegistry。
每个工具映射到一个真实的服务或操作，涵盖提醒、记忆、文件、知识库、MCP、自进化等功能。

工具分类：
- 基础工具：echo, get_current_time
- 提醒/任务：schedule_reminder, remind_alert, confirm_reminder, snooze_reminder, list_tasks, cancel_task, show_notifications
- 记忆管理：remember_or_update, forget, run_memory_maintenance
- 记忆召回（V2 Agent-Initiated，只读，结果作证据返回）：memory_search, memory_timeline, memory_event_log
- 文件操作：safe_delete, read_text_file
- 知识库：ingest_document, reindex_document, search_knowledge
- MCP 集成：search_mcp, install_mcp_sandbox
- 自进化：create_selfdev_plan, apply_patch_to_inactive_slot, promote_slot, rollback_slot, run_selfdev_repair
- 节奏/注意力：query_rhythm, query_attention
"""

import logging
from typing import Any

from aiive.context.run_context import require_ctx as require_ctx
from aiive.db.base import SessionLocal as SessionLocal  # noqa: PLC0414 - 显式再导出，见下方说明
from aiive.memory.memory_types import MEMORY_KEY_GUIDE

# SessionLocal 以同名别名显式再导出：测试与 tests/support.py 依赖
# `aiive.tools.builtin_tools.SessionLocal` 命名空间契约做 monkeypatch（把会话重定向到
# 隔离测试库）；同名 re-export 同时让 basedpyright 放行其他模块对该符号的引用
# （reportPrivateLocalImportUsage）。PLC0414（别名未改名）是本模式的预期告警。

logger = logging.getLogger(__name__)
from aiive.tools.forget_tool import ForgetTool, ForgetStatusTool
from aiive.tools.base import CancellationPolicy, DatabaseTool
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolDefinition,
    ToolRegistry,
)


def _build_safety(capability_id: str, **overrides: Any) -> CapabilitySafetySchema:
    """构建工具的安全配置 schema。

    使用默认安全配置作为基础，允许通过 overrides 覆盖特定字段。

    参数:
        capability_id: 工具能力标识符
        **overrides: 需要覆盖的安全字段（如 risk_level、writes_external_world 等）

    返回:
        构建好的 CapabilitySafetySchema 实例
    """
    base: dict[str, Any] = dict(
        capability_id=capability_id,
        definition_source="local_builtin",
        definition_trust_level="trusted",
        risk_level="low",
        requires_confirmation=False,
        writes_external_world=False,
        can_access_secret=False,
        can_delete=False,
    )
    base.update(overrides)
    # 不在此处预计算 descriptor_hash：交由 registry.register 统一补算，
    # 使指纹覆盖 description/parameters，让 descriptor_changed 防护对内置工具生效。
    fields = CapabilitySafetySchema.__dataclass_fields__
    return CapabilitySafetySchema(**{k: v for k, v in base.items() if k in fields})


# Domain classes own their business methods; registration adds immutable definitions.
from aiive.tools.builtin_basic import (
    EchoTool,
    GetCurrentTimeTool,
)
from aiive.tools.builtin_shell import RunShellTool
from aiive.tools.builtin_files import (
    ReadTextFileTool,
    SafeDeleteTool,
)
from aiive.tools.builtin_knowledge import (
    IngestDocumentTool,
    ReadDocumentTool,
    ReindexDocumentTool,
    SearchKnowledgeTool,
)
from aiive.tools.builtin_mcp import (
    InstallMcpSandboxTool,
    PlanCapabilityTool,
    SearchMcpTool,
)
from aiive.tools.builtin_memory_recall import (
    HistorySearchTool,
    MemoryEventLogTool,
    MemorySearchTool,
    MemoryTimelineTool,
)
from aiive.tools.builtin_memory_write import (
    RememberOrUpdateTool,
    RunMemoryMaintenanceTool,
)
from aiive.tools.builtin_reminders import (
    CancelTaskTool,
    CancelReminderTool,
    ConfirmReminderTool,
    DismissNotificationsTool,
    ListTasksTool,
    ListRemindersTool,
    RemindAlertTool,
    ScheduleReminderTool,
    ShowNotificationsTool,
    SnoozeReminderTool,
)
from aiive.tools.builtin_runtime_state import (
    QueryAttentionTool,
    QueryRhythmTool,
    UpdateWorkingStateTool,
)
from aiive.tools.builtin_selfdev import (
    ApplyPatchToInactiveSlotTool,
    CreateSelfdevPlanTool,
    PromoteSlotTool,
    PromotionStatusTool,
    RollbackSlotTool,
    RunSelfdevRepairTool,
)
from aiive.tools.builtin_web import (
    FetchWebPageTool,
    WebSearchTool,
)


def register_builtin_tools(registry: ToolRegistry) -> None:
    """将所有内置工具注册到 ToolRegistry。

    工具元组格式: (cap_id, tool_type, description, params, risk_level, writes_external_world, can_delete)

    参数:
        registry: 目标 ToolRegistry 实例
    """
    from aiive.tools.attachment_tools import register_attachment_tools
    from aiive.tool_discovery.builtins import builtin_discovery
    from aiive.tools.builtin_knowledge import ReadKnowledgeChunkTool
    from aiive.memory.recall_config import RecallConfig
    from aiive.tools.task_file_tools import ListTaskFilesTool, ReadTaskArtifactTool, ReadTaskEvidenceTool

    register_attachment_tools(registry, worker=True)
    task_file_range = {
        "offset": {"type": "int", "minimum": 0, "description": "字节偏移，首段为 0，续读用 next_offset。"},
        "max_bytes": {"type": "int", "minimum": 16, "maximum": 4096, "description": "片段大小，默认 4096 字节。"},
    }
    recall = RecallConfig()
    tools = [
        ("list_task_files", ListTaskFilesTool, "分页列出本任务成果或证据，使用 next_cursor 续页。",
         {"kind": {"type": "str", "enum": ["artifacts", "evidence"], "required": True},
          "cursor": {"type": "str", "maxLength": 512}, "limit": {"type": "int", "minimum": 1, "maximum": 10}}, "low", False, False),
        ("read_task_evidence", ReadTaskEvidenceTool, "按 ID 分段读取当前任务的持久证据。",
         {"evidence_id": {"type": "str", "minLength": 1, "required": True}, **task_file_range}, "low", False, False),
        ("read_task_artifact", ReadTaskArtifactTool, "按 ID 分段读取当前任务文本产物和 Shell 日志。",
         {"artifact_id": {"type": "str", "minLength": 1, "required": True}, **task_file_range}, "low", False, False),
        # 基础工具
        ("echo", EchoTool, "回显输入消息", {"message": "str"}, "low", False, False),
        ("get_current_time", GetCurrentTimeTool,
         "获取当前 UTC 时间与用户时间 user_local、user_timezone。缺少用户时区时两者为空，local 是服务器时间。",
         {}, "low", False, False),
        # 本机 Shell（常驻核心能力）：真实 bash，无需 Desktop Node。
        ("run_shell", RunShellTool,
         "在 AIive 所在主机上用真实 bash 执行一条命令，返回退出码、stdout、stderr 与超时状态。"
         + "cwd 必须位于任务允许的根目录内；只读命令可直接执行，其余命令需要审批。"
         + "stdout/stderr 会流式写入日志，结果里只回填有限预览与总字节数（truncated 标记预览被截）；"
         + "任务通过结果中的 artifact_id 和 read_task_artifact 续读日志。"
         + "capture_truncated 为 true 表示日志达到容量上限；无法确认超时进程终止时返回 execution_unknown。",
         {
             "command": {
                 "type": "str",
                 "description": "交给 bash -c 执行的命令文本；不要包含交互式提示",
                 "minLength": 1,
                 "required": True,
             },
             "cwd": {
                 "type": "str",
                 "description": "工作目录绝对路径，必须在任务 allowed_roots 内",
                 "minLength": 1,
                 "required": True,
             },
             "timeout_seconds": {
                 "type": "int",
                 "description": "超时秒数；0 表示默认 60s，上限 300",
                 "minimum": 0,
                 "maximum": 300,
             },
         }, "medium", True, False),
        # 提醒 / 任务
        ("schedule_reminder", ScheduleReminderTool,
         "创建一次性提醒。选 at（ISO 8601，可带偏移）或 delay_minutes；无偏移的 at 按指定 timezone_name 或用户时区解析。",
         {
             "content": {"type": "str", "description": "提醒内容", "minLength": 1},
             "delay_minutes": {
                 "type": "int",
                 "description": "相对当前时间的延迟分钟数；默认 1",
                 "minimum": 1,
                 "maximum": 525600,
             },
             "at": {"type": "str", "minLength": 1, "maxLength": 64},
             "timezone_name": {"type": "str", "minLength": 1, "maxLength": 100},
         }, "low", True, False),
        ("remind_alert", RemindAlertTool, "激活到期提醒警报，前端显示确认/延期操作按钮",
         {"reminder_id": {"type": "str", "minLength": 1}}, "low", True, False),
        ("confirm_reminder", ConfirmReminderTool, "确认提醒已完成",
         {"reminder_id": {"type": "str", "minLength": 1}}, "low", True, False),
        ("snooze_reminder", SnoozeReminderTool, "延迟提醒 N 分钟，创建新的延时任务",
         {
             "reminder_id": {"type": "str", "minLength": 1},
             "delay_minutes": {"type": "int", "description": "默认 5", "minimum": 1, "maximum": 525600},
         }, "low", True, False),
        ("list_reminders", ListRemindersTool, "分页列出当前会话的提醒。用 next_cursor 续页。",
         {"status": {"type": "str", "enum": ["", "pending", "dispatching", "completed", "cancelled", "failed"]},
          "cursor": {"type": "str", "maxLength": 130}, "limit": {"type": "int", "minimum": 1, "maximum": 50}}, "low", False, False),
        ("cancel_reminder", CancelReminderTool, "取消或清除当前会话的一次性提醒，task_id 取自 list_reminders。",
         {"task_id": {"type": "str", "minLength": 1, "required": True}}, "low", True, False),
        ("list_tasks", ListTasksTool, "兼容旧任务记录，最多 50 条；查询提醒请使用 list_reminders。",
         {"status": {
             "type": "str",
             "description": "空或 all=全部; pending/completed 可选",
             "enum": ["", "all", "pending", "completed"],
         }}, "low", False, False),
        ("cancel_task", CancelTaskTool, "兼容旧调度任务。取消一次性提醒优先用 cancel_reminder。",
         {"task_id": {"type": "str", "minLength": 1}}, "low", True, False),
        ("dismiss_notifications", DismissNotificationsTool,
         "清除未执行的通知（待提醒/提醒中/已延时），将其标记为已取消",
         {"scope": {
             "type": "str",
             "description": "thread(默认，仅当前线程) / all(跨线程清除)",
             "enum": ["thread", "all"],
         }}, "low", True, False),
        ("show_notifications", ShowNotificationsTool, "显示已触发的通知", {}, "low", False, False),
        # 记忆管理 —— 写入
        ("remember_or_update", RememberOrUpdateTool,
         "保存本轮用户提供的长期信息。按 MemoryKeyRegistry 处理同键记录：相同内容可强化；"
         + "单值键按冲突策略修订或替换旧值，多值键允许不同内容并存。请检查 written、outcome 和 state。\n"
         + MEMORY_KEY_GUIDE,
         {
             "content": {"type": "str", "description": "记忆内容文本；身份键只填纯值", "minLength": 1},
             "memory_type": {
                 "type": "str",
                 "description": "user_profile / agent_self / project / policy / procedural / episodic / knowledge / environment",
                 "enum": ["fact", "user_profile", "agent_self", "project", "policy", "procedural", "episodic", "knowledge", "environment"],
             },
             "memory_key": {"type": "str", "description": "稳定键，推荐格式: user.preference.<topic> / agent.persona.<trait> / project.<name>.<topic>"},
             "keywords": {"type": "list", "description": "可选检索关键词。用于词汇召回命中；"},
         }, "low", True, False),
        # Phase 6A: 统一 forget 工具
        ("forget", ForgetTool,
         "mode: everywhere(默认/忘记一切) / memory_only(仅删记忆保留聊天) / history_only(仅删聊天及派生)",
         {
             "mode": {
                 "type": "str",
                 "description": "memory_only / history_only / everywhere (默认 everywhere)",
                 "enum": ["memory_only", "history_only", "everywhere"],
             },
             "memory_ids": {"type": "list", "description": "memory_only/everywhere: 精确记忆 ID"},
             "turn_ids": {"type": "list", "description": "history_only/everywhere: 精确 Turn ID"},
             "event_ids": {"type": "list", "description": "history_only/everywhere: 精确 Event ID"},
             "thread_id": {"type": "str", "description": "history_only/everywhere: 整个 Thread"},
             "canonical_key": {"type": "str", "description": "memory_only/everywhere: 按内容键全删"},
             "reason": {"type": "str", "description": "遗忘原因"},
         }, "high", True, False),
        ("forget_status", ForgetStatusTool,
         "查询 forget Operation 的各阶段进度：shield/cascade/rebuild/purge/verify 的完成状态，不返回已删除内容。",
         {
             "operation_key": {
                 "type": "str",
                 "description": "forget 工具返回的 operation_key",
                 "minLength": 1,
                 "required": True,
             },
         }, "low", False, False),
        ("run_memory_maintenance", RunMemoryMaintenanceTool,
         "扫描记忆库健康状态并启动真实后台维护；返回入队前诊断快照，最终结果稍后更新",
         {}, "low", True, False),
        # Agent-Initiated Recall —— 只读检索（结果作为 Tool Observation 返回，不写回 System Contract）
        ("memory_search", MemorySearchTool,
         "语义搜索长期记忆（最常用的深挖工具）。根据查询语义检索所有类型记忆，" +
         "支持按 memory_type 过滤，受 Runtime ScopeContext 约束。空查询返回空。",
         {
             "query": {"type": "str", "description": "自然语言查询"},
             "memory_types": {"type": "list", "description": "可选过滤: user_profile/project/policy/procedural/episodic/knowledge"},
             "top_k": {"type": "int", "default": recall.memory_tool_top_k, "minimum": 1, "maximum": 100},
             "token_budget": {"type": "int", "default": recall.memory_tool_token_budget, "minimum": 1, "maximum": 20000},
         }, "low", False, False),
        ("memory_timeline", MemoryTimelineTool,
         "获取单条记忆详情 + 完整版本历史 + 证据链。memory_id 精确获取一条（跨生命周期）；" +
         "canonical_key 返回全部版本（含已替代的历史版本）。include_evidence=True 附加证据。",
         {
             "memory_id": {"type": "str", "description": "记忆 ID，精确获取一条（优先于 canonical_key）"},
             "canonical_key": {"type": "str", "description": "规范键，获取该 key 的完整版本历史（memory_id 为空时生效）"},
             "include_evidence": {"type": "bool", "description": "是否附带证据链，默认 false"},
         }, "low", False, False),
        ("memory_event_log", MemoryEventLogTool,
         "按关键词搜索原始 episodic 记忆（dialogue/tool 结果/失败日志等原始事件记录）。" +
         "区别于 memory_search 的语义检索，本工具做字符串匹配，适合回溯\"那次对话说了什么\"\"某工具结果\"",
         {"query": {"type": "str", "description": "关键词，在 episodic 记忆的 content 中做子串匹配"}}, "low", False, False),
        ("history_search", HistorySearchTool,
         "统一检索历史（记忆/阶段摘要/检查点，可选回溯原始事件）。根据查询语义跨三类源检索，" +
         "deep=True 时二阶段回溯原始 Turn/Event。受 Runtime ScopeContext 约束。空查询返回空。",
         {
             "query": {"type": "str", "description": "自然语言查询"},
             "deep": {"type": "bool", "description": "是否回溯原始历史，默认 false"},
             "top_k": {"type": "int", "description": "返回条数，默认 20", "minimum": 1, "maximum": 100},
             "token_budget": {"type": "int", "description": "结果 token 上限，默认 2000", "minimum": 1, "maximum": 20000},
         }, "low", False, False),
        # 文件操作
        ("safe_delete", SafeDeleteTool, "在允许范围内安全删除文件",
         {"path": {"type": "str", "minLength": 1},
          "scope_id": {"type": "str", "description": "默认 test_artifacts"},
          "mode": {
              "type": "str",
              "description": "trash / quarantine / hard_delete_for_test_only",
              "enum": ["trash", "quarantine", "hard_delete_for_test_only"],
          }}, "high", True, True),
        ("read_text_file", ReadTextFileTool,
         "分页读取文本文件（限 ~/Documents 与工具输出日志目录）。"
         + "单页受字符预算约束，返回 total_lines/total_chars/next_offset；"
         + "大文件或大日志必须用 offset/limit 分页读取，不要一次读全文。",
         {
             "path": {"type": "str", "description": "文件路径（~/Documents 或工具日志目录）", "minLength": 1, "required": True},
             "offset": {"type": "int", "description": "起始行号，从 0 开始；默认 0", "minimum": 0},
             "limit": {"type": "int", "description": "本次最多读取行数，默认 50，上限 1000", "minimum": 1, "maximum": 1000},
             "max_chars": {"type": "int", "description": "单页字符预算，默认 1000，上限 8000", "minimum": 1, "maximum": 8000},
             "char_offset": {"type": "int", "description": "超长行的行内续读偏移；默认 0", "minimum": 0},
         }, "low", False, False),
        # 知识库
        ("read_document", ReadDocumentTool,
         "安全读取并提取 TXT/Markdown/HTML/JSON/CSV/TSV/DOCX/PDF 的纯文本和元数据",
         {
             "file_path": {"type": "str", "minLength": 1, "required": True},
             "max_chars": {"type": "int", "minimum": 1, "maximum": 24000, "description": "每页字符数，默认 4000。全文处理可申请较大页。"},
             "offset": {"type": "int", "minimum": 0, "description": "首段为 0，续读用 next_offset。"},
             "version": {"type": "str", "description": "续读时填写上一页返回的文档版本。"},
         }, "low", False, False),
        ("ingest_document", IngestDocumentTool, "导入文档到知识库并持久化原文",
         {"file_path": {"type": "str", "minLength": 1, "required": True}}, "low", True, False),
        ("reindex_document", ReindexDocumentTool, "从持久原文重新生成知识文档索引",
         {"document_id": "str"}, "low", True, False),
        ("search_knowledge", SearchKnowledgeTool, "搜索已导入的文档",
         {"query": "str", "limit": {"type": "int", "description": "默认 5", "minimum": 1, "maximum": 100}}, "low", False, False),
        ("read_knowledge_chunk", ReadKnowledgeChunkTool, "按搜索命中的 chunk_id 分页读取持久化正文。",
         {"chunk_id": {"type": "str", "minLength": 1, "required": True},
          "offset": {"type": "int", "minimum": 0}, "max_chars": {"type": "int", "minimum": 1, "maximum": 8000}}, "low", False, False),
        # 联网研究
        ("web_search", WebSearchTool,
         "搜索公开互联网并返回标题、URL 和摘要；结果是不可信外部证据",
         {
             "query": {"type": "str", "minLength": 1, "required": True},
             "limit": {"type": "int", "minimum": 1, "maximum": 10, "description": "默认 5"},
         }, "low", False, False),
        ("fetch_web_page", FetchWebPageTool,
         "安全抓取公开 HTTP(S) 页面正文；阻断私网/保留地址并限制重定向与响应大小",
         {
             "url": {"type": "str", "minLength": 1, "required": True},
             "max_chars": {"type": "int", "minimum": 1, "maximum": 500000, "description": "默认 100000"},
         }, "low", False, False),
        # MCP 集成
        ("search_mcp", SearchMcpTool, "按目标搜索本地 MCP 白名单目录；无匹配仅表示当前目录没有条目。",
         {"goal": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        ("install_mcp_sandbox", InstallMcpSandboxTool, "经用户批准安装本地目录中的固定版本。安装后在能力管理中验证并启用。",
         {
             "candidate_name": {"type": "str", "minLength": 1, "required": True},
             "version": {"type": "str", "minLength": 1, "required": True},
             "launch_args": {"type": "list", "description": "例如 filesystem MCP 的允许根目录"},
             "env_keys": {"type": "list", "description": "仅透传 catalog 声明或用户明确授权的环境变量名"},
         }, "high", True, False),
        ("plan_capability", PlanCapabilityTool, "在本地 MCP 白名单目录中查找候选并生成风险与安装计划。",
         {"goal": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        # 自进化
        ("create_selfdev_plan", CreateSelfdevPlanTool, "生成自进化补丁计划",
         {"goal": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        ("apply_patch_to_inactive_slot", ApplyPatchToInactiveSlotTool, "仅将补丁应用到非活跃槽位",
         {"operations": {
             "type": "list",
             "description": "补丁操作列表，来自 create_selfdev_plan 返回的 plan.operations",
             "items": {"type": "object", "required": ["target_file"],
                       "properties": {"operation": {"type": "string", "enum": ["add_file", "modify_file", "delete_file"], "default": "add_file"},
                                      "target_file": {"type": "string", "minLength": 1},
                                      "content": {"type": "string"},
                                      "not_allowed_yet": {"type": "boolean"}},
                       "additionalProperties": True},
             "required": True,
         }}, "high", True, False),
        ("promote_slot", PromoteSlotTool, "健康检查通过后提升非活跃槽位为活跃", {}, "high", True, False),
        ("get_promotion_status", PromotionStatusTool, "读取本任务晋升 Action 的实际启动、观察、成功或回滚结果。",
         {"action_id": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        ("rollback_slot", RollbackSlotTool, "回滚到上一个活跃槽位", {}, "high", True, False),
        ("run_selfdev_repair", RunSelfdevRepairTool,
         "自主修复闭环：读取相关代码 → 生成补丁 → 应用到非活跃槽位 → 定向测试 → "
         + "失败时按真实错误有限次修复 → 安全门；promote=True 才提交受控晋升",
         {
             "goal": {"type": "str", "minLength": 1, "required": True},
             "max_rounds": {"type": "int", "minimum": 0, "maximum": 5, "description": "默认 3"},
             "promote": {"type": "bool", "description": "默认 false；true 时仍走安全门"},
         }, "high", True, False),
        # 节奏 / 注意力
        ("query_rhythm", QueryRhythmTool, "获取每日节奏摘要", {}, "low", False, False),
        ("query_attention", QueryAttentionTool, "只读查询当前注意力状态与空闲评估",
         {"thread_id": {"type": "str", "description": "可选, 默认当前线程"}}, "low", False, False),
        # Working State（仅显式语义字段）
        ("update_working_state", UpdateWorkingStateTool,
        "显式维护 WorkingState 语义字段: current_objective（设置当前目标）/ open_loops / active_constraints。"
        + "确定性字段（pending_approvals/artifact_refs/verified_tool_states/uncommitted_side_effects/running_tool_state）由系统自动维护，不要通过此工具修改。",
         {
             "field": {
                 "type": "str",
                 "description": "current_objective / open_loops / active_constraints",
                 "enum": ["current_objective", "open_loops", "active_constraints"],
             },
             "operation": {
                 "type": "str",
                 "description": "add / remove / update；current_objective 忽略此参数",
                 "enum": ["add", "remove", "update"],
             },
             "payload": {"type": "dict", "properties": {"id": {"type": "string"}, "value": {"type": "string"}},
                         "additionalProperties": True,
                         "description": "字段内容。open_loops/active_constraints 单项需带 id；current_objective 为 {'value': '...'}"},
             "idempotency_key": {"type": "str", "description": "可选，重复提交保护"},
         }, "low", True, False),
    ]

    external_effect_modes = {
        "run_shell": "non_repeatable_external",
        "install_mcp_sandbox": "non_repeatable_external",
        "forget": "externally_reconcilable",
        "safe_delete": "non_repeatable_external",
        "apply_patch_to_inactive_slot": "non_repeatable_external",
        "promote_slot": "non_repeatable_external",
        "rollback_slot": "non_repeatable_external",
        "run_selfdev_repair": "non_repeatable_external",
    }
    network_capabilities = {"web_search", "fetch_web_page", "install_mcp_sandbox"}
    for cap_id, tool_type, desc, params, risk, writes_ext, can_del in tools:
        safety = _build_safety(
            cap_id, risk_level=risk,
            uses_network=cap_id in network_capabilities,
            requires_confirmation=(risk == "high"),
            writes_external_world=writes_ext,
            can_delete=can_del,
            effect_mode=external_effect_modes.get(cap_id, "db_transactional"),
        )
        registry.register(tool_type(ToolDefinition(safety=safety, description=desc, parameters=params,
                                                   discovery=builtin_discovery(cap_id),
                                                   cancellation_policy=CancellationPolicy.TRANSACTIONAL if issubclass(tool_type, DatabaseTool) else CancellationPolicy.COOPERATIVE if cap_id == "run_shell" else CancellationPolicy.NONE)))
