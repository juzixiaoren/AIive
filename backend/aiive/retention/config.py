"""
Phase 6B 保留策略版本化配置对象。

v1 不使用数据库表；通过代码内版本化对象管理策略。
每个 RetentionCleanupRun 启动时冻结 policy_snapshot。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetentionLaneConfig:
    """单条 lane 的保留策略。"""
    lane: str
    retention_days: int            # scrub 阶段保留天数
    delete_days: int | None = None # delete 阶段天数，None 表示不自动删结构
    batch_size: int = 500
    enabled: bool = True


@dataclass(frozen=True)
class RetentionPolicyConfig:
    """保留策略版本化配置对象（非 DB 表）。

    policy_version 每次修改 bump，各 lane 独立配置。
    """

    policy_version: int = 1

    lanes: list[RetentionLaneConfig] = field(default_factory=lambda: [
        RetentionLaneConfig(
            lane="outbox_job",
            retention_days=30,
            delete_days=180,
        ),
        RetentionLaneConfig(
            lane="maintenance_input",
            retention_days=30,
            delete_days=90,
        ),
        RetentionLaneConfig(
            lane="retrieval_generation",
            retention_days=30,
            delete_days=60,
        ),
        RetentionLaneConfig(
            lane="segment_summary",
            retention_days=60,
            delete_days=90,
        ),
        RetentionLaneConfig(
            lane="epoch_checkpoint",
            retention_days=60,
            delete_days=90,
        ),
        RetentionLaneConfig(
            lane="compaction_input",
            retention_days=90,
            delete_days=None,  # 仅 forget 且 Verifier 通过后删
        ),
        RetentionLaneConfig(
            lane="forget_action",
            retention_days=90,
            delete_days=180,
        ),
        RetentionLaneConfig(
            lane="retention_run",
            retention_days=30,
            delete_days=90,
        ),
        RetentionLaneConfig(
            lane="retention_batch",
            retention_days=30,
            delete_days=90,
        ),
    ])

    def lane_config(self, lane: str) -> RetentionLaneConfig | None:
        """按名称查找 lane 配置。"""
        for lc in self.lanes:
            if lc.lane == lane:
                return lc
        return None

    def to_snapshot(self) -> dict[str, Any]:
        """序列化为 policy_snapshot JSON。"""
        return {
            "policy_version": self.policy_version,
            "lanes": [
                {
                    "lane": lc.lane,
                    "retention_days": lc.retention_days,
                    "delete_days": lc.delete_days,
                    "batch_size": lc.batch_size,
                    "enabled": lc.enabled,
                }
                for lc in self.lanes
            ],
        }


# 默认 v1 实例
RETENTION_POLICY_V1 = RetentionPolicyConfig()
