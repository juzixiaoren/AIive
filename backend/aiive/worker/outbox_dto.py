"""Phase 0.5B: Outbox/Ingestion DTOs and enums."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


# ============================================================================
# HandlerResult / HandlerOutcome
# ============================================================================


class HandlerOutcome(StrEnum):
    """Handler 执行结果枚举。"""
    COMPLETED = "completed"
    RETRY_LATER = "retry_later"
    NON_RETRYABLE = "non_retryable"
    RETRYABLE_ERROR = "retryable_error"
    CLAIM_LOST = "claim_lost"


@dataclass(frozen=True)
class HandlerResult:
    """Handler 执行结果，指导 Worker 如何更新 OutboxJob。"""
    outcome: HandlerOutcome
    reason: str = ""
    retry_available_at: datetime | None = None
    ingestion_run_id: str = ""
    terminal_reason: str = ""


# ============================================================================
# ClaimedJob
# ============================================================================


@dataclass(frozen=True)
class ClaimedJob:
    """claim 成功后从 ORM 对象复制的不可变 DTO。"""
    id: str
    job_type: str
    payload: dict[str, Any]
    trace_id: str | None
    retry_count: int
    max_retries: int
    claim_token: str
    schema_version: int
    worker_id: str


# ============================================================================
# ActiveClaim
# ============================================================================


@dataclass
class ActiveClaim:
    """活跃 Claim 记录。"""
    job_id: str
    claim_token: str
    worker_id: str
    started_at: datetime
    lost: bool = False


# ============================================================================
# ValidatedExtractionSource
# ============================================================================


@dataclass(frozen=True)
class ValidatedExtractionSource:
    """Phase A 验证后的纯数据对象。"""
    turn_record_id: str
    thread_id: str
    turn_id: str


# ============================================================================
# IngestionRunResolution
# ============================================================================


@dataclass
class IngestionRunResolution:
    """Phase A IngestionRun 解析结果。"""
    decision: str  # already_succeeded | deadletter | busy | acquired | takeover
    run_id: str = ""
    lease_expires_at: datetime | None = None
    previous_status: str = ""


# ============================================================================
# FencingViolationError / NonRetryableJobError / MemoryBatchWriteError
# ============================================================================


class FencingViolationError(Exception):
    """claim_token / execution_token fencing 失败。"""


class NonRetryableJobError(Exception):
    """Job 不可重试（source Turn 验证失败、schema 不支持等）。"""


class MemoryBatchWriteError(Exception):
    """write_batch 中某个 Proposal 不可恢复失败。"""
