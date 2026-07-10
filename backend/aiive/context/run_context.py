"""
请求级上下文：替代模块级全局变量 _current_thread_id。

所有工具 handler、MemoryWriteService、EventLogger 等需要 thread/trace
信息时，统一从 RunContext 获取。

RunContext 通过 _make_handler 闭包捕获 → registry.execute() 注入 handler，
全程不经过 LLM tool schema，模型不可见。
"""
from dataclasses import dataclass


@dataclass
class RunContext:
    """单次 Agent 调用轮次的运行时上下文。

    Attributes:
        thread_id: 已 committed 的会话线程 ID
        trace_id: 链路追踪 ID
        source: 调用来源（"user_chat"、"system_reminder"、"outbox_worker" 等）
    """
    thread_id: str
    trace_id: str
    source: str = "user_chat"

    @property
    def is_valid(self) -> bool:
        """thread_id 和 trace_id 均非空。"""
        return bool(self.thread_id and self.trace_id)
