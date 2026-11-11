"""Read-only work projections. No execution services or persistence operations."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import func
from sqlalchemy.orm import Session

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
)
from aiive.task_runtime.read_repository import TaskReadRepository
from aiive.task_runtime.repository import TERMINAL_TASK_STATES, canonical_json

Metadata = TypeVar("Metadata", TaskArtifact, TaskEvidence)


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
    question_id: str | None = None
    step_id: str | None = None
    approval_id: str | None = None
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
    return result


TIMELINE_TITLES = {
    "task_created": "任务已创建",
    "task_completed": "任务已结束",
    "goal_proposed": "目标待确认",
    "goal_waiting": "目标等待中",
    "goal_paused": "目标已暂停",
    "goal_resumed": "目标已恢复",
    "goal_step_completed": "目标步骤已完成",
    "task_blocked_node": "等待设备",
    "task_reconciling": "执行核对中",
    "task_cancelled": "任务已取消",
    "task_blocked_user": "等待用户回答",
    "task_watching": "等待事件",
    "run_started": "开始执行",
    "task_recovered": "任务已恢复",
    "task_reconciliation_required": "等待执行核对",
    "goal_step_continued": "步骤继续执行",
}


class WorkReadModel:
    def __init__(self, db: Session) -> None:
        self.db: Session = db
        self.reader: TaskReadRepository = TaskReadRepository(db)
        self._nodes: dict[str, DesktopNode] = {}

    def project_many(self, tasks: list[AgentTask]) -> list[dict[str, Any]]:
        ids = [task.id for task in tasks]
        if not ids:
            return []
        actions = self.reader.filter_children(
            "agent_action",
            self.db.query(AgentAction)
            .filter(AgentAction.task_id.in_(ids))
            .order_by(AgentAction.sequence.desc())
            .all(),
        )
        events = self.reader.filter_children(
            "agent_task_event",
            self.db.query(AgentTaskEvent)
            .filter(
                AgentTaskEvent.task_id.in_(ids),
                AgentTaskEvent.event_type.in_(["task_blocked_user", "task_watching"]),
            )
            .order_by(AgentTaskEvent.sequence.desc())
            .all(),
        )
        approvals = (
            self.db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.task_id.in_(ids), ApprovalRequest.status == "pending"
            )
            .all()
        )
        node_ids = {a.target_node_id for a in actions if a.target_node_id} | {
            t.target_node_id for t in tasks if t.target_node_id
        }
        self._nodes = (
            {
                node.id: node
                for node in self.db.query(DesktopNode)
                .filter(DesktopNode.id.in_(node_ids))
                .all()
            }
            if node_ids
            else {}
        )
        questions = list_open_questions_for_tasks(self.db, ids)
        high = dict(
            self.db.query(AgentTaskEvent.task_id, func.max(AgentTaskEvent.sequence))
            .filter(AgentTaskEvent.task_id.in_(ids))
            .group_by(AgentTaskEvent.task_id)
            .all()
        )
        return [
            self.project(
                task,
                [a for a in actions if a.task_id == task.id],
                [e for e in events if e.task_id == task.id],
                approvals,
                questions.get(task.id, []),
                high.get(task.id, 0),
            )
            for task in tasks
        ]

    def project(
        self,
        task: AgentTask,
        actions: list[AgentAction],
        events: list[AgentTaskEvent],
        approvals: list[ApprovalRequest],
        questions: list[PendingQuestion],
        high: int,
    ) -> dict[str, Any]:
        plan = None
        warning = None
        raw = (task.task_state or {}).get("goal")
        if raw is not None:
            try:
                plan = GoalPlan.model_validate(raw)
            except ValueError:
                warning = "invalid_goal_plan"
        items = []

        def add(kind: str, identity: str, text: str, **kwargs: Any) -> None:
            items.append(
                asdict(WorkAttentionItem(f"{kind}:{identity}", kind, text, **kwargs))
            )

        terminal = task.status in TERMINAL_TASK_STATES or (
            plan and plan.status in TERMINAL_GOAL_STATES
        )
        if not terminal and not (plan and plan.status == "paused"):
            if plan and plan.status == "proposed":
                add("goal_confirmation", task.id, plan.summary)
            elif plan and plan.status == "blocked":
                add("goal_blocked", task.id, plan.blocked_reason)
            for approval in approvals:
                action = next((a for a in actions if a.id == approval.action_id), None)
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
                        preview=approval_preview(approval),
                    )
            for q in questions:
                add(
                    "goal_question",
                    q.question_id,
                    q.question,
                    question_id=q.question_id,
                    step_id=q.step_id,
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
                    if kind == "goal_question" and any(
                        q.step_id == step.id for q in questions
                    ):
                        continue
                    add(
                        kind,
                        f"{task.id}:{step.id}",
                        step.note or step.title,
                        step_id=step.id,
                    )
            if (
                not plan
                and task.status == "blocked_user"
                and events
                and events[0].event_type == "task_blocked_user"
            ):
                question = (events[0].payload or {}).get("question")
                if isinstance(question, str) and question.strip():
                    add("task_question", events[0].id, question)
        if terminal:
            bucket, reason = "ended", "terminal"
        elif plan and plan.status == "paused":
            bucket, reason = "waiting", "goal_paused"
        elif items:
            bucket, reason = "attention", items[0]["kind"]
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
        node = self._nodes.get(node_id) if node_id else None
        from aiive.desktop.connection_manager import desktop_connection_manager

        device = {
            "node_id": node_id,
            "source": source,
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
            "attention_count": len(items),
            "primary_attention": items[0] if items else None,
            "attention_items": items,
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

    def timeline(
        self, task_id: str, after_sequence: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        rows = (
            self.db.query(AgentTaskEvent)
            .filter(
                AgentTaskEvent.task_id == task_id,
                AgentTaskEvent.sequence > after_sequence,
            )
            .order_by(AgentTaskEvent.sequence)
            .limit(limit)
            .all()
        )
        items = []
        for event in rows:
            if (
                event.visibility == "internal"
                or event.event_type not in TIMELINE_TITLES
                or not self.reader.visible_child("agent_task_event", event)
            ):
                continue
            items.append(
                {
                    "id": event.id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "title": TIMELINE_TITLES[event.event_type],
                    "summary": "",
                    "created_at": event.created_at,
                    "action_id": event.action_id,
                    "run_id": event.run_id,
                }
            )
        cursor = rows[-1].sequence if rows else after_sequence
        more = (
            self.db.query(AgentTaskEvent.id)
            .filter(AgentTaskEvent.task_id == task_id, AgentTaskEvent.sequence > cursor)
            .first()
            is not None
        )
        return {"items": items, "next_sequence": cursor, "has_more": more}

    def inbox(
        self, bucket: str = "attention", limit: int = 30, cursor: str | None = None
    ) -> dict[str, Any]:
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
            projections = {
                p["task_id"]: p
                for p in self.project_many(
                    [t for t in batch if self.reader.task_visible(t)]
                )
            }
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
        for p in items:
            p.pop("attention_items")
        return {"items": items, "next_cursor": token, "has_more": more}

    def work_view(self, task: AgentTask) -> dict[str, Any]:
        projection = self.project_many([task])[0]
        goal = None
        if (task.task_state or {}).get("goal"):
            try:
                plan = GoalPlan.model_validate(task.task_state["goal"])
                goal = plan.model_dump(mode="json", exclude={"reminder"})
                for step in goal["steps"]:
                    step.pop("last_result", None)
                    step["evidence_refs"] = [
                        ref
                        for ref in step["evidence_refs"]
                        if self.reader.get_evidence(task.id, ref)
                    ]
                    if step.get("trigger"):
                        step["trigger"].pop("match", None)
            except ValueError:
                pass
        visible_events = (
            self.db.query(AgentTaskEvent)
            .filter_by(task_id=task.id)
            .order_by(AgentTaskEvent.sequence.desc())
            .yield_per(128)
        )
        timeline = []
        for e in visible_events:
            if (
                e.visibility != "internal"
                and e.event_type in TIMELINE_TITLES
                and self.reader.visible_child("agent_task_event", e)
            ):
                timeline.append(
                    {
                        "id": e.id,
                        "sequence": e.sequence,
                        "event_type": e.event_type,
                        "title": TIMELINE_TITLES[e.event_type],
                        "summary": "",
                        "created_at": e.created_at,
                        "action_id": e.action_id,
                        "run_id": e.run_id,
                    }
                )
                if len(timeline) == 50:
                    break

        def metadata(model: type[Metadata], owner: str) -> list[dict[str, Any]]:
            rows = self.reader.filter_children(
                owner, self.db.query(model).filter_by(task_id=task.id).all()
            )
            return [
                {
                    key: getattr(row, key)
                    for key in ("id", "mime_type", "content_size", "created_at")
                }
                for row in rows
            ]

        return {
            "task": {
                k: projection[k]
                for k in (
                    "task_id",
                    "title",
                    "task_type",
                    "task_status",
                    "created_at",
                    "updated_at",
                )
            },
            "bucket": projection["bucket"],
            "attention_items": projection["attention_items"],
            "goal": goal,
            "current_activity": projection["current_activity"],
            "target_device": projection["target_device"],
            "timeline": list(reversed(timeline)),
            "artifacts": metadata(TaskArtifact, "task_artifact"),
            "evidence": metadata(TaskEvidence, "task_evidence"),
            "last_sequence": projection["last_sequence"],
        }
