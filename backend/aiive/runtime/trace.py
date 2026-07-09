"""
运行时层 - 链路追踪。

提供轻量级的 Trace 数据类，为每次 Agent 运行生成唯一的 trace_id，
用于关联同一次交互中的所有事件、工具调用和日志。
"""
import uuid
from dataclasses import dataclass, field


@dataclass
class Trace:
    """链路追踪数据类：封装一次 Agent 交互的唯一标识。

    Attributes:
        trace_id: 唯一追踪 ID（默认自动生成 UUID4）
    """
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @staticmethod
    def new() -> "Trace":
        """创建新的 Trace 实例。"""
        return Trace()
