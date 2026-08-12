"""确定性 Watcher：观察外部状态、追加事件、唤醒 Task。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.control.scope import TaskScope, path_within
from aiive.db.models import AgentTask, AgentTaskWatch, DesktopNode, ThreadDesktopBinding
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskStatus


SUPPORTED_WATCHES = frozenset({"time", "file", "node"})


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class WatcherService:
    def __init__(self, db: Session):
        self.db: Session = db
        self.repo: TaskRepository = TaskRepository(db)

    def create_from_proposal(self, task: AgentTask, proposal: dict[str, Any]) -> AgentTaskWatch:
        watch_type = str(proposal.get("watch_type") or "")
        if watch_type not in SUPPORTED_WATCHES:
            raise ValueError("unsupported_watch_type")
        config = dict(proposal.get("config") or {})
        if watch_type == "file":
            path = str(config.get("path") or "")
            scope = TaskScope.from_brief(task.task_brief or {})
            if not path or not any(path_within(path, root) for root in scope.allowed_roots):
                raise ValueError("watch_path_outside_task_scope")
        if watch_type == "node" and not config.get("node_id"):
            config["node_id"] = task.target_node_id
        if watch_type == "node" and not config.get("node_id"):
            raise ValueError("node_watch_requires_node_id")
        next_check = _parse_time(proposal.get("next_check_at")) or utcnow()
        watch = AgentTaskWatch(
            task_id=task.id,
            watch_type=watch_type,
            config=config,
            cursor=self._snapshot(watch_type, config),
            status="active",
            next_check_at=next_check,
        )
        self.db.add(watch)
        self.db.flush()
        return watch

    def poll_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        now = now or utcnow()
        watches = self.db.query(AgentTaskWatch).filter(
            AgentTaskWatch.status == "active",
            AgentTaskWatch.next_check_at <= now,
        ).order_by(AgentTaskWatch.next_check_at.asc()).limit(limit).with_for_update(skip_locked=True).all()
        triggered = 0
        for watch in watches:
            watch.last_checked_at = now
            current = self._snapshot(watch.watch_type, watch.config or {})
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
                    if task.status in {TaskStatus.BLOCKED_USER.value, TaskStatus.BLOCKED_NODE.value}:
                        self.repo.transition_task(task, TaskStatus.QUEUED.value)
                        self.repo.enqueue_run(task, trigger="watch_triggered")
                    self.repo.checkpoint(task.id, reason="watch_triggered")
                triggered += 1
            else:
                watch.cursor = current
                watch.next_check_at = now + timedelta(seconds=max(5, int((watch.config or {}).get("interval_seconds", 30))))
        self.db.flush()
        return triggered

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
            except OSError:
                return {"exists": False}
        if watch_type == "node":
            node = self.db.get(DesktopNode, config.get("node_id"))
            lease = node.lease_expires_at if node else None
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=timezone.utc)
            status = node.status if node and lease is not None and lease > utcnow() else "offline"
            return {
                "status": status if node else "missing",
                "lease_expires_at": node.lease_expires_at.isoformat() if node and node.lease_expires_at else None,
            }
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
        return False
