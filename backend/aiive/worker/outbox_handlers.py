"""
发件箱处理器：异步作业的具体处理逻辑。
集成 V20 MemoryGate + MemoryWriteService，实现记忆提取和管家信号提取。
"""
import logging

from aiive.config import settings
from aiive.context.run_context import RunContext
from aiive.core.llm_client import LLMClient
from aiive.memory.memory_extractor import MemoryExtractor
from aiive.memory.memory_gate import MemoryGate, MemoryGateInput
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.memory.steward_signal_extractor import StewardSignalExtractor

logger = logging.getLogger(__name__)


def _get_llm_client() -> LLMClient:
    """创建 LLM 客户端实例，使用全局配置。
    
    返回:
        配置好的 LLMClient 实例
    """
    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


def handle_memory_extraction(db, payload: dict, trace_id: str | None) -> None:
    """处理记忆提取作业。

    流程：
    1. 调用 MemoryExtractor 从用户消息和回复中提取候选记忆
    2. 对每条候选记忆通过 MemoryGate 决策（接受/拒绝/更新）
    3. 接受的记忆通过 MemoryWriteService 写入数据库

    参数:
        db: 数据库会话
        payload: 包含 user_message、reply 等字段的负载
        trace_id: 跟踪 ID
    """
    try:
        llm = _get_llm_client()
        extractor = MemoryExtractor(llm)
        gate = MemoryGate()
        writer = MemoryWriteService(db)

        candidates = extractor.extract(
            user_message=payload["user_message"],
            reply=payload["reply"],
            trace_id=trace_id,
        )

        intent_type = payload.get("intent_type", "chat")
        execution_mode = payload.get("execution_mode", "explain_only")
        should_execute = payload.get("should_execute", False)
        evidence_source = payload.get("source", "trusted_user_message")

        for c in candidates:
            if isinstance(c, dict):
                content = c.get("content", "")
                memory_type = c.get("memory_type", "fact")
                confidence = c.get("confidence", 0.5)
                memory_key = c.get("memory_key", "")
            else:
                content = c.content
                memory_type = c.memory_type
                confidence = c.confidence
                memory_key = c.memory_key

            decision = gate.decide(MemoryGateInput(
                content=content,
                user_message=payload["user_message"],
                source="auto_extraction",
                intent_type=intent_type,
                execution_mode=execution_mode,
                should_execute=should_execute,
                evidence_source=evidence_source,
                extracted_memory_type=memory_type,
                extracted_memory_key=memory_key or None,
                confidence=confidence,
                trace_id=trace_id or "",
            ))

            if decision.decision == "reject":
                continue

            thread_id = payload.get("thread_id", "")
            run_ctx = RunContext(thread_id=thread_id, trace_id=trace_id or "", source="outbox_worker")
            writer.write(decision, content, run_context=run_ctx)
            db.flush()
    except Exception:
        logger.exception("记忆提取处理失败: trace_id=%s", trace_id)
        raise


def handle_steward_extraction(db, payload: dict, trace_id: str | None) -> None:
    """处理管家信号提取作业。

    流程与记忆提取类似，但使用 StewardSignalExtractor 提取管家信号
    （如用户偏好、工作流习惯等），并通过 MemoryGate + MemoryWriteService 写入。

    参数:
        db: 数据库会话
        payload: 包含 user_message、reply 等字段的负载
        trace_id: 跟踪 ID
    """
    try:
        llm = _get_llm_client()
        extractor = StewardSignalExtractor(llm)
        gate = MemoryGate()
        writer = MemoryWriteService(db)

        signals = extractor.extract(
            user_message=payload["user_message"],
            reply=payload["reply"],
            trace_id=trace_id,
        )

        intent_type = payload.get("intent_type", "chat")
        execution_mode = payload.get("execution_mode", "explain_only")
        should_execute = payload.get("should_execute", False)
        evidence_source = payload.get("source", "trusted_user_message")

        for s in signals:
            if isinstance(s, dict):
                content = s.get("content", "")
                signal_type = s.get("signal_type", "routine")
                confidence = s.get("confidence", 0.7)
                memory_key = s.get("memory_key", "")
            else:
                content = s.content
                signal_type = s.memory_type if hasattr(s, 'memory_type') else "routine"
                confidence = s.confidence if hasattr(s, 'confidence') else 0.7
                memory_key = s.memory_key if hasattr(s, 'memory_key') else ""

            decision = gate.decide(MemoryGateInput(
                content=content,
                user_message=payload["user_message"],
                source="steward",
                intent_type=intent_type,
                execution_mode=execution_mode,
                should_execute=should_execute,
                evidence_source=evidence_source,
                extracted_memory_type=signal_type,
                extracted_memory_key=memory_key or None,
                confidence=confidence,
                trace_id=trace_id or "",
            ))

            if decision.decision == "reject":
                continue

            thread_id = payload.get("thread_id", "")
            run_ctx = RunContext(thread_id=thread_id, trace_id=trace_id or "", source="outbox_worker")
            writer.write(decision, content, run_context=run_ctx)
            db.flush()
    except Exception:
        logger.exception("管家信号提取处理失败: trace_id=%s", trace_id)
        raise


def register_all(worker):
    """将所有处理器注册到发件箱工作器。

    参数:
        worker: OutboxWorker 实例
    """
    worker.register_handler("memory_extraction", handle_memory_extraction)
    worker.register_handler("steward_extraction", handle_steward_extraction)
