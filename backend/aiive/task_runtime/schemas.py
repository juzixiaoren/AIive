"""Persistent Task 的稳定 API、事件和 Agent 输出契约。"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aiive.config import WORKER_BUDGET_LIMITS, settings
from aiive.tool_discovery import DEFAULT_LIMIT, MAX_LIMIT, MAX_QUERY_CHARS

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
    """新任务的 Worker 预算。省略预算、空对象或字段为 null 时使用服务器默认值。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    max_runs: int | None = Field(
        default=settings.aiive_worker_default_max_runs, ge=1, le=WORKER_BUDGET_LIMITS["max_runs"],
        description="任务累计执行 Run 上限。等待后恢复或拆批可能开启新 Run；仅成稿的 Run 不计。",
    )
    max_model_calls: int | None = Field(
        default=settings.aiive_worker_default_max_model_calls, ge=1, le=WORKER_BUDGET_LIMITS["max_model_calls"],
        description="任务累计 Worker 决策模型调用上限，包含能力发现与格式纠错调用，不含独立成稿。",
    )
    max_actions: int | None = Field(
        default=settings.aiive_worker_default_max_actions, ge=1, le=WORKER_BUDGET_LIMITS["max_actions"],
        description="任务累计创建的 Action 上限，含随后被拦截或进入审批的 Action；续跑可执行已创建的 Action。",
    )
    max_actions_per_run: int | None = Field(
        default=settings.aiive_worker_default_max_actions_per_run, ge=1, le=WORKER_BUDGET_LIMITS["max_actions_per_run"],
        description="单次 Run 的 Action 批大小上限；本轮能力发现也占一个批位置。任务总 Action 上限见 max_actions。",
    )
    max_tool_failures: int | None = Field(
        default=settings.aiive_worker_default_max_tool_failures, ge=1, le=WORKER_BUDGET_LIMITS["max_tool_failures"],
        description="累计工具执行失败次数上限；审批等待、取消、前置条件失效和执行结果未知不计入。",
    )
    max_total_tokens: int | None = Field(
        default=settings.aiive_worker_default_max_total_tokens or max(
            1000, min(2 * settings.aiive_llm_context_window, WORKER_BUDGET_LIMITS["max_total_tokens"]),
        ), ge=1000, le=WORKER_BUDGET_LIMITS["max_total_tokens"],
        description="Worker 决策模型累计输入加输出 Token 上限，含纠错；独立成稿单独计量。",
    )
    max_wall_time_seconds: int | None = Field(
        default=settings.aiive_worker_default_max_wall_time_seconds, ge=1, le=WORKER_BUDGET_LIMITS["max_wall_time_seconds"],
        description="从任务创建起经过的秒数上限，含排队、等待用户或审批时间；在 Worker 执行检查点生效。",
    )
    max_cost_usd: float | None = Field(
        default=None, ge=0.01, le=1_000_000,
        description="Worker 决策模型的估算美元费用上限，须配置输入与输出 Token 单价；不含第三方工具及独立成稿的费用。省略时不设美元上限。",
    )
    deadline_at: str | None = Field(
        default=None, description="可选的 ISO 8601 绝对截止时间，无时区按 UTC；与经过秒数上限同时生效，省略时不设绝对截止时间。",
    )

    @classmethod
    def for_new_task(cls, supplied: dict[str, Any] | None) -> "TaskBudget":
        return cls.model_validate({key: value for key, value in (supplied or {}).items()
                                   if value is not None or key not in cls.model_fields})

    @classmethod
    def from_stored(cls, stored: dict[str, Any] | None) -> "TaskBudget":
        # Persisted budgets were validated at creation, including limits accepted by older versions.
        values: dict[str, Any] = dict.fromkeys(cls.model_fields) | {"max_actions_per_run": 8} | (stored or {})
        return cls.model_construct(**values)


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
    required_capabilities: list[str] = Field(default_factory=list, max_length=128)
    scope: TaskScopeModel = Field(default_factory=TaskScopeModel)
    initial_input: dict[str, Any] = Field(default_factory=dict)


class ActionProposal(BaseModel):
    """服务端内部标准化决策 DTO；由已校验 Function Calling 转换，不直接暴露给模型。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    decision: Literal[
        "action", "finish", "ask_user", "watch", "search_capabilities", "describe_capability",
    ]
    capability_id: str = ""
    descriptor_hash: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    progress_draft: str = Field(default="", max_length=8000)
    # search_capabilities 的检索关键词；发现类决策只读，不产生 Action。
    query: str = Field(default="", max_length=MAX_QUERY_CHARS)
    namespace_ids: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=MAX_LIMIT,
    )
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
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
    stop_reason: str | None = None
    budget_exhausted: bool | None = None
    report_usage: dict[str, int] | None = None
    report_incomplete: bool | None = None
