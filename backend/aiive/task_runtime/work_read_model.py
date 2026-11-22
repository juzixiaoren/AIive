"""Read-only work projections. No execution services or persistence operations."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import and_, func, or_, tuple_
from sqlalchemy.orm import Session, load_only

from aiive.db.models import (
    AgentAction,
    AgentTask,
    AgentTaskEvent,
    ApprovalRequest,
    ConversationState,
    DesktopNode,
    TaskArtifact,
    TaskEvidence,
)
from aiive.task_runtime.goal_plan import (
    TERMINAL_GOAL_STATES,
    GoalPlan,
    next_runnable_step,
)
from aiive.task_runtime.goal_questions import (
    PendingQuestion,
    list_open_questions_for_tasks,
    task_question_candidates,
)
from aiive.forget.concurrency import forget_revision
from aiive.task_runtime.read_repository import (
    TASK_VISIBILITY_COLUMNS,
    Child,
    ChildObject,
    ReadVisibilityContext,
    TaskContentUnavailable,
    TaskReadRepository,
)
from aiive.task_runtime.repository import TERMINAL_TASK_STATES, canonical_json

class WorkBucket(StrEnum):
    ATTENTION = "attention"
    ACTIVE = "active"
    WAITING = "waiting"
    ENDED = "ended"


@dataclass(frozen=True)
class WorkAttentionItem:
    id: str
    kind: str
    text: str
    title: str
    description: str
    resource_id: str
    created_at: datetime
    expires_at: datetime | None
    allowed_actions: list[str]
    question_id: str | None = None
    step_id: str | None = None
    approval_id: str | None = None
    attempt_id: str | None = None
    preview: dict[str, Any] | None = None


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def approval_preview(approval: ApprovalRequest) -> dict[str, Any]:
    args = approval.tool_args or {}
    tool = approval.tool_name
    result = {
        "tool_name": tool,
        "risk_level": (approval.risk_snapshot or {}).get("risk_level"),
        "description": "工具执行审批",
    }
    fields = {
        "desktop_exec": ("command", "cwd"),
        "run_shell": ("command", "cwd"),
        "desktop_fs_delete": ("path", "recursive"),
        "desktop_fs_write": ("path",),
        "install_mcp_sandbox": ("candidate_name", "version"),
    }
    for key in fields.get(tool, ()):
        if isinstance(args.get(key), (str, bool, int)):
            result[key] = args[key]
    if tool in fields:
        result["env_names"] = (
            sorted(str(key) for key in args.get("env", {}))
            if isinstance(args.get("env"), dict)
            else []
        )
    if tool == "desktop_fs_write":
        content = args.get("content")
        result["content_size"] = (
            len(content.encode("utf-8")) if isinstance(content, str) else None
        )
    if tool == "install_mcp_sandbox":
        from aiive.mcp.discovery import get_candidate_by_name

        candidate = get_candidate_by_name(str(args.get("candidate_name", "")))
        result["source"] = candidate.source if candidate else "local_allowlist"
        result["env_names"] = list(args.get("env_keys") or [])
        result["launch_args"] = list(args.get("launch_args") or [])
        result["lifecycle_scripts"] = "disabled"
    return result


TIMELINE_TITLES = {
    "task_created": "任务已创建",
    "task_completed": "任务已结束",
    "goal_proposed": "目标待确认",
    "goal_confirmed": "目标已确认",
    "goal_modified": "目标计划已修改",
    "goal_rejected": "目标已拒绝",
    "goal_ended": "目标已结束",
    "goal_completed": "目标已完成",
    "goal_blocked": "目标受阻",
    "goal_step_answered": "目标问题已回答",
    "goal_step_triggered": "目标步骤已触发",
    "goal_step_waiting_user": "目标步骤等待用户",
    "goal_step_due": "目标步骤已到期",
    "goal_step_missed": "目标步骤已错过",
    "goal_adjustment_suggested": "目标计划需要调整",
    "goal_proposal_dormant": "目标草案停止提醒",
    "goal_proposal_reminded": "目标确认已提醒",
    "goal_step_reminded": "目标步骤已提醒",
    "approval_granted": "操作已获批准",
    "approval_denied": "操作审批已拒绝",
    "approval_expired": "操作审批已过期",
    "goal_waiting": "目标等待中",
    "goal_paused": "目标已暂停",
    "goal_resumed": "目标已恢复",
    "goal_step_completed": "目标步骤已完成",
    "goal_step_done": "目标步骤已完成",
    "goal_step_failed": "目标步骤执行失败",
    "goal_step_running": "目标步骤执行中",
    "goal_step_started_by_user": "用户已启动步骤",
    "goal_step_approved": "目标步骤已获批准",
    "task_blocked_node": "等待设备",
    "task_reconciling": "执行核对中",
    "task_cancelled": "任务已取消",
    "task_blocked_user": "等待用户回答",
    "task_watching": "等待事件",
    "user_input_received": "收到用户补充信息",
    "watch_triggered": "等待条件已满足",
    "watch_failed": "等待条件不可用",
    "run_started": "开始执行",
    "task_recovered": "任务已恢复",
    "task_reconciliation_required": "等待执行核对",
    "action_reconciled_by_user": "用户已确认操作结果",
    "goal_step_continued": "步骤继续执行",
}


# Action events are emitted as action_{status} by transition_action().
TIMELINE_TITLES.update(
    {
        f"action_{status}": title
        for status, title in {
            "planned": "操作已规划",
            "validated": "操作已校验",
            "awaiting_approval": "操作等待审批",
            "ready": "操作已就绪",
            "dispatched": "操作已派发",
            "running": "操作执行中",
            "succeeded": "操作执行成功",
            "failed": "操作执行失败",
            "cancelled": "操作已取消",
            "unknown": "操作结果待核对",
            "invalidated": "操作已失效",
        }.items()
    }
)
ATTENTION_ACTIONS = {
    "action_reconciliation": ("确认未知操作", ["completed", "not_executed"]),
    "action_approval": ("审批操作", ["approve", "deny"]),
    "task_question": ("任务等待回答", ["send_input"]),
    "goal_confirmation": ("确认目标计划", ["confirm", "modify", "reject"]),
    "goal_question": ("目标等待回答", ["answer", "modify_plan", "reject"]),
    "goal_step_start": ("启动目标步骤", ["start_step"]),
    "goal_step_approval": ("审批目标步骤", ["approve_step"]),
    "goal_blocked": ("目标推进受阻", ["modify", "end"]),
}
HISTORY_SCAN_LIMIT = 512
METADATA_LIMIT = 50


T = TypeVar("T")
ACTION_COLUMNS = (
    AgentAction.id, AgentAction.task_id, AgentAction.sequence, AgentAction.status,
    AgentAction.capability_id, AgentAction.target_node_id, AgentAction.result_summary,
    AgentAction.arguments_hash, AgentAction.descriptor_hash, AgentAction.execution_token,
)
TIMELINE_COLUMNS = (
    AgentTaskEvent.id, AgentTaskEvent.task_id, AgentTaskEvent.sequence, AgentTaskEvent.event_type,
    AgentTaskEvent.visibility, AgentTaskEvent.created_at, AgentTaskEvent.action_id, AgentTaskEvent.run_id,
)


@dataclass
class WorkProjectionInputs:
    task: AgentTask
    actions: list[AgentAction]
    events: list[AgentTaskEvent]
    approvals: list[ApprovalRequest]
    questions: list[PendingQuestion]
    high_sequence: int
    action_scan_limited: bool


class WorkReadRetry(RuntimeError):
    """Concurrent Forget invalidated both read attempts."""


def group_by_task(rows: list[Child]) -> dict[str, list[Child]]:
    groups: dict[str, list[Child]] = defaultdict(list)
    for row in rows:
        if row.task_id is not None:
            groups[row.task_id].append(row)
    return groups


class WorkReadModel:
    def __init__(self, db: Session) -> None:
        self.db: Session = db
        self.reader: TaskReadRepository = TaskReadRepository(db)
        self._nodes: dict[str, DesktopNode] = {}
        self._question_steps: set[tuple[str, str]] = set()
        self._commands: dict[str, str] | None = None

    def _stable_read(self, read: Callable[[WorkReadModel, int], T]) -> T:
        with self.db.no_autoflush:
            for _ in range(2):
                revision = forget_revision(self.db)
                attempt = WorkReadModel(self.db)
                attempt.reader = TaskReadRepository(self.db, ReadVisibilityContext(self.db))
                try:
                    result = read(attempt, revision)
                except (TaskContentUnavailable, ValueError):
                    if revision == forget_revision(self.db):
                        raise
                else:
                    if revision == forget_revision(self.db):
                        return result
                # READ COMMITTED sees new rows, but existing ORM identities need refreshing.
                self.db.expire_all()
        raise WorkReadRetry("work_visibility_changed_retry")

    def project_many(self, tasks: list[AgentTask]) -> list[dict[str, Any]]:
        return self._stable_read(lambda attempt, _: [
            attempt._project_detail(inputs) for inputs in attempt._load_projections(tasks, detail=True)
        ])

    def _load_projections(
        self, tasks: list[AgentTask], *, detail: bool,
        extra: dict[str, list[ChildObject]] | None = None, high: dict[str, int] | None = None,
    ) -> list[WorkProjectionInputs]:
        ids = [task.id for task in tasks]
        if not ids:
            return []
        # The existing per-status scan cap also controls the data warning.
        action_rank = (
            self.db.query(
                AgentAction.id.label("id"),
                func.row_number()
                .over(
                    partition_by=(AgentAction.task_id, AgentAction.status),
                    order_by=AgentAction.sequence.desc(),
                )
                .label("rank"),
            )
            .filter(
                AgentAction.task_id.in_(ids),
                AgentAction.status.in_(
                    [
                        "running",
                        "dispatched",
                        "awaiting_approval",
                        "succeeded",
                        "failed",
                        "unknown",
                    ]
                ),
            )
            .subquery()
        )
        awaiting_tasks = [task.id for task in tasks if task.status == "blocked_approval"]
        columns = ACTION_COLUMNS + ((
            AgentAction.risk_level, AgentAction.requires_approval, AgentAction.created_at,
            AgentAction.started_at, AgentAction.completed_at,
        ) if detail else ())
        action_query = (
            self.db.query(AgentAction, action_rank.c.rank)
            .options(load_only(*columns))
            .join(action_rank, action_rank.c.id == AgentAction.id)
            .filter(action_rank.c.rank <= 33)
        )
        if not detail:
            action_query = action_query.filter(or_(
                action_rank.c.rank == 33,
                AgentAction.status.in_(("running", "dispatched", "unknown")),
                and_(AgentAction.status.in_(("succeeded", "failed")), AgentAction.target_node_id.is_not(None)),
                and_(AgentAction.status == "awaiting_approval", AgentAction.task_id.in_(awaiting_tasks)),
            ))
        ranked = action_query.order_by(AgentAction.sequence.desc()).all()
        budget_exceeded = {action.task_id for action, rank in ranked if rank == 33}
        action_candidates = [action for action, rank in ranked if rank <= 32]

        blocked_tasks = [
            task.id for task in tasks if task.status == "blocked_user"
            and not self._parse_plan(task)[0]
        ]
        event_candidates = []
        if blocked_tasks:
            event_rank = (
                self.db.query(
                    AgentTaskEvent.id.label("id"),
                    func.row_number().over(
                        partition_by=AgentTaskEvent.task_id,
                        order_by=AgentTaskEvent.sequence.desc(),
                    ).label("rank"),
                )
                .filter(
                    AgentTaskEvent.task_id.in_(blocked_tasks),
                    AgentTaskEvent.event_type.in_(["task_blocked_user", "task_watching"]),
                )
                .subquery()
            )
            event_candidates = (
                self.db.query(AgentTaskEvent)
                .join(event_rank, event_rank.c.id == AgentTaskEvent.id)
                .filter(event_rank.c.rank <= 32)
                .order_by(AgentTaskEvent.sequence.desc())
                .all()
            )
        awaiting_ids = [
            action.id for action in action_candidates
            if action.status == "awaiting_approval" and action.task_id in awaiting_tasks
        ]
        approval_candidates = list(self.db.query(ApprovalRequest).options(load_only(
            ApprovalRequest.id, ApprovalRequest.task_id, ApprovalRequest.action_id, ApprovalRequest.thread_id,
            ApprovalRequest.tool_name, ApprovalRequest.tool_args, ApprovalRequest.tool_args_hash,
            ApprovalRequest.descriptor_hash, ApprovalRequest.risk_snapshot, ApprovalRequest.created_at,
            ApprovalRequest.expires_at,
        )).filter(
            ApprovalRequest.action_id.in_(awaiting_ids),
            ApprovalRequest.status == "pending",
        ).all()) if awaiting_ids else []

        now = datetime.now(UTC)
        question_candidates = task_question_candidates(self.db, tasks, now)
        groups: dict[str, Sequence[ChildObject]] = dict(extra or {})
        groups["agent_task_event"] = [*groups.get("agent_task_event", ()), *event_candidates, *question_candidates.events]
        groups["agent_action"] = action_candidates
        groups["approval_request"] = approval_candidates
        context = self.reader.visibility
        assert context is not None
        context.resolve(tasks, groups)
        visible_tasks = self.reader.task_visible_many(tasks)
        tasks = [task for task in tasks if task.id in visible_tasks]
        actions = group_by_task(self.reader.filter_children("agent_action", action_candidates))
        events = group_by_task(self.reader.filter_children("agent_task_event", event_candidates))
        approvals = group_by_task(self.reader.filter_children("approval_request", approval_candidates))
        node_ids = {
            node_id for task in tasks for _, node_id, _ in [self._activity(task, actions.get(task.id, []))]
            if node_id
        }
        self._nodes = {
            node.id: node for node in self.db.query(DesktopNode).options(load_only(
                DesktopNode.id, DesktopNode.name, DesktopNode.platform, DesktopNode.lease_expires_at,
            )).filter(DesktopNode.id.in_(node_ids))
        } if node_ids else {}
        command_ids = []
        for task in tasks:
            reconciliation = [
                action for action in actions.get(task.id, [])
                if action.status == "unknown" and not action.target_node_id
            ]
            command_ids.extend(action.id for action in (reconciliation if detail else reconciliation[:1])
                               if action.capability_id == "run_shell")
        self._commands = {
            identity: str((arguments or {}).get("command") or "")[:1000]
            for identity, arguments in self.db.query(AgentAction.id, AgentAction.arguments)
            .filter(AgentAction.id.in_(command_ids))
        } if command_ids else {}
        # Existence only: an expired/resolved/forgotten question must not reappear
        # through the legacy step.note fallback. Restrict to the current plan's steps.
        pairs: list[tuple[str, str]] = []
        for task in tasks:
            try:
                plan = GoalPlan.model_validate((task.task_state or {}).get("goal"))
                pairs.extend((task.id, step.id) for step in plan.steps)
            except ValueError:
                continue
        step_key = AgentTaskEvent.payload["step_id"].as_string()
        self._question_steps = (
            {
                (row[0], row[1])
                for row in self.db.query(AgentTaskEvent.task_id, step_key)
                .filter(
                    AgentTaskEvent.event_type == "goal_question_asked",
                    tuple_(AgentTaskEvent.task_id, step_key).in_(pairs),
                )
                .distinct()
                .all()
            }
            if pairs
            else set()
        )
        questions = list_open_questions_for_tasks(
            self.db, ids, now=now, reader=self.reader, candidates=question_candidates,
        )
        if high is None:
            high = self._high_water(ids)
        return [
            WorkProjectionInputs(
                task, actions.get(task.id, []), events.get(task.id, []), approvals.get(task.id, []),
                questions.get(task.id, []), high.get(task.id, 0), task.id in budget_exceeded,
            )
            for task in tasks
        ]

    def _inbox_summaries(self, tasks: list[AgentTask]) -> dict[str, dict[str, Any]]:
        return {
            inputs.task.id: self._project_summary(inputs, detail=False)
            for inputs in self._load_projections(tasks, detail=False)
        }

    def _project_summary(self, inputs: WorkProjectionInputs, *, detail: bool) -> dict[str, Any]:
        projection = self.project(
            inputs.task, inputs.actions, inputs.events, inputs.approvals,
            inputs.questions, inputs.high_sequence, detail=detail,
        )
        if inputs.action_scan_limited:
            projection["data_warning"] = projection["data_warning"] or "action_history_scan_limit"
        return projection

    def _project_detail(self, inputs: WorkProjectionInputs) -> dict[str, Any]:
        projection = self._project_summary(inputs, detail=True)
        projection["actions"] = [
            {"id": action.id, "sequence": action.sequence, "capability_id": action.capability_id,
             "status": action.status, "risk_level": action.risk_level,
             "requires_approval": action.requires_approval, "created_at": action.created_at,
             "started_at": action.started_at, "completed_at": action.completed_at}
            for action in inputs.actions
        ]
        return projection

    def _high_water(self, ids: list[str]) -> dict[str, int]:
        return dict(self.db.query(AgentTaskEvent.task_id, func.max(AgentTaskEvent.sequence))
                    .filter(AgentTaskEvent.task_id.in_(ids)).group_by(AgentTaskEvent.task_id).all())

    @staticmethod
    def _parse_plan(task: AgentTask) -> tuple[GoalPlan | None, str | None]:
        raw = (task.task_state or {}).get("goal")
        if raw is not None:
            try:
                return GoalPlan.model_validate(raw), None
            except ValueError:
                return None, "invalid_goal_plan"
        return None, None

    def project(
        self,
        task: AgentTask,
        actions: list[AgentAction],
        events: list[AgentTaskEvent],
        approvals: list[ApprovalRequest],
        questions: list[PendingQuestion],
        high: int,
        *,
        detail: bool = True,
    ) -> dict[str, Any]:
        plan, warning = self._parse_plan(task)
        items = []
        attention_count = 0

        def add(
            kind: str, identity: str, text: str, *,
            preview: Callable[[], dict[str, Any]] | None = None, **kwargs: Any,
        ) -> None:
            nonlocal attention_count
            attention_count += 1
            if not detail and items:
                return
            title, allowed = ATTENTION_ACTIONS[kind]
            resource_id = kwargs.pop("resource_id", identity)
            created_at = kwargs.pop("created_at", task.updated_at)
            expires_at = kwargs.pop("expires_at", None)
            # These hints never grant permission; write services validate independently.
            items.append(
                asdict(
                    WorkAttentionItem(
                        id=f"{kind}:{identity}",
                        kind=kind,
                        text=text,
                        title=title,
                        description=text,
                        resource_id=resource_id,
                        created_at=created_at,
                        expires_at=expires_at,
                        allowed_actions=list(allowed),
                        preview=preview() if preview else None,
                        **kwargs,
                    )
                )
            )

        terminal = task.status in TERMINAL_TASK_STATES or (
            plan and plan.status in TERMINAL_GOAL_STATES
        )
        def reconciliation_preview(action: AgentAction) -> dict[str, Any]:
            preview = {"tool_name": action.capability_id, "action_id": action.id}
            if action.capability_id == "run_shell":
                preview["command"] = (self._commands.get(action.id, "") if self._commands is not None
                                      else str((action.arguments or {}).get("command") or "")[:1000])
            return preview

        for action in actions:
            if action.status == "unknown" and not action.target_node_id:
                add("action_reconciliation", action.id,
                    "请核对外部操作是否已完成，或确认它从未执行。",
                    preview=lambda action=action: reconciliation_preview(action),
                    attempt_id=action.execution_token or action.id)
        if not terminal and not (plan and plan.status == "paused"):
            if plan and plan.status == "proposed":
                add("goal_confirmation", task.id, plan.summary)
            elif plan and plan.status == "blocked":
                add("goal_blocked", task.id, plan.blocked_reason)
            by_action = {action.id: action for action in actions}
            for approval in approvals:
                action = by_action.get(approval.action_id or "")
                if (
                    approval.task_id == task.id
                    and approval.thread_id == task.thread_id
                    and task.status == "blocked_approval"
                    and action
                    and action.status == "awaiting_approval"
                    and action.arguments_hash == approval.tool_args_hash
                    and action.descriptor_hash == approval.descriptor_hash
                    and self.reader.visible_child("approval_request", approval)
                    and hashlib.sha256(
                        canonical_json(approval.tool_args or {}).encode("utf-8")
                    ).hexdigest()
                    == approval.tool_args_hash
                    and approval.tool_name == action.capability_id
                    and (
                        approval.expires_at is None
                        or utc(approval.expires_at) > datetime.now(UTC)
                    )
                ):
                    add(
                        "action_approval",
                        approval.id,
                        "等待执行审批",
                        approval_id=approval.id,
                        preview=lambda approval=approval: approval_preview(approval),
                        created_at=approval.created_at,
                        expires_at=approval.expires_at,
                    )
            for q in questions:
                add(
                    "goal_question",
                    q.question_id,
                    q.question,
                    question_id=q.question_id,
                    step_id=q.step_id,
                    created_at=q.created_at,
                    expires_at=q.expires_at,
                )
            if plan and plan.status == "active":
                for step in plan.steps:
                    if step.status != "waiting_user":
                        continue
                    kind = {
                        "interactive": "goal_step_start",
                        "approval_required": "goal_step_approval",
                        "autonomous": "goal_question",
                    }[step.kind]
                    if kind == "goal_question" and (
                        any(q.step_id == step.id for q in questions)
                        or (task.id, step.id) in self._question_steps
                    ):
                        continue
                    add(
                        kind,
                        f"{task.id}:{step.id}",
                        step.note or step.title,
                        step_id=step.id,
                        resource_id=step.id,
                    )
            if (
                not plan
                and task.status == "blocked_user"
                and events
                and events[0].event_type == "task_blocked_user"
            ):
                question = (events[0].payload or {}).get("question")
                if isinstance(question, str) and question.strip():
                    add(
                        "task_question",
                        events[0].id,
                        question,
                        created_at=events[0].created_at,
                    )
        if terminal:
            bucket, reason = "ended", "terminal"
        elif plan and plan.status == "paused":
            bucket, reason = "waiting", "goal_paused"
        elif items:
            bucket, reason = "attention", items[0]["kind"]
        elif task.status in {
            "blocked_node",
            "reconciling",
            "blocked_approval",
            "blocked_user",
        }:
            bucket, reason = "waiting", task.status
        elif warning:
            bucket, reason = "waiting", "data_warning"
        elif plan:
            active = (
                any(s.status in {"ready", "running"} for s in plan.steps)
                or next_runnable_step(plan, datetime.now(UTC)) is not None
            )
            bucket, reason = (
                ("active", "goal_executing") if active else ("waiting", "goal_waiting")
            )
        elif task.status in {"queued", "dispatching", "running", "verifying"}:
            bucket, reason = "active", task.status
        else:
            bucket, reason = "waiting", task.status
            if task.status not in {
                "blocked_user",
                "blocked_node",
                "reconciling",
                "blocked_approval",
            }:
                warning = "unknown_task_status"
        current, node_id, source = self._activity(task, actions)
        node = self._nodes.get(node_id) if node_id else None
        from aiive.desktop.connection_manager import desktop_connection_manager

        device = {
            "node_id": node_id,
            "source": source,
            "relationship": source,
            "name": node.name if node else None,
            "platform": node.platform if node else None,
            "online": bool(
                node_id
                and node
                and node.lease_expires_at
                and utc(node.lease_expires_at) > datetime.now(UTC)
                and desktop_connection_manager.is_connected(node_id)
            ),
        }
        return {
            "task_id": task.id,
            "title": task.title,
            "task_type": task.task_type,
            "task_status": task.status,
            "bucket": bucket,
            "bucket_reason": reason,
            "data_warning": warning,
            "attention_count": attention_count,
            "primary_attention": items[0] if items else None,
            **({"attention_items": items} if detail else {}),
            "progress": plan.progress() if plan else None,
            "current_activity": {
                "action_id": current.id,
                "capability_id": current.capability_id,
                "status": current.status,
            }
            if current
            else None,
            "target_device": device,
            "last_sequence": high,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    @staticmethod
    def _activity(task: AgentTask, actions: list[AgentAction]) -> tuple[AgentAction | None, str | None, str]:
        current = next(
            (a for a in actions if a.status in {"dispatched", "running"}), None
        )
        recent = next(
            (
                a
                for a in actions
                if a.target_node_id
                and a.status
                not in {
                    "planned",
                    "validated",
                    "awaiting_approval",
                    "ready",
                    "invalidated",
                    "cancelled",
                }
            ),
            None,
        )
        device_action = current if current and current.target_node_id else recent
        node_id = device_action.target_node_id if device_action else task.target_node_id
        source = (
            ("current" if device_action is current and current else "last_used")
            if device_action
            else ("configured" if node_id else "unknown")
        )
        return current, node_id, source

    def project_event(self, event: AgentTaskEvent) -> dict[str, Any] | None:
        """Shared, fail-closed event projection; never copy arbitrary payload strings."""
        if (
            event.visibility == "internal"
            or event.event_type not in TIMELINE_TITLES
        ):
            return None
        title = TIMELINE_TITLES[event.event_type]
        scope = (
            "执行操作"
            if event.event_type.startswith("action_")
            else (
                "目标步骤" if event.event_type.startswith("goal_step_") else "工作任务"
            )
        )
        return {
            "id": event.id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "title": title,
            "summary": f"{scope}状态更新：{title}。",
            "created_at": event.created_at,
            "action_id": event.action_id,
            "run_id": event.run_id,
        }

    def _load_task(self, task_id: str, *, detail: bool = False) -> AgentTask:
        columns = TASK_VISIBILITY_COLUMNS
        if detail:
            columns = (*columns, AgentTask.title, AgentTask.task_type, AgentTask.status,
                       AgentTask.target_node_id, AgentTask.updated_at, AgentTask.goal, AgentTask.last_error)
        task = self.db.query(AgentTask).options(load_only(*columns)).filter(AgentTask.id == task_id).one_or_none()
        if task is None:
            raise TaskContentUnavailable("task_unavailable")
        return task

    def _require_visible(self, task: AgentTask, groups: dict[str, list[ChildObject]]) -> None:
        context = self.reader.visibility
        assert context is not None
        context.resolve([task], groups)
        if not self.reader.task_visible(task):
            raise TaskContentUnavailable("task_unavailable")

    def timeline(
        self, task_id: str, after_sequence: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        return self._stable_read(lambda attempt, _: attempt._timeline(task_id, after_sequence, limit))

    def _timeline(self, task_id: str, after_sequence: int, limit: int) -> dict[str, Any]:
        task = self._load_task(task_id)
        rows = (
            self.db.query(AgentTaskEvent).options(load_only(*TIMELINE_COLUMNS))
            .filter(
                AgentTaskEvent.task_id == task_id,
                AgentTaskEvent.sequence > after_sequence,
            )
            .order_by(AgentTaskEvent.sequence)
            .limit(limit + 1)
            .all()
        )
        candidates = [row for row in rows[:limit]
                      if row.visibility != "internal" and row.event_type in TIMELINE_TITLES]
        self._require_visible(task, {"agent_task_event": list(candidates)})
        items = [
            item for row in self.reader.filter_children("agent_task_event", candidates)
            if (item := self.project_event(row)) is not None
        ]
        cursor = rows[min(limit, len(rows)) - 1].sequence if rows and limit else after_sequence
        return {"items": items, "next_sequence": cursor, "has_more": len(rows) > limit}

    def timeline_history(
        self, task_id: str, before_sequence: int | None = None, limit: int = 50, *, after_sequence: int = 0,
    ) -> dict[str, Any]:
        def read(attempt: WorkReadModel, _: int) -> dict[str, Any]:
            task = attempt._load_task(task_id)
            if after_sequence:
                attempt._require_visible(task, {})
                raise ValueError("choose_one_timeline_cursor")
            rows = attempt._timeline_history_rows(task_id, before_sequence)
            attempt._require_visible(task, {"agent_task_event": list(rows[:HISTORY_SCAN_LIMIT])})
            return attempt._project_history(rows, before_sequence, limit)
        return self._stable_read(read)

    def _timeline_history_rows(
        self, task_id: str, before_sequence: int | None,
    ) -> list[AgentTaskEvent]:
        query = self.db.query(AgentTaskEvent).options(load_only(*TIMELINE_COLUMNS)).filter(
            AgentTaskEvent.task_id == task_id,
            AgentTaskEvent.visibility != "internal",
            AgentTaskEvent.event_type.in_(TIMELINE_TITLES),
        )
        if before_sequence is not None:
            query = query.filter(AgentTaskEvent.sequence < before_sequence)
        return list(query.order_by(AgentTaskEvent.sequence.desc()).limit(HISTORY_SCAN_LIMIT + 1).all())

    def _project_history(
        self, rows: list[AgentTaskEvent], before_sequence: int | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        scanned_rows = rows[:HISTORY_SCAN_LIMIT]
        visible = self.reader.visible_children_many("agent_task_event", scanned_rows)
        items = []
        cursor = before_sequence
        scanned = 0
        for row in scanned_rows:
            cursor = row.sequence
            scanned += 1
            if row.id in visible and (item := self.project_event(row)) is not None:
                items.append(item)
                if len(items) == limit:
                    break
        return {"items": list(reversed(items)), "next_sequence": cursor, "has_more": scanned < len(rows)}

    def metadata_page(
        self, task_id: str, kind: str, cursor: str | None = None, limit: int = METADATA_LIMIT,
    ) -> dict[str, Any]:
        def read(attempt: WorkReadModel, _: int) -> dict[str, Any]:
            task = attempt._load_task(task_id)
            try:
                owner, fields, rows = attempt._metadata_rows(task_id, kind, cursor)
            except ValueError:
                # Hidden and absent tasks have the same response even with a bad cursor.
                attempt._require_visible(task, {})
                raise
            attempt._require_visible(task, {owner: list(rows[:HISTORY_SCAN_LIMIT])})
            return attempt._project_metadata(task_id, kind, owner, fields, rows, limit)
        return self._stable_read(read)

    def _metadata_rows(
        self, task_id: str, kind: str, cursor: str | None = None,
    ) -> tuple[str, tuple[str, ...], list[TaskArtifact | TaskEvidence]]:
        model, owner, extras = {
            "artifacts": (TaskArtifact, "task_artifact", ("name", "artifact_type")),
            "evidence": (TaskEvidence, "task_evidence", ("kind",)),
        }[kind]
        fields = ("id", "summary", "mime_type", "content_size", "content_hash", "created_at", *extras)
        query = self.db.query(model).options(load_only(
            model.task_id, *(getattr(model, field) for field in fields),
        )).filter(model.task_id == task_id)
        if cursor:
            try:
                scope, table, created_at, row_id = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if scope != task_id or table != kind or not isinstance(row_id, str) or not row_id:
                    raise ValueError("invalid scope")
                before = datetime.fromisoformat(created_at), row_id
            except (ValueError, TypeError):
                raise ValueError("invalid_work_cursor") from None
            query = query.filter(tuple_(model.created_at, model.id) < before)
        rows = query.order_by(model.created_at.desc(), model.id.desc()).limit(HISTORY_SCAN_LIMIT + 1).all()
        return owner, fields, rows

    def _project_metadata(
        self, task_id: str, kind: str, owner: str, fields: tuple[str, ...],
        rows: list[TaskArtifact | TaskEvidence], limit: int = METADATA_LIMIT,
    ) -> dict[str, Any]:
        scanned_rows = rows[:HISTORY_SCAN_LIMIT]
        visible = self.reader.visible_children_many(owner, scanned_rows)
        items = []
        last = None
        scanned = 0
        for row in scanned_rows:
            last = row
            scanned += 1
            if row.id in visible:
                items.append({key: getattr(row, key) for key in fields})
                if len(items) == limit:
                    break
        more = scanned < len(rows)
        token = None
        if more and last is not None:
            token = base64.urlsafe_b64encode(json.dumps([task_id, kind, last.created_at.isoformat(), last.id]).encode()).decode()
        return {"items": items, "next_cursor": token, "has_more": more}

    def inbox(
        self, bucket: str = "attention", limit: int = 30, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._stable_read(lambda attempt, _: attempt._inbox(bucket, limit, cursor))

    def _inbox(self, bucket: str, limit: int, cursor: str | None) -> dict[str, Any]:
        state = self.db.get(ConversationState, "default")
        thread = state.current_thread_id if state else None
        before = None
        if cursor:
            try:
                data = json.loads(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                if (
                    set(data)
                    != {"version", "thread_id", "bucket", "updated_at", "task_id"}
                    or type(data["version"]) is not int
                    or data["version"] != 1
                    or data["thread_id"] != thread
                    or data["bucket"] != bucket
                    or not isinstance(data["task_id"], str)
                    or not data["task_id"]
                ):
                    raise ValueError()
                before = datetime.fromisoformat(data["updated_at"]), data["task_id"]
            except (ValueError, TypeError, KeyError):
                raise ValueError("invalid_work_cursor") from None
        if not thread:
            return {"items": [], "next_cursor": None, "has_more": False}
        items = []
        scanned = 0
        last = None
        more = False
        while scanned < 2048:
            batch = self.reader.task_candidates(thread, before)
            if not batch:
                break
            visible = self.reader.task_visible_many(batch)
            projections = self._inbox_summaries([t for t in batch if t.id in visible])
            for task in batch:
                scanned += 1
                p = projections.get(task.id)
                if p and p["bucket"] == bucket:
                    if len(items) == limit:
                        more = True
                        break
                    items.append(p)
                    last = task
            if more:
                break
            before = batch[-1].updated_at, batch[-1].id
            if len(batch) < 128:
                break
            if scanned >= 2048:
                more = bool(self.reader.task_candidates(thread, before, 1))
                if len(items) < limit:
                    last = batch[-1]
        token = None
        if more and last:
            token = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "version": 1,
                        "thread_id": thread,
                        "bucket": bucket,
                        "updated_at": last.updated_at.isoformat(),
                        "task_id": last.id,
                    }
                ).encode()
            ).decode()
        return {"items": items, "next_cursor": token, "has_more": more}

    def work_view(self, task: str | AgentTask) -> dict[str, Any]:
        task_id = task if isinstance(task, str) else task.id
        return self._stable_read(lambda attempt, revision: attempt._work_view(task_id, revision))

    def _work_view(self, task_id: str, visibility_revision: int) -> dict[str, Any]:
        task = self._load_task(task_id, detail=True)
        # Capture the raw high water before loading history so the client can
        # recover every event that arrives while the rest of the snapshot is read.
        high = self._high_water([task_id])
        history_rows = self._timeline_history_rows(task_id, None)
        artifact_owner, artifact_fields, artifact_rows = self._metadata_rows(task_id, "artifacts")
        evidence_owner, evidence_fields, evidence_rows = self._metadata_rows(task_id, "evidence")
        inputs = self._load_projections([task], detail=True, high=high, extra={
            "agent_task_event": list(history_rows[:HISTORY_SCAN_LIMIT]),
            artifact_owner: list(artifact_rows[:HISTORY_SCAN_LIMIT]),
            evidence_owner: list(evidence_rows[:HISTORY_SCAN_LIMIT]),
        })
        if not inputs:
            raise TaskContentUnavailable("task_unavailable")
        projection = self._project_detail(inputs[0])
        goal = None
        if (task.task_state or {}).get("goal"):
            try:
                plan = GoalPlan.model_validate(task.task_state["goal"])
                goal = plan.model_dump(mode="json", exclude={"reminder"})
                for step in goal["steps"]:
                    step.pop("last_result", None)
                    if step.get("trigger"):
                        step["trigger"].pop("match", None)
            except ValueError:
                pass
        history = self._project_history(history_rows)
        artifact_page = self._project_metadata(task_id, "artifacts", artifact_owner, artifact_fields, artifact_rows)
        evidence_page = self._project_metadata(task_id, "evidence", evidence_owner, evidence_fields, evidence_rows)
        artifacts = artifact_page["items"]
        evidence = evidence_page["items"]
        truncation = {
            "timeline": history["has_more"],
            "task_artifact": artifact_page["has_more"],
            "task_evidence": evidence_page["has_more"],
        }
        if goal is not None:
            visible_evidence_ids = {item["id"] for item in evidence}
            valid_question_steps = {
                item["step_id"]
                for item in projection["attention_items"]
                if item["question_id"]
            }
            for step in goal["steps"]:
                if (task.id, step["id"]) in self._question_steps and step[
                    "id"
                ] not in valid_question_steps:
                    step["note"] = ""
                step["evidence_refs"] = [
                    ref for ref in step["evidence_refs"] if ref in visible_evidence_ids
                ]
            goal["progress"] = projection["progress"]
        task_info = {
            k: projection[k]
            for k in (
                "task_id",
                "title",
                "task_type",
                "task_status",
                "created_at",
                "updated_at",
            )
        }
        report_summary = (task.report or {}).get("summary")
        task_info.update(
            {
                "goal": task.goal[:2000],
                "report": {
                    "summary": report_summary[:2000] if isinstance(report_summary, str) else "执行结果已记录。",
                    "risks": (task.report or {}).get("risks", []),
                    "unresolved": (task.report or {}).get("unresolved", []),
                }
                if task.report
                else None,
                "last_error": "任务执行遇到错误，请查看执行状态与可见证据。"
                if task.last_error
                else None,
            }
        )

        return {
            "task": task_info,
            "bucket": projection["bucket"],
            "data_warning": projection["data_warning"],
            "attention_items": projection["attention_items"],
            "actions": projection["actions"],
            "goal": goal,
            "current_activity": projection["current_activity"],
            "target_device": projection["target_device"],
            "timeline": history["items"],
            "visibility_revision": visibility_revision,
            "history_cursor": history["next_sequence"] if history["has_more"] else None,
            "metadata_cursors": {
                "artifacts": artifact_page["next_cursor"], "evidence": evidence_page["next_cursor"],
            },
            "artifacts": artifacts,
            "evidence": evidence,
            "truncated": truncation,
            "last_sequence": projection["last_sequence"],
        }
