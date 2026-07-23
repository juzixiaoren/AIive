"""统一的回合执行上下文（方案 C 单一事实源）。

集中携带一个回合执行所需的全部可信元数据，避免 message_source /
thread_id / turn_id / turn_record_id / execution_id / trace_id 在
TurnExecutionService、AgentGraph、工具 RunContext 与记忆策略之间分散传递，
从而杜绝任一阶段漏传导致来源静默退化为 user。

该结构为不可变（frozen），构造后不可修改，确保同一回合内各处读取到的
元数据始终一致，并让图执行接口从「多个可独立省略的形参」收敛为「必须整体
提供的上下文对象」——任何调用方遗漏来源都会在构造处立即暴露。
"""
from __future__ import annotations

from dataclasses import dataclass

from aiive.memory.extraction_policy import MessageSource


@dataclass(frozen=True)
class TurnExecutionContext:
    """单个回合执行的不可变元数据集合（单一事实源）。

    Attributes:
        message_source: 由服务端入口确定的可信消息来源（user /
            system_command / runtime_event），客户端正文不能改变该值。
        thread_id: 已 committed 的会话线程 ID。
        turn_id: 当前轮次业务 turn_id（稳定审计标识，非 DB 主键）。
        turn_record_id: 当前 TurnRecord 数据库主键（用于 FK 关联）。
        execution_id: 本轮执行的唯一 ID（用于租约 / 心跳 / 幂等）。
        trace_id: 链路追踪 ID。
    """

    message_source: MessageSource
    thread_id: str
    turn_id: str
    turn_record_id: str
    execution_id: str
    trace_id: str
