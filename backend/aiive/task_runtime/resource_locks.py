"""Task Action 的数据库资源锁与前台 GUI lease。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from aiive.db.models import TaskResourceLock


@dataclass(frozen=True)
class LockGrant:
    token: str
    resource_keys: tuple[str, ...]


class ResourceBusyError(RuntimeError):
    def __init__(self, resource_key: str):
        super().__init__(f"resource_busy:{resource_key}")
        self.resource_key = resource_key


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ResourceLockService:
    def acquire(
        self,
        db: Session,
        *,
        task_id: str,
        action_id: str,
        resource_keys: list[str],
        lease_seconds: int = 180,
    ) -> LockGrant:
        now = datetime.now(timezone.utc)
        token = str(uuid.uuid4())
        keys = tuple(sorted(set(resource_keys)))
        # 固定顺序获取，避免两个 Action 以相反顺序锁多个路径而死锁。
        for key in keys:
            current = db.query(TaskResourceLock).filter(
                TaskResourceLock.resource_key == key,
            ).with_for_update().one_or_none()
            if current is not None and _aware(current.lease_expires_at) > now:
                if current.task_id != task_id or current.action_id != action_id:
                    raise ResourceBusyError(key)
            if current is None:
                current = TaskResourceLock(
                    resource_key=key,
                    task_id=task_id,
                    action_id=action_id,
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
                db.add(current)
            else:
                current.task_id = task_id
                current.action_id = action_id
                current.lease_token = token
                current.lease_expires_at = now + timedelta(seconds=lease_seconds)
            db.flush()
        return LockGrant(token=token, resource_keys=keys)

    def release(self, db: Session, grant: LockGrant) -> int:
        if not grant.resource_keys:
            return 0
        return db.query(TaskResourceLock).filter(
            TaskResourceLock.resource_key.in_(grant.resource_keys),
            TaskResourceLock.lease_token == grant.token,
        ).delete(synchronize_session=False)

    def release_action(self, db: Session, *, task_id: str, action_id: str) -> int:
        """对账确认 Action 终态后提前释放其遗留租约。"""
        return db.query(TaskResourceLock).filter(
            TaskResourceLock.task_id == task_id,
            TaskResourceLock.action_id == action_id,
        ).delete(synchronize_session=False)

    def expire(self, db: Session) -> int:
        return db.query(TaskResourceLock).filter(
            TaskResourceLock.lease_expires_at <= datetime.now(timezone.utc),
        ).delete(synchronize_session=False)
