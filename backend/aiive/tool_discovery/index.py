"""Copy-on-write lexical indexes shared by registry snapshots and overlays."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from aiive.tool_discovery.documents import DiscoveryMetadata, SearchDocument

BM25_TOP_K = 20


@dataclass(frozen=True)
class LexicalIndex:
    documents: dict[str, SearchDocument] = field(default_factory=dict)
    _postings: dict[str, dict[str, int]] = field(default_factory=dict)
    _exact: dict[str, frozenset[str]] = field(default_factory=dict)
    _total_length: int = 0

    def updated(self, key: str, document: SearchDocument | None) -> LexicalIndex:
        old = self.documents.get(key)
        if old is document or (old and document and old.discovery_hash == document.discovery_hash):
            return self
        documents = self.documents.copy()
        postings = self._postings.copy()
        exact = self._exact.copy()
        if document:
            documents[key] = document
        else:
            documents.pop(key)
        previous = old.frequencies if old else {}
        frequencies = document.frequencies if document else {}
        for token in previous.keys() | frequencies.keys():
            posting = postings.get(token, {}).copy()
            if token in frequencies:
                posting[key] = frequencies[token]
            else:
                posting.pop(key)
            if posting:
                postings[token] = posting
            else:
                postings.pop(token)
        previous_terms = old.exact_terms if old else frozenset()
        terms = document.exact_terms if document else frozenset()
        for term in previous_terms ^ terms:
            matches = exact.get(term, frozenset())
            matches = matches | {key} if term in terms else matches - {key}
            if matches:
                exact[term] = matches
            else:
                exact.pop(term)
        length = self._total_length - (old.length if old else 0) + (document.length if document else 0)
        return LexicalIndex(documents, postings, exact, length)

    def exact(self, normalized_query: str, allowed: frozenset[str] | None) -> frozenset[str]:
        matches = self._exact.get(normalized_query, frozenset())
        return matches if allowed is None else matches & allowed

    def bm25(self, query_tokens: set[str], allowed: frozenset[str] | None) -> list[str]:
        count = len(self.documents) if allowed is None else len(allowed)
        if not count:
            return []
        length = self._total_length if allowed is None else sum(self.documents[key].length for key in allowed)
        average = length / count or 1.0
        scores: dict[str, float] = defaultdict(float)
        k1, b = 1.2, 0.75
        for token in sorted(query_tokens):
            posting = self._postings.get(token, {})
            keys = posting.keys() if allowed is None else posting.keys() & allowed
            idf = math.log1p((count - len(keys) + 0.5) / (len(keys) + 0.5))
            for key in keys:
                tf = posting[key]
                normalization = 1 - b + b * self.documents[key].length / average
                scores[key] += idf * tf * (k1 + 1) / (tf + k1 * normalization)
        return sorted(scores, key=lambda key: (-scores[key], key))[:BM25_TOP_K]


@dataclass(frozen=True)
class Namespace:
    name: str
    description: str
    members: frozenset[str]

    def summary(self, namespace_id: str) -> dict[str, Any]:
        return {
            "namespace_id": namespace_id,
            "namespace_name": self.name,
            "namespace_description": self.description[:500],
            "tool_count": len(self.members),
        }


@dataclass(frozen=True)
class DiscoveryIndex:
    tools: LexicalIndex = field(default_factory=LexicalIndex)
    namespaces: LexicalIndex = field(default_factory=LexicalIndex)
    groups: dict[str, Namespace] = field(default_factory=dict)
    metadata: dict[str, DiscoveryMetadata] = field(default_factory=dict)

    def updated(
        self, key: str, document: SearchDocument | None, metadata: DiscoveryMetadata | None,
    ) -> DiscoveryIndex:
        previous = self.metadata.get(key)
        if previous == metadata and self.tools.documents.get(key) == document:
            return self
        tool_metadata = self.metadata.copy()
        if metadata:
            tool_metadata[key] = metadata
        else:
            tool_metadata.pop(key, None)
        groups = self.groups.copy()
        namespaces = self.namespaces
        for namespace_id in {item.namespace_id for item in (previous, metadata) if item}:
            group = groups.get(namespace_id)
            members = group.members - {key} if group else frozenset()
            if metadata and metadata.namespace_id == namespace_id:
                members |= {key}
            if not members:
                groups.pop(namespace_id)
                namespaces = namespaces.updated(namespace_id, None)
                continue
            names = sorted({tool_metadata[member].namespace_name for member in members})
            descriptions = sorted({
                tool_metadata[member].namespace_description for member in members
                if tool_metadata[member].namespace_description
            })
            groups[namespace_id] = Namespace(names[0], "\n".join(descriptions), members)
            namespaces = namespaces.updated(namespace_id, SearchDocument.build(
                namespace_id, (namespace_id,), [namespace_id, *names, *descriptions],
            ))
        return DiscoveryIndex(self.tools.updated(key, document), namespaces, groups, tool_metadata)
