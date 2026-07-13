"""
Outbox handlers: async job processing for memory extraction and projection.
"""
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.context.run_context import RunContext
from aiive.core.llm_client import LLMClient
from aiive.memory.extraction_policy import MemoryExtractionPolicy
from aiive.memory.memory_extractor import UnifiedMemoryExtractor
from aiive.memory.memory_types import MemoryProposal
from aiive.memory.memory_write_service import MemoryWriteService

if TYPE_CHECKING:
    from aiive.worker.outbox_worker import OutboxWorker

logger = logging.getLogger(__name__)


def _get_llm_client() -> LLMClient:
    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


# ============================================================================
# Extraction
# ============================================================================


def handle_memory_extraction(
    db: Session,
    payload: dict[str, Any],
    trace_id: str | None,
) -> None:
    """Unified memory extraction handler. Structural skip only."""
    user_message: str = payload.get("user_message", "")
    reply: str = payload.get("reply", "")
    thread_id: str = payload.get("thread_id", "")

    if MemoryExtractionPolicy.should_skip_system_message(user_message):
        return

    try:
        llm = _get_llm_client()
        extractor = UnifiedMemoryExtractor(llm)
        writer = MemoryWriteService(db)

        proposals: list[MemoryProposal] = extractor.extract(
            user_message=user_message, reply=reply,
            trace_id=trace_id, thread_id=thread_id,
        )

        # Log extraction trace for debugging
        if proposals:
            from aiive.runtime.event_logger import EventLogger
            _elog = EventLogger(db)
            _elog.log_event(
                trace_id=trace_id or "", thread_id=thread_id,
                event_type="memory_extracted",
                payload={
                    "source_message": user_message[:200],
                    "proposals": [
                        {
                            "proposal_id": p.proposal_id,
                            "memory_type": p.memory_type,
                            "canonical_key": p.canonical_key,
                            "content_preview": (p.content or "")[:100],
                            "confidence": p.confidence,
                            "importance": p.importance,
                            "proposed_operation": p.proposed_operation,
                        }
                        for p in proposals
                    ],
                    "total": len(proposals),
                },
            )

        for proposal in proposals:
            run_ctx = RunContext(
                thread_id=thread_id, trace_id=trace_id or "", source="outbox_worker",
            )
            writer.write(proposal, run_context=run_ctx)
            db.flush()

    except Exception:
        logger.exception("Memory extraction failed: trace_id=%s", trace_id)
        raise


def handle_steward_extraction(
    db: Session,
    payload: dict[str, Any],
    trace_id: str | None,
) -> None:
    """DEPRECATED: delegates to memory_extraction."""
    logger.info(
        "steward_extraction is deprecated; delegating to memory_extraction."
        + " Remove steward_extraction jobs from AgentGraph._finalize()."
    )
    handle_memory_extraction(db, payload, trace_id)


# ============================================================================
# Projection stubs
# ============================================================================


def handle_memory_vector_upsert(
    db: Session,
    payload: dict[str, Any],
    _trace_id: str | None,
) -> None:
    memory_id: str = payload.get("memory_id", "")
    record_version: int = payload.get("record_version", 0)
    from aiive.db.models import MemoryRecord

    record = db.get(MemoryRecord, memory_id)
    if record is None:
        return
    if (record.record_version or 0) > record_version:
        logger.info("Vector upsert skipped: stale version")
        return
    logger.debug("Vector upsert stub: mid=%s", memory_id)


def handle_memory_vector_delete(
    _db: Session,
    payload: dict[str, Any],
    _trace_id: str | None,
) -> None:
    logger.debug("Vector delete stub: mid=%s", payload.get("memory_id", ""))


def handle_memory_markdown_project(
    db: Session,
    payload: dict[str, Any],
    _trace_id: str | None,
) -> None:
    memory_id: str = payload.get("memory_id", "")
    record_version: int = payload.get("record_version", 0)
    from aiive.db.models import MemoryRecord

    record = db.get(MemoryRecord, memory_id)
    if record is None:
        return
    if (record.record_version or 0) > record_version:
        logger.info("Markdown projection skipped: stale version")
        return
    logger.debug("Markdown projection stub: mid=%s", memory_id)


def handle_memory_cache_invalidate(
    _db: Session,
    payload: dict[str, Any],
    _trace_id: str | None,
) -> None:
    logger.debug("Cache invalidate stub: mid=%s", payload.get("memory_id", ""))


def handle_core_memory_refresh(
    db: Session,
    payload: dict[str, Any],
    _trace_id: str | None,
) -> None:
    """Rebuild and persist the Core Memory projection blocks.

    Reconstructs the small, stable Core Memory projection from memory_records
    (the single source of truth). Stale-version protection is applied inside
    CoreMemoryProjection.refresh() so out-of-order jobs are skipped.
    """
    memory_id: str = payload.get("memory_id", "")
    record_version: int = payload.get("record_version", 0)
    from aiive.memory.core_memory_projection import CoreMemoryProjection
    from aiive.memory.recall_config import RecallConfig

    CoreMemoryProjection.refresh(db, memory_id, record_version, RecallConfig())


# ============================================================================
# Registration
# ============================================================================


def register_all(worker: "OutboxWorker") -> None:
    worker.register_handler("memory_extraction", handle_memory_extraction)
    worker.register_handler("steward_extraction", handle_steward_extraction)
    worker.register_handler("memory_vector_upsert", handle_memory_vector_upsert)
    worker.register_handler("memory_vector_delete", handle_memory_vector_delete)
    worker.register_handler("memory_markdown_project", handle_memory_markdown_project)
    worker.register_handler("memory_cache_invalidate", handle_memory_cache_invalidate)
    worker.register_handler("core_memory_refresh", handle_core_memory_refresh)
