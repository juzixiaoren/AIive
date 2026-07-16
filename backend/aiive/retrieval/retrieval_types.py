"""Phase 5：统一检索数据类型（纯 Pydantic，无 DB 访问）。

所有检索 route 最终返回统一结构 RetrievalHit，便于去重、预算打包与
证据追溯。token_count 必须由真实 TokenCounter 计算。
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RetrievalMode(str, Enum):
    """检索模式：自动召回 / 工具显式检索 / 深度原始回溯。"""

    AUTO = "auto"
    SEARCH = "search"
    DEEP = "deep"


class RetrievalSourceType(str, Enum):
    """可被统一索引的源类型（v1 不含原始事件）。"""

    MEMORY_RECORD = "memory_record"
    SEGMENT_SUMMARY = "segment_summary"
    EPOCH_CHECKPOINT = "epoch_checkpoint"


class RetrievalRequest(BaseModel):
    """一次统一检索请求。"""

    query: str = ""
    mode: RetrievalMode = RetrievalMode.AUTO
    # ScopeContext（运行时提供，模型只缩窄不扩权）
    scope_context: Any | None = None
    thread_id: str | None = None
    source_types: list[str] | None = None
    include_sleeping: bool = False
    # archived 源仅在 SEARCH/DEEP + include_archived=true 时返回；AUTO 永远过滤
    include_archived: bool = False
    # 上下文装配去重：排除这些 source_id（通常是 legacy 热分区已加载的最近摘要/检查点）
    exclude_source_ids: set[str] | None = None
    max_results: int | None = None
    token_budget: int | None = None
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


class RetrievalHit(BaseModel):
    """统一检索命中。每条命中可定位回真实源记录。"""

    source_type: str
    source_id: str
    source_version: str
    retrieval_tier: str = "warm"
    lifecycle_state: str = ""

    score: float = 0.0
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    recency_score: float = 0.0
    authority_score: float = 0.0
    lifecycle_score: float = 0.0
    final_score: float = 0.0

    title: str = ""
    snippet: str = ""
    canonical_key: str | None = None
    scope_type: str = "global"
    scope_id: str | None = None
    thread_id: str | None = None
    epoch_id: str | None = None
    segment_id: str | None = None
    memory_record_id: str | None = None

    provenance: dict[str, Any] = Field(default_factory=dict)
    retrieval_reason: str = ""
    token_count: int = 0
    route: str = ""

    # 仅 deep 模式：二阶段原始回溯命中（不在索引中）
    is_raw: bool = False


class RetrievalResult(BaseModel):
    """一次统一检索的结果。"""

    request_id: str
    hits: list[RetrievalHit] = Field(default_factory=list)
    token_count: int = 0
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)
