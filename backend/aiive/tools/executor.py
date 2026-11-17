"""One validation/dispatch/error lifecycle for ordinary, approved and brokered calls."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any

from aiive.context.run_context import RunContext
from aiive.tools.base import (
    BaseTool,
    CancellationPolicy,
    CancelResult,
    DatabaseTool,
    ToolCancelled,
    ToolError,
    ToolExecutionContext,
    ToolResult,
)
from aiive.tools.registry import DEFAULT_TOOL_TIMEOUT, ToolRegistry, _tool_executor
from aiive.tools.tool_validation import validate_tool_params

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


class DirectAdapter:
    @staticmethod
    def invoke(
        tool: BaseTool, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> Any:
        if tool.executor_kind == "desktop_node" and context.channel == "task_action":
            return DesktopAdapter.invoke(tool, context, arguments)
        if isinstance(tool, DatabaseTool) and context.db is None:
            # Resolve the factory inside the actual execution thread.
            from aiive.tools.builtin_tools import SessionLocal

            with SessionLocal() as db:
                context.db = db
                try:
                    result = tool.execute(context, arguments)
                    context.cancellation.checkpoint()
                    db.commit()
                    return result
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
            return ToolResult(
                error=ToolError(
                    code,
                    str(result.get("error")),
                    outcome_known=code != "execution_unknown",
                    side_effect_started=code == "execution_unknown",
                )
            )
        except DesktopDispatchError as error:
            return ToolResult(
                error=ToolError(
                    "execution_unknown",
                    str(error),
                    outcome_known=False,
                    side_effect_started=True,
                )
            )


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
        self.registry = registry

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
        if cancellation_requested(context):
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

        def invoke() -> Any:
            with _active_lock:
                _active[context.execution_id] = (tool, context)
            try:
                if cancellation_requested(context):
                    raise ToolCancelled()
                return DirectAdapter.invoke(tool, context, arguments)
            finally:
                with _active_lock:
                    _active.pop(context.execution_id, None)

        try:
            result = execute_in_tool_thread(
                _tool_executor,
                invoke,
                timeout,
                observe_cancel=lambda: cancellation_requested(context),
                await_terminal=context.channel == "turn_operation",
            )
            return {
                **(
                    result.payload()
                    if isinstance(result, ToolResult)
                    else ToolResult(result).payload()
                ),
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
            return {
                **tool.normalize_error(error, context).payload(),
                "execution_id": context.execution_id,
            }
