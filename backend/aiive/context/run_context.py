"""
请求级上下文：替代模块级全局变量 _current_thread_id。

所有工具 handler、MemoryWriteService、EventLogger 等需要 thread/trace
信息时，统一从 RunContext 获取。

RunContext 通过 _make_handler 闭包捕获 → registry.execute() 注入 handler，
全程不经过 LLM tool schema，模型不可见。
"""
from dataclasses import dataclass, field

from aiive.memory.recall_models import ScopeContext


@dataclass
class RunContext:
    """单次 Agent 调用轮次的运行时上下文。

    Attributes:
        thread_id: 已 committed 的会话线程 ID
        trace_id: 链路追踪 ID
        source: 调用来源（"user_chat"、"system_reminder"、"outbox_worker" 等）
        memory_tool_calls: Kernel 强制的单轮 Agent-Initiated Recall 预算计数
        project_id / workspace_id / environment_id: Scope Chain 高层 scope（V2 §九）。
            当前运行时尚未独立追踪这些实体，由 chat 入口在可用时填充；
            未填充时留空，scope 链退化为 thread → global，但扩展点完整保留。
        capability_ids: 当前活跃 capability（MCP）列表，由 Runtime 从 capabilities
            表解析并注入，是 Scope Chain 中唯一已有真实数据源的高层 scope。
    """
    thread_id: str
    trace_id: str
    source: str = "user_chat"
    memory_tool_calls: int = 0  # Kernel-enforced per-turn Agent-Initiated Recall budget
    project_id: str | None = None
    workspace_id: str | None = None
    capability_ids: list[str] = field(default_factory=list)
    environment_id: str | None = None

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
