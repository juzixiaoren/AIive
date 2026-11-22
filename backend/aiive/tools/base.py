"""Executable tool protocol. Shared instances contain definitions, never execution state."""

from __future__ import annotations

import inspect
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
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


# 明确表示"外部副作用真实结果未知"的错误码；其余错误码都视为确定性结论。
UNKNOWN_OUTCOME_CODES: frozenset[str] = frozenset({
    "execution_unknown",
    "desktop_operation_timeout",
    "desktop_node_disconnected",
})

# 业务结果字典里属于执行状态封套的键；其余键都是业务数据，原样保留在 details。
_RESULT_ENVELOPE_KEYS: frozenset[str] = frozenset({
    "ok", "error", "error_type", "code", "reason", "message",
    "execution_status", "outcome_known", "side_effect_started", "retry_hint",
})


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    outcome_known: bool = True
    side_effect_started: bool = False
    retry_hint: str = "never"
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_business_result(cls, value: Mapping[str, Any]) -> ToolError:
        """把工具业务字典的显式失败标记转换为统一错误。

        execution_unknown 与"结果未知"标记优先于 cancelled：只有确认已停止且
        没有未提交副作用时才能记录取消，否则必须保留未知语义。
        """
        raw_code = str(value.get("error_type") or value.get("code") or "")
        status = str(value.get("execution_status") or "")
        unknown = (
            value.get("outcome_known") is False
            or status == "execution_unknown"
            or raw_code in UNKNOWN_OUTCOME_CODES
        )
        cancelled = not unknown and (
            raw_code == "cancelled" or value.get("cancelled") is True
        )
        if unknown:
            code = "execution_unknown"
        elif cancelled:
            code = "cancelled"
        else:
            code = raw_code or "execution_failed"
        message = str(
            value.get("error") or value.get("reason") or value.get("message") or ""
        ) or f"Tool reported {code}"
        details = {
            key: item
            for key, item in value.items()
            if key not in _RESULT_ENVELOPE_KEYS
        }
        # outcome_known 与 side_effect_started 相互独立：结果确定的失败也可能
        # 已经产生了部分外部影响。只有 unknown 强制 outcome_known=False；
        # side_effect_started 以显式声明为准，未声明时按执行阶段推导
        # （未知 → 无法排除已开始执行；确定性失败/取消 → 默认未产生）。
        explicit_started = value.get("side_effect_started")
        side_effect_started = unknown if explicit_started is None else bool(explicit_started)
        return cls(
            code=code,
            message=message,
            outcome_known=not unknown,
            side_effect_started=side_effect_started,
            retry_hint=str(value.get("retry_hint") or "never"),
            details=details,
        )

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
        # 业务字典里的显式失败不能被包装成成功；这里与 coerce_tool_result
        # 共用同一判定，任何构造路径都不会丢失失败语义。
        if self.error is not None:
            return self.error.payload()
        if isinstance(self.value, ToolResult):
            return self.value.payload()
        if business_result_failed(self.value):
            # 直接构造 ToolResult(value={"ok": false}) 也不能被当成成功。
            return tool_error_from_business_result(self.value).payload()
        return {"ok": True, "result": self.value}


def business_result_failed(value: Any) -> bool:
    """只检查顶层显式失败标记。written、outcome、state 等业务字段由各工具解释。"""
    if not isinstance(value, Mapping):
        return False
    return value.get("ok") is False or value.get("allowed") is False


def tool_error_from_business_result(value: Mapping[str, Any]) -> ToolError:
    return ToolError.from_business_result(value)


def coerce_tool_result(value: Any) -> ToolResult:
    """工具返回值进入调用方之前的唯一归一化入口。

    兼容尚未改造的业务字典：只有顶层 ok=false / allowed=false 才判定失败。
    失败时同时保留原始业务字典（value），供诊断与既有调用方读取；是否成功
    只由 ToolResult.error 决定，payload() 永远优先输出错误封套。
    """
    if isinstance(value, ToolResult):
        if value.error is None and business_result_failed(value.value):
            # ToolResult 包裹了业务失败字典：同样不能当成成功。
            return ToolResult(
                value=value.value,
                error=tool_error_from_business_result(value.value),
            )
        return value
    if isinstance(value, ToolError):
        return ToolResult(error=value)
    if business_result_failed(value):
        return ToolResult(value=value, error=tool_error_from_business_result(value))
    return ToolResult(value=value)


def outcome_is_unknown(payload: Mapping[str, Any]) -> bool:
    """统一 payload 是否代表"外部副作用真实结果未知"。

    Callers must not downgrade these payloads to success, failure or cancellation.
    """
    if payload.get("ok") is True:
        return False
    if payload.get("outcome_known") is False:
        return True
    if str(payload.get("execution_status") or "") == "execution_unknown":
        return True
    return str(payload.get("error_type") or "") in UNKNOWN_OUTCOME_CODES


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


class DeadlineManagedTool(BaseTool):
    """Owns cancellation at context.deadline and returns after request cleanup."""

    @abstractmethod
    def deadline_error(self) -> ToolError:
        raise NotImplementedError


class DatabaseTool(BaseTool, ABC):
    """Business method uses the adapter's execution-thread transaction."""
