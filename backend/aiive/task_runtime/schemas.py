"""Persistent Task 的稳定 API、事件和 Agent 输出契约。"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    BLOCKED_APPROVAL = "blocked_approval"
    BLOCKED_USER = "blocked_user"
    BLOCKED_NODE = "blocked_node"
    RECONCILING = "reconciling"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    INVALIDATED = "invalidated"


class TaskBudget(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    max_runs: int = Field(default=20, ge=1, le=1000)
    max_model_calls: int = Field(default=30, ge=1, le=5000)
    max_actions: int = Field(default=50, ge=1, le=5000)
    max_actions_per_run: int = Field(default=8, ge=1, le=100)
    max_tool_failures: int = Field(default=10, ge=1, le=1000)
    max_total_tokens: int = Field(default=200_000, ge=1000)
    max_wall_time_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    max_cost_usd: float | None = Field(default=None, ge=0.01, le=1_000_000)
    deadline_at: str | None = None


class TaskScopeModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    allowed_capabilities: list[str] = Field(default_factory=list, max_length=128)
    allowed_roots: list[str] = Field(default_factory=list, max_length=64)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=64)
    allow_network: bool = False
    allow_secrets: bool = False
    isolation_mode: Literal["shared", "git_worktree"] = "shared"


class TaskBrief(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=64)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    scope: TaskScopeModel = Field(default_factory=TaskScopeModel)
    initial_input: dict[str, Any] = Field(default_factory=dict)


class ActionProposal(BaseModel):
    """Worker LLM 每次只能提出一个 Action 或结束/等待决策。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    decision: Literal["action", "finish", "ask_user", "watch"]
    capability_id: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    report_status: Literal["succeeded", "partial", "failed"] = "succeeded"
    question: str = ""
    watch: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_decision_payload(self) -> "ActionProposal":
        if self.decision == "action" and not self.capability_id:
            raise ValueError("action decision requires capability_id")
        if self.decision == "ask_user" and not self.question.strip():
            raise ValueError("ask_user decision requires question")
        if self.decision == "watch" and not self.watch:
            raise ValueError("watch decision requires watch config")
        return self


class TaskReport(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    status: Literal["succeeded", "partial", "failed"]
    summary: str
    completed_actions: int = 0
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
