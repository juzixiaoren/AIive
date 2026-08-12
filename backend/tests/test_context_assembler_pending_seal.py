"""回归测试：ContextAssembler.assemble() 在软阈值触发 mark_pending_seal 后，
必须真正提交事务，否则 flush 会随会话关闭被回滚丢弃
（原 bug：Segment.pending_seal_at 永远写不进数据库）。
"""
from __future__ import annotations

from aiive.db.base import SessionLocal
from aiive.db.models import Segment
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.context_budget import ContextBudget, PartitionBudget
from aiive.runtime.token_models import ModelProfile, TokenCount
from backend.tests._util import new_epoch, new_segment, new_thread


class _FakeTokenCounter:
    """返回超过 soft_input_limit 但未超过 hard gate 的固定 token 数。"""

    def __init__(self, safe_tokens: int):
        self.safe_tokens = safe_tokens

    def count_messages(self, model, messages, tools=None) -> TokenCount:
        return TokenCount(
            estimated_tokens=self.safe_tokens,
            safety_margin_tokens=0,
            safe_tokens=self.safe_tokens,
        )


class _StubWorkingStateService:
    def render_for_context(self, *args, **kwargs) -> str:
        return ""


def _build_budget() -> ContextBudget:
    def pb(name: str, n: int) -> PartitionBudget:
        return PartitionBudget(
            name=name, soft_limit_tokens=n, hard_limit_tokens=n, priority=n,
        )

    return ContextBudget(
        model_context_window=200_000,
        reserved_output=pb("reserved_output", 2_000),
        stable_contract=pb("stable_contract", 10_000),
        core_memory=pb("core_memory", 10_000),
        working_state=pb("working_state", 10_000),
        tool_definitions=pb("tool_definitions", 10_000),
        recent_messages=pb("recent_messages", 10_000),
        retrieved_memory=pb("retrieved_memory", 10_000),
        tool_results=pb("tool_results", 10_000),
        epoch_checkpoint=pb("epoch_checkpoint", 10_000),
        segment_summaries=pb("segment_summaries", 10_000),
        sealing_bridge=pb("sealing_bridge", 10_000),
        retrieved_history_summary=pb("retrieved_history_summary", 10_000),
        deep_history_raw=pb("deep_history_raw", 10_000),
    )


def test_assemble_persists_pending_seal_after_session_close(db) -> None:
    """软阈值触发后，pending_seal 必须在 assemble 持有的会话关闭后仍落库。"""
    import aiive.runtime.context_assembler as ca_mod

    thread = new_thread(db)
    epoch = new_epoch(db, thread.id)
    seg = new_segment(db, epoch.id, thread.id, status="open")
    db.commit()
    seg_id = seg.id  # 提前保存主键，避免会话关闭后访问已脱离的实例

    budget = _build_budget()
    # soft_input_limit = 0.8 * (200000 - 2000) = 158400
    # 用 160000 触发 soft_exceeded，但 160000 + max_output(4096) <= 200000 通过 hard gate
    assembler = ContextAssembler(
        token_counter=_FakeTokenCounter(160_000),
        budget=budget,
        profile=ModelProfile(
            provider="deepseek", model_id="deepseek-chat",
            full_name="deepseek/deepseek-chat",
            context_window=200_000, max_output_tokens=2_000,
        ),
    )

    # 屏蔽重型私有方法（召回/检索/工作态等），只保留含修复点的核心循环
    assembler._load_agent_context = lambda db, message, thread, trace_id=None: {
        "system_content": "", "recall_messages": [], "recall_pack": None,
        "history_summary_text": "", "stable_contract_text": "",
        "core_memory_text": "", "recall_text": "",
    }
    assembler._load_history_bounded = lambda *a, **k: []
    assembler._load_epoch_checkpoint = lambda *a, **k: ""
    assembler._load_segment_summaries = lambda *a, **k: ""
    assembler._load_sealing_bridge = lambda *a, **k: ""
    assembler._build_tools_schema_list = lambda *a, **k: []
    assembler._touch_injected_memory = lambda *a, **k: None
    ca_mod.WorkingStateService = _StubWorkingStateService

    assembled = assembler.assemble(
        db=db, message="hello", thread=thread, upper_bound_sequence=0,
    )

    assert assembled.is_safe is True

    # 复现原 bug 场景：assemble 持有的会话被关闭
    # （对应 _load_context 的 finally: db.close()）
    db.close()

    # 重新开一个会话，验证 pending_seal 已真正落库
    verify_db = SessionLocal()
    try:
        reloaded = verify_db.get(Segment, seg_id)
        assert reloaded is not None
        assert reloaded.pending_seal_at is not None, \
            "pending_seal 未持久化（flush 被会话关闭回滚丢弃）"
        assert reloaded.sealed_by_turn == 0
    finally:
        verify_db.close()
