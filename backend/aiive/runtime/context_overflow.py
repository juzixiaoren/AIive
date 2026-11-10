"""Source-aware overflow packing. Pure logic: no DB reads and no LLM calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from aiive.runtime.token_counter import TokenCounter
from aiive.runtime.token_models import TokenCount


@dataclass
class HistoryTurn:
    turn_id: str
    events: list[dict[str, Any]]
    protected: bool = False


@dataclass
class ContextCandidates:
    history: list[HistoryTurn] = field(default_factory=list)
    retrieval: list[Any] = field(default_factory=list)
    graph: list[Any] = field(default_factory=list)
    summaries: list[Any] = field(default_factory=list)  # oldest first
    working_state: dict[str, Any] = field(default_factory=dict)
    core: list[Any] = field(default_factory=list)  # most important first
    removed: dict[str, list[str]] = field(default_factory=dict)

    def record(self, source: str, reason: str) -> bool:
        self.removed.setdefault(source, []).append(reason)
        return True

    def remove_next(self) -> bool:
        for index, turn in enumerate(self.history):
            if not turn.protected:
                self.history.pop(index)
                return self.record("recent_messages", f"oldest complete Turn: {turn.turn_id}")
        if self.retrieval:
            hit = min(self.retrieval, key=lambda h: (h.final_score, h.source_id))
            self.retrieval.remove(hit)
            source = "retrieved_memory" if hit.source_type == "memory_record" else "retrieved_history_summary"
            return self.record(source, f"lowest final_score: {hit.source_id}")
        if self.graph:
            relation = min(self.graph, key=lambda r: (r.score, -r.hop, r.confidence))
            self.graph.remove(relation)
            return self.record("graph", "lowest score / farther hop: " + str(relation.source_memory_id))
        if self.summaries:
            summary = self.summaries.pop(0)
            return self.record("segment_summaries", "oldest complete summary: " + str(summary.id))
        artifacts = self.working_state.get("artifact_refs") or []
        if artifacts:
            artifacts.pop()
            return self.record("working_state", "artifact reference")
        loops = self.working_state.get("open_loops") or []
        # P0/P1 loops may carry necessary execution facts. Only optional lower-priority items go.
        optional: list[tuple[int, dict[str, Any]]] = [(i, item) for i, item in enumerate(loops)
                    if str(item.get("priority", "P2")) not in ("P0", "P1")]
        if optional:
            index, _ = max(optional, key=lambda pair: (str(pair[1].get("priority", "P2")), pair[0]))
            loops.pop(index)
            return self.record("working_state", "low priority open loop")
        if self.core:
            block = self.core.pop()
            return self.record("core_memory", "complete block: " + block.block_name)
        return False


class ContextOverflowPacker:
    def pack(
        self, candidates: ContextCandidates,
        render: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
        counter: TokenCounter, model: str, context_window: int, requested_output: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], TokenCount, bool]:
        while True:
            messages, tools = render()
            count = counter.count_messages(model, messages, tools)
            if count.safe_tokens + requested_output <= context_window:
                return messages, tools, count, True
            if not candidates.remove_next():
                return messages, tools, count, False
