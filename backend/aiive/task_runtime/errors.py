"""Typed runtime failures: output defects must never become state-conflict retries."""
from typing import Any


class TaskNotFoundError(ValueError):
    pass


class TaskTerminalError(ValueError):
    pass


class TaskNotRunnableError(ValueError):
    pass


class AgentOutputValidationError(Exception):
    def __init__(self, issues: list[dict[str, Any]], *, token_usage: dict[str, int] | None = None, model: str = ""):
        super().__init__("agent_output_validation_error")
        self.issues: list[dict[str, Any]] = issues
        self.token_usage: dict[str, int] = token_usage or {}
        self.model: str = model

    def payload(self) -> dict[str, Any]:
        return {"error_type": "agent_output_validation_error", "retryable": True,
                "issues": self.issues, "instruction": "只修正输出结构，返回一个符合 ActionProposal Schema 的 JSON 对象，不重复无关内容。"}
