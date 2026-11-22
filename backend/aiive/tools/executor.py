"""One validation/dispatch/error lifecycle for ordinary, approved and brokered calls."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any

from aiive.context.run_context import RunContext
from aiive.tools.base import (
    UNKNOWN_OUTCOME_CODES,
    BaseTool,
    CancellationPolicy,
    CancelResult,
    DatabaseTool,
    DeadlineManagedTool,
    ToolCancelled,
    ToolError,
    ToolExecutionContext,
    ToolResult,
    coerce_tool_result,
)
from aiive.config import settings
from aiive.tools.registry import DEFAULT_TOOL_TIMEOUT, ToolRegistry
from aiive.tools.tool_validation import validate_tool_params

tool_thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=settings.tool_executor_max_workers,
    thread_name_prefix="tool-worker",
)

_active_lock = threading.Lock()
_active: dict[str, tuple[BaseTool, ToolExecutionContext]] = {}


def request_cancel(execution_id: str) -> CancelResult:
    with _active_lock:
        entry = _active.get(execution_id)
        if entry is None:
            return CancelResult(execution_id, "unknown")
        tool, context = entry
        return tool.request_cancel(context)


def cancellation_requested(context: ToolExecutionContext) -> bool:
    """Read persistent intent in a fresh execution-thread session, never a caller Session."""
    run = context.run_context
    if run is None:
        return context.cancellation.requested
    from aiive.db.base import SessionLocal
    from aiive.db.models import AgentTask, TurnRecord
    from aiive.worker.outbox_execution_auth import owns_session_factory
    from sqlalchemy.orm import Session

    factory_session = SessionLocal()
    db = Session(bind=factory_session.get_bind())
    try:
        if run.task_id:
            status = (
                db.query(AgentTask.status).filter(AgentTask.id == run.task_id).scalar()
            )
            if status == "cancelled":
                context.cancellation.request()
        if run.turn_record_id:
            cancelled = (
                db.query(TurnRecord.cancel_requested_at)
                .filter(TurnRecord.id == run.turn_record_id)
                .scalar()
            )
            if cancelled is not None:
                context.cancellation.request()
    finally:
        db.close()
        if owns_session_factory(SessionLocal):
            factory_session.close()
    return context.cancellation.requested


def desktop_dispatch_result(error: Any) -> ToolResult:
    """把带执行阶段的 Desktop 派发异常映射为统一工具结果。

    NOT_SUBMITTED：确认请求没有发出，判确定性失败（副作用一定没有发生）。
    SUBMITTED：请求已发出或无法确认，保留 UNKNOWN，等待 Journal 对账。
    TERMINAL：Node 已给出明确原因，按错误码判定。
    """
    from aiive.desktop.connection_manager import classify_pending_error
    from aiive.desktop.protocol import DesktopDispatchPhase

    phase = getattr(error, "phase", DesktopDispatchPhase.SUBMITTED)
    details = {"dispatch_phase": getattr(phase, "value", str(phase))}
    if phase == DesktopDispatchPhase.NOT_SUBMITTED:
        return ToolResult(
            error=ToolError("execution_failed", str(error), details=details),
        )
    if phase == DesktopDispatchPhase.TERMINAL:
        code = classify_pending_error(str(error))
        unknown = code in UNKNOWN_OUTCOME_CODES
        return ToolResult(
            error=ToolError(
                code,
                str(error),
                outcome_known=not unknown,
                side_effect_started=unknown,
                details=details,
            )
        )
    return ToolResult(
        error=ToolError(
            "execution_unknown",
            str(error),
            outcome_known=False,
            side_effect_started=True,
            details=details,
        )
    )


class DirectAdapter:
    @staticmethod
    def invoke(
        tool: BaseTool, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> Any:
        """执行一次工具业务方法。

        DatabaseTool 在自己的事务里先归一化再提交/回滚，返回规范化结果；
        其余工具的原始返回值由 ToolExecutor.execute_direct 统一归一化。
        """
        if tool.executor_kind == "desktop_node" and context.channel == "task_action":
            return DesktopAdapter.invoke(tool, context, arguments)
        if isinstance(tool, DatabaseTool) and context.db is None:
            # Resolve the factory inside the actual execution thread.
            from aiive.tools.builtin_tools import SessionLocal

            with SessionLocal() as db:
                context.db = db
                try:
                    # 先归一化再决定提交/回滚：ToolError / ToolResult / 业务字典
                    # 走同一份失败判断，绝不在提交之后才发现结构化失败。
                    normalized = coerce_tool_result(tool.execute(context, arguments))
                    context.cancellation.checkpoint()
                    if normalized.error is not None:
                        db.rollback()
                        return normalized
                    db.commit()
                    return normalized
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    context.db = None
        return tool.execute(context, arguments)


class DesktopAdapter:
    @staticmethod
    def invoke(
        tool: BaseTool, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> ToolResult:
        del arguments
        from aiive.desktop.connection_manager import (
            DesktopDispatchError,
            desktop_connection_manager,
        )

        try:
            result = desktop_connection_manager.dispatch_action_sync(
                **context.authorization,
                cancel_check=(lambda: cancellation_requested(context))
                if tool.definition.cancellation_policy == CancellationPolicy.REMOTE
                else None,
            )
            if result.get("ok"):
                return ToolResult(result.get("result"))
            code = str(result.get("error_type", "execution_failed"))
            unknown = code in UNKNOWN_OUTCOME_CODES
            return ToolResult(
                error=ToolError(
                    code,
                    str(result.get("error")),
                    outcome_known=not unknown,
                    side_effect_started=unknown,
                )
            )
        except DesktopDispatchError as error:
            return desktop_dispatch_result(error)


class TurnOperationAdapter:
    @staticmethod
    def execute(
        tool: BaseTool,
        arguments: dict[str, Any],
        run: RunContext,
        tool_call_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        from aiive.tools.operation_executor import (
            enqueue_tool_operation,
            wait_for_tool_operation,
        )

        operation = enqueue_tool_operation(
            tool.safety.capability_id,
            arguments,
            tool.safety.descriptor_hash,
            tool.safety.effect_mode,
            run,
            tool_call_id,
        )
        return wait_for_tool_operation(operation.id, timeout)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry: ToolRegistry = registry

    def execute(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        instruction_source: str,
        run_context: RunContext | None = None,
        *,
        tool_call_id: str = "",
        expected_descriptor_hash: str | None = None,
        channel: str = "direct",
        authorization: Any = None,
    ) -> dict[str, Any]:
        tool = self.registry.get(capability_id)
        if tool is None:
            return ToolError("unknown_tool", f"Unknown tool: {capability_id}").payload()
        # Task scope/policy/approval/lock checks are owned by CapabilityBroker.
        if (
            channel != "task_action"
            and instruction_source not in tool.safety.allowed_instruction_sources
        ):
            return ToolError(
                "unauthorized_source", "Instruction source not authorized for this tool"
            ).payload()
        if expected_descriptor_hash is not None and not expected_descriptor_hash:
            return ToolError(
                "missing_descriptor_hash", "Approval fingerprint missing"
            ).payload()
        if (
            expected_descriptor_hash is not None
            and tool.safety.descriptor_hash != expected_descriptor_hash
        ):
            return ToolError(
                "descriptor_changed",
                "Tool definition changed or approval fingerprint missing",
            ).payload()
        if (
            tool.executor_kind == "desktop_node"
            and channel == "task_action"
            and not authorization
        ):
            return ToolError(
                "missing_execution_identity",
                "Desktop execution requires a broker action envelope",
            ).payload()
        validation = validate_tool_params(tool, arguments)
        if not validation.ok:
            return validation.error_payload(capability_id)
        arguments = validation.validated_params
        timeout = tool.safety.timeout_seconds or DEFAULT_TOOL_TIMEOUT
        context = ToolExecutionContext(
            capability_id,
            run_context,
            channel=channel,
            authorization=authorization,
            deadline=time.monotonic() + timeout,
            action_id=run_context.action_id if run_context else "",
        )
        if not isinstance(tool, DeadlineManagedTool) and cancellation_requested(context):
            return {
                **ToolError("cancelled", "Cancelled before execution").payload(),
                "execution_status": "cancelled",
            }
        if (
            channel != "task_action"
            and tool.executor_kind != "desktop_node"
            and (tool.safety.writes_external_world or tool.safety.can_delete)
        ):
            if (
                run_context is None
                or not run_context.turn_record_id
                or not tool_call_id
            ):
                return ToolError(
                    "missing_execution_identity",
                    "Side effects require durable execution identity",
                ).payload()
            try:
                return TurnOperationAdapter.execute(
                    tool, arguments, run_context, tool_call_id, timeout
                )
            except ToolCancelled:
                return {
                    **ToolError("cancelled", "Cancelled before enqueue").payload(),
                    "execution_status": "cancelled",
                }
        return self.execute_direct(tool, context, arguments, timeout)

    @staticmethod
    def execute_direct(
        tool: BaseTool,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        from aiive.worker.outbox_execution_auth import (
            execute_in_tool_thread,
            require_execution_authorization,
        )

        managed_deadline = isinstance(tool, DeadlineManagedTool)

        def check_cancel() -> None:
            if cancellation_requested(context):
                raise ToolCancelled()

        def invoke() -> Any:
            with _active_lock:
                _active[context.execution_id] = (tool, context)
            try:
                if managed_deadline:
                    context.cancellation.checkpoint()
                else:
                    check_cancel()
                return DirectAdapter.invoke(tool, context, arguments)
            finally:
                with _active_lock:
                    _active.pop(context.execution_id, None)

        wait_timeout = timeout
        if managed_deadline and context.deadline is not None:
            wait_timeout = max(0.0, context.deadline - time.monotonic())
        try:
            result = execute_in_tool_thread(
                tool_thread_pool,
                invoke,
                wait_timeout,
                observe_cancel=None if managed_deadline else lambda: cancellation_requested(context),
                await_terminal=context.channel == "turn_operation",
                deadline_managed_preflight=check_cancel if managed_deadline else None,
            )
            return {
                **coerce_tool_result(result).payload(),
                "execution_id": context.execution_id,
            }
        except (ToolCancelled, concurrent.futures.CancelledError):
            return {
                **ToolError("cancelled", "Execution stopped").payload(),
                "execution_status": "cancelled",
                "execution_id": context.execution_id,
            }
        except concurrent.futures.TimeoutError:
            tool.request_cancel(context)
            if isinstance(tool, DeadlineManagedTool):
                return {**tool.deadline_error().payload(), "execution_id": context.execution_id}
            external = (
                tool.safety.writes_external_world
                or tool.executor_kind == "desktop_node"
            )
            error = ToolError(
                "execution_unknown" if external else "timeout",
                "Tool deadline exceeded",
                outcome_known=not external,
                side_effect_started=external,
            )
            return {
                **error.payload(),
                "execution_status": "execution_unknown" if external else "failed",
                "execution_id": context.execution_id,
                "timeout_seconds": timeout,
            }
        except Exception as error:
            require_execution_authorization()
            if tool.executor_kind == "desktop_node":
                from aiive.desktop.connection_manager import DesktopDispatchError

                if isinstance(error, DesktopDispatchError):
                    return {
                        **desktop_dispatch_result(error).payload(),
                        "execution_id": context.execution_id,
                    }
            return {
                **tool.normalize_error(error, context).payload(),
                "execution_id": context.execution_id,
            }
