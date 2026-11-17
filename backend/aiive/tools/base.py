"""Executable tool protocol. Shared instances contain definitions, never execution state."""

from __future__ import annotations

import inspect
import threading
import uuid
from abc import ABC, abstractmethod
from copy import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from aiive.context.run_context import RunContext
    from aiive.tools.registry import ToolDefinition


class CancellationPolicy(StrEnum):
    NONE = "none"
    COOPERATIVE = "cooperative"
    REMOTE = "remote"
    TRANSACTIONAL = "transactional"


@dataclass(frozen=True)
class CancelResult:
    execution_id: str
    status: str  # requested / cancelled / already_completed / not_cancellable / unknown


class ToolCancelled(Exception):
    """Raised only when execution has stopped before committing its effects."""


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def checkpoint(self) -> None:
        if self.requested:
            raise ToolCancelled()


@dataclass
class ToolExecutionContext:
    capability_id: str
    run_context: RunContext | None = None
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    channel: str = "direct"
    deadline: float | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    db: Session | None = None
    authorization: Any = None
    operation_id: str = ""
    action_id: str = ""


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    outcome_known: bool = True
    side_effect_started: bool = False
    retry_hint: str = "never"
    details: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": self.code,
            "error": self.message,
            "outcome_known": self.outcome_known,
            "side_effect_started": self.side_effect_started,
            "retry_hint": self.retry_hint,
            "retryable": self.retry_hint == "correct_arguments",
            "details": self.details,
            "execution_status": "execution_unknown"
            if not self.outcome_known
            else "cancelled"
            if self.code == "cancelled"
            else "failed",
        }


@dataclass(frozen=True)
class ToolResult:
    value: Any = None
    error: ToolError | None = None

    def payload(self) -> dict[str, Any]:
        return (
            self.error.payload() if self.error else {"ok": True, "result": self.value}
        )


class BaseTool(ABC):
    """Tools implement business logic in a method; no callable registration exists.

    The default execute binds typed business parameters and the execution-local
    context. Transaction ownership belongs to the execution adapter.
    """

    def __init__(self, definition: ToolDefinition):
        self.definition: ToolDefinition = definition

    @property
    def safety(self):
        return self.definition.safety

    @property
    def description(self):
        return self.definition.description

    @property
    def parameters(self):
        return self.definition.parameters

    @property
    def input_schema(self):
        return self.definition.input_schema

    @property
    def executor_kind(self):
        return self.definition.executor_kind

    @property
    def executor_node_id(self):
        return self.definition.executor_node_id

    def with_definition(self, definition: ToolDefinition) -> BaseTool:
        tool = copy(self)
        tool.definition = definition
        return tool

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> Any:
        context.cancellation.checkpoint()
        injected: dict[str, Any] = {}
        signature = inspect.signature(self._run)
        if "_execution" in signature.parameters:
            injected["_execution"] = context
        if "ctx" in signature.parameters:
            injected["ctx"] = context.run_context
        if "db" in signature.parameters:
            if context.db is None:
                raise RuntimeError("tool_transaction_required")
            injected["db"] = context.db
        return self._run(**injected, **arguments)

    @abstractmethod
    def _run(self, *args: Any, **arguments: Any) -> Any:
        raise NotImplementedError

    def normalize_error(
        self, error: Exception, context: ToolExecutionContext
    ) -> ToolError:
        external = self.executor_kind == "desktop_node" or (
            self.safety.writes_external_world
            and self.safety.effect_mode != "db_transactional"
        )
        return ToolError(
            "execution_unknown" if external else "execution_failed",
            str(error),
            outcome_known=not external,
            side_effect_started=external,
            details={"execution_id": context.execution_id},
        )

    def request_cancel(self, context: ToolExecutionContext) -> CancelResult:
        if self.definition.cancellation_policy == CancellationPolicy.NONE:
            return CancelResult(context.execution_id, "not_cancellable")
        context.cancellation.request()
        return CancelResult(context.execution_id, "requested")

    def reconcile(self, context: ToolExecutionContext) -> ToolResult:
        return ToolResult(
            error=ToolError(
                "reconciliation_unavailable",
                "No reconciliation provider",
                outcome_known=False,
                details={"execution_id": context.execution_id},
            )
        )

    def compensate(self, context: ToolExecutionContext, receipt: Any) -> ToolResult:
        del receipt
        return ToolResult(
            error=ToolError(
                "compensation_unavailable",
                "Compensation requires a separate authorized operation",
                details={"execution_id": context.execution_id},
            )
        )


class DatabaseTool(BaseTool, ABC):
    """Business method uses the adapter's execution-thread transaction."""
