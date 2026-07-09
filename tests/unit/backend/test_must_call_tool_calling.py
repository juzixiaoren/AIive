"""Chat 到工具调用确定性修复（must-call 调度）的针对性测试。

这些测试验证修复的 1-3 阶段：
- 相同的执行请求确定性调用工具（不依赖 LLM 标签运气），
- 工具失败绝不能产生虚假的成功回复，
- 解析失败不能静默降级为自由形式的回答，
- 假设性/元问题不得执行，
- 畸形工具标签不得泄露给用户，
- 运行时查询数据库而非信任先前的助手声明。

使用隔离的 SQLite 数据库对真实内置工具进行测试（SessionLocal 被 patch 到测试引擎），
因此测试断言的是真实的数据库写入/读取。
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from aiive.core.action_planner import AgentDecision
from aiive.core.llm_client import FakeLLMClient
from aiive.db.models import Base, Event, Task, Thread
from aiive.runtime.agent_graph import AgentGraph
from aiive.runtime.event_logger import EventLogger as _RealEventLogger
from aiive.tools.registry import get_tool_registry


class _CommittingEventLogger(_RealEventLogger):
    """仅测试使用的 EventLogger，在每次写入后提交事务。

    真实的 EventLogger 只 flush，留下 app_db 的开放写事务，会在工具处理器的
    独立 session 提交期间锁定 SQLite 文件。急切提交可避免 SQLite 上的
    'database is locked' 错误，同时仍然持久化事件（包括 tool_error）供断言使用。
    """

    def log_event(self, **kwargs):
        super().log_event(**kwargs)
        try:
            self._db.commit()
        except Exception:
            pass

    def log_llm_call(self, **kwargs):
        super().log_llm_call(**kwargs)
        try:
            self._db.commit()
        except Exception:
            pass


def _count_tasks(session) -> int:
    """通过新连接统计 Task 行数。

    内置工具通过其自身的（已 patch 的）SessionLocal 连接提交；
    通过 fixture session 查询可能会漏掉 SQLite 上跨连接已提交的行。
    """
    from sqlalchemy.orm import Session as _Sess

    fresh = _Sess(session.get_bind())
    try:
        return fresh.query(Task).count()
    finally:
        fresh.close()


def _thread_exists(session, thread_id: str) -> bool:
    return session.get(Thread, thread_id) is not None


def _ensure_thread(session, thread_id: str) -> None:
    if not _thread_exists(session, thread_id):
        session.add(Thread(id=thread_id))
        session.commit()


@pytest.fixture(autouse=True)
def _register_tools():
    """确保内置工具在整个测试会话中注册一次。"""
    get_tool_registry()


@pytest.fixture
def app_db():
    """隔离的 SQLite 数据库；patch SessionLocal 使内置工具写入此处。"""
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    # NullPool：每个 session 获得独立连接，因此工具处理器的
    # SessionLocal() 提交独立于 AgentGraph session 并实际持久化
    # （默认的 SingletonThreadPool 会共享一个连接，在开放事务下提交将失败）。
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    test_sessionmaker = sessionmaker(bind=engine)
    with patch("aiive.db.base.SessionLocal", test_sessionmaker), patch(
        "aiive.tools.builtin_tools.SessionLocal", test_sessionmaker
    ):
        session = test_sessionmaker()
        try:
            yield session
        finally:
            session.close()
    engine.dispose()
    shutil.rmtree(tmp, ignore_errors=True)


def _decision(tool_name, intent_type, args=None, execution_mode="execute"):
    return AgentDecision(
        decision_type="tool_call" if execution_mode == "execute" else "final_response",
        execution_mode=execution_mode,
        intent_type=intent_type,
        should_execute=(execution_mode == "execute"),
        tool_name=tool_name,
        tool_params=args or {},
        args=args or {},
        tool_call_policy="must_call" if execution_mode == "execute" else "never_call",
        tool_required=(execution_mode == "execute"),
        success_requires_tool_result=(execution_mode == "execute"),
        reason="test",
    )


def _run(app_db, message, decision, thread_id="thread-mustcall"):
    # get_or_create_thread only reuses a thread when its id already exists,
    # so seed a stable Thread to keep create/list on the same thread.
    _ensure_thread(app_db, thread_id)
    fake_planner = MagicMock()
    fake_planner.plan.return_value = decision
    with patch("aiive.runtime.agent_graph.ActionPlanner", return_value=fake_planner), patch(
        "aiive.runtime.agent_graph.OutboxWorker"
    ), patch("aiive.runtime.agent_graph.register_all"), patch(
        "aiive.runtime.agent_graph.EventLogger", _CommittingEventLogger
    ):
        graph = AgentGraph(FakeLLMClient(), app_db)
        return graph.run(message, thread_id=thread_id)


# ---------------------------------------------------------------------------
# 阶段 2/3：相同的执行请求必须始终调用工具
# ---------------------------------------------------------------------------


class TestReminderCreateMustCall:
    """测试提醒创建必须调用工具的确定性行为."""

    def test_reminder_create_must_call_tool_repeated(self, app_db):
        """10 次相同的 '提醒我' 请求必须全部调用 schedule_reminder
        并写入 Task，绝不能返回纯自然语言的 '成功'。"""
        for i in range(10):
            decision = _decision(
                "schedule_reminder", "reminder_create",
                args={"content": "赫赫", "delay_minutes": 1},
            )
            result = _run(app_db, "1 分钟后提醒我 赫赫", decision)

            # 工具实际已执行（非自然语言回答）
            assert any(
                c["name"] == "schedule_reminder" and c["status"] in ("completed", "failed")
                for c in result["tool_calls"]
            ), f"iter {i}: schedule_reminder not called"
            # 有 Task 行写入数据库
            assert _count_tasks(app_db) >= (i + 1), f"iter {i}: no Task row written"
            # task_created action_card 存在
            assert any(
                c["card_type"] == "task_created" for c in result["action_cards"]
            ), f"iter {i}: no task_created card"
            # 回复基于工具结果，非预设成功消息
            assert "赫赫" in result["reply"]

        app_db.expire_all()
        assert _count_tasks(app_db) == 10


# ---------------------------------------------------------------------------
# 阶段 2：列表请求必须调用 list_tasks 并报告真实数据库状态
# ---------------------------------------------------------------------------


class TestTaskListMustCall:
    def test_task_list_must_call_tool_repeated(self, app_db):
        """先创建一条提醒。"""
        # 先在该线程中创建一个提醒
        create_dec = _decision(
            "schedule_reminder", "reminder_create",
            args={"content": "赫赫", "delay_minutes": 1},
        )
        _run(app_db, "1 分钟后提醒我 赫赫", create_dec)

        # 然后问 10 次 "我有哪些提醒？"
        list_dec = _decision("list_tasks", "task_list", args={})
        for i in range(10):
            result = _run(app_db, "我有哪些提醒？", list_dec)
            assert any(
                c["name"] == "list_tasks" for c in result["tool_calls"]
            ), f"iter {i}: list_tasks not called"
            # 回复基于数据库查询，应包含真实标题
            assert "赫赫" in result["reply"], f"iter {i}: reply lacks real task"


# ---------------------------------------------------------------------------
# 阶段 3：工具失败绝不能产生虚假成功
# ---------------------------------------------------------------------------


class TestToolFailureCannotFakeSuccess:
    def test_tool_failure_cannot_fake_success(self, app_db):
        """工具失败时，运行时不能声称'已安排'。"""
        class _FailingSession(MagicMock):
            def commit(self):
                raise RuntimeError("db down")

        decision = _decision(
            "schedule_reminder", "reminder_create",
            args={"content": "赫赫", "delay_minutes": 1},
        )
        # 强制工具的数据库写入失败；运行时绝不能输出 "已经安排上了"。
        with patch(
            "aiive.tools.builtin_tools.SessionLocal",
            side_effect=lambda: _FailingSession(),
        ):
            result = _run(app_db, "1 分钟后提醒我 赫赫", decision)

        # 不能有虚假成功措辞，必须是明确的失败。
        assert "已经安排" not in result["reply"]
        assert "操作未能完成" in result["reply"] or "未能" in result["reply"]
        # 不能有 task_created 卡片。
        assert not any(
            c["card_type"] == "task_created" for c in result["action_cards"]
        )
        # 不能有 Task 行写入。
        app_db.expire_all()
        assert _count_tasks(app_db) == 0
        # 应记录 tool_error 事件。
        assert (
            app_db.query(Event).filter(Event.event_type == "tool_error").count() >= 1
        )


# ---------------------------------------------------------------------------
# 阶段 1：假设性/元问题不得执行
# ---------------------------------------------------------------------------


class TestHypotheticalShouldNotExecute:
    def test_hypothetical_should_not_execute(self, app_db):
        """假设性问题不应执行任何工具。"""
        decision = AgentDecision(
            decision_type="final_response",
            execution_mode="explain_only",
            intent_type="normal_chat",
            should_execute=False,
            tool_call_policy="never_call",
            tool_required=False,
            reason="hypothetical",
        )
        result = _run(app_db, "如果我想让你一分钟后提醒我，你会调用哪个工具？", decision)
        assert len(result["tool_calls"]) == 0
        app_db.expire_all()
        assert _count_tasks(app_db) == 0


# ---------------------------------------------------------------------------
# 阶段 5：畸形工具标签不得泄露给用户
# ---------------------------------------------------------------------------


class TestNoRawToolCallLeak:
    def test_no_raw_tool_call_leak(self, app_db):
        """畸形标签不应出现在最终用户回复中。"""
        decision = AgentDecision(
            decision_type="final_response",
            execution_mode="explain_only",
            intent_type="normal_chat",
            should_execute=False,
            tool_call_policy="never_call",
            tool_required=False,
            reason="test",
        )
        # 主 LLM 发出畸形标签
        llm = FakeLLMClient(fixed_content="I tried </tool_cost> but it failed.")
        fake_planner = MagicMock()
        fake_planner.plan.return_value = decision
        with patch("aiive.runtime.agent_graph.ActionPlanner", return_value=fake_planner), patch(
            "aiive.runtime.agent_graph.OutboxWorker"
        ), patch("aiive.runtime.agent_graph.register_all"), patch(
            "aiive.runtime.agent_graph.EventLogger", _CommittingEventLogger
        ):
            graph = AgentGraph(llm, app_db)
            result = graph.run("test", thread_id="thread-leak")
        assert "<tool_call>" not in result["reply"]
        assert "</tool_cost>" not in result["reply"]
        assert len(result["parse_errors"]) > 0


# ---------------------------------------------------------------------------
# 阶段 4：不要信任先前的助手声明；查询数据库
# ---------------------------------------------------------------------------


class TestContextDoesNotTreatAssistantClaimAsFact:
    def test_context_does_not_treat_assistant_claim_as_fact(self, app_db):
        """即使之前的助手声称已设置提醒，must-call 也应查询数据库。"""
        # 持久化一条先前助手消息，虚假声称已设置提醒
        app_db.add(Event(
            id="evt-seed",
            trace_id="evt-seed",
            thread_id="thread-claim",
            event_type="llm_response",
            payload={"content": "已经安排上了。"},
        ))
        app_db.commit()

        # 没有真正的 Task 存在。请求列表；must-call 强制数据库查询。
        decision = _decision("list_tasks", "task_list", args={})
        result = _run(app_db, "我有哪些提醒？", decision, thread_id="thread-claim")

        assert any(c["name"] == "list_tasks" for c in result["tool_calls"])
        # 回复反映了空数据库，而非虚假声明。
        assert "没有任何提醒" in result["reply"] or "没有" in result["reply"]
        assert "已经安排" not in result["reply"]
