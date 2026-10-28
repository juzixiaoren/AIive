"""主动消息：把触发决策产出的文本写成聊天时间线里的真实助手消息。

与通知的区别：
- 通知（Event(notification_created)）只进角标与通知页；
- 主动消息写入 TurnRecord(source=runtime_event) + Event(llm_response)，
  与用户消息共享同一条时间线、同一个 turn_sequence 排序；
- 前台 Turn 正在执行时拒绝写入（ProactiveBlocked），由 Outbox 稍后重试，
  绝不在同一线程里并发插入导致乱序。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.models import Event, Thread, TurnRecord
from aiive.runtime.epoch_manager import EpochManager, peek_next_turn_sequence
from aiive.runtime.event_logger import EventLogger

MESSAGE_SOURCE = "runtime_event"
MAX_MESSAGE_CHARS = 4000


class ProactiveBlocked(RuntimeError):
    """线程存在活跃前台 Turn：本轮不写入，交给 Outbox 重试。"""


class ProactiveMessageService:
    def __init__(self, db: Session):
        self.db: Session = db

    def post_in_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        turn_record_id: str,
        content: str,
        trace_id: str,
        dedupe_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在正在执行的 Turn 内写入一条真实助手消息（不新建 TurnRecord）。

        与 post 的区别：post 面向后台主动消息，遇到前台 Turn 会阻塞重试；
        post_in_turn 由当前 Turn 的 notify_user 工具调用，消息作为同一 Turn 的
        llm_response 事件插入，turn_event_index 递增，因此最终答复仍在它之后。
        同一 dedupe_key 生成稳定 Event.id，重复调用不会重复落库。
        """
        text = (content or "").strip()
        if not text:
            raise ValueError("empty_message")
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS]
        if not dedupe_key:
            raise ValueError("dedupe_key_required")

        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"proactive:{thread_id}:{dedupe_key}"))
        existing = self.db.get(Event, event_id)
        if existing is not None:
            return {
                "event_id": existing.id,
                "turn_id": turn_id,
                "turn_event_index": existing.turn_event_index,
                "created": False,
            }

        turn = self.db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.turn_id == turn_id,
        ).one_or_none()
        if turn is None or turn.id != turn_record_id:
            raise ValueError("turn_not_found")
        if turn.status != "running":
            raise ValueError("turn_not_running")

        thread = self.db.query(Thread).filter(Thread.id == thread_id).with_for_update().one_or_none()
        if thread is None:
            raise ValueError("thread_not_found")

        max_index = self.db.query(func.max(Event.turn_event_index)).filter(
            Event.thread_id == thread_id,
            Event.turn_id == turn_id,
        ).scalar()
        next_index = int(max_index or 0) + 1
        now = datetime.now(timezone.utc)
        event = Event(
            id=event_id,
            trace_id=trace_id,
            thread_id=thread_id,
            event_type="llm_response",
            turn_id=turn_id,
            turn_event_index=next_index,
            payload={
                "content": text,
                "message_source": MESSAGE_SOURCE,
                "proactive": True,
                "trigger": dict(metadata or {}),
            },
            created_at=now,
        )
        self.db.add(event)
        thread.last_activity_at = now
        self.db.flush()
        return {
            "event_id": event.id,
            "turn_id": turn_id,
            "turn_event_index": next_index,
            "created": True,
        }

    def post(
        self,
        *,
        thread_id: str,
        content: str,
        trace_id: str,
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """写入一条用户可见的助手消息；重复调用同一 turn_id 返回已有事件。

        dedupe_key 用于后台决策：turn_id 与 Event.id 都由该稳定标识派生，
        因此不依赖生成文案，重放同一决策也只会有一条时间线消息。
        """
        text = (content or "").strip()
        if not text:
            raise ValueError("empty_message")
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS]
        event_id = ""
        if dedupe_key:
            turn_id = turn_id or str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"proactive-turn:{thread_id}:{dedupe_key}",
            ))
            event_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"proactive:{thread_id}:{dedupe_key}",
            ))
            existing_event = self.db.get(Event, event_id)
            if existing_event is not None:
                existing_turn = self.db.query(TurnRecord).filter(
                    TurnRecord.thread_id == thread_id,
                    TurnRecord.turn_id == existing_event.turn_id,
                ).one_or_none()
                return {
                    "event_id": existing_event.id,
                    "turn_id": str(existing_event.turn_id or ""),
                    "turn_sequence": existing_turn.turn_sequence if existing_turn else 0,
                    "created": False,
                }
        turn_id = turn_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trace_id}:{text[:200]}"))
        now = datetime.now(timezone.utc)

        thread = self.db.query(Thread).filter(Thread.id == thread_id).with_for_update().one_or_none()
        if thread is None:
            raise ValueError("thread_not_found")

        existing = self.db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.turn_id == turn_id,
        ).one_or_none()
        if existing is not None:
            event = self.db.query(Event).filter(
                Event.thread_id == thread_id,
                Event.turn_id == turn_id,
                Event.event_type == "llm_response",
            ).order_by(Event.turn_event_index.asc()).first()
            if event is not None:
                return {
                    "event_id": event.id,
                    "turn_id": turn_id,
                    "turn_sequence": existing.turn_sequence,
                    "created": False,
                }
            if existing.status == "running":
                raise ProactiveBlocked("turn_in_progress")

        active = self.db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.status == "running",
            TurnRecord.lease_expires_at.is_not(None),
            TurnRecord.lease_expires_at > now,
        ).first()
        if active is not None:
            raise ProactiveBlocked("turn_in_progress")

        sequence = peek_next_turn_sequence(self.db, thread_id)
        epoch, segment = EpochManager().ensure_epoch_and_segment(self.db, thread_id, sequence)
        fingerprint = hashlib.sha256(
            f"{thread_id}:{turn_id}:{text}".encode("utf-8")
        ).hexdigest()
        record = TurnRecord(
            thread_id=thread_id,
            turn_id=turn_id,
            turn_sequence=sequence,
            status="completed",
            attempt_no=1,
            request_fingerprint=fingerprint,
            source=MESSAGE_SOURCE,
            epoch_id=epoch.id,
            segment_id=segment.id,
        )
        self.db.add(record)
        event_payload = {
            "content": text,
            "message_source": MESSAGE_SOURCE,
            "proactive": True,
            "trigger": dict(metadata or {}),
        }
        if event_id:
            event = Event(
                id=event_id,
                trace_id=trace_id,
                thread_id=thread_id,
                event_type="llm_response",
                turn_id=turn_id,
                turn_event_index=0,
                payload=event_payload,
                created_at=now,
            )
            self.db.add(event)
            self.db.flush()
        else:
            event = EventLogger(self.db).log_event(
                trace_id=trace_id,
                thread_id=thread_id,
                event_type="llm_response",
                turn_id=turn_id,
                turn_event_index=0,
                payload=event_payload,
            )
        thread.last_activity_at = now
        self.db.flush()
        return {
            "event_id": event.id,
            "turn_id": turn_id,
            "turn_sequence": sequence,
            "created": True,
        }
