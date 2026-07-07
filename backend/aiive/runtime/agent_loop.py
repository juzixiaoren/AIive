from typing import Optional

from sqlalchemy.orm import Session

from aiive.core.context_builder import ContextBuilder
from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.db.models import ContextSnapshot
from aiive.memory.memory_extractor import MemoryExtractor
from aiive.memory.memory_gate import MemoryGate
from aiive.memory.memory_store import MemoryStore
from aiive.memory.steward_signal_extractor import StewardSignalExtractor
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.trace import Trace


class AgentLoop:
    def __init__(self, llm_client: LLMClient, db: Session):
        self._llm_client = llm_client
        self._db = db
        self._logger = EventLogger(db)
        self._thread_state = ThreadState(db)
        self._ctx_builder = ContextBuilder()
        self._memory_store = MemoryStore(db)
        self._memory_extractor = MemoryExtractor(llm_client)
        self._steward_extractor = StewardSignalExtractor(llm_client)
        self._memory_gate = MemoryGate()

    def run(self, message: str, thread_id: Optional[str] = None) -> dict:
        trace = Trace.new()
        thread = self._thread_state.get_or_create_thread(thread_id)

        if not thread_id:
            self._logger.log_event(
                trace_id=trace.trace_id,
                thread_id=thread.id,
                event_type="chat_started",
            )

        self._logger.log_event(
            trace_id=trace.trace_id,
            thread_id=thread.id,
            event_type="user_message",
            payload={"content": message},
        )

        # Load active memories
        active_memories = self._memory_store.get_active()
        memory_data = [
            {"id": m.id, "content": m.content, "memory_type": m.memory_type}
            for m in active_memories
        ]

        history = self._thread_state.get_recent_messages(thread.id)
        messages, context_items, meta = self._ctx_builder.build(
            history=history,
            current_message=message,
            active_memories=memory_data,
        )

        if meta.get("truncated"):
            self._logger.log_event(
                trace_id=trace.trace_id,
                thread_id=thread.id,
                event_type="context_truncated",
                payload={
                    "total_messages": meta["truncated_from"],
                    "kept_messages": meta["working_limit"],
                },
            )

        response: LLMResponse = self._llm_client.chat(
            messages, trace_id=trace.trace_id
        )

        self._logger.log_llm_call(
            trace_id=response.trace_id,
            thread_id=thread.id,
            model=response.model,
            latency_ms=response.latency_ms,
            input_preview=message[:500],
            output_preview=response.content[:500],
        )

        self._logger.log_event(
            trace_id=response.trace_id,
            thread_id=thread.id,
            event_type="llm_response",
            payload={"content": response.content},
        )

        # Memory extraction
        candidates = self._memory_extractor.extract(
            user_message=message,
            reply=response.content,
            trace_id=trace.trace_id,
        )

        for candidate in candidates:
            content = candidate.get("content", "")
            memory_type = candidate.get("memory_type", "fact")
            confidence = candidate.get("confidence", 0.5)

            state = self._memory_gate.decide(content, message)
            if state == "reject":
                continue

            record = self._memory_store.create(
                content=content,
                memory_type=memory_type,
                lifecycle_state=state,
                source_event_id=None,
                confidence=confidence,
                lineage=f"trace:{trace.trace_id}",
            )

            self._logger.log_event(
                trace_id=trace.trace_id,
                thread_id=thread.id,
                event_type=f"memory_{state}",
                payload={
                    "memory_id": record.id,
                    "content": content,
                    "memory_type": memory_type,
                    "confidence": confidence,
                },
            )

        # Steward signal extraction
        steward_signals = self._steward_extractor.extract(
            user_message=message,
            reply=response.content,
            trace_id=trace.trace_id,
        )

        for signal in steward_signals:
            content = signal.get("content", "")
            signal_type = signal.get("signal_type", "routine")
            confidence = signal.get("confidence", 0.7)
            schedule_text = signal.get("schedule_text", "")

            record = self._memory_store.create(
                content=content,
                memory_type=signal_type,
                lifecycle_state="active",
                source_event_id=None,
                confidence=confidence,
                lineage=f"trace:{trace.trace_id}",
            )

            steward_payload = {
                "memory_id": record.id,
                "content": content,
                "signal_type": signal_type,
                "confidence": confidence,
            }
            if schedule_text:
                steward_payload["schedule_text"] = schedule_text

            self._logger.log_event(
                trace_id=trace.trace_id,
                thread_id=thread.id,
                event_type="steward_signal",
                payload=steward_payload,
            )

        # Save context snapshot
        snapshot = ContextSnapshot(
            trace_id=response.trace_id,
            thread_id=thread.id,
            stable_prefix_hash=meta["stable_prefix_hash"],
            context_items=[
                {
                    "item_id": ci.item_id,
                    "kind": ci.kind,
                    "source": ci.source,
                    "trust_level": ci.trust_level,
                    "content_preview": ci.content_preview[:200],
                    "token_estimate": ci.token_estimate,
                }
                for ci in context_items
            ],
            meta={
                "working_limit": meta["working_limit"],
                "truncated": meta["truncated"],
                "truncated_from": meta["truncated_from"],
                "total_items": len(context_items),
                "total_tokens": sum(ci.token_estimate for ci in context_items),
                "injected_memory_ids": meta.get("injected_memory_ids", []),
            },
        )
        self._db.add(snapshot)

        self._db.commit()

        return {
            "reply": response.content,
            "thread_id": thread.id,
            "trace_id": response.trace_id,
        }
