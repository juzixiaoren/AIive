"""Search metadata and documents, independent of the execution contract."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiive.tools.base import BaseTool


@dataclass(frozen=True)
class DiscoveryMetadata:
    namespace_id: str = ""
    namespace_name: str = ""
    namespace_description: str = ""
    raw_name: str = ""
    aliases: tuple[str, ...] = ()
    use_cases: tuple[str, ...] = ()
    input_concepts: tuple[str, ...] = ()
    output_concepts: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


def tokens(text: str) -> list[str]:
    from aiive.retrieval.index_tokenizer import tokenize_for_index

    # Underscores join identifiers in the shared tokenizer. Index their words too.
    return [word for word, _ in tokenize_for_index(text.replace("_", " "), unique=False)]


def schema_terms(schema: dict[str, Any]) -> list[str]:
    """Walk JSON Schema keywords, including definitions, arrays and alternatives."""
    terms: list[str] = []
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key in ("description", "title"):
            if isinstance(node.get(key), str):
                terms.append(node[key])
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            children = node.get(key)
            if isinstance(children, dict):
                if key in {"properties", "patternProperties"}:
                    terms.extend(sorted(children))
                stack.extend(children[name] for name in sorted(children, reverse=True))
        for key in ("items", "additionalItems", "contains", "additionalProperties", "unevaluatedProperties",
                    "unevaluatedItems", "propertyNames", "not", "if", "then", "else"):
            child = node.get(key)
            stack.extend(reversed(child) if isinstance(child, list) else [child])
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = node.get(key)
            if isinstance(children, list):
                stack.extend(reversed(children))
    return terms


@dataclass(frozen=True)
class SearchDocument:
    key: str
    exact_terms: frozenset[str]
    text: str
    discovery_hash: str
    frequencies: Counter[str]
    length: int

    @classmethod
    def build(cls, key: str, exact: tuple[str, ...], fields: list[str]) -> SearchDocument:
        # Tokenize each field independently to preserve BM25 term frequency.
        frequencies = Counter(word for field in fields for word in tokens(field))
        text = "\n".join(field for field in fields if field)
        exact_terms = frozenset(term.strip().casefold() for term in exact if term.strip())
        payload = json.dumps([key, sorted(exact_terms), fields], ensure_ascii=False)
        return cls(key, exact_terms, text, hashlib.sha256(payload.encode()).hexdigest(),
                   frequencies, frequencies.total())


def tool_document(tool: BaseTool) -> tuple[DiscoveryMetadata, SearchDocument]:
    safety = tool.safety
    metadata = tool.definition.discovery
    namespace_id = metadata.namespace_id or safety.definition_source
    metadata = replace(
        metadata, namespace_id=namespace_id,
        namespace_name=metadata.namespace_name or namespace_id,
        raw_name=metadata.raw_name or safety.capability_id,
    )
    names = tuple(dict.fromkeys((safety.capability_id, metadata.raw_name, *metadata.aliases)))
    document = SearchDocument.build(safety.capability_id, names, [
        *names, tool.description, *metadata.use_cases, *metadata.input_concepts,
        *metadata.output_concepts, *metadata.tags, metadata.namespace_id,
        metadata.namespace_name, metadata.namespace_description, *schema_terms(tool.input_schema or {}),
    ])
    return metadata, document
