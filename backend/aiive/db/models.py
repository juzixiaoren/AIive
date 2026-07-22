"""
模块功能说明：
- 数据库 ORM 模型定义模块，使用 SQLAlchemy 2.0 Declarative API
- 定义所有数据表结构：Thread、Event、LLMCall、ContextSnapshot、MemoryRecord
- 以及 Capability、Task、OutboxJob、Document 等支撑功能表
- 每个模型通过 Mapped 和 mapped_column 声明字段类型和约束
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from typing import Any


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类，所有 ORM 模型均继承此类。"""
    pass


def _utcnow() -> datetime:
    """返回当前 UTC 时间，作为时间戳字段的默认值工厂函数。"""
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    """生成 UUID4 字符串，作为主键字段的默认值工厂函数。"""
    return str(uuid.uuid4())


class Thread(Base):
    """对话线程模型：代表一个完整的对话会话。"""
    __tablename__: str = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    # 最后活动时间：Idle Scanner（Phase 3）与维护 Idle 触发（Phase 4）据此判定空闲线程。
    # 缺省为 NULL（即“未观测到活动”），NULL 不参与 `last_activity_at <= cutoff` 过滤。
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    events: Mapped[list["Event"]] = relationship(back_populates="thread", cascade="all, delete-orphan")
    llm_calls: Mapped[list["LLMCall"]] = relationship(back_populates="thread", cascade="all, delete-orphan")
    context_snapshots: Mapped[list["ContextSnapshot"]] = relationship(back_populates="thread", cascade="all, delete-orphan")


class Event(Base):
    """事件模型：记录对话中的各类事件（用户消息、工具调用、系统事件等）。"""
    __tablename__: str = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    turn_event_index: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped["Thread"] = relationship(back_populates="events")

    __table_args__: tuple[Index, ...] = (
        Index("ix_events_thread_created", "thread_id", "created_at"),
        Index("ix_events_turn_order", "thread_id", "turn_id", "turn_event_index"),
    )


class LLMCall(Base):
    """LLM 调用记录模型：记录每次大模型 API 调用的元信息。"""
    __tablename__: str = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), index=True)
    model: Mapped[str] = mapped_column(String(128))
    latency_ms: Mapped[float] = mapped_column()
    input_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 1: token estimation tracking (observability only)
    estimated_prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    safe_prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    actual_prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    actual_completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    token_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped["Thread"] = relationship(back_populates="llm_calls")


class TurnRecord(Base):
    """Turn 幂等记录：一个用户轮次的完整生命周期。

    状态机：not_started → running → (completed | interrupted_unknown)
    原子抢占：UPDATE WHERE status='not_started' → affected_rows=1 方可进入。
    """
    __tablename__: str = "turn_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_started")
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_no: Mapped[int] = mapped_column(default=1)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Phase 1: immutable epoch/segment attribution (nullable for legacy, enforced by app)
    epoch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    segment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("thread_id", "turn_id", name="uq_turn_record_thread_turn"),
        Index("ix_turn_records_thread_status", "thread_id", "status"),
        Index("ix_turn_records_thread_sequence", "thread_id", "turn_sequence"),
    )


class ApprovalRequest(Base):
    """工具审批请求：保存不可由客户端修改的原始调用和执行状态。"""
    __tablename__: str = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), nullable=False)
    turn_record_id: Mapped[str] = mapped_column(String(36), ForeignKey("turn_records.id"), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_args: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tool_args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index | CheckConstraint, ...] = (
        UniqueConstraint("turn_record_id", "tool_call_id", name="uq_approval_turn_tool_call"),
        Index("ix_approval_thread_status", "thread_id", "status"),
        Index("ix_approval_turn_record", "turn_record_id"),
        CheckConstraint(
            "status IN ('pending','executing','succeeded','denied','failed','interrupted_unknown')",
            name="ck_approval_status",
        ),
    )


class ContextSnapshot(Base):
    """上下文快照模型：保存每次对话的上下文构建结果，用于调试和审计。"""
    __tablename__: str = "context_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), index=True)
    stable_prefix_hash: Mapped[str] = mapped_column(String(32))
    context_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Phase 1 additions
    epoch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    segment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    turn_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    retention: Mapped[str] = mapped_column(String(32), default="temporary")
    token_total: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped["Thread"] = relationship(back_populates="context_snapshots")

    __table_args__: tuple[Index, ...] = (
        Index("ix_snapshots_trace", "trace_id", "thread_id"),
        Index("ix_snapshots_thread_retention", "thread_id", "retention"),
    )


class MemoryRecord(Base):
    """记忆记录模型：持久化存储用户的各类记忆，支持生命周期管理和版本控制。

    memory_records 是记忆生命周期的唯一真相源。
    lifecycle_state (candidate/active/sleeping/archived/forgotten) 与
    validity_state (valid/superseded/contradicted/expired) 是两个独立维度。
    """
    __tablename__: str = "memory_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    memory_type: Mapped[str] = mapped_column(String(64), index=True)
    canonical_key: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    cardinality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scope_type: Mapped[str] = mapped_column(
        String(32), default="global", index=True
    )
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    structured_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    validity_state: Mapped[str] = mapped_column(
        String(32), default="valid"
    )
    trust_level: Mapped[str] = mapped_column(String(32), default="semi_trusted")
    stability: Mapped[str] = mapped_column(
        String(32), default="contextual"
    )
    sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, default="normal", server_default="normal"
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    structured_value_hash: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    stability_score: Mapped[float | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.5)
    importance: Mapped[float] = mapped_column(default=0.5)
    record_version: Mapped[int] = mapped_column(default=1)
    reinforce_count: Mapped[int] = mapped_column(default=0)
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pinned: Mapped[bool] = mapped_column(default=False)
    revision_num: Mapped[int] = mapped_column("revision_num", default=1)
    supersedes: Mapped[str | None] = mapped_column(String(36), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_policy: Mapped[str] = mapped_column(
        String(32), default="normal"
    )  # "ephemeral" | "normal" | "pinned"
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_reinforced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 4：最近一次被注入上下文的时间。nullable、无默认、不历史回填；
    # 只有 MemoryAccessTracker 在记忆实际进入上下文时写入，且不会更新 updated_at。
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "sensitivity IN ('normal','personal','confidential','secret')",
            name="ck_memory_records_sensitivity",
        ),
    )


class MemoryVectorProjection(Base):
    """记忆的 pgvector 派生投影；MemoryRecord 仍是唯一真相源。"""
    __tablename__: str = "memory_vector_projections"

    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True,
    )
    record_version: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(1536).with_variant(JSON(), "sqlite"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class Capability(Base):
    """能力注册模型：记录 Agent 的各类能力及其状态。"""
    __tablename__: str = "capabilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    capability_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(
        String(32), default="candidate", index=True
    )
    descriptor_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CapabilityVersion(Base):
    """能力版本模型：记录每个能力的版本变更历史。"""
    __tablename__: str = "capability_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    capability_id: Mapped[str] = mapped_column(String(36), ForeignKey("capabilities.id"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    descriptor_hash: Mapped[str] = mapped_column(String(32))
    tool_list_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    smoke_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class MCPInstallRecord(Base):
    """MCP 安装记录模型：记录 MCP 服务器的安装信息。"""
    __tablename__: str = "mcp_install_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    capability_id: Mapped[str] = mapped_column(String(36), ForeignKey("capabilities.id"), index=True)
    server_name: Mapped[str] = mapped_column(String(256))
    package_ref: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(64))
    transport: Mapped[str] = mapped_column(String(32))
    declared_tools: Mapped[list[Any]] = mapped_column(JSON, default=list)
    sandbox_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class CapabilityPlan(Base):
    """能力安装计划：记录 MCP 自举的完整生命周期。"""
    __tablename__: str = "capability_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    goal: Mapped[str] = mapped_column(Text)
    goal_summary: Mapped[str | None] = mapped_column(String(256), nullable=True)
    missing_capability_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    risk_scores: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    selected_candidate: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    install_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    smoke_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    capability_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lesson_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class SelfDevRequest(Base):
    """自进化请求模型：Agent 自行提出的代码修改请求。"""
    __tablename__: str = "selfdev_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    goal: Mapped[str] = mapped_column(Text)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class PatchOperation(Base):
    """补丁操作模型：记录自进化请求对应的代码修改操作。"""
    __tablename__: str = "patch_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    request_id: Mapped[str] = mapped_column(String(36), ForeignKey("selfdev_requests.id"), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    target_file: Mapped[str] = mapped_column(String(512))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_schema_change: Mapped[bool] = mapped_column(default=False)
    safe_delete_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    not_allowed_yet: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class OutboxJob(Base):
    """发件箱任务模型：用于异步任务调度，支持重试和幂等。"""
    __tablename__: str = "outbox_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    operation_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    retry_count: Mapped[int] = mapped_column(default=0)
    max_retries: Mapped[int] = mapped_column(default=3)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 0.5B 新增
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schema_version: Mapped[int] = mapped_column(default=1)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Phase 6B: 终态不可变时间，completed/deadletter/non-retryable 终态路径原子写入一次
    # claim / retry / payload scrub 不得更新此字段
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    migration_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[Index, ...] = (
        Index("ix_outbox_claim", "status", "available_at", "lease_expires_at"),
        Index("ix_outbox_migration_batch", "migration_batch_id"),
    )


class ToolOperation(Base):
    """副作用工具操作：持久化请求、执行状态、幂等身份和真实终态。"""
    __tablename__: str = "tool_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), nullable=False)
    turn_record_id: Mapped[str] = mapped_column(String(36), ForeignKey("turn_records.id"), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    effect_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    outbox_job_id: Mapped[str] = mapped_column(String(36), ForeignKey("outbox_jobs.id"), nullable=False, unique=True)
    execution_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    effect_receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index | CheckConstraint, ...] = (
        UniqueConstraint("turn_record_id", "tool_call_id", name="uq_tool_operation_turn_call"),
        Index("ix_tool_operation_thread_status", "thread_id", "status"),
        CheckConstraint(
            "status IN ('queued','running','committed','failed','execution_unknown')",
            name="ck_tool_operation_status",
        ),
        CheckConstraint(
            "effect_mode IN ('db_transactional','externally_reconcilable','non_repeatable_external')",
            name="ck_tool_operation_effect_mode",
        ),
    )


class Document(Base):
    """知识文档模型：持久化原文对象引用、来源信息和索引状态。"""
    __tablename__: str = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source_path: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    object_bucket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_size: Mapped[int | None] = mapped_column(nullable=True)
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(32), default="text")
    status: Mapped[str] = mapped_column(String(32), default="indexed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('indexed','index_failed','source_unavailable')",
            name="ck_documents_status",
        ),
    )


class Chunk(Base):
    """文档分块模型：存储文档的语义分块内容。"""
    __tablename__: str = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    line_start: Mapped[int] = mapped_column()
    line_end: Mapped[int] = mapped_column()
    chunk_index: Mapped[int] = mapped_column()
    token_estimate: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RetrievalRun(Base):
    """检索运行模型：记录每次知识检索的执行信息。"""
    __tablename__: str = "retrieval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    query: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(String(32), default="hybrid")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RetrievalCandidate(Base):
    """检索候选模型：记录每次检索返回的候选文档块。"""
    __tablename__: str = "retrieval_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("retrieval_runs.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(36))
    source_type: Mapped[str] = mapped_column(String(32))
    score: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemoryProposal(Base):
    """记忆提案模型：持久化所有记忆写入的提案记录，支持审计与重放。

    每次记忆提取、工具调用或维护操作产生的 MemoryProposal 在此持久化。
    """
    __tablename__: str = "memory_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    proposal_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_event_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    memory_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    trust_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sensitivity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    importance: Mapped[float | None] = mapped_column(nullable=True)
    stability: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stability_score: Mapped[float | None] = mapped_column(nullable=True)
    proposed_operation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_operation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requires_confirmation: Mapped[bool] = mapped_column(default=False)
    extractor_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extractor_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    normalized_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Phase 0.5B 新增
    source_turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ingestion_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    proposal_index: Mapped[int | None] = mapped_column(nullable=True)
    # Phase 2: execution mode + retention + durability
    execution_mode: Mapped[str] = mapped_column(
        String(32), default="system_best_effort"
    )  # "user_required" | "system_best_effort"
    retention_policy: Mapped[str] = mapped_column(
        String(32), default="normal"
    )  # "ephemeral" | "normal" | "pinned"
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    durable: Mapped[bool] = mapped_column(default=True)
    source_turn_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("ingestion_run_id", "proposal_index",
                         name="uq_memory_proposal_run_index"),
    )


class MemoryIngestionRun(Base):
    """记忆提取运行记录：跟踪每次异步 memory_extraction 的完整生命周期。

    execution_token 复用 OutboxJob.claim_token。
    无独立租约 —— 活跃性通过 OutboxJob 判断。
    """
    __tablename__: str = "memory_ingestion_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source_turn_record_id: Mapped[str] = mapped_column(String(36), ForeignKey("turn_records.id"), nullable=False)
    extractor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    execution_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposal_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("source_turn_record_id", "extractor_name", "extractor_version",
                         name="uq_ingestion_run_source_extractor"),
    )


class MemoryEvidence(Base):
    """记忆证据模型：逐条记录记忆的证据来源和关系。

    支持 supports/contradicts/confirms/corrects/derived_from 五种关系。
    """
    __tablename__: str = "memory_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.id"), index=True
    )
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32))
    trust_level: Mapped[str] = mapped_column(String(32), default="semi_trusted")
    relation: Mapped[str] = mapped_column(String(32), default="supports")
    content_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class MemoryLineage(Base):
    """记忆谱系模型：正式记录记忆之间的演进关系。

    revise/supersede/merge 等操作在此创建正式谱系记录。
    memory_records 上的快捷字段仅作索引，本表为权威来源。
    """
    __tablename__: str = "memory_lineage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    predecessor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.id"), index=True
    )
    successor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.id"), index=True
    )
    operation: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Task(Base):
    """任务模型：记录定时任务、提醒、条件检查等异步任务。"""
    __tablename__: str = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AttentionState(Base):
    """注意力状态模型：记录 Agent 在每个线程中的关注焦点和决策状态。"""
    __tablename__: str = "attention_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), index=True)
    focus_topic: Mapped[str | None] = mapped_column(String(256), nullable=True)
    recent_topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(32), default="continue")
    suggestion: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CoreMemoryBlock(Base):
    """Core Memory 投影表：memory_records 的极小、稳定派生投影。

    memory_records 仍是事实与生命周期唯一真相源；本表只是可重建投影。
    只有 MemoryKeyRegistry 中显式声明 core_memory_role 的键才能参与。
    """
    __tablename__: str = "core_memory_blocks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    block_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    source_memory_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    projection_version: Mapped[int] = mapped_column(default=1)
    record_versions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    token_count: Mapped[int] = mapped_column(default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class MemoryRecallRun(Base):
    """召回运行记录：每次 Automatic / Agent-Initiated Recall 的可追溯记录。"""
    __tablename__: str = "memory_recall_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_query: Mapped[str] = mapped_column(Text)
    scope_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    routes_executed: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_budget: Mapped[int] = mapped_column(default=0)
    result_count: Mapped[int] = mapped_column(default=0)
    total_latency_ms: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemoryRecallCandidate(Base):
    """召回候选记录：每次召回的每个候选及其原始分、融合分、入选/排除原因。"""
    __tablename__: str = "memory_recall_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_recall_runs.id", ondelete="CASCADE"), index=True,
    )
    memory_id: Mapped[str] = mapped_column(String(36), index=True)
    route: Mapped[str] = mapped_column(String(32))
    raw_score: Mapped[float] = mapped_column(default=0.0)
    fused_score: Mapped[float] = mapped_column(default=0.0)
    selected: Mapped[bool] = mapped_column(default=False)
    exclusion_reason: Mapped[str] = mapped_column(String(64), default="")
    token_cost: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ============================================================================
# Phase 1: Epoch, Segment, WorkingState, Artifact
# ============================================================================


class Epoch(Base):
    """后端运行阶段。每个 Thread 可有多个 Epoch，自动轮换但用户无感。"""
    __tablename__: str = "epochs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), nullable=False, index=True)
    epoch_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    start_turn_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    end_turn_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("thread_id", "epoch_no", name="uq_epoch_thread_no"),
        Index("ix_epochs_thread_status", "thread_id", "status"),
    )


class Segment(Base):
    """Epoch 内一个可独立密封的工作片段。"""
    __tablename__: str = "segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    epoch_id: Mapped[str] = mapped_column(String(36), ForeignKey("epochs.id"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), nullable=False)
    segment_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    start_turn_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=False)
    end_turn_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pending_seal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sealed_by_turn: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__: tuple[UniqueConstraint | Index | CheckConstraint, ...] = (
        UniqueConstraint("epoch_id", "segment_no", name="uq_segment_epoch_no"),
        Index("ix_segments_epoch_status", "epoch_id", "status"),
        CheckConstraint("start_turn_sequence IS NOT NULL", name="ck_segment_start_turn"),
        CheckConstraint(
            "end_turn_sequence IS NULL OR end_turn_sequence >= start_turn_sequence",
            name="ck_segment_turn_range",
        ),
        # 合法 sealed 必须已生成 Summary（L.4 不变量）
        CheckConstraint(
            "status <> 'sealed' OR summary_id IS NOT NULL",
            name="ck_segment_sealed_requires_summary",
        ),
    )


class SegmentSummary(Base):
    """Segment 的 LLM 摘要（Phase 3 填充，Phase 1 仅建表）。"""
    __tablename__: str = "segment_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    segment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    open_loops: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    entities: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    artifacts: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    important_tool_results: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    source_turn_ids: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    source_event_ids: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    active_constraints: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    unresolved_failures: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    omitted_artifact_refs: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    source_turn_range: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_version: Mapped[int] = mapped_column(default=1)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("segment_id", "summary_version", name="uq_segment_summary_segment_version"),
    )


class EpochCheckpoint(Base):
    """Epoch 的工作检查点（Phase 3 填充，Phase 1 仅建表）。"""
    __tablename__: str = "epoch_checkpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    epoch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    current_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_milestones: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    open_loops: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    active_constraints: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    current_decisions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    referenced_artifacts: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    relevant_entities: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    latest_verified_tool_states: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    source_segment_ids: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    source_hashes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(default=1)
    token_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("epoch_id", "version", name="uq_epoch_checkpoint_epoch_version"),
    )


class WorkingState(Base):
    """当前操作上下文的 WorkingState。1:1 Thread。"""
    __tablename__: str = "working_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), nullable=False)
    epoch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_loops: Mapped[list[Any]] = mapped_column(JSON, default=list)
    active_constraints: Mapped[list[Any]] = mapped_column(JSON, default=list)
    pending_approvals: Mapped[list[Any]] = mapped_column(JSON, default=list)
    artifact_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verified_tool_states: Mapped[list[Any]] = mapped_column(JSON, default=list)
    uncommitted_side_effects: Mapped[list[Any]] = mapped_column(JSON, default=list)
    running_tool_state: Mapped[list[Any]] = mapped_column(JSON, default=list)
    applied_idempotency_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_count: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("thread_id", name="uq_working_state_thread"),
    )


class Artifact(Base):
    """大型工具结果或结构化数据的持久化存储。"""
    __tablename__: str = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), default="tool_result")
    ref: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(default=0)
    # provenance
    turn_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__: tuple[UniqueConstraint, ...] = (
        # 同一 Turn 内同一工具调用只允许一个 Artifact，防止重试重复创建
        UniqueConstraint("turn_record_id", "tool_call_id", name="uq_artifact_turn_tool"),
    )


# ============================================================================
# Phase 3: SegmentSummary / EpochCheckpoint 运行记录与不可变输入快照
# ============================================================================


class CompactionInput(Base):
    """Segment 压缩的不可变输入快照。

    在 begin_segment_sealing 同一事务中持久化，是 Phase B 内容校验与覆盖校验的
    唯一权威来源。turn_manifest / event_manifest 为完整清单，不再单独存储
    散列的 source_turn_ids / source_event_ids（由之派生）。
    """

    __tablename__: str = "compaction_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("segments.id"), nullable=False, index=True
    )
    start_turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_manifest: Mapped[list[Any]] = mapped_column(JSON, default=list)
    event_manifest: Mapped[list[Any]] = mapped_column(JSON, default=list)
    working_state_version: Mapped[int] = mapped_column(nullable=False)
    working_state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "segment_id", "source_hash", "summary_version",
            name="uq_compaction_input_segment_hash_version",
        ),
    )


class CompactionRun(Base):
    """Segment 压缩运行记录：一个 OutboxJob 固定对应一个 CompactionRun。"""

    __tablename__: str = "compaction_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("segments.id"), nullable=False, index=True
    )
    compaction_input_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compaction_inputs.id"), nullable=False
    )
    outbox_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox_jobs.id"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    execution_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint(
            "segment_id", "source_hash", "summary_version",
            name="uq_compaction_segment_hash_version",
        ),
        UniqueConstraint("outbox_job_id", name="uq_compaction_run_outbox_job"),
        Index("ix_compaction_segment_status", "segment_id", "status"),
    )


class EpochCompactionInput(Base):
    """Epoch rollover 边界的不可变 WorkingState 快照。"""

    __tablename__: str = "epoch_compaction_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    epoch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("epochs.id"), nullable=False, index=True
    )
    boundary_turn_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    working_state_version: Mapped[int] = mapped_column(nullable=False)
    current_objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_loops: Mapped[list[Any]] = mapped_column(JSON, default=list)
    active_constraints: Mapped[list[Any]] = mapped_column(JSON, default=list)
    artifact_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verified_tool_states: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_segment_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_hashes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "epoch_id", "snapshot_hash", "checkpoint_version",
            name="uq_epoch_input_epoch_hash_version",
        ),
    )


class CheckpointRun(Base):
    """EpochCheckpoint 运行记录：一个 OutboxJob 固定对应一个 CheckpointRun。"""

    __tablename__: str = "checkpoint_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    epoch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("epochs.id"), nullable=False, index=True
    )
    epoch_compaction_input_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("epoch_compaction_inputs.id"), nullable=False
    )
    outbox_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox_jobs.id"), nullable=False
    )
    boundary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    execution_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint(
            "epoch_id", "boundary_hash", "checkpoint_version",
            name="uq_checkpoint_epoch_hash_version",
        ),
        UniqueConstraint("outbox_job_id", name="uq_checkpoint_run_outbox_job"),
        Index("ix_checkpoint_epoch_status", "epoch_id", "status"),
    )


# ============================================================================
# Phase 4: 长期记忆生命周期维护（Daily Dream）
# ============================================================================


class MemoryMaintenanceRun(Base):
    """记忆维护运行记录：一个 `memory_maintenance` OutboxJob 固定对应一个 Run。

    - 单一全量 Run 覆盖所有 MemoryRecord（`scope_type=all_user_memories`）。
    - 与 `MemoryRecord.scope_type=global` 无关。
    - `claim_count` 仅计接管次数；`failure_attempt_count` 仅真正失败重试时累计，
      CONTINUE 分页不计入失败。
    - `cutoff_updated_at` 为固定快照上界（提交后不可变）；
      `cursor_updated_at`/`cursor_id` 为 changed lane 的续跑游标（仅 seed 推进）。
    """

    __tablename__: str = "memory_maintenance_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    scope_type: Mapped[str] = mapped_column(String(32), default="all_user_memories")
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outbox_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox_jobs.id"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    execution_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_count: Mapped[int] = mapped_column(default=0)
    failure_attempt_count: Mapped[int] = mapped_column(default=0)
    policy_version: Mapped[str] = mapped_column(String(32), default="phase4.v1")
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cutoff_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cursor_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_count: Mapped[int] = mapped_column(default=0)
    applied_count: Mapped[int] = mapped_column(default=0)
    skipped_stale_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("outbox_job_id", name="uq_maintenance_run_outbox_job"),
        UniqueConstraint("operation_id", name="uq_maintenance_run_operation"),
        Index("ix_maintenance_run_scope_status", "scope_type", "status"),
    )


class MemoryMaintenanceBatch(Base):
    """维护有界批次：一个 Run 内的有界处理单元，只属于一条 lane。

    - `candidate_lane` ∈ {changed, expired_ephemeral, candidate_due, sleep_due}。
    - `lane_cursor_time`/`lane_cursor_id` 为该 lane 的复合续跑游标。
    - 崩溃恢复优先接管 `frozen/planned/applying` 状态的 Batch。
    - 终态统一为 `done` 或 `deadletter`（禁止混用 aborted+abort_reason）。
    """

    __tablename__: str = "memory_maintenance_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_maintenance_runs.id"), nullable=False
    )
    batch_no: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    candidate_lane: Mapped[str] = mapped_column(String(32), default="changed")
    status: Mapped[str] = mapped_column(String(32), default="frozen", index=True)
    lane_cursor_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lane_cursor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cursor_start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_start_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cursor_end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_end_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_count: Mapped[int] = mapped_column(default=0)
    action_count: Mapped[int] = mapped_column(default=0)
    applied_count: Mapped[int] = mapped_column(default=0)
    skipped_stale_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("run_id", "batch_no", name="uq_maintenance_batch_run_no"),
        Index("ix_maintenance_batch_run_status", "run_id", "status"),
    )


class MemoryMaintenanceInput(Base):
    """维护不可变输入快照：Phase A 冻结的候选记忆，崩溃后可复现相同 plan。

    - `input_sequence` 为本 Batch 内稳定序号（确定性重算复现）。
    - `record_state_hash` 含记录态（不含访问时间）；`decision_hash` 额外纳入访问时间维度。
    - `user_required_protected`/`user_required_source_ids` 为冻结时计算的沿 lineage 保护标记。
    - 写入后不可变；续跑复用既有 Batch 的 Input，不重建。
    """

    __tablename__: str = "memory_maintenance_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_maintenance_runs.id"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_maintenance_batches.id"), nullable=False
    )
    input_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    memory_record_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.id"), nullable=False
    )
    input_role: Mapped[str] = mapped_column(String(16), default="seed")
    record_version: Mapped[int] = mapped_column(default=1)
    record_state_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_required_protected: Mapped[bool] = mapped_column(default=False)
    user_required_source_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint(
            "batch_id", "memory_record_id", name="uq_maintenance_input_batch_record"
        ),
        UniqueConstraint(
            "batch_id", "input_sequence", name="uq_maintenance_input_batch_seq"
        ),
        Index("ix_maintenance_input_batch", "batch_id"),
    )


class MemoryMaintenanceAction(Base):
    """维护动作审计表：Phase B2 持久化的确定性动作，Phase C 消费执行。

    - `action_sequence` 为本 Batch plan 内稳定序号，不依赖数据库返回顺序。
    - `source_input_ids` 记录来源 Input（一个 Input 可产生多个 Action）。
    - `operation_group_id` 由确定性公式生成，保证复合动作整组原子 skip_stale。
    - `preconditions` 区分 `record_state_hash` 与 `decision_hash`。
    """

    __tablename__: str = "memory_maintenance_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_maintenance_runs.id"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_maintenance_batches.id"), nullable=False
    )
    action_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_input_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    operation_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_memory_record_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memory_records.id"), nullable=True
    )
    related_record_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_record_version: Mapped[int | None] = mapped_column(nullable=True)
    preconditions: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("idempotency_key", name="uq_maintenance_action_idem"),
        UniqueConstraint(
            "batch_id", "action_sequence", name="uq_maintenance_action_batch_seq"
        ),
        Index("ix_maintenance_action_batch", "batch_id"),
        Index("ix_maintenance_action_run", "run_id"),
    )


# ============================================================================
# Phase 5: 冷热历史分层与统一检索（RetrievalIndex）
# ============================================================================


class RetrievalIndexGeneration(Base):
    """检索索引全局 generation（Phase 5）。

    查询真相源是唯一 `status='active'` 的 generation 的 `index_version`；
    每条 Entry 的 `is_current` 仅表示「同 source 仅一个当前版本」，不充当全局真相。
    """

    __tablename__: str = "retrieval_index_generations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    index_version: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    build_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    build_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32), default="phase5.v1")
    # Phase 6B: 真实终态时间，任何 status 翻转时原子写入
    # retention cutoff 使用此字段而非可能为空的 build_completed_at
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 该 generation 是否已完成初始全量 backfill（bootstrap 流程判定）
    backfill_done: Mapped[bool] = mapped_column(default=False)

    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint(
            "status IN ('building','active','retired','failed')",
            name="ck_retrieval_gen_status",
        ),
    )


class RetrievalIndexEntry(Base):
    """统一检索投影 Entry（Phase 5）。

    v1 不定义 embedding 列；语义检索接口仅代码层占位。
    """

    __tablename__: str = "retrieval_index_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    epoch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    segment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    memory_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    scope_type: Mapped[str] = mapped_column(String(32), default="global")
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    lifecycle_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validity_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retrieval_tier: Mapped[str] = mapped_column(String(16), default="warm")
    title: Mapped[str] = mapped_column(Text, default="")
    search_text: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    extra_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_current: Mapped[bool] = mapped_column(default=True)
    is_searchable: Mapped[bool] = mapped_column(default=True)

    __table_args__: tuple[UniqueConstraint | Index | CheckConstraint, ...] = (
        UniqueConstraint(
            "source_type", "source_id", "source_version", "index_version",
            name="uq_retrieval_entry_src_ver_gen",
        ),
        # 部分唯一索引：同 (index_version, source_type, source_id) 最多一个 is_current=True
        Index(
            "uq_retrieval_entry_current",
            "index_version", "source_type", "source_id",
            unique=True,
            postgresql_where=text("is_current IS TRUE"),
            sqlite_where=text("is_current = 1"),
        ),
        Index("ix_retrieval_entry_active", "index_version", "is_current", "source_type"),
        Index("ix_retrieval_entry_thread", "thread_id"),
        Index("ix_retrieval_entry_scope", "scope_type", "scope_id"),
        Index("ix_retrieval_entry_tier", "retrieval_tier"),
        CheckConstraint(
            "source_type IN ('memory_record','segment_summary','epoch_checkpoint')",
            name="ck_retrieval_entry_src",
        ),
        CheckConstraint(
            "retrieval_tier IN ('hot','warm','cold')",
            name="ck_retrieval_entry_tier",
        ),
    )


class RetrievalIndexToken(Base):
    """可移植倒排 posting（Phase 5）。

    按 token 精确查 posting 并聚合命中数；`search_text` 不在此路径。
    """

    __tablename__: str = "retrieval_index_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    entry_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("retrieval_index_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    index_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    token_kind: Mapped[str] = mapped_column(String(16), default="word")
    term_frequency: Mapped[int] = mapped_column(default=1)

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("entry_id", "token", name="uq_retrieval_token_entry"),
        Index("ix_retrieval_token_lookup", "index_version", "token", "entry_id"),
        Index("ix_retrieval_token_entry", "entry_id"),
    )


class RetrievalIndexRun(Base):
    """全量 rebuild Run（Phase 5）。一 `retrieval_index_rebuild` OutboxJob 一 Run。"""

    __tablename__: str = "retrieval_index_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    outbox_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("outbox_jobs.id"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    execution_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    failure_attempt_count: Mapped[int] = mapped_column(default=0)
    index_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    batch_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    indexed_count: Mapped[int] = mapped_column(default=0)
    skipped_stale_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__: tuple[UniqueConstraint | Index, ...] = (
        UniqueConstraint("outbox_job_id", name="uq_retrieval_run_outbox_job"),
        UniqueConstraint("operation_id", name="uq_retrieval_run_operation"),
        Index("ix_retrieval_run_status", "status"),
    )
