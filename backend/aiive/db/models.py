"""
模块功能说明：
- 数据库 ORM 模型定义模块，使用 SQLAlchemy 2.0 Declarative API
- 定义所有数据表结构：Thread、Event、LLMCall、ContextSnapshot、MemoryRecord
- 以及 Capability、Task、OutboxJob、Document 等支撑功能表
- 每个模型通过 Mapped 和 mapped_column 声明字段类型和约束
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped["Thread"] = relationship(back_populates="events")

    __table_args__: tuple[Index, ...] = (
        Index("ix_events_thread_created", "thread_id", "created_at"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped["Thread"] = relationship(back_populates="llm_calls")


class ContextSnapshot(Base):
    """上下文快照模型：保存每次对话的上下文构建结果，用于调试和审计。"""
    __tablename__: str = "context_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("threads.id"), index=True)
    stable_prefix_hash: Mapped[str] = mapped_column(String(32))
    context_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped["Thread"] = relationship(back_populates="context_snapshots")

    __table_args__: tuple[Index, ...] = (
        Index("ix_snapshots_trace", "trace_id", "thread_id"),
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
    sensitivity: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None
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
    lineage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pinned: Mapped[bool] = mapped_column(default=False)
    memory_key: Mapped[str | None] = mapped_column(
        "memory_key", String(128), nullable=True, index=True
    )
    revision_num: Mapped[int] = mapped_column("revision_num", default=1)
    supersedes: Mapped[str | None] = mapped_column(String(36), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    merged_from: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_reinforced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Document(Base):
    """文档模型：记录已导入的知识文档元信息。"""
    __tablename__: str = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source_path: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(32), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    chunk_id: Mapped[str] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(32))
    score: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ForgetRequest(Base):
    """遗忘请求模型：记录用户要求删除特定记忆的请求。"""
    __tablename__: str = "forget_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    memory_id: Mapped[str] = mapped_column(String(36), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tombstone: Mapped[str] = mapped_column(String(256))
    saga_state: Mapped[str] = mapped_column(
        String(32), default="running"
    )
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
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
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    memory_id: Mapped[str] = mapped_column(String(36), index=True)
    route: Mapped[str] = mapped_column(String(32))
    raw_score: Mapped[float] = mapped_column(default=0.0)
    fused_score: Mapped[float] = mapped_column(default=0.0)
    selected: Mapped[bool] = mapped_column(default=False)
    exclusion_reason: Mapped[str] = mapped_column(String(64), default="")
    token_cost: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
