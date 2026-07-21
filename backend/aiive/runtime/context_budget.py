"""Phase 1: ContextBudget 配置。

所有分区上限在此定义，业务代码中无魔术数字。
"""
from __future__ import annotations

import json as _json
import logging
import os
from dataclasses import dataclass
from typing import ClassVar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PartitionBudget:
    """单个上下文分区的 token 预算。"""
    name: str
    soft_limit_tokens: int
    hard_limit_tokens: int
    priority: int
    description: str = ""


@dataclass(frozen=True)
class ContextBudget:
    """全局上下文预算，从 model context_window 切分。

    安全余量通过 TokenCount.safe_tokens 表达，不在此处作为独立分区。
    """
    model_context_window: int
    reserved_output: PartitionBudget
    stable_contract: PartitionBudget
    core_memory: PartitionBudget
    working_state: PartitionBudget
    tool_definitions: PartitionBudget
    recent_messages: PartitionBudget
    retrieved_memory: PartitionBudget
    tool_results: PartitionBudget
    # Phase 3 新增分区
    epoch_checkpoint: PartitionBudget
    segment_summaries: PartitionBudget
    sealing_bridge: PartitionBudget
    # Phase 5 新增分区
    retrieved_history_summary: PartitionBudget
    deep_history_raw: PartitionBudget

    @property
    def hard_input_limit(self) -> int:
        return self.model_context_window - self.reserved_output.hard_limit_tokens

    @property
    def soft_input_limit(self) -> int:
        return int(self.hard_input_limit * 0.80)

    @property
    def all_partitions(self) -> tuple[PartitionBudget, ...]:
        return (
            self.stable_contract, self.core_memory, self.working_state,
            self.tool_definitions, self.recent_messages, self.retrieved_memory,
            self.tool_results, self.epoch_checkpoint,
            self.segment_summaries, self.sealing_bridge,
            self.retrieved_history_summary, self.deep_history_raw,
        )

    def validate(self) -> None:
        total = sum(p.hard_limit_tokens for p in self.all_partitions)
        total += self.reserved_output.hard_limit_tokens
        if total > self.model_context_window:
            raise ValueError(
                f"分区 hard limits ({total}) 超过 "
                + f"context window ({self.model_context_window})"
            )

    # ── 默认实例：窗口值取自 settings.aiive_llm_context_window ──
    DEFAULT: ClassVar["ContextBudget"]

    @classmethod
    def default(cls) -> "ContextBudget":
        """构建默认预算：窗口值取自配置，recent_messages 弹性吸收剩余空间。

        除 recent_messages 外的分区为固定绝对值（不随窗口缩放），
        recent_messages 的 hard = 窗口 - 其余全部分区 hard 之和，
        soft = hard 的约 87%。这样用户在 .env 调整 aiive_llm_context_window
        后预算自动自洽，validate() 恒通过。
        """
        from aiive.config import settings

        window = settings.aiive_llm_context_window
        reserved_output_hard = settings.aiive_llm_max_output_tokens

        # 固定分区（除 recent_messages）的 hard 之和，含 reserved_output
        fixed_hard = (
            reserved_output_hard
            + 4000   # stable_contract
            + 600    # core_memory
            + 2000   # working_state
            + 6000   # tool_definitions
            + 1200   # retrieved_memory
            + 8000   # tool_results
            + 800    # epoch_checkpoint
            + 2000   # segment_summaries
            + 2300   # sealing_bridge
            + 1000   # retrieved_history_summary
            + 1500   # deep_history_raw
        )
        # recent_messages 吸收剩余空间；给极小窗口留一个下限兜底
        recent_hard = max(4000, window - fixed_hard)
        recent_soft = int(recent_hard * 0.87)

        return cls(
            model_context_window=window,
            reserved_output=PartitionBudget("reserved_output", -1, reserved_output_hard, -1, "保留输出 token"),
            stable_contract=PartitionBudget("stable_contract", 3500, 4000, 1, "内核契约"),
            core_memory=PartitionBudget("core_memory", 500, 600, 2, "核心记忆块"),
            working_state=PartitionBudget("working_state", 1500, 2000, 3, "结构化工作状态"),
            tool_definitions=PartitionBudget("tool_definitions", 4000, 6000, 4, "工具 Schema"),
            recent_messages=PartitionBudget("recent_messages", recent_soft, recent_hard, 5, "近期 Turn 消息"),
            retrieved_memory=PartitionBudget("retrieved_memory", 1000, 1200, 6, "召回记忆"),
            tool_results=PartitionBudget("tool_results", 5000, 8000, 7, "工具结果"),
            epoch_checkpoint=PartitionBudget("epoch_checkpoint", 500, 800, 8, "最近 Epoch 检查点"),
            segment_summaries=PartitionBudget("segment_summaries", 1200, 2000, 9, "近期 Segment 摘要"),
            sealing_bridge=PartitionBudget("sealing_bridge", 1500, 2300, 10, "密封中 Segment 原始尾部桥接"),
            retrieved_history_summary=PartitionBudget("retrieved_history_summary", 800, 1000, 11, "统一检索命中的历史摘要/检查点"),
            deep_history_raw=PartitionBudget("deep_history_raw", 0, 1500, 12, "deep 模式原始历史回溯（auto 模式不使用）"),
        )

    @classmethod
    def from_env(cls) -> "ContextBudget":
        """从环境变量读取可选的分区覆盖，返回（可能定制后的）预算。

        通过 AIIVE_CONTEXT_BUDGET_JSON 提供 JSON 覆盖，例如:
          {"tool_results": {"soft_limit_tokens": 3000, "hard_limit_tokens": 5000}}
        未设置或解析失败时回退到默认预算，不影响既有行为。
        """
        raw = os.environ.get("AIIVE_CONTEXT_BUDGET_JSON")
        if not raw:
            return cls.default()
        try:
            overrides = _json.loads(raw)
        except (_json.JSONDecodeError, TypeError):
            logger.warning("AIIVE_CONTEXT_BUDGET_JSON 解析失败，使用默认预算")
            return cls.default()

        base = cls.default()
        if not isinstance(overrides, dict):
            return base
        new_partitions = {p.name: p for p in base.all_partitions}
        for p in base.all_partitions:
            ov = overrides.get(p.name)
            if isinstance(ov, dict):
                new_partitions[p.name] = PartitionBudget(
                    name=p.name,
                    soft_limit_tokens=int(ov.get("soft_limit_tokens", p.soft_limit_tokens)),
                    hard_limit_tokens=int(ov.get("hard_limit_tokens", p.hard_limit_tokens)),
                    priority=p.priority,
                    description=p.description,
                )
        return cls(
            model_context_window=base.model_context_window,
            reserved_output=base.reserved_output,
            stable_contract=new_partitions["stable_contract"],
            core_memory=new_partitions["core_memory"],
            working_state=new_partitions["working_state"],
            tool_definitions=new_partitions["tool_definitions"],
            recent_messages=new_partitions["recent_messages"],
            retrieved_memory=new_partitions["retrieved_memory"],
            tool_results=new_partitions["tool_results"],
            epoch_checkpoint=new_partitions["epoch_checkpoint"],
            segment_summaries=new_partitions["segment_summaries"],
            sealing_bridge=new_partitions["sealing_bridge"],
            retrieved_history_summary=new_partitions["retrieved_history_summary"],
            deep_history_raw=new_partitions["deep_history_raw"],
        )


ContextBudget.DEFAULT = ContextBudget.default()
