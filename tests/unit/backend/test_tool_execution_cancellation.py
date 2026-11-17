"""Concurrent cancellation through the shared execution entry, with per-call state."""

from concurrent.futures import ThreadPoolExecutor
import threading

from aiive.tools.base import BaseTool, CancellationPolicy, ToolExecutionContext
from aiive.tools.executor import ToolExecutor, request_cancel
from aiive.tools.registry import CapabilitySafetySchema, ToolDefinition, ToolRegistry


class CooperativeTool(BaseTool):
    def _run(self, _execution=None):
        started, release = _execution.authorization
        started.set()
        assert release.wait(5)
        _execution.cancellation.checkpoint()
        return _execution.execution_id


class UninterruptibleTool(BaseTool):
    def _run(self, _execution=None):
        started, release = _execution.authorization
        started.set()
        assert release.wait(5)
        return "committed"


def registered(tool_type, policy):
    registry = ToolRegistry()
    registry.register(
        tool_type(
            ToolDefinition(
                CapabilitySafetySchema("probe", "test", "trusted", "low"),
                parameters={},
                cancellation_policy=policy,
            )
        )
    )
    return registry.get("probe")


def test_cooperative_cancellation_is_execution_scoped_and_confirmed():
    tool = registered(CooperativeTool, CancellationPolicy.COOPERATIVE)
    one_started, two_started, release = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    one = ToolExecutionContext("probe", authorization=(one_started, release))
    two = ToolExecutionContext("probe", authorization=(two_started, release))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(ToolExecutor.execute_direct, tool, one, {}, 5)
        second = pool.submit(ToolExecutor.execute_direct, tool, two, {}, 5)
        assert one_started.wait(5) and two_started.wait(5)
        assert request_cancel(one.execution_id).status == "requested"
        assert not first.done()  # Request is not stop confirmation.
        release.set()
        assert first.result(5)["execution_status"] == "cancelled"
        assert second.result(5)["result"] == two.execution_id


def test_unsupported_cancellation_preserves_actual_success():
    tool = registered(UninterruptibleTool, CancellationPolicy.NONE)
    started, release = threading.Event(), threading.Event()
    context = ToolExecutionContext("probe", authorization=(started, release))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(ToolExecutor.execute_direct, tool, context, {}, 5)
        assert started.wait(5)
        assert request_cancel(context.execution_id).status == "not_cancellable"
        release.set()
        assert future.result(5)["result"] == "committed"
    assert request_cancel(context.execution_id).status == "unknown"


def test_cancelled_before_start_never_invokes_business_method():
    tool = registered(UninterruptibleTool, CancellationPolicy.NONE)
    started = threading.Event()
    context = ToolExecutionContext("probe", authorization=(started, threading.Event()))
    context.cancellation.request()
    assert (
        ToolExecutor.execute_direct(tool, context, {}, 5)["execution_status"]
        == "cancelled"
    )
    assert not started.is_set()


def test_registry_rejects_non_executable_definitions():
    import pytest

    definition = ToolDefinition(
        CapabilitySafetySchema("probe", "test", "trusted", "low")
    )
    with pytest.raises(TypeError, match="BaseTool"):
        ToolRegistry().register(definition)
