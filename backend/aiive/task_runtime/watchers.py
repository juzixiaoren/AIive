"""确定性 Watcher：观察外部状态、追加事件、唤醒 Task。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.control.scope import TaskScope
from aiive.db.models import AgentTask, AgentTaskWatch, DesktopNode, ThreadDesktopBinding
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskStatus
from aiive.task_runtime.watch_contract import WATCH_PROPOSAL, FileWatch, NodeWatch, TimeWatch, parse_instant


SUPPORTED_WATCHES = frozenset({"time", "file", "node", "promotion"})


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_instant(value, legacy=True)
    except ValueError:
        return None


class WatcherService:
    def __init__(self, db: Session):
        self.db: Session = db
        self.repo: TaskRepository = TaskRepository(db)

    def create_from_proposal(self, task: AgentTask, proposal: dict[str, Any]) -> AgentTaskWatch:
        parsed = WATCH_PROPOSAL.validate_python(proposal, strict=True)
        config = parsed.config.model_dump(exclude_none=True)
        next_check = _parse_time(parsed.next_check_at) or utcnow()
        if isinstance(parsed, TimeWatch):
            next_check = parse_instant(parsed.config.at)
        if isinstance(parsed, FileWatch):
            config["path"] = str(self._local_path(task, parsed.config.path))
        if isinstance(parsed, NodeWatch):
            node_id = parsed.config.node_id or task.target_node_id
            if not node_id:
                raise ValueError("node_watch_requires_node_id")
            if self.db.get(DesktopNode, node_id) is None:
                raise ValueError("node_watch_unknown_node")
            config["node_id"] = node_id
        watch = AgentTaskWatch(
            task_id=task.id,
            watch_type=parsed.watch_type,
            config=config,
            cursor=self._snapshot(parsed.watch_type, config),
            status="active",
            next_check_at=next_check,
        )
        self.db.add(watch)
        self.db.flush()
        return watch

    def wait_for_promotion(self, task: AgentTask, request_id: str, deadline: datetime) -> None:
        self.db.add(AgentTaskWatch(
            task_id=task.id, watch_type="promotion", status="active",
            config={"request_id": request_id, "deadline": deadline.isoformat()},
            cursor={}, next_check_at=utcnow() + timedelta(seconds=15),
        ))

    @staticmethod
    def _local_path(task: AgentTask, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("watch_path_requires_absolute_backend_path")
        scope = TaskScope.from_brief(task.task_brief or {})
        path = path.resolve()
        for root in map(Path, scope.allowed_roots):
            root = root.expanduser()
            if root.is_absolute() and path.is_relative_to(root.resolve()):
                return path
        raise ValueError("watch_path_outside_task_scope")

    def poll_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        now = now or utcnow()
        watches = self.db.query(AgentTaskWatch).filter(
            AgentTaskWatch.status == "active",
            AgentTaskWatch.next_check_at <= now,
        ).order_by(AgentTaskWatch.next_check_at.asc()).limit(limit).with_for_update(skip_locked=True).all()
        triggered = 0
        for watch in watches:
            watch.last_checked_at = now
            config = watch.config or {}
            try:
                interval = int(config.get("interval_seconds", 30))
                if not 5 <= interval <= 3600:
                    raise ValueError("watch_invalid_interval")
                if watch.watch_type == "time" and _parse_time(config.get("at")) is None:
                    raise ValueError("watch_invalid_at")
                if watch.watch_type == "file":
                    if config.get("condition", "changed") not in {"changed", "exists", "missing"}:
                        raise ValueError("watch_invalid_file_condition")
                    owner = self.repo.get_task(watch.task_id)
                    if owner is None:
                        raise ValueError("watch_task_not_found")
                    config = {**config, "path": str(self._local_path(owner, str(config.get("path") or "")))}
                if watch.watch_type == "node" and config.get("status", "online") not in {"online", "offline"}:
                    raise ValueError("watch_invalid_node_status")
                current = self._snapshot(watch.watch_type, config)
                if watch.watch_type not in SUPPORTED_WATCHES or current.get("status") == "missing":
                    raise ValueError("watch_target_unavailable")
            except (ValueError, TypeError, OSError, RuntimeError) as error:
                self._fail(watch, str(error))
                continue
            if self._triggered(watch, current, now):
                watch.status = "completed"
                watch.cursor = current
                task = self.repo.get_task(watch.task_id, for_update=True)
                if task is not None and task.status not in {
                    TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL.value,
                    TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
                }:
                    self.repo.append_event(
                        task.id, "watch_triggered",
                        {"watch_id": watch.id, "watch_type": watch.watch_type, "snapshot": current},
                    )
                    config = watch.config or {}
                    if config.get("goal_step_id"):
                        # 长期目标：到期事件交给目标服务结算（错过判定 / 下一次触发）。
                        from aiive.task_runtime.goal_service import GoalService

                        GoalService(self.db).on_watch_due(task, watch)
                    if not config.get("notify_only") and task.status in {
                        TaskStatus.BLOCKED_USER.value, TaskStatus.BLOCKED_NODE.value,
                    }:
                        self.repo.transition_task(task, TaskStatus.QUEUED.value)
                        self.repo.enqueue_run(task, trigger="watch_triggered")
                    self.repo.checkpoint(task.id, reason="watch_triggered")
                triggered += 1
            else:
                watch.cursor = current
                watch.next_check_at = now + timedelta(seconds=interval)
        self.db.flush()
        return triggered

    def _fail(self, watch: AgentTaskWatch, reason: str) -> None:
        watch.status = "completed"
        watch.cursor = {"error": reason[:240]}
        task = self.repo.get_task(watch.task_id, for_update=True)
        if task is None or task.status not in {TaskStatus.BLOCKED_USER.value, TaskStatus.BLOCKED_NODE.value}:
            return
        self.repo.append_event(task.id, "watch_failed", {
            "watch_id": watch.id, "watch_type": watch.watch_type, "reason": reason[:240],
        }, visibility="conversation")
        self.repo.transition_task(task, TaskStatus.QUEUED.value)
        self.repo.enqueue_run(task, trigger="watch_failed")
        self.repo.checkpoint(task.id, reason="watch_failed")

    def wake_node_tasks(self, node_id: str) -> int:
        """NodeOnline 事件直接恢复显式/绑定任务；无需等待 LLM 或轮询。"""
        bound_threads = [row[0] for row in self.db.query(ThreadDesktopBinding.thread_id).filter(
            ThreadDesktopBinding.node_id == node_id,
        ).all()]
        now = utcnow()
        online_count = self.db.query(DesktopNode).filter(
            DesktopNode.status == "online",
            DesktopNode.lease_expires_at.is_not(None),
            DesktopNode.lease_expires_at > now,
        ).count()
        routing = [AgentTask.target_node_id == node_id]
        if bound_threads:
            routing.append(AgentTask.thread_id.in_(bound_threads))
        if online_count == 1:
            routing.append(AgentTask.target_node_id.is_(None))
        tasks = self.db.query(AgentTask).filter(
            AgentTask.status == TaskStatus.BLOCKED_NODE.value,
            or_(*routing),
        ).with_for_update(skip_locked=True).all()
        for task in tasks:
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
            self.repo.append_event(
                task.id, "node_online", {"node_id": node_id}, visibility="conversation",
            )
            self.repo.enqueue_run(task, trigger="node_online")
            self.repo.checkpoint(task.id, reason="node_online")
        return len(tasks)

    def _snapshot(self, watch_type: str, config: dict[str, Any]) -> dict[str, Any]:
        if watch_type == "time":
            return {"now": utcnow().isoformat(), "at": config.get("at")}
        if watch_type == "file":
            path = Path(str(config.get("path") or "")).expanduser()
            try:
                stat = path.stat()
                return {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
            except (FileNotFoundError, NotADirectoryError):
                return {"exists": False}
        if watch_type == "node":
            node = self.db.get(DesktopNode, config.get("node_id"))
            lease = node.lease_expires_at if node else None
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=timezone.utc)
            status = "online" if node and node.status == "online" and lease is not None and lease > utcnow() else "offline"
            return {
                "status": status if node else "missing",
                "lease_expires_at": node.lease_expires_at.isoformat() if node and node.lease_expires_at else None,
            }
        if watch_type == "promotion":
            from aiive.supervisor.promotion_status import promotion_status

            return {"status": promotion_status(str(config.get("request_id") or ""))}
        return {}

    @staticmethod
    def _triggered(watch: AgentTaskWatch, current: dict[str, Any], now: datetime) -> bool:
        config = watch.config or {}
        if watch.watch_type == "time":
            target = _parse_time(config.get("at"))
            return target is not None and now >= target
        if watch.watch_type == "file":
            condition = str(config.get("condition") or "changed")
            previous = watch.cursor or {}
            if condition == "exists":
                return bool(current.get("exists"))
            if condition == "missing":
                return not bool(current.get("exists"))
            return current != previous
        if watch.watch_type == "node":
            expected = str(config.get("status") or "online")
            return current.get("status") == expected
        if watch.watch_type == "promotion":
            deadline = _parse_time(config.get("deadline"))
            return current.get("status") not in {"requested", "observing"} or deadline is None or now >= deadline
        return False
