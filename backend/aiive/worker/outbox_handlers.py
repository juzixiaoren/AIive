"""Outbox handlers: async job processing with V20 MemoryGate + MemoryWriteService."""

from aiive.config import settings
from aiive.core.llm_client import LLMClient
from aiive.memory.memory_extractor import MemoryExtractor
from aiive.memory.memory_gate import MemoryGate, MemoryGateInput
from aiive.memory.memory_write_service import MemoryWriteService
from aiive.memory.steward_signal_extractor import StewardSignalExtractor


def _get_llm_client() -> LLMClient:
    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


def handle_memory_extraction(db, payload: dict, trace_id: str | None) -> None:
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
        content = c.get("content", "")
        memory_type = c.get("memory_type", "fact")
        confidence = c.get("confidence", 0.5)

        decision = gate.decide(MemoryGateInput(
            content=content,
            user_message=payload["user_message"],
            source="auto_extraction",
            intent_type=intent_type,
            execution_mode=execution_mode,
            should_execute=should_execute,
            evidence_source=evidence_source,
            extracted_memory_type=memory_type,
            confidence=confidence,
            trace_id=trace_id or "",
        ))

        if decision.decision == "reject":
            continue

        writer.write(decision, content)
        db.flush()


def handle_steward_extraction(db, payload: dict, trace_id: str | None) -> None:
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
        content = s.get("content", "")
        signal_type = s.get("signal_type", "routine")
        confidence = s.get("confidence", 0.7)

        decision = gate.decide(MemoryGateInput(
            content=content,
            user_message=payload["user_message"],
            source="steward",
            intent_type=intent_type,
            execution_mode=execution_mode,
            should_execute=should_execute,
            evidence_source=evidence_source,
            extracted_memory_type=signal_type,
            confidence=confidence,
            trace_id=trace_id or "",
        ))

        if decision.decision == "reject":
            continue

        writer.write(decision, content)
        db.flush()


def register_all(worker):
    worker.register_handler("memory_extraction", handle_memory_extraction)
    worker.register_handler("steward_extraction", handle_steward_extraction)
