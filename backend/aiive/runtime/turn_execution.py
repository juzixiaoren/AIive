"""
TurnExecutionService：统一的 Turn 生命周期管理 (Phase 1).

Phase 1: 使用 ContextAssembler 保证有界上下文。
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import threading
import uuid as _uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func

from aiive.core.llm_client import LLMClient, LLMClientError, default_llm_client
from aiive.db.base import SessionLocal
from aiive.db.models import (
    ApprovalRequest,
    ContextSnapshot,
    Epoch,
    Event,
    LLMCall,
    OutboxJob,
    Segment,
    Thread,
    TurnRecord,
)
from aiive.memory.extraction_policy import MessageSource, MemorySignalAction
from aiive.runtime.action_cards import ChatResponse
from aiive.runtime.agent_graph import (
    AgentGraph,
    AgentGraphResult,
    GraphExecutionInterrupted,
    ToolRecord,
    flatten_tool_result,
)
from aiive.runtime.context_assembler import (
    AssembledContext,
    ContextAssembler,
    ContextBudgetExceededError,
)
from aiive.runtime.context_budget import ContextBudget
from aiive.runtime.epoch_manager import EpochManager
from aiive.runtime.thread_bootstrap import ThreadBootstrapService
from aiive.runtime.token_counter import LiteLLMTokenCounter
from aiive.runtime.token_models import ModelProfile, TokenSafetyConfig
from aiive.runtime.tool_normalizer import ToolResultNormalizer
from aiive.runtime.working_state import WorkingStateService

from functools import lru_cache

LEASE_DURATION = timedelta(seconds=300)
HEARTBEAT_INTERVAL = 60


def _compute_request_fingerprint(message: str, thread_id: str | None) -> str:
    canonical = _json.dumps({
        "message": message,
        "thread_id": thread_id or "",
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _next_turn_sequence(db: Session, thread_id: str) -> int:
    """在 Thread 行锁保护下计算下一个递增 turn_sequence。

    返回 (当前最大 turn_sequence 或 0) + 1，作为新 Turn 的不可变序号。
    """
    max_seq = db.query(func.max(TurnRecord.turn_sequence)).filter(
        TurnRecord.thread_id == thread_id,
    ).scalar()
    return (max_seq or 0) + 1


class TurnConflictError(Exception):
    """Turn idempotency conflict."""


class FencingViolationError(Exception):
    """Turn fencing/lease violation."""


# ============================================================================
# TurnHeartbeat
# ============================================================================


class TurnHeartbeat:
    def __init__(self, turn_record_id: str, execution_id: str):
        self._turn_record_id: str = turn_record_id
        self._execution_id: str = execution_id
        self._stop_event: threading.Event = threading.Event()
        self._lease_lost: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="turn-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    def _loop(self) -> None:
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            db: Session | None = None
            try:
                db = SessionLocal()
                now = datetime.now(timezone.utc)
                affected = db.query(TurnRecord).filter(
                    TurnRecord.id == self._turn_record_id,
                    TurnRecord.execution_id == self._execution_id,
                    TurnRecord.status == "running",
                ).update({
                    TurnRecord.lease_expires_at: now + LEASE_DURATION,
                    TurnRecord.last_heartbeat_at: now,
                    TurnRecord.updated_at: now,
                }, synchronize_session=False)
                db.commit()
                if affected == 0:
                    self._lease_lost.set()
            except Exception:
                logger.exception("Heartbeat error")
            finally:
                if db is not None:
                    db.close()


# ============================================================================
# ContextBundle (pure data)
# ============================================================================


@dataclass
class ContextBundle:
    """跨数据库会话传递的纯数据上下文。"""

    assembled_ctx: AssembledContext


# ============================================================================
# TurnExecutionService
# ============================================================================


# 无状态共享组件缓存（卡点 4 方案 B）：
#   token_counter / budget / profile / safety / normalizer 均为纯计算、
#   不持有 per-request 状态，可跨请求复用，避免每次请求重建（尤其
#   ContextBudget.from_env() 每次读环境变量）。按 model 名缓存以支持
#   不同模型。WorkingStateService / EpochManager 仍每请求新建（见 __init__）。
@lru_cache(maxsize=8)
def _shared_budget() -> ContextBudget:
    return ContextBudget.from_env()


_SHARED_SAFETY: TokenSafetyConfig = TokenSafetyConfig()


@lru_cache(maxsize=8)
def _shared_profile(model: str) -> ModelProfile:
    return ModelProfile.from_config("deepseek", model)


@lru_cache(maxsize=8)
def _shared_token_counter(model: str) -> LiteLLMTokenCounter:
    return LiteLLMTokenCounter(_SHARED_SAFETY, _shared_profile(model))


@lru_cache(maxsize=8)
def _shared_normalizer(model: str) -> ToolResultNormalizer:
    return ToolResultNormalizer(_shared_token_counter(model), _shared_profile(model).full_name)


class TurnExecutionService:
    """Unified Turn lifecycle (Phase 1: bounded context)."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        message_source: str | MessageSource = MessageSource.USER,
    ):
        self._llm_client: LLMClient = llm_client or default_llm_client()
        self._message_source: MessageSource = (
            message_source
            if isinstance(message_source, MessageSource)
            else MessageSource(message_source)
        )
        model = self._llm_client.default_model
        self._profile: ModelProfile = _shared_profile(model)
        self._safety: TokenSafetyConfig = _SHARED_SAFETY
        self._token_counter: LiteLLMTokenCounter = _shared_token_counter(model)
        self._budget: ContextBudget = _shared_budget()
        self._normalizer: ToolResultNormalizer = _shared_normalizer(model)
        self._epoch_mgr: EpochManager = EpochManager()
        self._ws_service: WorkingStateService = WorkingStateService()

    # =====================================================================
    # Public API
    # =====================================================================

    def execute_turn(
        self, message: str, thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        return self._execute_turn_impl(message, thread_id, turn_id)

    async def execute_turn_stream(
        self, message: str, thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式执行 Turn：逐 token / tool_call / tool_result 产出 SSE 事件。

        使用 LangGraph astream_events(v2) 实现 token 级真正流式传输，
        替代伪流式（sync 跑完再一次性 yield done）。
        """
        # ── Phase 1: 抢占与上下文加载 ──
        # 这些是同步阻塞调用（行锁、多次 DB 会话、上下文组装与 token 计数）。
        # 若直接跑在事件循环线程上，会冻结整个 ASGI 事件循环（含 WS 通知通道），
        # 导致并发请求被串行化。用 asyncio.to_thread 丢到线程池执行，立即让出
        # 事件循环。每段内部各自新建并关闭 DB 会话，会话生命周期不跨线程，
        # 满足 SQLAlchemy Session 的线程约束。
        try:
            turn, execution_id = await asyncio.to_thread(
                self._resolve_and_preempt, message, thread_id, turn_id,
            )
        except TurnConflictError as e:
            err_msg = str(e)
            yield {"type": "error", "error": err_msg}
            return

        heartbeat = TurnHeartbeat(turn.id, execution_id)
        heartbeat.start()
        trace_id = str(_uuid.uuid4())
        try:
            yield {
                "type": "started",
                "thread_id": turn.thread_id,
                "turn_id": turn.turn_id,
                "trace_id": trace_id,
            }
        except (asyncio.CancelledError, GeneratorExit):
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "stream_cancelled_after_started")
            logger.warning(
                "started 后流式 Turn 已取消: turn_id=%s execution_id=%s thread_id=%s",
                turn.turn_id, execution_id, turn.thread_id,
            )
            raise

        def _recover_orphaned() -> None:
            recovery_db = SessionLocal()
            try:
                self._ws_service.recover_orphaned_tools(recovery_db, turn.thread_id)
                recovery_db.commit()
            finally:
                recovery_db.close()

        await asyncio.to_thread(_recover_orphaned)

        try:
            ctx_bundle = await asyncio.to_thread(self._load_context, message, turn, trace_id)
        except asyncio.CancelledError:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "context_loading_cancelled")
            raise
        except ContextBudgetExceededError as e:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "context_budget_exceeded")
            yield {
                "type": "error",
                "error": "context_budget_exceeded",
                "message": "上下文超过模型安全预算",
                "retryable": False,
                "trace_id": trace_id,
                "_status": 413,
                "safe_tokens": e.safe_tokens,
                "context_window": e.context_window,
                "hard_input_limit": e.hard_input_limit,
                "partition_reports": [report.__dict__ for report in e.partition_reports],
            }
            return
        except Exception:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "context_loading_failed")
            logger.exception("流式 Turn 上下文加载失败: thread_id=%s", turn.thread_id)
            yield {"type": "error", "error": "internal_error", "trace_id": trace_id}
            return

        # ── Phase 2: 流式图执行 ──
        graph_db = SessionLocal()
        try:
            graph = AgentGraph(self._llm_client, graph_db)
            ag_result: AgentGraphResult | None = None

            async for event in graph._execute_graph_stream(  # pyright: ignore[reportPrivateUsage]
                message=message, thread_id=turn.thread_id,
                turn_id=turn.turn_id, turn_record_id=turn.id, ctx_bundle=ctx_bundle,
                execution_id=execution_id, trace_id=trace_id,
            ):
                if event.get("type") == "__graph_result__":
                    ag_result = event["result"]
                else:
                    yield event
        except asyncio.CancelledError:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "graph_execution_cancelled")
            raise
        except GraphExecutionInterrupted as interrupted:
            heartbeat.stop()
            try:
                self._persist_interrupted_tool_facts(
                    turn, execution_id, interrupted.trace_id, interrupted.tool_records,
                )
            except FencingViolationError:
                yield {"type": "error", "error": "lease_lost", "trace_id": trace_id}
                return
            except Exception:
                yield {"type": "error", "error": "internal_error", "trace_id": trace_id}
                return
            error = interrupted.error
            if isinstance(error, LLMClientError):
                yield {"type": "error", "error": error.code, "message": str(error),
                       "retryable": error.retryable, "trace_id": error.trace_id,
                       "retry_after_seconds": error.retry_after_seconds,
                       "_status": error.status_code or 503,
                       "partial_tool_records": len(interrupted.tool_records)}
            else:
                yield {"type": "error", "error": "internal_error", "trace_id": trace_id,
                       "partial_tool_records": len(interrupted.tool_records)}
            return
        except LLMClientError as error:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "llm_client_failed")
            yield {"type": "error", "error": error.code, "message": str(error),
                   "retryable": error.retryable, "trace_id": error.trace_id,
                   "retry_after_seconds": error.retry_after_seconds,
                   "_status": error.status_code or 503}
            return
        except Exception:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "graph_execution_failed")
            logger.exception("流式 Turn 图执行失败: thread_id=%s trace_id=%s", turn.thread_id, trace_id)
            yield {"type": "error", "error": "internal_error", "trace_id": trace_id}
            return
        finally:
            graph_db.close()

        if ag_result is None:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "graph_result_missing")
            logger.error("流式 Turn 未获得图结果: thread_id=%s trace_id=%s", turn.thread_id, trace_id)
            yield {"type": "error", "error": "internal_error", "trace_id": trace_id}
            return

        # LLM 返回空回复且未执行任何工具 → 异常空响应
        if not ag_result.reply and not ag_result.tool_records:
            logger.error(
                "[TurnExecution] LLM 空响应（无回复且无工具调用）: "
                + "trace_id=%s thread_id=%s message=%s",
                ag_result.trace_id, turn.thread_id, message[:100],
            )
            ag_result.reply = "抱歉，模型返回了空响应。请重试或检查 API 配置。"
            response = self._finalize_turn(turn, execution_id, ag_result, heartbeat, assembled_ctx=None)
            response["error"] = "empty_llm_response"
            yield {"type": "done", **response}
            return

        # ── Phase 3: Finalize + 记忆提取 Outbox ──
        try:
            assembled_ctx = ctx_bundle.assembled_ctx if ctx_bundle else None
            response = self._finalize_turn(
                turn, execution_id, ag_result, heartbeat, assembled_ctx,
            )
        except FencingViolationError:
            heartbeat.stop()
            yield {"type": "error", "error": "lease_lost"}
            return
        except asyncio.CancelledError:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "stream_cancelled")
            logger.warning(
                "流式 Turn 已取消: turn_id=%s execution_id=%s thread_id=%s",
                turn.turn_id, execution_id, turn.thread_id,
            )
            raise
        except Exception:
            heartbeat.stop()
            self._mark_turn_interrupted_safely(turn.id, execution_id, "finalize_failed")
            raise
        finally:
            heartbeat.stop()

        yield {"type": "done", **response}

    # =====================================================================
    # Phase 1: Resolve, preempt, attribute epoch/segment
    # =====================================================================

    def _resolve_and_preempt(
        self, message: str, thread_id: str | None, turn_id: str | None,
    ) -> tuple[TurnRecord, str]:
        turn_id = turn_id or str(_uuid.uuid4())
        fingerprint = _compute_request_fingerprint(message, thread_id)
        committed_tid = ThreadBootstrapService.ensure_committed_thread(thread_id)
        execution_id = str(_uuid.uuid4())

        # ── Short transaction: Thread row lock + epoch/segment attribution ──
        db = SessionLocal()
        turn_record_id = ""
        try:
            # Ensure Thread row exists (lazy creation if needed)
            existing_thread = db.get(Thread, committed_tid)
            if existing_thread is None:
                existing_thread = Thread(id=committed_tid)
                db.add(existing_thread)
                db.flush()
            # Lock Thread row for epoch/segment serialization
            db.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()

            existing = db.query(TurnRecord).filter(
                TurnRecord.thread_id == committed_tid,
                TurnRecord.turn_id == turn_id,
            ).first()

            if existing is None:
                # Attribute epoch/segment（在 Thread 行锁保护下计算递增 turn_sequence）
                try:
                    next_seq = _next_turn_sequence(db, committed_tid)
                    epoch, segment = self._epoch_mgr.ensure_epoch_and_segment(
                        db, committed_tid, next_seq,
                    )
                except IntegrityError:
                    db.rollback()
                    db.close()
                    # Re-read: another concurrent request created epoch/segment
                    db2 = SessionLocal()
                    try:
                        db2.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()
                        next_seq = _next_turn_sequence(db2, committed_tid)
                        epoch, segment = self._epoch_mgr.ensure_epoch_and_segment(db2, committed_tid, next_seq)
                    finally:
                        db2.close()
                    db = SessionLocal()
                    db.query(Thread).with_for_update().filter(Thread.id == committed_tid).one()
                    epoch = db.query(Epoch).filter(Epoch.thread_id == committed_tid, Epoch.status == "active").first()
                    if epoch is not None:
                        segment = db.query(Segment).filter(Segment.epoch_id == epoch.id, Segment.status == "open").first()
                    else:
                        segment = None

                turn = TurnRecord(
                    thread_id=committed_tid,
                    turn_id=turn_id,
                    turn_sequence=next_seq,
                    status="not_started",
                    attempt_no=1,
                    request_fingerprint=fingerprint,
                    epoch_id=epoch.id if epoch else None,
                    segment_id=segment.id if segment else None,
                )
                event_id = str(_uuid.uuid4())
                event = Event(
                    id=event_id, thread_id=committed_tid, turn_id=turn_id,
                    turn_event_index=0, trace_id="", event_type="user_message",
                    payload={
                        "content": message,
                        "message_source": self._message_source.value,
                    },
                )
                db.add(turn)
                db.add(event)
                db.flush()
                turn.request_event_id = event.id
                db.commit()
                db.refresh(turn)
                turn_record_id = turn.id
            else:
                # Existing Turn logic unchanged
                if existing.status == "completed":
                    if existing.request_fingerprint != fingerprint:
                        raise TurnConflictError("idempotency_key_mismatch")
                    cached = existing.response_payload or {}
                    cached["_turn_cached"] = True
                    raise TurnConflictError("turn_cached")
                if existing.status == "running":
                    if existing.lease_expires_at and existing.lease_expires_at > datetime.now(timezone.utc):
                        raise TurnConflictError("turn_in_progress")
                    self._mark_interrupted(db, existing.id, existing.execution_id or "")
                    db.commit()
                    raise TurnConflictError("turn_interrupted")
                if existing.status == "interrupted_unknown":
                    raise TurnConflictError("turn_interrupted")
                if existing.status == "not_started":
                    if existing.request_fingerprint != fingerprint:
                        raise TurnConflictError("idempotency_key_mismatch")
                    existing.attempt_no = (existing.attempt_no or 1) + 1
                    db.commit()
                    turn_record_id = existing.id
        finally:
            db.close()

        # ── Atomic preemption ──
        db2 = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            affected = db2.query(TurnRecord).filter(
                TurnRecord.id == turn_record_id,
                TurnRecord.status == "not_started",
            ).update({
                TurnRecord.status: "running",
                TurnRecord.execution_id: execution_id,
                TurnRecord.lease_expires_at: now + LEASE_DURATION,
                TurnRecord.updated_at: now,
            }, synchronize_session=False)
            db2.commit()
            if affected != 1:
                turn = db2.get(TurnRecord, turn_record_id)
                if turn and turn.status == "completed":
                    if turn.request_fingerprint != fingerprint:
                        raise TurnConflictError("idempotency_key_mismatch")
                    raise TurnConflictError("turn_cached")
                raise TurnConflictError("preemption_failed")
        finally:
            db2.close()

        # Reload
        db3 = SessionLocal()
        try:
            turn = db3.get(TurnRecord, turn_record_id)
            if turn is None or turn.status != "running":
                raise TurnConflictError("preemption_failed")
            result = TurnRecord(
                id=turn.id, thread_id=turn.thread_id, turn_id=turn.turn_id,
                turn_sequence=turn.turn_sequence, status=turn.status,
                execution_id=turn.execution_id, request_event_id=turn.request_event_id,
                request_fingerprint=turn.request_fingerprint,
                epoch_id=turn.epoch_id, segment_id=turn.segment_id,
            )
            return result, execution_id
        finally:
            db3.close()

    def _mark_interrupted(self, db: Session, turn_record_id: str, execution_id: str) -> None:
        db.query(TurnRecord).filter(
            TurnRecord.id == turn_record_id,
            TurnRecord.execution_id == execution_id,
            TurnRecord.status == "running",
        ).update({
            TurnRecord.status: "interrupted_unknown",
            TurnRecord.lease_expires_at: None,
            TurnRecord.updated_at: datetime.now(timezone.utc),
        }, synchronize_session=False)

    def _mark_turn_interrupted_safely(
        self, turn_record_id: str, execution_id: str, stage: str,
    ) -> None:
        """在独立事务中标记中断，并保留失败日志。"""
        db: Session | None = None
        try:
            db = SessionLocal()
            self._mark_interrupted(db, turn_record_id, execution_id)
            db.commit()
        except Exception:
            if db is not None:
                db.rollback()
            logger.exception(
                "标记流式 Turn 中断失败: turn_record_id=%s execution_id=%s stage=%s",
                turn_record_id, execution_id, stage,
            )
        finally:
            if db is not None:
                db.close()

    def _persist_interrupted_tool_facts(
        self,
        turn: TurnRecord,
        execution_id: str,
        trace_id: str,
        tool_records: list[ToolRecord],
    ) -> None:
        """原子保存流式中断前完成的工具事实，并将 Turn 标记为中断。"""
        db = SessionLocal()
        try:
            affected = db.query(TurnRecord).filter(
                TurnRecord.id == turn.id,
                TurnRecord.execution_id == execution_id,
                TurnRecord.status == "running",
            ).update({
                TurnRecord.status: "interrupted_unknown",
                TurnRecord.updated_at: datetime.now(timezone.utc),
            }, synchronize_session=False)
            if affected != 1:
                raise FencingViolationError("中断工具事实持久化时执行租约已失效")

            for index, record in enumerate(tool_records):
                event_index = 1 + index * 2
                db.add(Event(
                    id=str(_uuid.uuid4()), trace_id=trace_id,
                    thread_id=turn.thread_id, event_type="tool_call", turn_id=turn.turn_id,
                    turn_event_index=event_index,
                    payload={
                        "name": record.name, "params": record.params,
                        "tool_call_id": record.tool_call_id, "batch_index": record.batch_index,
                        "interrupted": True,
                    },
                ))
                db.add(Event(
                    id=str(_uuid.uuid4()), trace_id=trace_id,
                    thread_id=turn.thread_id, event_type="tool_result", turn_id=turn.turn_id,
                    turn_event_index=event_index + 1,
                    payload={
                        "name": record.name, "result": record.result, "status": record.status,
                        "tool_call_id": record.tool_call_id, "batch_index": record.batch_index,
                        "interrupted": True,
                    },
                ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "持久化流式中断工具事实失败: turn_record_id=%s execution_id=%s",
                turn.id, execution_id,
            )
            raise
        finally:
            db.close()

    # =====================================================================
    # Phase 5: Finalize (with snapshot rotation)
    # =====================================================================

    def _finalize_turn(
        self, turn: TurnRecord, execution_id: str,
        ag_result: AgentGraphResult, heartbeat: TurnHeartbeat,
        assembled_ctx: AssembledContext | None = None,
        assistant_event_id: str | None = None,
    ) -> dict[str, Any]:
        if heartbeat.lease_lost:
            raise FencingViolationError("lease lost")

        # 同步抽取路径会提前生成 assistant 回复事件的真实 Event.id 并传入，
        # 以保证记忆 provenance 与 finalize 持久化事件一致；其余路径在此生成。
        if not assistant_event_id:
            assistant_event_id = str(_uuid.uuid4())

        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            response_dict = ChatResponse(
                reply=ag_result.reply,
                event_id=assistant_event_id,
                thread_id=turn.thread_id,
                trace_id=ag_result.trace_id,
                action_cards=ag_result.action_cards,
                pending_operations=ag_result.pending_operations,
                tool_calls=[
                    {"tool_call_id": r.tool_call_id, "batch_index": r.batch_index, "order_index": r.order_index,
                     "name": r.name, "params": r.params, "status": r.status}
                    for r in ag_result.tool_records
                ],
                tool_results=[
                    {"tool_call_id": r.tool_call_id, "batch_index": r.batch_index, "order_index": r.order_index,
                     "name": r.name, "params": r.params, "result": flatten_tool_result(r.result), "status": r.status}
                    for r in ag_result.tool_records if r.status in ("completed", "failed", "execution_unknown")
                ],
                parse_errors=[],
            ).model_dump(mode="json", exclude_none=True)

            affected = db.query(TurnRecord).filter(
                TurnRecord.id == turn.id,
                TurnRecord.execution_id == execution_id,
                TurnRecord.status == "running",
            ).update({
                TurnRecord.status: "completed",
                TurnRecord.response_payload: response_dict,
                TurnRecord.lease_expires_at: None,
                TurnRecord.last_heartbeat_at: None,
                TurnRecord.updated_at: now,
            }, synchronize_session=False)
            db.flush()
            if affected != 1:
                db.rollback()
                raise FencingViolationError("fencing violation")

            # Events：pending_approval 工具不写入 tool_call/tool_result，等待审批后补充
            idx = 1
            executed_records = [r for r in ag_result.tool_records if r.status != "pending_approval"]
            for r in executed_records:
                db.add(Event(id=str(_uuid.uuid4()), trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                             event_type="tool_call", turn_id=turn.turn_id, turn_event_index=idx,
                             payload={"name": r.name, "params": r.params, "tool_call_id": r.tool_call_id, "batch_index": r.batch_index}))
                idx += 1
                db.add(Event(id=str(_uuid.uuid4()), trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                             event_type="tool_result", turn_id=turn.turn_id, turn_event_index=idx,
                             payload={"name": r.name, "result": r.result, "status": r.status, "tool_call_id": r.tool_call_id, "batch_index": r.batch_index}))
                idx += 1
            db.add(Event(id=assistant_event_id, trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                         event_type="llm_response", turn_id=turn.turn_id, turn_event_index=idx,
                         payload={"content": ag_result.reply,
                                  "action_cards": response_dict["action_cards"],
                                  "pending_operations": response_dict["pending_operations"]}))
            idx += 1

            for approval in ag_result.pending_approvals:
                approval_id = str(approval.get("approval_id", "") or "")
                tool_call_id = str(approval.get("id", "") or "")
                tool_name = str(approval.get("name", "") or "")
                tool_args = approval.get("args", {}) if isinstance(approval.get("args"), dict) else {}
                if not approval_id or not tool_call_id or not tool_name:
                    raise ValueError("待审批工具缺少稳定标识")
                db.add(ApprovalRequest(
                    id=approval_id,
                    thread_id=turn.thread_id,
                    turn_record_id=turn.id,
                    turn_id=turn.turn_id,
                    trace_id=ag_result.trace_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_args_hash=str(approval.get("tool_args_hash", "") or ""),
                    descriptor_hash=str(approval.get("descriptor_hash", "") or ""),
                    risk_snapshot=approval.get("risk_snapshot", {}) if isinstance(approval.get("risk_snapshot"), dict) else {},
                    status="pending",
                ))
                self._ws_service.add_pending_approval(
                    db, turn.thread_id, approval_id, f"confirm:{tool_name}",
                )

            # 审批中工具计入 pending_approvals 数量
            pending_count = sum(1 for r in ag_result.tool_records if r.status == "pending_approval")
            db.add(Event(id=str(_uuid.uuid4()), trace_id=ag_result.trace_id, thread_id=turn.thread_id,
                         event_type="chat_ended", turn_id=turn.turn_id, turn_event_index=idx,
                         payload={"tool_calls": len(ag_result.tool_records), "tool_succeeded": sum(1 for r in ag_result.tool_records if r.status == "completed"), "tool_failed": sum(1 for r in ag_result.tool_records if r.status == "failed"), "pending_approvals": pending_count, "parse_errors": 0}))

            # ContextSnapshot with Phase 1 rotation
            self._rotate_snapshot(db, turn, ag_result, assembled_ctx)

            # Phase 1: write token estimation record for observability (§7.7)
            if assembled_ctx is not None:
                tc = assembled_ctx.total_token_count
                db.add(LLMCall(
                    id=str(_uuid.uuid4()),
                    trace_id=ag_result.trace_id,
                    thread_id=turn.thread_id,
                    model=self._profile.full_name,
                    latency_ms=0,
                    input_preview=(ag_result.user_message or "")[:200],
                    output_preview=ag_result.reply[:200] if ag_result.reply else None,
                    estimated_prompt_tokens=tc.estimated_tokens,
                    safe_prompt_tokens=tc.safe_tokens,
                    actual_prompt_tokens=None,
                    actual_completion_tokens=None,
                    token_source=tc.source,
                ))

            # 自动记忆抽取统一走 Outbox，与 Turn/Event 在同一事务提交。
            memory_action = ag_result.memory_signal.action
            if memory_action in (
                MemorySignalAction.EXTRACT_ASYNC.value,
                MemorySignalAction.EXTRACT_SYNC.value,
            ):
                extraction_mode = (
                    "priority_async"
                    if memory_action == MemorySignalAction.EXTRACT_SYNC.value
                    else "async"
                )
                db.add(OutboxJob(
                    operation_id=f"memory_extraction:{turn.id}:{_uuid.uuid4().hex[:8]}",
                    job_type="memory_extraction", status="pending",
                    payload={
                        "user_message": getattr(ag_result, 'user_message', ''),
                        "message_source": self._message_source.value,
                        "reply": ag_result.reply,
                        "thread_id": turn.thread_id,
                        "source_turn_record_id": turn.id,
                        "source_turn_id": turn.turn_id,
                        "extraction_mode": extraction_mode,
                        "source_event_ids": [
                            turn.request_event_id,    # 用户消息真实 Event.id
                            assistant_event_id,        # Assistant 回复真实 Event.id
                        ],
                        # 显式标注 assistant 回复事件，供 worker 区分 source_type
                        # （标为 llm_reply）；旧任务缺此字段时 worker 用 .get() 兼容。
                        "assistant_event_id": assistant_event_id,
                    },
                    trace_id=ag_result.trace_id, max_retries=3,
                ))

            db.commit()
            return response_dict
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _rotate_snapshot(
        self, db: Session, turn: TurnRecord,
        ag_result: AgentGraphResult, assembled_ctx: AssembledContext | None,
    ) -> None:
        """Atomic snapshot rotation by turn_sequence."""
        db.query(Thread).with_for_update().filter(Thread.id == turn.thread_id).one()

        current = db.query(ContextSnapshot).filter(
            ContextSnapshot.thread_id == turn.thread_id,
            ContextSnapshot.retention == "current",
        ).first()

        total_tokens = assembled_ctx.total_token_count.safe_tokens if assembled_ctx else 0
        seq = turn.turn_sequence or 0

        snapshot = ag_result.context_snapshot

        new_snap = ContextSnapshot(
            trace_id=ag_result.trace_id,
            thread_id=turn.thread_id,
            stable_prefix_hash=snapshot.stable_prefix_hash,
            context_items=[item.to_dict() for item in snapshot.items],
            meta={
                "total_tool_calls": len(ag_result.tool_records),
                "full_contents": dict(snapshot.full_contents),
                "injected_memory_ids": list(snapshot.injected_memory_ids),
            },
            epoch_id=turn.epoch_id,
            segment_id=turn.segment_id,
            turn_sequence=seq,
            token_total=total_tokens,
        )

        if current is None or seq > (current.turn_sequence or 0):
            db.query(ContextSnapshot).filter(
                ContextSnapshot.thread_id == turn.thread_id,
                ContextSnapshot.retention == "previous",
            ).update({"retention": "temporary"})
            if current:
                current.retention = "previous"
            new_snap.retention = "current"
        else:
            new_snap.retention = "temporary"

        db.add(new_snap)

        # 乱序完成且无审计价值的 temporary 快照直接删除，不持久保留（§14.1）
        db.query(ContextSnapshot).filter(
            ContextSnapshot.thread_id == turn.thread_id,
            ContextSnapshot.retention == "temporary",
        ).delete()

        # 审计快照上限 5（保留最新），保证 current≤1 / previous≤1 / audit≤5 / 总数≤7
        audit_excess = (
            db.query(ContextSnapshot)
            .filter(
                ContextSnapshot.thread_id == turn.thread_id,
                ContextSnapshot.retention == "audit",
            )
            .order_by(ContextSnapshot.turn_sequence.desc())
            .offset(5)
            .all()
        )
        for snap in audit_excess:
            db.delete(snap)

    # =====================================================================
    # Shared lifecycle
    # =====================================================================

    def _execute_turn_impl(
        self, message: str, thread_id: str | None, turn_id: str | None,
    ):
        try:
            turn, execution_id = self._resolve_and_preempt(message, thread_id, turn_id)
        except TurnConflictError as e:
            err_msg = str(e)
            if err_msg == "turn_cached":
                db = SessionLocal()
                try:
                    existing = db.query(TurnRecord).filter(
                        TurnRecord.thread_id == ThreadBootstrapService.ensure_committed_thread(thread_id),
                        TurnRecord.turn_id == (turn_id or ""),
                    ).first()
                    if existing and existing.response_payload:
                        return existing.response_payload
                finally:
                    db.close()
                return {"reply": "", "error": "turn_cached_no_payload"}
            if err_msg == "turn_in_progress":
                return {"reply": "", "error": "turn_in_progress", "_status": 202}
            return {"reply": "", "error": err_msg, "_status": 409}

        heartbeat = TurnHeartbeat(turn.id, execution_id)
        heartbeat.start()

        # Crash recovery: clean orphaned running tools
        recovery_db = SessionLocal()
        try:
            self._ws_service.recover_orphaned_tools(recovery_db, turn.thread_id)
            recovery_db.commit()
        finally:
            recovery_db.close()

        try:
            # Phase 2: Context assembly (via ContextAssembler)
            trace_id = str(_uuid.uuid4())
            ctx_bundle = self._load_context(message, turn, trace_id)

            # Phase 3: AgentGraph execution
            graph_db = SessionLocal()
            try:
                graph = AgentGraph(self._llm_client, graph_db)
                ag_result = graph._execute_graph(  # pyright: ignore[reportPrivateUsage]
                    message=message, thread_id=turn.thread_id,
                    turn_id=turn.turn_id, turn_record_id=turn.id, ctx_bundle=ctx_bundle,
                    execution_id=execution_id, trace_id=trace_id,
                )
            finally:
                graph_db.close()

            # LLM 返回空回复且未执行任何工具 → 异常空响应，向用户明确提示，
            # 同时标记 turn 为 completed 避免阻塞后续消息。
            if not ag_result.reply and not ag_result.tool_records:
                logger.error(
                    "[TurnExecution] LLM 空响应（无回复且无工具调用）: "
                    + "trace_id=%s thread_id=%s message=%s",
                    ag_result.trace_id, turn.thread_id, message[:100],
                )
                # 仍然走 finalize 路径以标记 turn 为 completed（避免卡住后续 turn），
                # 但注入一条用户可见的错误回复。
                ag_result.reply = (
                    "抱歉，模型返回了空响应。请重试或检查 API 配置。"
                )
                response = self._finalize_turn(turn, execution_id, ag_result, heartbeat, assembled_ctx=None)
                # 在最终响应中附加错误字段，便于前端展示
                response["error"] = "empty_llm_response"
                return response

            # Phase 4: Finalize + 记忆提取 Outbox
            assembled_ctx = ctx_bundle.assembled_ctx if ctx_bundle else None
            response = self._finalize_turn(
                turn, execution_id, ag_result, heartbeat, assembled_ctx,
            )
            return response

        except ContextBudgetExceededError as e:
            logger.error("Context budget exceeded: %s", e)
            db = SessionLocal()
            try:
                self._mark_interrupted(db, turn.id, execution_id)
                db.commit()
            finally:
                db.close()
            return {
                "reply": "",
                "error": "context_budget_exceeded",
                "message": "上下文超过模型安全预算",
                "retryable": False,
                "safe_tokens": e.safe_tokens,
                "context_window": e.context_window,
                "hard_input_limit": e.hard_input_limit,
                "partition_reports": [report.__dict__ for report in e.partition_reports],
                "_status": 413,
            }
        except Exception:
            db = SessionLocal()
            try:
                self._mark_interrupted(db, turn.id, execution_id)
                db.commit()
            except Exception:
                logger.exception("mark_interrupted failed")
            finally:
                db.close()
            raise
        finally:
            heartbeat.stop()

    # =====================================================================
    # Phase 2: Context loading via ContextAssembler
    # =====================================================================

    def _load_context(
        self, message: str, turn: TurnRecord, trace_id: str | None = None,
    ) -> ContextBundle:
        db = SessionLocal()
        try:
            thread = db.get(Thread, turn.thread_id)
            if thread is None:
                thread = Thread(id=turn.thread_id)
                db.add(thread)
                db.flush()

            assembler = ContextAssembler(
                token_counter=self._token_counter,
                budget=self._budget,
                profile=self._profile,
                normalizer=self._normalizer,
            )

            assembled = assembler.assemble(
                db=db,
                message=message,
                thread=thread,
                _source=self._message_source.value,
                upper_bound_sequence=(turn.turn_sequence or 1) - 1,
                trace_id=trace_id,
            )
            db.commit()

            return ContextBundle(assembled_ctx=assembled)
        finally:
            db.close()
