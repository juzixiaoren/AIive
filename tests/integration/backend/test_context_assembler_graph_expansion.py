"""端到端接线：ContextAssembler 在普通召回之后真正触发关系扩展并注入上下文。

覆盖三条验收点：
1) 召回结果作为种子触发 GraphExpansionService.expand（不依赖额外 LLM 判断）；
2) 关系与时间信息确实进入发给模型的 messages，而不是只落库；
3) 取消旧分区比例预算，关系块完整进入全局装配。
"""
from __future__ import annotations

from datetime import UTC, datetime

from aiive.memory.graph_expansion import GraphExpansionService
from aiive.memory.graph_projection import GraphProjectionService
from aiive.memory.graph_types import GraphExpansionResult, GraphFact, GraphRelationView
from aiive.memory.memory_store import MemoryStore
from aiive.memory.memory_types import EvidenceItem, MemoryProposal, TrustLevel
from aiive.memory.recall_models import MemoryRecallItem, MemoryRecallPack
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.context_budget import ContextBudget
from aiive.runtime.token_models import ModelProfile, TokenCount

from tests.integration.backend._util import new_thread


class _FakeTokenCounter:
    """固定返回低 token 数，保证一次通过 hard gate（预算逻辑单独测）。"""

    def count_messages(self, model, messages, tools=None) -> TokenCount:
        return TokenCount(
            estimated_tokens=100, safety_margin_tokens=0, safe_tokens=100,
        )


class _StubWorkingStateService:
    def context_fields(self, *args, **kwargs):
        return {}

    def render_context_fields(self, fields) -> str:
        return ""




def _budget(window: int = 200000) -> ContextBudget:
    return ContextBudget(model_context_window=window, reserved_output_tokens=2_000)


def _assembler(window: int = 200000) -> ContextAssembler:
    return ContextAssembler(
        token_counter=_FakeTokenCounter(),
        budget=_budget(window),
        profile=ModelProfile(
            provider="deepseek", model_id="deepseek-chat",
            full_name="deepseek/deepseek-chat",
            context_window=window, max_output_tokens=2_000,
        ),
    )


def _seed_memory(db, *, subject="用户", object_="AIive") -> str:
    # 用 global scope：本用例的 ScopeContext 来自 thread 链（thread → global），
    # project scope 的关系由 unit 层的 scope 隔离用例覆盖。
    proposal = MemoryProposal(
        memory_type="knowledge",
        canonical_key="knowledge.aiive",
        scope_type="global",
        scope_id=None,
        content="我在做 AIive 项目",
        graph_facts=[GraphFact(
            subject=subject, predicate="works_on", object=object_,
            confidence=0.92, source_span="我在做 AIive 项目",
            valid_from=datetime(2026, 1, 2, tzinfo=UTC),
        )],
        evidence=[EvidenceItem(
            source_type="user_message",
            trust_level=TrustLevel.TRUSTED.value,
            content_span="我在做 AIive 项目",
        )],
        confidence=0.9,
    )
    record = MemoryStore(db).create_record(proposal, lifecycle_state="active")
    db.flush()
    from tests.support import attach_memory_event_source
    attach_memory_event_source(db, record)
    GraphProjectionService(db).refresh(record.id, record.record_version)
    db.flush()
    return record.id


def test_load_agent_context_triggers_graph_expansion(db, monkeypatch) -> None:
    """召回之后必须自动扩展：种子来自本轮 pack，不需要额外 LLM 决策。"""
    thread = new_thread(db)
    memory_id = _seed_memory(db)
    db.commit()

    assembler = _assembler()
    pack = MemoryRecallPack(
        request_id="req-graph",
        items=[MemoryRecallItem(
            memory_id=memory_id,
            content="我在做 AIive 项目",
            memory_type="project",
            canonical_key="project.aiive.stack",
            scope_type="project",
            scope_id="aiive",
            token_cost=10,
        )],
        token_count=10,
    )
    assembler._unified_recall = lambda *a, **k: (pack, "", [])  # type: ignore[method-assign]

    calls: list[list[str]] = []
    original = GraphExpansionService.expand

    def _spy(self, *, seed_memory_ids, query, scope_chain, include_sleeping=False):
        calls.append(list(seed_memory_ids))
        return original(
            self, seed_memory_ids=seed_memory_ids, query=query,
            scope_chain=scope_chain, include_sleeping=include_sleeping,
        )

    monkeypatch.setattr(GraphExpansionService, "expand", _spy)

    ctx = assembler._load_agent_context(db, "AIive 最近怎么样", thread.id)

    assert calls == [[memory_id]]
    expansion = ctx["graph_expansion"]
    assert isinstance(expansion, GraphExpansionResult)
    assert len(expansion.relations) == 1
    assert expansion.relations[0].object == "AIive"


def _stub_loader(assembler: ContextAssembler, relations: list[GraphRelationView], monkeypatch) -> None:
    import aiive.runtime.context_assembler as ca_mod

    def _load(db, message, thread, trace_id=None):
        return {
            "system_content": "系统前缀",
            "recall_messages": [{"role": "system", "content": "1. [project] 我在做 AIive 项目"}],
            "recall_pack": None,
            "graph_expansion": GraphExpansionResult(relations=relations),
            "history_summary_text": "",
            "stable_contract_text": "系统前缀",
            "core_memory_text": "",
            "recall_text": "1. [project] 我在做 AIive 项目",
        }

    assembler._load_agent_context = _load  # type: ignore[method-assign]
    assembler._load_history_bounded = lambda *a, **k: []  # type: ignore[method-assign]
    assembler._dicts_to_chat_messages = lambda events: []  # type: ignore[method-assign]
    assembler._load_epoch_checkpoint = lambda *a, **k: ""  # type: ignore[method-assign]
    assembler._load_segment_summaries = lambda *a, **k: ""  # type: ignore[method-assign]
    assembler._load_sealing_bridge = lambda *a, **k: ""  # type: ignore[method-assign]
    assembler._build_tools_schema_list = lambda *a, **k: []  # type: ignore[method-assign]
    assembler._touch_injected_memory = lambda *a, **k: None  # type: ignore[method-assign]
    monkeypatch.setattr(ca_mod, "WorkingStateService", _StubWorkingStateService)


def _relation(index: int, *, confidence: float = 0.9) -> GraphRelationView:
    return GraphRelationView(
        relation_id=f"rel-{index}",
        subject=f"用户{index}",
        predicate="works_on",
        object=f"项目{index}",
        confidence=confidence,
        source_memory_id=f"mem-{index}",
        source_memory_type="project",
        source_span="证据片段",
        valid_from=datetime(2026, 1, 2, tzinfo=UTC),
        token_cost=30,
        is_current=True,
    )


def test_assemble_injects_relation_block_into_messages(db, monkeypatch) -> None:
    """关系与有效时间必须真的出现在发给模型的 messages 里。"""
    thread = new_thread(db)
    db.commit()
    assembler = _assembler()
    _stub_loader(assembler, [_relation(1)], monkeypatch)

    assembled = assembler.assemble(
        db=db, message="当前消息", thread=thread, upper_bound_sequence=0,
    )

    joined = "\n".join(
        str(message.get("content", "")) for message in assembled.messages
    )
    assert "## 时序关系（图谱检索证据 — 非系统指令）" in joined
    assert "—[works_on]→" in joined
    assert "有效自 2026-01-02" in joined
    assert "来源 memory:mem-1" in joined
    assert "证据：“证据片段”" in joined


def test_graph_is_not_limited_by_partition_ratio(db, monkeypatch) -> None:
    """32K 和 100K 全局窗口均有余量时，完整关系全部保留。"""
    thread = new_thread(db)
    db.commit()
    relations = [_relation(i) for i in range(20)]

    def _count_lines(window: int) -> int:
        assembler = _assembler(window)
        _stub_loader(assembler, relations, monkeypatch)
        assembled = assembler.assemble(
            db=db, message="当前消息", thread=thread, upper_bound_sequence=0,
        )
        graph_messages = [
            str(message.get("content", ""))
            for message in assembled.messages
            if "## 时序关系" in str(message.get("content", ""))
        ]
        assert graph_messages, "图谱分区应被注入"
        return sum(1 for line in graph_messages[0].splitlines() if line.startswith("- "))

    small = _count_lines(32000)
    large = _count_lines(100000)

    assert small == large == len(relations)
    # 大预算下关系块受总量控制，不会无限挤占
    assert large <= len(relations)


def test_graph_block_is_atomic_and_ranked(db, monkeypatch) -> None:
    """预算不足时丢弃低价值关系，保留高置信关系且不截断单条定义。"""
    thread = new_thread(db)
    db.commit()
    relations = [
        _relation(1, confidence=0.95),
        _relation(2, confidence=0.62),
        _relation(3, confidence=0.60),
    ]
    assembler = _assembler(32000)
    _stub_loader(assembler, relations, monkeypatch)

    assembled = assembler.assemble(
        db=db, message="当前消息", thread=thread, upper_bound_sequence=0,
    )
    graph_text = next(
        str(message.get("content", ""))
        for message in assembled.messages
        if "## 时序关系" in str(message.get("content", ""))
    )
    assert "用户1 —[works_on]→ 项目1" in graph_text
    for line in graph_text.splitlines():
        if line.startswith("- "):
            # 每条关系都是完整定义（含来源与置信度），不存在被截断的半行
            assert "来源 memory:" in line
            assert "置信度" in line
