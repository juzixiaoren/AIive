"""V2 记忆读取架构单元测试：Automatic Recall + Core Memory 投影。

验证：
- AutomaticRecallEngine.query-aware 召回（exact / lexical / vector / episode 路由）
- fuse_and_pack 去重、阈值过滤、token 预算裁剪
- CoreMemoryProjection.build_blocks / load_core_memory（含丢失投影后从 memory_records 重建）
- core key 写入会入队 core_memory_refresh outbox 任务
"""
from aiive.db.models import MemoryRecord, OutboxJob
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import MemoryRecallRequest, ScopeContext
from aiive.memory.automatic_recall import AutomaticRecallEngine
from aiive.memory.core_memory_projection import build_blocks, load_core_memory


def _mk(db, canonical_key, content, memory_type="knowledge", scope_type="global",
        scope_id=None, importance=0.5, trust="semi_trusted", observed=None):
    rec = MemoryRecord(
        canonical_key=canonical_key,
        content=content,
        memory_type=memory_type,
        scope_type=scope_type,
        scope_id=scope_id,
        lifecycle_state=LifecycleState.ACTIVE.value,
        validity_state=ValidityState.VALID.value,
        importance=importance,
        trust_level=trust,
        record_version=1,
        observed_at=observed,
    )
    db.add(rec)
    db.flush()
    return rec


def test_fts_route_recalls_relevant_memory(db_session):
    _mk(db_session, "project.x", "User prefers dark mode in the IDE", memory_type="user_profile")
    _mk(db_session, "project.y", "The sky is blue and the grass is green", memory_type="knowledge")

    engine = AutomaticRecallEngine(db_session, RecallConfig())
    req = MemoryRecallRequest(
        query="user prefers dark mode",
        scope_context=ScopeContext(thread_id="t1"),
        top_k=8, token_budget=2000,
    )
    pack, traces = engine.recall(req)

    assert pack.items, "应当召回至少一条相关记忆"
    contents = [it.content for it in pack.items]
    assert any("dark mode" in c for c in contents)
    assert {route for item in pack.items for route in item.route.split("|")} <= {
        "exact", "lexical", "episode",
    }
    assert any("lexical" in item.route.split("|") for item in pack.items)
    # 不相关记忆不应入选（低于相关性阈值）
    assert not any("sky is blue" in c for c in pack.items)


def test_vector_route_participates_in_fusion(db_session):
    record = _mk(
        db_session,
        "user.preference.editor",
        "用户喜欢使用深色代码编辑器",
        memory_type="user_profile",
    )

    class StubVectorService:
        """仅用于验证路由装配；生产路径不使用该测试替身。"""

        @staticmethod
        def search(request, *, include_sleeping, include_archived, limit):
            assert request.query == "更适合夜间工作的界面"
            assert include_sleeping is False
            assert include_archived is False
            assert limit == 16
            return [(record, 0.92)]

    engine = AutomaticRecallEngine(
        db_session,
        RecallConfig(),
        vector_service=StubVectorService(),
    )
    pack, traces = engine.recall(MemoryRecallRequest(
        query="更适合夜间工作的界面",
        scope_context=ScopeContext(),
        top_k=8,
        token_budget=2000,
    ))

    assert [item.memory_id for item in pack.items] == [record.id]
    assert pack.items[0].route == "vector"
    assert traces[0].selected is True


def test_exact_route_passes_threshold(db_session):
    _mk(db_session, "user.preference.response_style", "be concise", memory_type="user_profile")

    engine = AutomaticRecallEngine(db_session, RecallConfig())
    req = MemoryRecallRequest(
        query="user.preference.response_style",
        scope_context=ScopeContext(thread_id="t1"),
        top_k=8, token_budget=2000,
    )
    pack, _ = engine.recall(req)
    assert pack.items, "精确键应命中"
    assert pack.items[0].route.split("|")[0] == "exact"


def test_token_budget_truncates_selection(db_session):
    for i in range(10):
        _mk(db_session, f"project.fact_{i}", "word " * 40, memory_type="knowledge",
            importance=0.9)

    cfg = RecallConfig(automatic_recall_token_budget=200)  # 很紧的预算
    engine = AutomaticRecallEngine(db_session, cfg)
    req = MemoryRecallRequest(
        query="word", scope_context=ScopeContext(thread_id="t1"),
        top_k=20, token_budget=200,
    )
    pack, _ = engine.recall(req)
    assert pack.token_count <= 200
    assert len(pack.items) < 10


def test_core_memory_build_and_load(db_session):
    _mk(db_session, "user.display_name", "Alice", memory_type="user_profile")
    _mk(db_session, "core.human_identity", "Alice is the user", memory_type="user_profile")
    _mk(db_session, "core.agent_persona", "I am a helpful agent", memory_type="agent_self")

    blocks = build_blocks(db_session, RecallConfig())
    names = {b.block_name for b in blocks}
    assert "core.human_identity" in names
    assert "core.agent_persona" in names

    # load_core_memory：表为空时应回退到 live rebuild
    loaded = load_core_memory(db_session, RecallConfig())
    assert {b.block_name for b in loaded} == names


def test_core_key_write_enqueues_refresh(db_session):
    from aiive.memory.memory_write_service import MemoryWriteService
    from aiive.memory.memory_types import MemoryProposal, TrustLevel, EvidenceItem
    from aiive.context.run_context import RunContext

    proposal = MemoryProposal(
        memory_type="user_profile",
        canonical_key="user.display_name",
        content="Bob",
        trust_level=TrustLevel.TRUSTED.value,
        evidence=[EvidenceItem(
            source_type="user_message",
            trust_level=TrustLevel.TRUSTED.value,
            source_event_id="ev-1",
        )],
    )
    writer = MemoryWriteService(db_session)
    res = writer.write(proposal, run_context=RunContext(thread_id="t1", trace_id="trace-1"))
    assert res.written, res.reason
    jobs = db_session.query(OutboxJob).filter(
        OutboxJob.job_type == "core_memory_refresh"
    ).all()
    assert jobs, "core key 写入应入队 core_memory_refresh 任务"
    assert jobs[0].payload["memory_id"] == res.memory_id


def test_capability_scope_recalls_capability_scoped_memory(db_session):
    """Scope Chain 的 capability 层级必须真正参与召回（V2 §九 #6）。"""
    _mk(db_session, "cap.fact", "capability scoped fact", memory_type="knowledge",
        scope_type="capability", scope_id="cap_X")
    _mk(db_session, "cap.other", "other capability fact", memory_type="knowledge",
        scope_type="capability", scope_id="cap_Y")

    engine = AutomaticRecallEngine(db_session, RecallConfig())
    req = MemoryRecallRequest(
        query="fact",
        scope_context=ScopeContext(thread_id="t1", capability_ids=["cap_X"]),
        top_k=8, token_budget=2000,
    )
    pack, _ = engine.recall(req)
    contents = [it.content for it in pack.items]
    assert any("capability scoped fact" in c for c in contents)
    assert not any("other capability fact" in c for c in contents)


def test_project_scope_recalls_project_scoped_memory(db_session):
    """Scope Chain 的 project 层级必须真正参与召回（V2 §九 #5）。"""
    _mk(db_session, "proj.fact", "project P1 fact", memory_type="knowledge",
        scope_type="project", scope_id="p1")
    _mk(db_session, "proj.other", "project P2 fact", memory_type="knowledge",
        scope_type="project", scope_id="p2")

    engine = AutomaticRecallEngine(db_session, RecallConfig())
    req = MemoryRecallRequest(
        query="fact",
        scope_context=ScopeContext(thread_id="t1", project_id="p1"),
        top_k=8, token_budget=2000,
    )
    pack, _ = engine.recall(req)
    contents = [it.content for it in pack.items]
    assert any("project P1 fact" in c for c in contents)
    assert not any("project P2 fact" in c for c in contents)


def test_build_scope_context_fills_active_capabilities(db_session):
    """Runtime 应从 capabilities 表解析活跃 capability 注入 Scope Chain（#1）。"""
    from aiive.db.models import Capability
    from aiive.memory.scope_resolver import build_scope_context

    db_session.add(Capability(capability_id="cap_active", name="active cap", state="active"))
    db_session.add(Capability(capability_id="cap_inactive", name="inactive cap", state="needs_review"))
    db_session.flush()

    sc = build_scope_context(db_session, None, "t1")
    assert sc.capability_ids == ["cap_active"]


def test_memory_search_events_respects_thread_scope(db_session):
    """memory_search_events 不得越权扫描全表，只能命中授权 scope（#2）。"""
    from unittest.mock import patch

    import aiive.tools.builtin_tools as bt
    from aiive.context.run_context import RunContext

    rec_a = _mk(db_session, "ep.a", "alpha happened here", memory_type="episodic",
                scope_type="thread", scope_id="t_A")
    rec_b = _mk(db_session, "ep.b", "alpha happened there", memory_type="episodic",
                scope_type="thread", scope_id="t_B")
    id_a, id_b = rec_a.id, rec_b.id

    run_ctx = RunContext(thread_id="t_A", trace_id="tr", source="user_chat")
    with patch.object(bt, "SessionLocal", lambda: db_session):
        result = bt._handle_memory_search(ctx=run_ctx, query="alpha")
    assert result["ok"]
    returned = {r["memory_id"] for r in result["results"]}
    assert id_a in returned
    assert id_b not in returned
