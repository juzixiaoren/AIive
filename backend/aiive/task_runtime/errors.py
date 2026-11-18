"""Typed runtime failures: output defects must never become state-conflict retries."""
from typing import Any


class TaskNotFoundError(ValueError):
    pass


class TaskTerminalError(ValueError):
    pass


class TaskNotRunnableError(ValueError):
    pass


class TaskModelBudgetError(ValueError):
    """The Worker request was rejected before contacting the model."""


class AgentOutputValidationError(Exception):
    def __init__(self, issues: list[dict[str, Any]], *, token_usage: dict[str, int] | None = None, model: str = "", feedback: list[dict[str, Any]] | None = None, payload: dict[str, Any] | None = None):
        super().__init__("agent_output_validation_error")
        self.issues: list[dict[str, Any]] = issues
        self.token_usage: dict[str, int] = token_usage or {}
        self.model: str = model
        self.feedback: list[dict[str, Any]] = feedback or []
        self.error_payload: dict[str, Any] | None = payload

    def payload(self) -> dict[str, Any]:
        if self.error_payload is not None:
            return self.error_payload
        return {"error_type": "agent_output_validation_error", "retryable": True,
                "issues": self.issues, "instruction": "只修正函数调用结构或参数，不重复无关内容。"}
