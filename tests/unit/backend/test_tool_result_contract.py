"""统一工具执行结果契约：业务失败不得被包装成成功。

覆盖三类真实缺陷：
1. 普通字典 ok=false 被 ToolResult 无条件包装成成功；
2. allowed=false / cancelled 被当成成功；
3. DatabaseTool 先提交业务副作用、再向上报告失败。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiive.db.models import Base, Thread
from aiive.tools.base import (
    BaseTool,
    DatabaseTool,
    ToolError,
    ToolExecutionContext,
    ToolResult,
    business_result_failed,
    coerce_tool_result,
    outcome_is_unknown,
)
from aiive.tools.executor import ToolExecutor
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolDefinition,
    get_tool_registry,
)


def _tool(tool_type, capability_id: str) -> BaseTool:
    return tool_type(
        ToolDefinition(
            CapabilitySafetySchema(capability_id, "test", "trusted", "low"),
            parameters={},
        )
    )


def test_plain_dict_ok_false_is_not_wrapped_as_success() -> None:
    payload = ToolResult(
        {"ok": False, "error_type": "execution_failed", "error": "业务拒绝"}
    ).payload()

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_failed"
    assert payload["error"] == "业务拒绝"
    assert "result" not in payload


def test_execution_unknown_keeps_unknown_outcome() -> None:
    raw = {
        "ok": False,
        "error_type": "execution_unknown",
        "error": "Process stopped after cancellation; partial effects require reconciliation",
        "cancelled": True,
        "exit_code": None,
        "stdout": "",
    }

    result = coerce_tool_result(raw)
    assert result.error is not None
    payload = result.payload()

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_unknown"
    assert payload["execution_status"] == "execution_unknown"
    assert payload["outcome_known"] is False
    assert payload["side_effect_started"] is True
    assert outcome_is_unknown(payload)
    # 业务细节必须保留，且取消标记不能被误读成"已确认取消"。
    assert result.error.details["exit_code"] is None
    assert result.error.details["cancelled"] is True


def test_allowed_false_is_a_deterministic_failure() -> None:
    payload = coerce_tool_result({
        "ok": False,
        "allowed": False,
        "reason": "Path outside scope 'test_artifacts'",
        "resolved_path": "/tmp/outside.txt",
    }).payload()

    assert payload["ok"] is False
    assert payload["execution_status"] == "failed"
    assert payload["outcome_known"] is True
    assert not outcome_is_unknown(payload)
    assert payload["error"] == "Path outside scope 'test_artifacts'"
    assert payload["details"]["allowed"] is False


def test_cancelled_is_not_reported_as_success() -> None:
    payload = coerce_tool_result({
        "ok": False,
        "error_type": "cancelled",
        "error": "Cancelled before execution",
    }).payload()

    assert payload["ok"] is False
    assert payload["error_type"] == "cancelled"
    assert payload["execution_status"] == "cancelled"
    assert payload["outcome_known"] is True
    assert not outcome_is_unknown(payload)


def test_nested_business_ok_false_is_not_a_failure_marker() -> None:
    nested = {"ok": True, "result": {"ok": False, "error": "row level business data"}}

    payload = ToolResult(nested).payload()

    assert payload["ok"] is True
    # 业务数据原样保留在 result 里，不被当成执行状态。
    assert payload["result"] == nested
    assert payload["result"]["result"]["ok"] is False
    assert business_result_failed(nested) is False
    assert business_result_failed({"payload": {"ok": False}}) is False
    assert coerce_tool_result(nested).error is None


def test_tool_error_and_result_inputs_are_already_canonical() -> None:
    error = ToolError("execution_unknown", "network dropped", outcome_known=False)
    assert coerce_tool_result(error).payload()["error_type"] == "execution_unknown"
    assert coerce_tool_result(ToolResult(error=error)).payload() == error.payload()


def test_read_text_file_denied_path_is_a_real_failure(tmp_path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    tool = get_tool_registry().get("read_text_file")
    assert tool is not None

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext("read_text_file"), {"path": str(target)}, 10,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "access_denied"
    assert payload["outcome_known"] is True
    assert payload["details"]["path"] == str(target)


def test_safe_delete_denied_path_is_a_real_failure(tmp_path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    tool = get_tool_registry().get("safe_delete")
    assert tool is not None

    payload = ToolExecutor.execute_direct(
        tool,
        ToolExecutionContext("safe_delete"),
        {"path": str(target), "scope_id": "not_registered_scope", "mode": "trash"},
        10,
    )

    assert payload["ok"] is False
    assert payload["execution_status"] == "failed"
    assert payload["details"]["allowed"] is False
    assert target.exists()


def test_run_shell_missing_cwd_is_a_real_failure(tmp_path) -> None:
    tool = get_tool_registry().get("run_shell")
    assert tool is not None

    payload = ToolExecutor.execute_direct(
        tool,
        ToolExecutionContext("run_shell"),
        {"command": "echo hi", "cwd": str(tmp_path / "missing")},
        10,
    )

    assert payload["ok"] is False
    assert payload["error_type"] == "cwd_not_found"
    assert payload["outcome_known"] is True
    # 启动前失败：命令没有执行，不能标记已经产生副作用。
    assert payload["side_effect_started"] is False
    assert payload["details"]["exit_code"] is None


def test_run_shell_timeout_keeps_partial_side_effect(tmp_path) -> None:
    """超时被终止的命令可能已经产生了外部副作用，契约必须保留这个事实。"""
    tool = get_tool_registry().get("run_shell")
    assert tool is not None

    payload = ToolExecutor.execute_direct(
        tool,
        ToolExecutionContext("run_shell"),
        {
            "command": 'echo "partial" > partial.txt; sleep 30',
            "cwd": str(tmp_path),
            "timeout_seconds": 1,
        },
        10,
    )

    assert (tmp_path / "partial.txt").read_text(encoding="utf-8").strip() == "partial"
    assert payload["ok"] is False
    assert payload["error_type"] == "timeout"
    assert payload["execution_status"] == "failed"
    assert payload["outcome_known"] is True
    assert payload["side_effect_started"] is True


@pytest.fixture
def write_session_factory(tmp_path, monkeypatch):
    """DatabaseTool 需要真实独立事务，因此使用可跨线程的独立 SQLite 库。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool-contract.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("aiive.db.base.SessionLocal", factory)
    monkeypatch.setattr("aiive.tools.builtin_tools.SessionLocal", factory)
    yield factory
    engine.dispose()


class _RejectingWriteTool(DatabaseTool):
    def _run(self, db):
        db.add(Thread(id="contract-rolled-back", title="must roll back"))
        db.flush()
        return {"ok": False, "error_type": "execution_failed", "error": "业务校验失败"}


class _CommittingWriteTool(DatabaseTool):
    def _run(self, db):
        db.add(Thread(id="contract-committed", title="must commit"))
        db.flush()
        return "written"


def test_database_tool_failure_rolls_back_business_transaction(write_session_factory) -> None:
    tool = _tool(_RejectingWriteTool, "contract_rejecting_write")

    payload = ToolExecutor.execute_direct(tool, ToolExecutionContext(tool.safety.capability_id), {}, 10)

    assert payload["ok"] is False
    assert payload["error_type"] == "execution_failed"
    with write_session_factory() as db:
        assert db.get(Thread, "contract-rolled-back") is None


def test_database_tool_success_still_commits_business_transaction(write_session_factory) -> None:
    tool = _tool(_CommittingWriteTool, "contract_committing_write")

    payload = ToolExecutor.execute_direct(tool, ToolExecutionContext(tool.safety.capability_id), {}, 10)

    assert payload["ok"] is True
    assert payload["result"] == "written"
    with write_session_factory() as db:
        assert db.get(Thread, "contract-committed") is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            {"ok": False, "error_type": "execution_failed", "error": "参数不合法"},
            (True, False),
            id="known_without_side_effect",
        ),
        pytest.param(
            {
                "ok": False,
                "error_type": "execution_failed",
                "error": "写了一半文件",
                "outcome_known": True,
                "side_effect_started": True,
                "written": 2,
            },
            (True, True),
            id="known_with_side_effect",
        ),
        pytest.param(
            {"ok": False, "error_type": "execution_unknown", "error": "对端无响应"},
            (False, True),
            id="unknown_started",
        ),
        pytest.param(
            {
                "ok": False,
                "error_type": "execution_unknown",
                "error": "无法确认请求是否发出",
                "side_effect_started": False,
            },
            (False, False),
            id="unknown_unconfirmed_start",
        ),
    ],
)
def test_outcome_known_and_side_effect_started_are_independent(raw, expected) -> None:
    outcome_known, side_effect_started = expected

    payload = coerce_tool_result(raw).payload()

    assert payload["outcome_known"] is outcome_known
    assert payload["side_effect_started"] is side_effect_started
    assert payload["ok"] is False


def test_known_failure_keeps_original_code_and_business_details() -> None:
    raw = {
        "ok": False,
        "error_type": "execution_failed",
        "error": "写入第 2 个文件时失败",
        "outcome_known": True,
        "side_effect_started": True,
        "written_files": ["a.txt", "b.txt"],
    }

    error = coerce_tool_result(raw).error

    assert error is not None
    assert error.code == "execution_failed"
    assert error.outcome_known is True
    assert error.side_effect_started is True
    assert error.details["written_files"] == ["a.txt", "b.txt"]


def test_coercion_keeps_raw_business_value_for_diagnostics() -> None:
    raw = {"ok": False, "error_type": "execution_failed", "error": "失败", "path": "/tmp/x"}

    result = coerce_tool_result(raw)

    assert result.error is not None
    # 原始业务字典保留给诊断与既有调用方；payload() 仍然只输出错误封套。
    assert result.value == raw
    assert result.payload()["ok"] is False


class _ScriptedWriteTool(DatabaseTool):
    """先写一行业务数据，再返回脚本化结果，用来验证提交/回滚边界。"""

    def __init__(self, definition, outcome):
        super().__init__(definition)
        self.outcome = outcome
        self.row_id = f"contract-row-{uuid.uuid4()}"

    def _run(self, db):
        db.add(Thread(id=self.row_id, title="scripted write"))
        db.flush()
        return self.outcome


def _scripted_tool(outcome) -> _ScriptedWriteTool:
    return _ScriptedWriteTool(
        ToolDefinition(
            CapabilitySafetySchema("contract_scripted_write", "test", "trusted", "low"),
            parameters={},
        ),
        outcome,
    )


STRUCTURED_FAILURES = [
    pytest.param(
        {"ok": False, "error_type": "execution_failed", "error": "失败"},
        id="dict_ok_false",
    ),
    pytest.param(
        {"ok": False, "allowed": False, "reason": "范围外"},
        id="dict_allowed_false",
    ),
    pytest.param(
        ToolError("execution_failed", "失败", side_effect_started=True),
        id="tool_error",
    ),
    pytest.param(
        ToolResult(error=ToolError("execution_unknown", "未知", outcome_known=False)),
        id="tool_result_error",
    ),
    pytest.param(
        ToolResult(value={"ok": False, "error_type": "execution_failed", "error": "失败"}),
        id="tool_result_value",
    ),
]


@pytest.mark.parametrize("outcome", STRUCTURED_FAILURES)
def test_database_tool_structured_failure_never_commits(write_session_factory, outcome) -> None:
    tool = _scripted_tool(outcome)

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 10,
    )

    assert payload["ok"] is False
    with write_session_factory() as db:
        assert db.get(Thread, tool.row_id) is None


def test_database_tool_success_commits_scripted_write(write_session_factory) -> None:
    tool = _scripted_tool("written")

    payload = ToolExecutor.execute_direct(
        tool, ToolExecutionContext(tool.safety.capability_id), {}, 10,
    )

    assert payload["ok"] is True
    assert payload["result"] == "written"
    with write_session_factory() as db:
        assert db.get(Thread, tool.row_id) is not None


def test_operation_response_keeps_persisted_side_effect_facts() -> None:
    from aiive.db.models import ToolOperation
    from aiive.tools.operation_executor import operation_response

    failed = ToolOperation(
        id="op-failed",
        idempotency_key="key",
        thread_id="thread",
        turn_record_id="turn-record",
        turn_id="turn",
        tool_call_id="call",
        capability_id="run_shell",
        params={},
        params_hash="hash",
        descriptor_hash="descriptor",
        status="failed",
        effect_mode="non_repeatable_external",
        outbox_job_id="job",
        trace_id="trace",
        error_message="写了一半",
        effect_receipt={"outcome_known": True, "side_effect_started": True},
    )

    payload = operation_response(failed)

    assert payload["ok"] is False
    assert payload["outcome_known"] is True
    assert payload["side_effect_started"] is True

    unknown = ToolOperation(
        id="op-unknown",
        idempotency_key="key-2",
        thread_id="thread",
        turn_record_id="turn-record",
        turn_id="turn",
        tool_call_id="call-2",
        capability_id="run_shell",
        params={},
        params_hash="hash",
        descriptor_hash="descriptor",
        status="execution_unknown",
        effect_mode="non_repeatable_external",
        outbox_job_id="job-2",
        trace_id="trace",
    )

    unknown_payload = operation_response(unknown)

    assert unknown_payload["outcome_known"] is False
    assert unknown_payload["side_effect_started"] is True
