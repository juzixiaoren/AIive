"""
请求级上下文：替代模块级全局变量 _current_thread_id。

所有工具 handler、MemoryWriteService、EventLogger 等需要 thread/trace
信息时，统一从 RunContext 获取。

RunContext 通过 _make_handler 闭包捕获 → registry.execute() 注入 handler，
全程不经过 LLM tool schema，模型不可见。
"""
from dataclasses import dataclass, field

from aiive.memory.recall_models import ScopeContext

# RunContext.source 取值（调用执行者标签）。
# 注意：它与 MessageSource（会话回合消息来源：user / system_command /
# runtime_event）是不同维度——前者标识「是谁/什么路径触发了这次工具执行」，
# 后者标识「这一回合的消息从哪来」。二者不可混用，亦不可合并到同一枚举。
# 当执行者恰好就是某个会话回合时，source 复用该回合的 MessageSource.value。
RUN_CTX_USER_CHAT = "user_chat"  # 用户聊天入口触发的工具执行
RUN_CTX_OUTBOX_WORKER = "outbox_worker"  # Outbox 异步作业（记忆抽取/遗忘等）触发
RUN_CTX_API = "api"  # 记忆维护类 API 直接触发（archive/sleep 等）
RUN_CTX_TRUSTED_APPROVAL = "trusted_approval"  # 已服务端审批的可信工具执行
RUN_CTX_MANUAL_MEMORY_API = "manual_memory_api"  # 手动记忆写入 API 触发


@dataclass
class RunContext:
    """单次 Agent 调用轮次的运行时上下文。

    Attributes:
        thread_id: 已 committed 的会话线程 ID
        trace_id: 链路追踪 ID
        source: 调用执行者标签（见上方 RUN_CTX_* 常量）。与 MessageSource
            （会话回合消息来源）不同轴：本字段标识「谁触发了这次工具执行」，
            仅作审计/诊断用途，不参与任何分支逻辑；为会话回合触发时复用
            其 MessageSource.value。
        turn_id: 当前轮次业务 turn_id（稳定审计标识，非 DB 主键）
        turn_record_id: 当前 TurnRecord 数据库主键（用于 FK 关联）
        execution_mode: 记忆写入失败语义控制（"user_required" | "system_best_effort"）
        source_event_ids: 当前 Turn 中已持久化的真实 Event.id 列表
        memory_tool_calls: Kernel 强制的单轮 Agent-Initiated Recall 预算计数
        project_id / workspace_id / environment_id: Scope Chain 高层 scope（V2 §九）。
            当前运行时尚未独立追踪这些实体，由 chat 入口在可用时填充；
            未填充时留空，scope 链退化为 thread → global，但扩展点完整保留。
        capability_ids: 当前活跃 capability（MCP）列表，由 Runtime 从 capabilities
            表解析并注入，是 Scope Chain 中唯一已有真实数据源的高层 scope。
    """
    thread_id: str
    trace_id: str
    source: str = RUN_CTX_USER_CHAT
    turn_id: str = ""  # 当前轮次业务 turn_id
    turn_record_id: str = ""  # TurnRecord 数据库主键
    execution_mode: str = "system_best_effort"  # "user_required" | "system_best_effort"
    source_event_ids: list[str] = field(default_factory=list)  # 真实 Event.id
    memory_tool_calls: int = 0  # Kernel-enforced per-turn Agent-Initiated Recall budget
    project_id: str | None = None
    workspace_id: str | None = None
    capability_ids: list[str] = field(default_factory=list)
    environment_id: str | None = None
    # Persistent Task Runtime 身份；不进入 LLM tool schema。
    task_id: str = ""
    agent_run_id: str = ""
    action_id: str = ""

    @property
    def is_valid(self) -> bool:
        """thread_id 和 trace_id 均非空。"""
        return bool(self.thread_id and self.trace_id)

    def to_scope_context(self) -> ScopeContext:
        """将 RunContext 的 scope 信息投影为召回用的 ScopeContext（V2 §九）。

        这是 Scope Chain 的单一出口：Automatic Recall 与 Agent-Initiated Recall
        工具均通过它构造 ScopeContext，确保 Runtime 是唯一授权来源。
        """
        return ScopeContext(
            thread_id=self.thread_id,
            project_id=self.project_id,
            workspace_id=self.workspace_id,
            capability_ids=list(self.capability_ids),
            environment_id=self.environment_id,
        )
