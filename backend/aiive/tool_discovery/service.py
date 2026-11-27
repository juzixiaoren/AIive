"""Worker discovery across an atomic tool catalog snapshot."""
from __future__ import annotations

from dataclasses import replace
from functools import partial
from itertools import chain
from threading import Lock
from typing import TYPE_CHECKING, Any

from aiive.config import settings
from aiive.tool_discovery import DEFAULT_LIMIT, MAX_LIMIT
from aiive.tool_discovery.dense import DenseIndex, ModelIdentity, rank_dense
from aiive.tool_discovery.documents import SearchDocument, tokens

if TYPE_CHECKING:
    from aiive.tools.registry import ToolRegistry

RRF_K = 60


def _rank(
    direct: frozenset[str], namespace_exact: frozenset[str], bm25: list[str], dense: list[str],
    namespace_bonus: dict[str, float] | None = None,
) -> list[str]:
    scores = {key: 1 / (RRF_K + 1) for key in direct}
    scores.update({key: 1 / (RRF_K + 2) for key in namespace_exact - direct})
    for channel in (bm25, dense):
        for rank, key in enumerate(channel, start=1):
            scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank)
    if namespace_bonus:
        for key in scores.keys() & namespace_bonus.keys():
            scores[key] += namespace_bonus[key]
    # Exact matches are independent of both top-K cutoffs. Prefer direct tool
    # matches to tools matched only through their namespace.
    return sorted(scores, key=lambda key: (key not in direct, key not in namespace_exact, -scores[key], key))


class DiscoveryService:
    def __init__(self, dense: DenseIndex | None = None) -> None:
        self.dense: DenseIndex | None = dense

    def warm(self, registry: ToolRegistry) -> None:
        if self.dense is not None:
            index = registry.discovery_index()
            self.dense.vectors(chain(index.tools.documents.values(), index.namespaces.documents.values()))

    def search(
        self, registry: ToolRegistry, query: str, *,
        namespace_ids: list[str] | None = None, limit: int = DEFAULT_LIMIT,
        experience_hints: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot = registry.snapshot()
        index = snapshot.discovery_index()
        query = query.strip()
        limit = min(limit, MAX_LIMIT)
        status = "disabled" if self.dense is None else "ready"
        if not query or limit <= 0:
            return {"tools": [], "namespaces": [], "mode": "exact_bm25", "status": "no_query"}
        scope = frozenset(namespace_ids or ()) & index.groups.keys() if namespace_ids else None
        allowed = None if scope is None else frozenset().union(*(index.groups[key].members for key in scope))
        query_tokens = set(tokens(query))
        exact_term = query.casefold()
        direct = index.tools.exact(exact_term, allowed)
        exact_namespaces = index.namespaces.exact(exact_term, scope)
        namespace_exact = frozenset().union(*(index.groups[key].members for key in exact_namespaces))
        tool_bm25 = index.tools.bm25(query_tokens, allowed)
        namespace_bm25 = index.namespaces.bm25(query_tokens, scope)
        tool_dense: list[str] = []
        namespace_dense: list[str] = []
        hybrid = False
        if self.dense is not None:
            tool_docs = (list(index.tools.documents.values()) if allowed is None
                         else [index.tools.documents[key] for key in sorted(allowed)])
            namespace_docs = (list(index.namespaces.documents.values()) if scope is None
                              else [index.namespaces.documents[key] for key in sorted(scope)])
            status, vectors = self.dense.vectors(chain(tool_docs, namespace_docs))
            if vectors:
                vector = self.dense.query(query)
                if vector is None:
                    status = "embedding_unavailable"
                else:
                    hybrid = True
                    tool_dense = rank_dense(tool_docs, vectors, vector)
                    namespace_dense = rank_dense(namespace_docs, vectors, vector)
            elif status == "ready":
                status = "empty_catalog"

        namespaces = _rank(exact_namespaces, frozenset(), namespace_bm25, namespace_dense)
        bonus = {
            member: 0.25 / (RRF_K + rank)
            for rank, key in enumerate(namespaces, start=1)
            for member in index.groups[key].members
        }
        selected = _rank(direct, namespace_exact, tool_bm25, tool_dense, bonus)[:limit]
        used = []
        if experience_hints:
            baseline = selected
            augmented = index
            for hint in experience_hints:
                key, alias = hint["capability_id"], hint["alias"]
                if key not in index.metadata or (allowed is not None and key not in allowed):
                    continue
                metadata = augmented.metadata[key]
                document = augmented.tools.documents[key]
                if alias in document.exact_terms:
                    continue
                metadata = replace(metadata, aliases=(*metadata.aliases, alias))
                augmented = augmented.updated(
                    key, SearchDocument.build(key, (*document.exact_terms, alias), [document.text, alias]), metadata,
                )
            if augmented is not index:
                selected = _rank(
                    augmented.tools.exact(exact_term, allowed), namespace_exact,
                    augmented.tools.bm25(query_tokens, allowed), tool_dense, bonus,
                )[:limit]
                used = [{field: hint[field] for field in ("id", "version", "kind")}
                        for hint in experience_hints if hint["capability_id"] in selected and (
                            hint["capability_id"] not in baseline
                            or selected.index(hint["capability_id"]) < baseline.index(hint["capability_id"])
                        )]
        namespace_hits = dict.fromkeys([*namespaces, *(index.metadata[key].namespace_id for key in selected)])
        tools = []
        for key in selected:
            entry = snapshot.summary(key)
            if entry is not None:
                entry["description"] = entry["description"][:500]
                tools.append(entry)
        return {
            "namespaces": [index.groups[key].summary(key) for key in list(namespace_hits)[:MAX_LIMIT]],
            "tools": tools,
            "mode": "hybrid" if hybrid else "exact_bm25",
            "status": status,
            **({"experience_uses": used} if used else {}),
        }


_service_lock = Lock()
_lexical = DiscoveryService()
_hybrid: DiscoveryService | None = None


def get_discovery_service() -> DiscoveryService:
    global _hybrid
    if not settings.aiive_tool_discovery_vector_enabled:
        return _lexical

    from aiive.memory.vector_projection import build_embedding_provider

    with _service_lock:
        config = settings.model_copy()
        identity = ModelIdentity(
            config.aiive_embedding_provider, config.aiive_embedding_base_url.rstrip("/"),
            config.aiive_embedding_model, config.aiive_embedding_model_revision,
            config.aiive_embedding_dimensions, config.aiive_embedding_query_prefix,
        )
        dense = _hybrid.dense if _hybrid else None
        if dense is None or dense.identity != identity:
            if dense is not None:
                dense.close()
            _hybrid = DiscoveryService(DenseIndex(
                identity, partial(build_embedding_provider, config), batch_size=config.aiive_embedding_batch_size,
            ))
        assert _hybrid is not None
        return _hybrid


def close_discovery_service() -> None:
    global _hybrid
    with _service_lock:
        if _hybrid is not None and _hybrid.dense is not None:
            _hybrid.dense.close()
        _hybrid = None
