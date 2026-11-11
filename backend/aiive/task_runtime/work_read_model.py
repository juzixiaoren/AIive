"""Read-only work projections. No execution services or persistence operations."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import func, tuple_
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
    title: str
    description: str
    resource_id: str
    created_at: datetime
    expires_at: datetime | None
    allowed_actions: list[str]
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
    "goal_confirmed": "目标已确认",
    "goal_modified": "目标计划已修改",
    "goal_rejected": "目标已拒绝",
    "goal_ended": "目标已结束",
    "goal_completed": "目标已完成",
    "goal_blocked": "目标受阻",
    "goal_step_answered": "目标问题已回答",
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
    "run_started": "开始执行",
    "task_recovered": "任务已恢复",
    "task_reconciliation_required": "等待执行核对",
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


class WorkReadModel:
    def __init__(self, db: Session) -> None:
        self.db: Session = db
        self.reader: TaskReadRepository = TaskReadRepository(db)
        self._nodes: dict[str, DesktopNode] = {}
        self._question_steps: set[tuple[str, str]] = set()

    def project_many(self, tasks: list[AgentTask]) -> list[dict[str, Any]]:
        ids = [task.id for task in tasks]
        if not ids:
            return []
        # Rank in SQL before any Forget checks: historical size must not increase
        # the number of ORM visibility queries. Each task/status has a fixed budget.
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
        action_candidates = (
            self.db.query(AgentAction)
            .join(action_rank, action_rank.c.id == AgentAction.id)
            .filter(action_rank.c.rank <= 33)
            .order_by(AgentAction.sequence.desc())
            .all()
        )
        action_counts: Counter[tuple[str, str]] = Counter()
        retained_actions = []
        budget_exceeded: set[str] = set()
        for action in action_candidates:
            key = (action.task_id, action.status)
            action_counts[key] += 1
            if action_counts[key] <= 32:
                retained_actions.append(action)
            else:
                budget_exceeded.add(action.task_id)
        actions = self.reader.filter_children("agent_action", retained_actions)

        event_rank = (
            self.db.query(
                AgentTaskEvent.id.label("id"),
                func.row_number()
                .over(
                    partition_by=AgentTaskEvent.task_id,
                    order_by=AgentTaskEvent.sequence.desc(),
                )
                .label("rank"),
            )
            .filter(
                AgentTaskEvent.task_id.in_(ids),
                AgentTaskEvent.event_type.in_(["task_blocked_user", "task_watching"]),
            )
            .subquery()
        )
        events = self.reader.filter_children(
            "agent_task_event",
            self.db.query(AgentTaskEvent)
            .join(event_rank, event_rank.c.id == AgentTaskEvent.id)
            .filter(event_rank.c.rank <= 32)
            .order_by(AgentTaskEvent.sequence.desc())
            .all(),
        )
        approvals = (
            self.db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.action_id.in_(
                    [a.id for a in actions if a.status == "awaiting_approval"]
                ),
                ApprovalRequest.status == "pending",
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
        questions = list_open_questions_for_tasks(self.db, ids)
        high = dict(
            self.db.query(AgentTaskEvent.task_id, func.max(AgentTaskEvent.sequence))
            .filter(AgentTaskEvent.task_id.in_(ids))
            .group_by(AgentTaskEvent.task_id)
            .all()
        )
        projections = [
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
        for projection in projections:
            projection["actions"] = [
                {"id": a.id, "sequence": a.sequence, "capability_id": a.capability_id,
                 "status": a.status, "risk_level": a.risk_level,
                 "requires_approval": a.requires_approval, "created_at": a.created_at,
                 "started_at": a.started_at, "completed_at": a.completed_at}
                for a in actions if a.task_id == projection["task_id"]
            ]
            if projection["task_id"] in budget_exceeded:
                projection["data_warning"] = (
                    projection["data_warning"] or "action_history_scan_limit"
                )
        return projections

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
                        **kwargs,
                    )
                )
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

    def project_event(self, event: AgentTaskEvent) -> dict[str, Any] | None:
        """Shared, fail-closed event projection; never copy arbitrary payload strings."""
        if (
            event.visibility == "internal"
            or event.event_type not in TIMELINE_TITLES
            or not self.reader.visible_child("agent_task_event", event)
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
        items = [
            item for event in rows if (item := self.project_event(event)) is not None
        ]
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
            p.pop("actions")
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
                    if step.get("trigger"):
                        step["trigger"].pop("match", None)
            except ValueError:
                pass
        visible_events = (
            self.db.query(AgentTaskEvent)
            .filter_by(task_id=task.id)
            .order_by(AgentTaskEvent.sequence.desc())
            .limit(HISTORY_SCAN_LIMIT + 1)
            .all()
        )
        projected_events = [
            item
            for e in visible_events[:HISTORY_SCAN_LIMIT]
            if (item := self.project_event(e)) is not None
        ]
        timeline = projected_events[:50]
        truncation: dict[str, bool] = {
            "timeline": len(visible_events) > HISTORY_SCAN_LIMIT
            or len(projected_events) > 50
        }

        def metadata(model: type[Metadata], owner: str) -> list[dict[str, Any]]:
            candidates = (
                self.db.query(model)
                .filter_by(task_id=task.id)
                .order_by(model.created_at.desc(), model.id.desc())
                .limit(HISTORY_SCAN_LIMIT + 1)
                .all()
            )
            visible = self.reader.filter_children(
                owner, candidates[:HISTORY_SCAN_LIMIT]
            )
            truncation[owner] = (
                len(candidates) > HISTORY_SCAN_LIMIT or len(visible) > METADATA_LIMIT
            )
            fields = (
                "id",
                "summary",
                "mime_type",
                "content_size",
                "content_hash",
                "created_at",
            )
            return [
                {
                    key: getattr(row, key)
                    for key in fields
                    + (
                        ("name", "artifact_type")
                        if isinstance(row, TaskArtifact)
                        else ("kind",)
                    )
                }
                for row in visible[:METADATA_LIMIT]
            ]

        artifacts = metadata(TaskArtifact, "task_artifact")
        evidence = metadata(TaskEvidence, "task_evidence")
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
                    "summary": report_summary[:2000]
                    if isinstance(report_summary, str)
                    else "执行结果已记录。"
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
            "timeline": list(reversed(timeline)),
            "artifacts": artifacts,
            "evidence": evidence,
            "truncated": truncation,
            "last_sequence": projection["last_sequence"],
        }
