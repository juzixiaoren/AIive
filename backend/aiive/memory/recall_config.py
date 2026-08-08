"""Configured budgets and weights for the V2 recall architecture.

No magic numbers scattered in business code — all knobs live here and are
overridable per-process.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RecallConfig:
    """All recall-side budgets, weights, and limits."""

    # --- Core Memory Blocks ---
    core_memory_total_token_budget: int = 600
    core_memory_max_blocks: int = 3

    # --- Automatic Recall ---
    automatic_recall_top_k: int = 8
    automatic_recall_token_budget: int = 1200
    recall_relevance_threshold: float = 0.08  # lowered for CJK bigram noise

    # --- Agent-Initiated Recall limits (Kernel enforcement) ---
    max_memory_tool_calls_per_turn: int = 6
    max_total_recall_token_budget: int = 2400
    memory_tool_top_k: int = 8
    memory_tool_token_budget: int = 1000

    # --- Route / fusion ---
    vector_top_k: int = 16
    route_timeout_ms: int = 500
    rrf_k: int = 60
    route_weights: dict[str, float] = field(default_factory=lambda: {
        "rrf": 0.40,
        "relevance": 0.30,
        "scope": 0.15,
        "recency": 0.05,
        "importance": 0.05,
        "trust": 0.05,
    })

    # --- Core projection refresh debounce ---
    core_projection_refresh_debounce_s: int = 5

    # --- Phase 3: Epoch rollover 阈值 ---
    max_segments_per_epoch: int = 5
    # --- Phase 3: Idle Scanner / 摘要加载 ---
    idle_threshold_seconds: int = 900          # 15 分钟无活动视为空闲
    min_segment_turn_records: int = 3          # 未标记 pending_seal 时的最小 Turn 数
    # 长期持续活跃会话不能只依赖 idle/上下文软阈值，否则单个压缩输入会无限增长。
    max_segment_turn_records: int = 32
    max_segment_summaries: int = 5             # ContextAssembler 加载的最近 Summary 数


# ============================================================================
# Phase 5: 统一检索（RetrievalIndex）预算、权重与 generation 策略
# ============================================================================


@dataclass
class RetrievalConfig:
    """Phase 5 统一检索的全部可调阈值、权重与 generation 策略。"""

    # --- 分区 token 预算 ---
    retrieved_history_summary_token_budget: int = 800
    deep_history_raw_token_budget: int = 1500

    # --- auto 模式（ContextAssembler 自动召回）---
    auto_max_results: int = 8
    auto_token_budget: int = 1000

    # --- search 模式（工具显式检索）---
    search_max_results: int = 20
    search_token_budget: int = 2000

    # --- deep 模式（二阶段原始回溯）---
    deep_max_results: int = 8
    deep_max_turns: int = 40

    # --- 确定性融合权重（配置化，不量纲混加）---
    retrieval_route_weights: dict[str, float] = field(default_factory=lambda: {
        "rrf": 0.30,
        "lexical": 0.30,
        "exact": 0.20,
        "scope": 0.10,
        "recency": 0.05,
        "authority": 0.05,
    })
    retrieval_relevance_threshold: float = 0.05
    rrf_k: int = 60

    # --- generation / 策略版本 ---
    policy_version: str = "phase5.v1"
    include_sleeping_auto: bool = False


# ============================================================================
# Phase 4: 长期记忆生命周期维护（Daily Dream）阈值与节奏
# ============================================================================


@dataclass
class MaintenanceConfig:
    """Phase 4 维护策略的全部可调阈值与节奏，集中于此、可测试、可覆盖。

    缺省值见确认项：candidate 晋升置信度 0.7、candidate TTL 30 天、
    importance 冷却阈值 0.3、normal cooling 30 天、user_required cooling 90 天。
    """

    # --- 触发节奏 ---
    daily_interval_hours: int = 24                 # Daily 触发最小间隔
    maintenance_scan_interval_seconds: int = 3600  # scheduler 轮询间隔
    idle_threshold_seconds: int = 900              # 复用 Phase 3 空闲阈值

    # --- 批次边界 ---
    max_maintenance_batch: int = 200               # 单批 seed 上限

    # --- 晋升 / 过期 ---
    candidate_promotion_confidence: float = 0.7    # candidate → active 置信度阈值
    candidate_promotion_min_evidence: int = 2      # 独立 user_message 证据最少条数
    candidate_ttl_days: int = 30                   # candidate 超过该时长无新证据则归档

    # --- 冷却 / 睡眠 ---
    importance_sleep_threshold: float = 0.3        # importance 低于此且长期未访问 → sleeping
    sleep_cooling_days: int = 30                   # normal 记忆冷却到 sleeping 的天数
    user_required_sleep_cooling_days: int = 90     # user_required+normal 的更长冷却天数

    # --- AccessTracker ---
    min_access_touch_interval_seconds: int = 60    # 同记忆最小 touch 间隔，去重
    access_tracker_batch_size: int = 50            # 单次 touch 短事务批量大小

    # --- 策略版本 ---
    policy_version: str = "phase4.v1"

ENABLED_OUTBOX_JOB_TYPES: frozenset[str] = frozenset({
    "memory_extraction",
    "core_memory_refresh",
    "memory_vector_refresh",
    "memory_markdown_project",
    # 提醒投递：到期任务必须由 Agent 生成真实回复
    "reminder_delivery",
    # Phase 3: Segment sealing / Epoch rollover / Epoch checkpoint
    "segment_sealing",
    "epoch_rollover",
    "epoch_checkpoint",
    # Phase 4: 长期记忆生命周期维护
    "memory_maintenance",
    # Phase 5: 统一检索索引刷新 / 全量重建
    "retrieval_index_refresh",
    "retrieval_index_rebuild",
    # Phase 6A: Forget Saga
    "forget_cascade",
    "forget_rebuild_dependencies",
    "forget_purge",
    "forget_verify",
    "forget_reconcile",
    # Phase 6B: Retention Cleanup
    "retention_cleanup",
    # 持久化副作用工具统一执行
    "tool_operation",
})
