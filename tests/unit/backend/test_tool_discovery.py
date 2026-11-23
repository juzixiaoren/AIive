"""Offline discovery ranking, versioning, and provider failure tests."""
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from aiive.mcp.bootstrap import activate_capability_tools, unregister_capability_tools
from aiive.tool_discovery.dense import DenseIndex, ModelIdentity
from aiive.tool_discovery.documents import DiscoveryMetadata
from aiive.tool_discovery.service import DiscoveryService
from aiive.tools.base import BaseTool
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition, ToolRegistry


class Probe(BaseTool):
    def _run(self, **kwargs):
        return kwargs


def tool(key, description="", *, namespace="fixtures", **metadata):
    return Probe(ToolDefinition(
        CapabilitySafetySchema(key, "local_builtin", "trusted", "low"),
        description=description,
        input_schema={"type": "object", "properties": {"entries": {
            "type": "array", "items": {"anyOf": [{"type": "object", "properties": {
                "checksum_digest": {"type": "string", "description": "cryptographic fingerprint"},
            }}]},
        }}},
        discovery=DiscoveryMetadata(namespace_id=namespace, **metadata),
    ))


def ids(result):
    return [item["capability_id"] for item in result["tools"]]


def test_selfdev_is_discoverable_without_embeddings():
    from aiive.tools.registry import get_tool_registry

    service = DiscoveryService()
    registry = get_tool_registry()
    assert ids(service.search(registry, "patch worker runtime"))[0] == "apply_patch_to_inactive_slot"
    assert ids(service.search(registry, "自主修复"))[0] == "run_selfdev_repair"


def search_until(service, registry, query, status="ready", **kwargs):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        result = service.search(registry, query, **kwargs)
        if result["status"] == status:
            return result
        sleep(0.01)
    pytest.fail(f"search did not reach {status}: {result}")


class SemanticProvider:
    """Synthetic embeddings pair a Chinese request with English document terms."""

    def __init__(self):
        self.documents = []
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.fail_documents = False
        self.fail_query = False

    def embed(self, text, *, input_type="document"):
        if input_type == "query" and self.fail_query:
            raise RuntimeError("query provider unavailable")
        return [1, 0] if any(word in text for word in ("报销", "invoice", "spreadsheet")) else [0, 1]

    def embed_batch(self, texts, *, batch_size):
        assert len(texts) <= batch_size
        self.entered.set()
        assert self.release.wait(5)
        if self.fail_documents:
            raise RuntimeError("document provider unavailable")
        self.documents.extend(texts)
        return [self.embed(text) for text in texts]


@pytest.fixture
def vector_search():
    provider = SemanticProvider()
    identity = ModelIdentity("local", "http://embedding/v1", "test", "r1", 2, "")
    index = DenseIndex(identity, lambda: provider, batch_size=2)
    try:
        yield DiscoveryService(index), provider
    finally:
        provider.release.set()
        index.close()


def test_chinese_semantics_with_multiple_relevant_tools(vector_search):
    service, _ = vector_search
    registry = ToolRegistry()
    registry.register(tool("reader", "Read invoice documents", namespace="pdf"))
    registry.register(tool("exporter", "Produce a spreadsheet", namespace="sheets"))
    registry.register(tool("unrelated", "Render audio", namespace="audio"))
    query = "把报销单里的金额整理成可复制的清单"
    assert ids(DiscoveryService().search(registry, query)) == []
    result = search_until(service, registry, query)
    assert result["mode"] == "hybrid"
    assert set(ids(result)) == {"reader", "exporter"}
    assert {item["namespace_id"] for item in result["namespaces"]} == {"pdf", "sheets"}
    assert all("input_schema" not in item and "parameters" not in item for item in result["tools"])


def test_bm25_indexes_technical_terms_nested_properties_and_descriptions():
    registry = ToolRegistry()
    registry.register(tool("reader", "Parse Parquet column metadata",
                           namespace_description="Column-oriented analytics"))
    registry.register(Probe(ToolDefinition(CapabilitySafetySchema("unrelated", "test", "trusted", "low"))))
    for query in ("Parquet", "column oriented analytics", "checksum digest", "cryptographic fingerprint"):
        assert ids(DiscoveryService().search(registry, query)) == ["reader"]
    registry.register(tool("cn", "支持中文发票解析"))
    assert "cn" in ids(DiscoveryService().search(registry, "怎样解析发票"))


def test_exact_matches_precede_bm25_top_k_and_namespace_members():
    registry = ToolRegistry()
    for index in range(25):
        registry.register(tool(f"keyword_{index}", "lookup lookup lookup", namespace="lookup"))
    registry.register(tool("zzz", aliases=("lookup",), raw_name="OriginalTool", namespace="other"))
    registry.register(tool("lookup", namespace="third"))
    for query, expected in (("  zzz ", ["zzz"]), (" ORIGINALTOOL ", ["zzz"])):
        assert ids(DiscoveryService().search(registry, query, limit=1)) == expected
    result = DiscoveryService().search(registry, "lookup", limit=99)
    assert set(ids(result)[:2]) == {"zzz", "lookup"}
    assert len(ids(result)) == 8
    assert result["namespaces"][0]["namespace_id"] == "lookup"


def test_rrf_combines_channel_ranks_and_retains_dense_only_and_exact(vector_search):
    service, _ = vector_search
    registry = ToolRegistry()
    registry.register(tool("rank_exact", "audio", aliases=("invoice",), namespace="exact"))
    registry.register(tool("invoice_lexical", "invoice invoice invoice", namespace="lexical"))
    registry.register(tool("other", "invoice metadata", namespace="both"))
    registry.register(tool("a_semantic", "spreadsheet", namespace="dense"))
    result = search_until(service, registry, "invoice")
    # Equal dense similarities are ordered by ID; the item in both ranked lists
    # beats the first dense-only hit. The exact alias still takes precedence.
    assert ids(result)[0] == "rank_exact"
    assert ids(result).index("other") < ids(result).index("a_semantic")
    assert len(ids(result)) == len(set(ids(result))) == 4


def test_namespace_scope_filters_before_top_k_and_global_is_available():
    registry = ToolRegistry()
    for index in range(22):
        registry.register(tool(f"primary_{index}", "kubernetes kubernetes", namespace="primary",
                               namespace_description="kubernetes"))
    registry.register(tool("scoped", "kubernetes documentation", namespace="secondary"))
    registry.register(tool("portable", "kubernetes", namespace="third"))
    service = DiscoveryService()
    result = service.search(registry, "kubernetes", namespace_ids=["secondary", "third"])
    assert set(ids(result)) == {"scoped", "portable"}
    assert {item["namespace_id"] for item in result["namespaces"]} == {"secondary", "third"}
    assert any(key.startswith("primary_") for key in ids(service.search(registry, "kubernetes", namespace_ids=[])))
    assert ids(service.search(registry, "kubernetes", namespace_ids=["missing"])) == []
    # Namespace relevance does not erase global tool hits in other groups.
    assert "portable" in ids(service.search(registry, "portable", namespace_ids=[]))


def test_warming_and_document_failure_keep_exact_and_bm25_available(vector_search):
    service, provider = vector_search
    provider.release.clear()
    provider.fail_documents = True
    registry = ToolRegistry()
    registry.register(tool("read_pdf", "Parse PDF"))
    result = service.search(registry, "PDF")
    assert provider.entered.wait(5)
    assert result["mode"] == "exact_bm25" and result["status"] == "index_warming"
    assert ids(result) == ["read_pdf"]
    provider.release.set()
    degraded = search_until(service, registry, "read_pdf", "embedding_unavailable")
    assert degraded["mode"] == "exact_bm25" and ids(degraded) == ["read_pdf"]


def test_query_failure_falls_back_after_index_is_ready(vector_search):
    service, provider = vector_search
    registry = ToolRegistry()
    registry.register(tool("reader", "invoice"))
    search_until(service, registry, "invoice")
    provider.fail_query = True
    result = service.search(registry, "invoice")
    assert (result["mode"], result["status"], ids(result)) == ("exact_bm25", "embedding_unavailable", ["reader"])


def test_registry_snapshots_and_fingerprints_isolate_incremental_updates(vector_search):
    service, provider = vector_search
    registry = ToolRegistry()
    original = tool("reader", "invoice extraction", namespace_description="Original catalog")
    registry.register(original)
    registry.register(tool("exporter", "spreadsheet", namespace="exports"))
    old = registry.snapshot()
    assert old.discovery_index() is registry.discovery_index()
    search_until(service, registry, "报销")
    initial = len(provider.documents)
    registry.register(original.with_definition(replace(
        original.definition, discovery=replace(original.definition.discovery, aliases=("fresh alias",),
                                               namespace_description="Fresh catalog"),
    )))
    assert registry.summary("reader")["descriptor_hash"] == old.summary("reader")["descriptor_hash"]
    assert registry.summary("reader")["discovery_hash"] != old.summary("reader")["discovery_hash"]
    assert "reader" in ids(search_until(service, registry, "fresh alias"))
    assert len(provider.documents) - initial == 2  # changed tool and namespace only
    assert ids(DiscoveryService().search(old, "fresh alias")) == []
    assert registry.unregister("reader")
    assert ids(search_until(service, registry, "报销")) == ["exporter"]
    assert "fixtures" not in registry.discovery_index().groups
    assert "reader" in ids(search_until(service, old, "报销"))


def test_vector_capacity_has_explicit_fallback():
    registry = ToolRegistry()
    registry.register(tool("reader", "invoice"))
    index = DenseIndex(ModelIdentity("local", "http://embedding/v1", "m", "r", 2, ""),
                       SemanticProvider, batch_size=2, capacity=1)
    try:
        result = DiscoveryService(index).search(registry, "reader")
        assert result["status"] == "index_capacity_exceeded"
        assert ids(result) == ["reader"]
    finally:
        index.close()


@pytest.mark.parametrize(("field", "value"), [
    ("aiive_embedding_model_revision", "another-revision"),
    ("aiive_embedding_provider", "openai_compatible"),
    ("aiive_embedding_model", "another-model"),
])
def test_model_identity_change_uses_a_fresh_cache(monkeypatch, field, value):
    from aiive.config import settings
    from aiive.memory import vector_projection
    from aiive.tool_discovery.service import close_discovery_service, get_discovery_service

    provider = SemanticProvider()
    monkeypatch.setattr(settings, "aiive_tool_discovery_vector_enabled", True)
    monkeypatch.setattr(settings, "aiive_embedding_dimensions", 2)
    monkeypatch.setattr(settings, "aiive_embedding_provider", "local")
    monkeypatch.setattr(settings, "aiive_embedding_model_revision", "revision")
    monkeypatch.setattr(settings, "aiive_embedding_model", "model")
    monkeypatch.setattr(vector_projection, "build_embedding_provider", lambda config: provider)
    registry = ToolRegistry()
    registry.register(tool("reader"))
    try:
        previous = get_discovery_service()
        search_until(previous, registry, "reader")
        count = len(provider.documents)
        monkeypatch.setattr(settings, field, value)
        revised = get_discovery_service()
        assert revised is not previous
        search_until(revised, registry, "reader")
        assert len(provider.documents) == 2 * count
    finally:
        close_discovery_service()


def test_mcp_enable_update_and_unload_during_embedding_outage(vector_search, monkeypatch):
    from aiive.mcp import bootstrap

    service, provider = vector_search
    provider.fail_documents = True
    registry = ToolRegistry()
    registry.register(tool("resident", "Always available"))
    search_until(service, registry, "resident", "embedding_unavailable")
    client = SimpleNamespace(configure=lambda *args: None)
    monkeypatch.setattr("aiive.mcp.bootstrap.get_runtime_client", lambda: client)
    cap = SimpleNamespace(name="pdf-mcp", capability_id="mcp:pdf-mcp", definition={
        "launch": {"runner": "python_builtin", "entry_py": str(Path(bootstrap.__file__).with_name("builtin_server.py"))},
        "tools": [{"name": "RenderPage", "description": "Parse PDF", "input_schema": {}}],
        "description": "Document conversion",
    })
    activation = activate_capability_tools(registry, cap)
    assert activation["ok"]
    result = service.search(registry, "RenderPage")
    assert ids(result) == activation["registered"]
    assert result["namespaces"][0]["namespace_id"] == "mcp:pdf-mcp"
    assert result["status"] == "embedding_unavailable"
    cap.definition["tools"][0]["description"] = "Rasterize bitmap"
    assert activate_capability_tools(registry, cap)["ok"]
    assert ids(service.search(registry, "Rasterize")) == activation["registered"]
    assert unregister_capability_tools(registry, cap) == activation["registered"]
    assert ids(service.search(registry, "RenderPage")) == []
    assert ids(service.search(registry, "resident")) == ["resident"]
