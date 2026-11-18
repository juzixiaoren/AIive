"""Persistent Task 的稳定 API、事件和 Agent 输出契约。"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReportNote = Annotated[str, Field(min_length=1, max_length=500)]


class FilePreconditions(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    exists: bool | None = Field(default=None, description="Expected existence of the path or source.")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    size: int | None = Field(default=None, ge=0)
    mtime_ns: int | Annotated[str, Field(pattern=r"^-?[0-9]{1,20}$")] | None = Field(
        default=None, description="Expected nanosecond timestamp. Use a decimal string to preserve all digits on Desktop.",
    )
    file_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def consistent(self) -> "FilePreconditions":
        if self.exists is False and any(value is not None for value in (self.sha256, self.size, self.mtime_ns, self.file_id)):
            raise ValueError("a missing file has no version metadata")
        return self


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
    max_cost_usd: float | None = Field(
        default=None, ge=0.01, le=1_000_000,
        description="Worker decision model budget. Requires the operator to configure model token rates.",
    )
    deadline_at: str | None = None


class TaskScopeModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    allowed_capabilities: list[str] = Field(default_factory=list, max_length=128)
    allowed_roots: list[str] = Field(default_factory=list, max_length=64)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=64)
    allow_network: bool = False
    allow_secrets: bool = False
    isolation_mode: Literal["shared", "git_worktree"] = "shared"
    # unified：不按任务类型/技能收窄能力面；restricted：allowed_capabilities 为授权白名单。
    capability_policy: Literal["unified", "restricted"] = "restricted"


class TaskBrief(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=64)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    scope: TaskScopeModel = Field(default_factory=TaskScopeModel)
    initial_input: dict[str, Any] = Field(default_factory=dict)


class ActionProposal(BaseModel):
    """服务端内部标准化决策 DTO；由已校验 Function Calling 转换，不直接暴露给模型。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    decision: Literal[
        "action", "finish", "ask_user", "watch", "search_capabilities", "describe_capability",
    ]
    capability_id: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    # search_capabilities 的检索关键词；发现类决策只读，不产生 Action。
    query: str = ""
    report_status: Literal["succeeded", "partial", "failed"] = "succeeded"
    risks: list[ReportNote] = Field(default_factory=list, max_length=16)
    unresolved: list[ReportNote] = Field(default_factory=list, max_length=16)
    question: str = ""
    watch: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_decision_payload(self) -> "ActionProposal":
        if self.decision == "action" and not self.capability_id:
            raise ValueError("action decision requires capability_id")
        if self.preconditions:
            if not (self.arguments.get("path") or self.arguments.get("source") or self.arguments.get("file_path")):
                raise ValueError("file preconditions require a file path or source")
            self.preconditions = FilePreconditions.model_validate(self.preconditions, strict=True).model_dump(exclude_none=True)
        if self.decision == "ask_user" and not self.question.strip():
            raise ValueError("ask_user decision requires question")
        if self.decision == "watch" and not self.watch:
            raise ValueError("watch decision requires watch config")
        if self.decision == "search_capabilities" and not self.query.strip():
            raise ValueError("search_capabilities decision requires query")
        if self.decision == "describe_capability" and not self.capability_id.strip():
            raise ValueError("describe_capability decision requires capability_id")
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
