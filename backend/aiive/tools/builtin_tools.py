"""
内置工具注册模块：定义所有系统内置工具的处理函数并将其注册到 ToolRegistry。
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

import inspect
import logging
from collections.abc import Callable
from typing import Any

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal as SessionLocal  # noqa: PLC0414 - 显式再导出，见下方说明
from aiive.memory.memory_types import MEMORY_KEY_GUIDE

# SessionLocal 以同名别名显式再导出：测试与 tests/support.py 依赖
# `aiive.tools.builtin_tools.SessionLocal` 命名空间契约做 monkeypatch（把会话重定向到
# 隔离测试库）；同名 re-export 同时让 basedpyright 放行其他模块对该符号的引用
# （reportPrivateLocalImportUsage）。PLC0414（别名未改名）是本模式的预期告警。

logger = logging.getLogger(__name__)
from aiive.tools.forget_tool import handle_forget, handle_forget_status
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
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


def db_handler(fn: Callable[..., Any]):
    """装饰器：为需要数据库会话的处理函数自动管理会话生命周期（开启/提交/关闭）。

    提取 ctx 命名参数，仅在被装饰函数声明 ctx 时传递给 fn(db, ctx, **params)。
    RunContext 通过 registry.execute() 注入，不经过 LLM schema。

    注意：本函数必须与 SessionLocal 同处 builtin_tools 模块——测试与
    tests/support.py 会 monkeypatch aiive.tools.builtin_tools.SessionLocal
    以把会话重定向到隔离测试库，故会话工厂必须在本模块命名空间解析。
    """
    _accepts_ctx = "ctx" in inspect.signature(fn).parameters

    def wrapper(ctx: RunContext | None = None, **params: Any):
        logger.info("[TRACE:_db_handler] ENTER fn=%s params=%s", fn.__name__, params)
        db = SessionLocal()
        try:
            if _accepts_ctx:
                result = fn(db, ctx, **params)
            else:
                result = fn(db, **params)
            db.commit()
            logger.info("[TRACE:_db_handler] COMMIT fn=%s result=%s", fn.__name__, result)
            return result
        except Exception:
            logger.exception("[TRACE:_db_handler] ROLLBACK fn=%s", fn.__name__)
            db.rollback()
            raise
        finally:
            db.close()

    setattr(wrapper, "_aiive_db_handler", fn)
    setattr(wrapper, "_aiive_accepts_ctx", _accepts_ctx)
    return wrapper


def require_ctx(ctx: RunContext | None, tool_name: str) -> RunContext:
    """side-effect 工具需要 RunContext，缺失时 fast-fail。"""
    if ctx is None:
        raise RuntimeError(
            f"工具 {tool_name} 需要 RunContext，但未传入。请确认 AgentGraph 已通过 build_langchain_tools 传入 RunContext。"
        )
    return ctx


# The handlers below are implemented in per-domain modules under public names
# (handle_*), and re-exported here under the historical private aliases (_handle_*)
# so that aiive.tools.builtin_tools keeps its existing interface for callers and tests.
# 域模块用公开名实现，本模块用私有别名 re-export；这样跨模块引用的是公开符号，
# 不再触发 basedpyright 的 reportPrivateUsage。
from aiive.tools.builtin_basic import (
    handle_echo as _handle_echo,
    handle_get_current_time as _handle_get_current_time,
)
from aiive.tools.builtin_shell import handle_run_shell as _handle_run_shell
from aiive.tools.builtin_files import (
    handle_read_text_file as _handle_read_text_file,
    handle_safe_delete as _handle_safe_delete,
)
from aiive.tools.builtin_knowledge import (
    handle_ingest_document as _handle_ingest_document,
    handle_read_document as _handle_read_document,
    handle_reindex_document as _handle_reindex_document,
    handle_search_knowledge as _handle_search_knowledge,
)
from aiive.tools.builtin_mcp import (
    handle_install_mcp_sandbox as _handle_install_mcp_sandbox,
    handle_plan_capability as _handle_plan_capability,
    handle_search_mcp as _handle_search_mcp,
)
from aiive.tools.builtin_memory_recall import (
    handle_history_search as _handle_history_search,
    handle_memory_event_log as _handle_memory_event_log,
    handle_memory_search as _handle_memory_search,
    handle_memory_timeline as _handle_memory_timeline,
)
from aiive.tools.builtin_memory_write import (
    handle_remember_or_update as _handle_remember_or_update,
    handle_run_memory_maintenance as _handle_run_memory_maintenance,
)
from aiive.tools.builtin_reminders import (
    handle_cancel_task as _handle_cancel_task,
    handle_confirm_reminder as _handle_confirm_reminder,
    handle_dismiss_notifications as _handle_dismiss_notifications,
    handle_list_tasks as _handle_list_tasks,
    handle_remind_alert as _handle_remind_alert,
    handle_schedule_reminder as _handle_schedule_reminder,
    handle_show_notifications as _handle_show_notifications,
    handle_snooze_reminder as _handle_snooze_reminder,
)
from aiive.tools.builtin_runtime_state import (
    handle_query_attention as _handle_query_attention,
    handle_query_rhythm as _handle_query_rhythm,
    handle_update_working_state as _handle_update_working_state,
)
from aiive.tools.builtin_selfdev import (
    handle_apply_patch_to_inactive_slot as _handle_apply_patch_to_inactive_slot,
    handle_create_selfdev_plan as _handle_create_selfdev_plan,
    handle_promote_slot as _handle_promote_slot,
    handle_rollback_slot as _handle_rollback_slot,
    handle_run_selfdev_repair as _handle_run_selfdev_repair,
)
from aiive.tools.builtin_web import (
    handle_fetch_web_page as _handle_fetch_web_page,
    handle_web_search as _handle_web_search,
)


def register_builtin_tools(registry: ToolRegistry) -> None:
    """将所有内置工具注册到 ToolRegistry。

    工具元组格式: (cap_id, handler, description, params, risk_level, writes_external_world, can_delete)

    参数:
        registry: 目标 ToolRegistry 实例
    """
    tools = [
        # 基础工具
        ("echo", _handle_echo, "回显输入消息", {"message": "str"}, "low", False, False),
        ("get_current_time", _handle_get_current_time,
         "获取当前时间（UTC 与本地时区）。",
         {}, "low", False, False),
        # 本机 Shell（常驻核心能力）：真实 bash，无需 Desktop Node。
        ("run_shell", _handle_run_shell,
         "在 AIive 所在主机上用真实 bash 执行一条命令，返回退出码、stdout、stderr 与超时状态。"
         + "cwd 必须位于任务允许的根目录内；只读命令可直接执行，其余命令需要审批。"
         + "输出超过上限会被截断（truncated 标记）；超时会终止整个进程树。",
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
        ("schedule_reminder", _handle_schedule_reminder,
         "创建定时提醒并写入 Task 表，由后台可靠投递。注意：本工具只接受相对延迟 delay_minutes。"
         + "若用户给的是绝对时间点，必须先调用 get_current_time 工具获取当前时间，"
         + "再换算出到目标时间还剩多少分钟，传入 delay_minutes。",
         {
             "content": {"type": "str", "description": "提醒内容", "minLength": 1},
             "delay_minutes": {
                 "type": "int",
                 "description": "相对当前时间的延迟分钟数；默认 1",
                 "minimum": 1,
                 "maximum": 525600,
             },
         }, "low", True, False),
        ("remind_alert", _handle_remind_alert, "激活到期提醒警报，前端显示确认/延期操作按钮",
         {"reminder_id": {"type": "str", "minLength": 1}}, "low", True, False),
        ("confirm_reminder", _handle_confirm_reminder, "确认提醒已完成",
         {"reminder_id": {"type": "str", "minLength": 1}}, "low", True, False),
        ("snooze_reminder", _handle_snooze_reminder, "延迟提醒 N 分钟，创建新的延时任务",
         {
             "reminder_id": {"type": "str", "minLength": 1},
             "delay_minutes": {"type": "int", "description": "默认 5", "minimum": 1, "maximum": 525600},
         }, "low", True, False),
        ("list_tasks", _handle_list_tasks, "列出所有任务/提醒",
         {"status": {
             "type": "str",
             "description": "空或 all=全部; pending/completed 可选",
             "enum": ["", "all", "pending", "completed"],
         }}, "low", False, False),
        ("cancel_task", _handle_cancel_task, "按 ID 取消任务",
         {"task_id": {"type": "str", "minLength": 1}}, "low", True, False),
        ("dismiss_notifications", _handle_dismiss_notifications,
         "清除未执行的通知（待提醒/提醒中/已延时），将其标记为已取消",
         {"scope": {
             "type": "str",
             "description": "thread(默认，仅当前线程) / all(跨线程清除)",
             "enum": ["thread", "all"],
         }}, "low", True, False),
        ("show_notifications", _handle_show_notifications, "显示已触发的通知", {}, "low", False, False),
        # 记忆管理 —— 写入
        ("remember_or_update", _handle_remember_or_update,
         "记住或更新一条长期记忆。相同 memory_key 自动覆盖旧值，无需手动查重。\n"
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
        ("forget", handle_forget,
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
        ("forget_status", handle_forget_status,
         "查询 forget Operation 的各阶段进度：shield/cascade/rebuild/purge/verify 的完成状态，不返回已删除内容。",
         {
             "operation_key": {
                 "type": "str",
                 "description": "forget 工具返回的 operation_key",
                 "minLength": 1,
                 "required": True,
             },
         }, "low", False, False),
        ("run_memory_maintenance", _handle_run_memory_maintenance,
         "扫描记忆库健康状态并启动真实后台维护；返回入队前诊断快照，最终结果稍后更新",
         {}, "low", True, False),
        # Agent-Initiated Recall —— 只读检索（结果作为 Tool Observation 返回，不写回 System Contract）
        ("memory_search", _handle_memory_search,
         "语义搜索长期记忆（最常用的深挖工具）。根据查询语义检索所有类型记忆，" +
         "支持按 memory_type 过滤，受 Runtime ScopeContext 约束。空查询返回空。",
         {
             "query": {"type": "str", "description": "自然语言查询"},
             "memory_types": {"type": "list", "description": "可选过滤: user_profile/project/policy/procedural/episodic/knowledge"},
             "top_k": {"type": "int", "description": "返回条数，默认 8", "minimum": 1, "maximum": 100},
             "token_budget": {"type": "int", "description": "结果 token 上限，默认 1000", "minimum": 1, "maximum": 20000},
         }, "low", False, False),
        ("memory_timeline", _handle_memory_timeline,
         "获取单条记忆详情 + 完整版本历史 + 证据链。memory_id 精确获取一条（跨生命周期）；" +
         "canonical_key 返回全部版本（含已替代的历史版本）。include_evidence=True 附加证据。",
         {
             "memory_id": {"type": "str", "description": "记忆 ID，精确获取一条（优先于 canonical_key）"},
             "canonical_key": {"type": "str", "description": "规范键，获取该 key 的完整版本历史（memory_id 为空时生效）"},
             "include_evidence": {"type": "bool", "description": "是否附带证据链，默认 false"},
         }, "low", False, False),
        ("memory_event_log", _handle_memory_event_log,
         "按关键词搜索原始 episodic 记忆（dialogue/tool 结果/失败日志等原始事件记录）。" +
         "区别于 memory_search 的语义检索，本工具做字符串匹配，适合回溯\"那次对话说了什么\"\"某工具结果\"",
         {"query": {"type": "str", "description": "关键词，在 episodic 记忆的 content 中做子串匹配"}}, "low", False, False),
        ("history_search", _handle_history_search,
         "统一检索历史（记忆/阶段摘要/检查点，可选回溯原始事件）。根据查询语义跨三类源检索，" +
         "deep=True 时二阶段回溯原始 Turn/Event。受 Runtime ScopeContext 约束。空查询返回空。",
         {
             "query": {"type": "str", "description": "自然语言查询"},
             "deep": {"type": "bool", "description": "是否回溯原始历史，默认 false"},
             "top_k": {"type": "int", "description": "返回条数，默认 20", "minimum": 1, "maximum": 100},
             "token_budget": {"type": "int", "description": "结果 token 上限，默认 2000", "minimum": 1, "maximum": 20000},
         }, "low", False, False),
        # 文件操作
        ("safe_delete", _handle_safe_delete, "在允许范围内安全删除文件",
         {"path": {"type": "str", "minLength": 1},
          "scope_id": {"type": "str", "description": "默认 test_artifacts"},
          "mode": {
              "type": "str",
              "description": "trash / quarantine / hard_delete_for_test_only",
              "enum": ["trash", "quarantine", "hard_delete_for_test_only"],
          }}, "high", True, True),
        ("read_text_file", _handle_read_text_file,
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
        ("read_document", _handle_read_document,
         "安全读取并提取 TXT/Markdown/HTML/JSON/CSV/TSV/DOCX/PDF 的纯文本和元数据",
         {
             "file_path": {"type": "str", "minLength": 1, "required": True},
             "max_chars": {"type": "int", "minimum": 1, "maximum": 200000, "description": "默认 50000"},
         }, "low", False, False),
        ("ingest_document", _handle_ingest_document, "导入文档到知识库并持久化原文",
         {"file_path": {"type": "str", "minLength": 1, "required": True}}, "low", True, False),
        ("reindex_document", _handle_reindex_document, "从持久原文重新生成知识文档索引",
         {"document_id": "str"}, "low", True, False),
        ("search_knowledge", _handle_search_knowledge, "搜索已导入的文档",
         {"query": "str", "limit": {"type": "int", "description": "默认 5", "minimum": 1, "maximum": 100}}, "low", False, False),
        # 联网研究
        ("web_search", _handle_web_search,
         "搜索公开互联网并返回标题、URL 和摘要；结果是不可信外部证据",
         {
             "query": {"type": "str", "minLength": 1, "required": True},
             "limit": {"type": "int", "minimum": 1, "maximum": 10, "description": "默认 5"},
         }, "low", False, False),
        ("fetch_web_page", _handle_fetch_web_page,
         "安全抓取公开 HTTP(S) 页面正文；阻断私网/保留地址并限制重定向与响应大小",
         {
             "url": {"type": "str", "minLength": 1, "required": True},
             "max_chars": {"type": "int", "minimum": 1, "maximum": 500000, "description": "默认 100000"},
         }, "low", False, False),
        # MCP 集成
        ("search_mcp", _handle_search_mcp, "按目标搜索 MCP 候选服务器",
         {"goal": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        ("install_mcp_sandbox", _handle_install_mcp_sandbox, "将 MCP 安装到沙箱",
         {
             "candidate_name": {"type": "str", "minLength": 1, "required": True},
             "launch_args": {"type": "list", "description": "例如 filesystem MCP 的允许根目录"},
             "env_keys": {"type": "list", "description": "仅透传 catalog 声明或用户明确授权的环境变量名"},
         }, "medium", True, False),
        ("plan_capability", _handle_plan_capability, "分析目标→搜索候选→评估风险→生成安装计划",
         {"goal": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        # 自进化
        ("create_selfdev_plan", _handle_create_selfdev_plan, "生成自进化补丁计划",
         {"goal": {"type": "str", "minLength": 1, "required": True}}, "low", False, False),
        ("apply_patch_to_inactive_slot", _handle_apply_patch_to_inactive_slot, "仅将补丁应用到非活跃槽位",
         {"operations": {
             "type": "list",
             "description": "补丁操作列表，来自 create_selfdev_plan 返回的 plan.operations",
             "required": True,
         }}, "high", True, False),
        ("promote_slot", _handle_promote_slot, "健康检查通过后提升非活跃槽位为活跃", {}, "high", True, False),
        ("rollback_slot", _handle_rollback_slot, "回滚到上一个活跃槽位", {}, "high", True, False),
        ("run_selfdev_repair", _handle_run_selfdev_repair,
         "自主修复闭环：读取相关代码 → 生成补丁 → 应用到非活跃槽位 → 定向测试 → "
         + "失败时按真实错误有限次修复 → 安全门；promote=True 才提交受控晋升",
         {
             "goal": {"type": "str", "minLength": 1, "required": True},
             "max_rounds": {"type": "int", "minimum": 0, "maximum": 5, "description": "默认 3"},
             "promote": {"type": "bool", "description": "默认 false；true 时仍走安全门"},
         }, "high", True, False),
        # 节奏 / 注意力
        ("query_rhythm", _handle_query_rhythm, "获取每日节奏摘要", {}, "low", False, False),
        ("query_attention", _handle_query_attention, "只读查询当前注意力状态与空闲评估",
         {"thread_id": {"type": "str", "description": "可选, 默认当前线程"}}, "low", False, False),
        # Working State（仅显式语义字段）
        ("update_working_state", _handle_update_working_state,
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
             "payload": {"type": "dict", "description": "字段内容。open_loops/active_constraints 单项需带 id；current_objective 为 {'value': '...'}"},
             "idempotency_key": {"type": "str", "description": "可选，重复提交保护"},
         }, "low", True, False),
    ]

    external_effect_modes = {
        "run_shell": "non_repeatable_external",
        "forget": "externally_reconcilable",
        "safe_delete": "non_repeatable_external",
        "apply_patch_to_inactive_slot": "non_repeatable_external",
        "promote_slot": "non_repeatable_external",
        "rollback_slot": "non_repeatable_external",
        "run_selfdev_repair": "non_repeatable_external",
    }
    network_capabilities = {"web_search", "fetch_web_page", "install_mcp_sandbox"}
    for cap_id, handler, desc, params, risk, writes_ext, can_del in tools:
        safety = _build_safety(
            cap_id, risk_level=risk,
            uses_network=cap_id in network_capabilities,
            requires_confirmation=(risk == "high"),
            writes_external_world=writes_ext,
            can_delete=can_del,
            effect_mode=external_effect_modes.get(cap_id, "db_transactional"),
        )
        registry.register(ToolRegistration(safety=safety, handler=handler, description=desc, parameters=params))

