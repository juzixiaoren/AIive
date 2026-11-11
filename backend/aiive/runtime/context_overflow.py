"""Global overflow packing. Pure candidate decisions, real prompt recounts, no DB/LLM."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiive.runtime.context_policy import (
    POLICY_VERSION,
    ContextCandidateDecision,
    eviction_key,
    rank_source,
)
from aiive.runtime.token_counter import TokenCounter
from aiive.runtime.token_models import TokenCount


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


@dataclass
class HistoryTurn:
    turn_id: str
    events: list[dict[str, Any]]
    protected: bool = False


@dataclass
class _Candidate:
    decision: ContextCandidateDecision
    remove: Callable[[], None]
    partition: str
    group: str


@dataclass
class ContextCandidates:
    history: list[HistoryTurn] = field(default_factory=list)  # chronological
    retrieval: list[Any] = field(default_factory=list)
    graph: list[Any] = field(default_factory=list)
    summaries: list[Any] = field(default_factory=list)
    working_state: dict[str, Any] = field(default_factory=dict)
    core: list[Any] = field(default_factory=list)
    removed: dict[str, list[str]] = field(default_factory=dict)
    fixed: dict[str, str] = field(default_factory=dict)
    selection_trace: dict[str, Any] = field(default_factory=dict)
    _entries: list[_Candidate] = field(default_factory=list, init=False, repr=False)
    _eviction_order: list[str] = field(default_factory=list, init=False, repr=False)
    _prepared: bool = field(default=False, init=False, repr=False)

    def prepare(self, estimate: Callable[[str, Any], int | None] | None = None) -> None:
        if self._prepared:
            return
        self._prepared = True

        def add(source: str, sid: str, item: Any, tier: int, partition: str,
                collection: list[Any] | None = None, score: float | None = None,
                version: Any = None, group: str | None = None) -> ContextCandidateDecision:
            cid = f"{source}:{sid}"
            size = estimate(source, item) if estimate else None
            d = ContextCandidateDecision(cid, source, sid.split(":", 1)[1] if source == "retrieval" else sid, tier, native_score=score,
                                         estimated_tokens=size, source_version=str(version) if version is not None else None)
            def remove() -> None:
                if collection is not None:
                    # Identity removal avoids equality collisions for structured items.
                    collection.pop(next(i for i, value in enumerate(collection) if value is item))
            self._entries.append(_Candidate(d, remove, partition, group or cid))
            return d

        rank_source([add("history", t.turn_id, t, 3 if t.protected else 1, "recent_messages", self.history)
                     for t in reversed(self.history)])
        rank_source([add("retrieval", f"{h.source_type}:{h.source_id}", h, 1,
                         "retrieved_memory" if h.source_type == "memory_record" else "retrieved_history_summary",
                         self.retrieval, h.final_score, h.source_version)
                     for h in sorted(self.retrieval, key=lambda h: (-h.final_score, h.source_type, h.source_id))])
        graph_decisions = []
        for r in sorted(self.graph, key=lambda r: (-r.score, -r.confidence, r.hop,
                                                   str(_get(r, "relation_id", r.source_memory_id)))):
            sid = str(_get(r, "relation_id", r.source_memory_id))
            group = None
            if _get(r, "cardinality") == "single" and _get(r, "is_conflicted"):
                group = "graph-conflict:" + repr((_get(r, "subject_entity_id"), _get(r, "predicate"),
                                                  _get(r, "scope_type"), _get(r, "scope_id")))
            graph_decisions.append(add("graph", sid, r, 1, "graph", self.graph, r.score, group=group))
        rank_source(graph_decisions)
        ranked_summaries = sorted(enumerate(self.summaries),
                                  key=lambda p: (str(_get(p[1], "created_at", "")), p[0]), reverse=True)
        rank_source([add("segment_summary", str(s.id), s,
                         2 if any(_get(s, k) for k in ("open_loops", "active_constraints", "unresolved_failures", "important_tool_results")) else 1,
                         "segment_summaries", self.summaries, version=_get(s, "summary_version"))
                     for _, s in ranked_summaries])
        ws_decisions = []
        for key, value in self.working_state.items():
            if key not in ("current_objective", "open_loops", "active_constraints", "pending_approvals",
                           "artifact_refs", "verified_tool_states", "uncommitted_side_effects", "running_tool_state") or not value:
                continue
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                priority = str(_get(item, "priority", "P2"))
                tier = 0 if key == "artifact_refs" or (key == "open_loops" and priority == "P3") else 1
                if key not in ("artifact_refs", "open_loops") or priority in ("P0", "P1"):
                    tier = 3
                ws_decisions.append(add("working_state", f"{key}:{index}", item, tier, "working_state",
                                        value if isinstance(value, list) else None, version=self.working_state.get("version")))
        # P3 before P2 for eviction; stable field/index tie breaking.
        rank_source(sorted(ws_decisions, key=lambda d: (-d.retention_tier, d.source_id)))
        roles = {"core.human_identity": 0, "core.interaction_defaults": 1, "core.agent_persona": 2}
        rank_source([add("core", b.block_name, b, 2 if b.block_name == "core.human_identity" else 0,
                         "core_memory", self.core, version=_get(b, "projection_version"))
                     for b in sorted(self.core, key=lambda b: (roles.get(b.block_name, 9), b.block_name))])
        for key, value in self.fixed.items():
            if value:
                add(key, key, value, 3, key)
        self._sync_trace()

    def _sync_trace(self) -> None:
        self.selection_trace = {"policy_version": POLICY_VERSION,
                                "candidates": [e.decision.to_dict() for e in self._entries],
                                "eviction_order": list(self._eviction_order)}

    def remove_next(self) -> bool:
        self.prepare()
        groups: dict[str, list[_Candidate]] = {}
        for entry in self._entries:
            if entry.decision.selected:
                groups.setdefault(entry.group, []).append(entry)
        removable = [g for g in groups.values() if all(e.decision.retention_tier < 3 for e in g)]
        if not removable:
            return False
        group = min(removable, key=lambda g: eviction_key([e.decision for e in g]))
        for entry in group:
            entry.remove()
            self._eviction_order.append(entry.decision.candidate_id)
            entry.decision.selected = False
            entry.decision.exclusion_reason = "context_budget_exceeded" + (":atomic_graph_conflict" if len(group) > 1 else "")
            self.removed.setdefault(entry.partition, []).append(entry.decision.candidate_id)
        self._sync_trace()
        return True


class ContextOverflowPacker:
    def pack(
        self, candidates: ContextCandidates,
        render: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
        counter: TokenCounter, model: str, context_window: int, requested_output: int,
        estimate: Callable[[str, Any], int | None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], TokenCount, bool]:
        candidates.prepare(estimate)
        while True:
            messages, tools = render()
            count = counter.count_messages(model, messages, tools)
            if count.safe_tokens + requested_output <= context_window:
                return messages, tools, count, True
            if not candidates.remove_next():
                return messages, tools, count, False
