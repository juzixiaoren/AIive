"""Desktop Node 在线租约、发现和线程绑定服务。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.db.models import DesktopNode, Thread, ThreadDesktopBinding
from aiive.desktop.schemas import DesktopHello, capability_dicts


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DesktopNodeService:
    """所有 Desktop Node 持久化状态的唯一业务入口。"""

    def register_online(self, db: Session, hello: DesktopHello) -> DesktopNode:
        now = _now()
        node = db.get(DesktopNode, hello.node_id)
        if node is None:
            node = DesktopNode(
                id=hello.node_id,
                name=hello.name,
                platform=hello.platform,
                arch=hello.arch,
            )
            db.add(node)
        node.name = hello.name
        node.platform = hello.platform
        node.arch = hello.arch
        node.app_version = hello.app_version
        node.capabilities = capability_dicts(hello.capabilities)
        node.status = "online"
        node.last_seen_at = now
        node.lease_expires_at = now + timedelta(
            seconds=settings.aiive_desktop_node_lease_seconds
        )
        db.flush()
        return node

    def heartbeat(self, db: Session, node_id: str) -> bool:
        now = _now()
        affected = db.query(DesktopNode).filter(DesktopNode.id == node_id).update({
            DesktopNode.status: "online",
            DesktopNode.last_seen_at: now,
            DesktopNode.lease_expires_at: now + timedelta(
                seconds=settings.aiive_desktop_node_lease_seconds
            ),
            DesktopNode.updated_at: now,
        }, synchronize_session=False)
        return affected == 1

    def mark_offline(self, db: Session, node_id: str) -> None:
        now = _now()
        db.query(DesktopNode).filter(DesktopNode.id == node_id).update({
            DesktopNode.status: "offline",
            DesktopNode.lease_expires_at: now,
            DesktopNode.updated_at: now,
        }, synchronize_session=False)

    def expire_stale(self, db: Session) -> int:
        now = _now()
        return db.query(DesktopNode).filter(
            DesktopNode.status == "online",
            DesktopNode.lease_expires_at.is_not(None),
            DesktopNode.lease_expires_at <= now,
        ).update({
            DesktopNode.status: "offline",
            DesktopNode.updated_at: now,
        }, synchronize_session=False)

    def list_nodes(self, db: Session) -> list[DesktopNode]:
        self.expire_stale(db)
        return db.query(DesktopNode).order_by(
            DesktopNode.status.desc(), DesktopNode.name.asc(), DesktopNode.id.asc()
        ).all()

    def bind_thread(self, db: Session, thread_id: str, node_id: str) -> ThreadDesktopBinding:
        if db.get(Thread, thread_id) is None:
            raise ValueError("thread_not_found")
        if db.get(DesktopNode, node_id) is None:
            raise ValueError("desktop_node_not_found")
        binding = db.get(ThreadDesktopBinding, thread_id)
        if binding is None:
            binding = ThreadDesktopBinding(thread_id=thread_id, node_id=node_id)
            db.add(binding)
        else:
            binding.node_id = node_id
        db.flush()
        return binding

    def resolve_online_node(self, db: Session, thread_id: str) -> DesktopNode | None:
        """解析本轮执行节点：显式绑定优先，单在线节点时自动选择。"""
        self.expire_stale(db)
        binding = db.get(ThreadDesktopBinding, thread_id)
        if binding is not None:
            return db.query(DesktopNode).filter(
                DesktopNode.id == binding.node_id,
                DesktopNode.status == "online",
                DesktopNode.lease_expires_at > _now(),
            ).one_or_none()

        online = db.query(DesktopNode).filter(
            DesktopNode.status == "online",
            DesktopNode.lease_expires_at > _now(),
        ).order_by(DesktopNode.id.asc()).limit(2).all()
        return online[0] if len(online) == 1 else None

    def resolve_task_node(
        self, db: Session, thread_id: str, target_node_id: str | None,
    ) -> DesktopNode | None:
        """Task 显式节点优先；否则复用线程绑定/单节点解析规则。"""
        if not target_node_id:
            return self.resolve_online_node(db, thread_id)
        self.expire_stale(db)
        return db.query(DesktopNode).filter(
            DesktopNode.id == target_node_id,
            DesktopNode.status == "online",
            DesktopNode.lease_expires_at > _now(),
        ).one_or_none()


desktop_node_service = DesktopNodeService()
