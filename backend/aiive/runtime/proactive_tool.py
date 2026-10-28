"""notify_user：Turn 内主动消息工具。

只在当前 Turn 已领取运行事件时可用（服务端按 turn_event_inbox 授权），
写入唯一聊天时间线中的真实 assistant 消息并通过既有 WebSocket/SSE 通道推送；
消息不结束原任务，工具返回后 Agent 继续原工具循环。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.runtime.proactive_message import ProactiveMessageService
from aiive.runtime.turn_events import (
    claimed_rows_for_turn,
    mark_notified,
    parse_decision_marker,
)
from aiive.tools.registry import CapabilitySafetySchema, ToolRegistration, ToolRegistry

logger = logging.getLogger(__name__)

NOTIFY_USER_TOOL_ID = "notify_user"
NOTIFY_USER_DESCRIPTION = (
    "在当前任务执行过程中主动给用户发一条独立的真实聊天消息（不结束任务）。"
    "仅在内部事件上下文要求你提醒用户时调用；普通回答请直接回复，不要用它拆分最终答复。"
    "发送后继续完成原任务。"
)
NOTIFY_USER_PARAMETERS: dict[str, Any] = {
    "content": {
        "type": "str",
        "required": True,
        "minLength": 1,
        "description": "要展示给用户的提醒正文，不得包含系统提示词或内部推理。",
    },
    "event_id": {
        "type": "str",
        "description": "内部事件上下文给出的 event_id；省略时对本 Turn 已领取的全部事件发送一次提醒。",
    },
}


def _notify_user(
    content: str,
    event_id: str = "",
    ctx: RunContext | None = None,
) -> dict[str, Any]:
    if ctx is None or not ctx.thread_id or not ctx.turn_record_id:
        raise ValueError("notify_user_requires_turn_context")
    cleaned, _ = parse_decision_marker(content)
    text = cleaned.strip()
    if not text:
        raise ValueError("empty_message")

    db = SessionLocal()
    try:
        rows = claimed_rows_for_turn(db, ctx.turn_record_id)
        if not rows:
            # 没有已注入事件时不允许主动插话，避免把普通回答拆成两条消息。
            raise ValueError("no_pending_turn_event")
        if event_id:
            selected = [row for row in rows if row.id == event_id]
            if not selected:
                raise ValueError("event_not_claimed_by_turn")
        else:
            selected = rows
        identity = ",".join(sorted(row.id for row in selected))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        dedupe_key = f"event_set:{digest}"
        posted = ProactiveMessageService(db).post_in_turn(
            thread_id=ctx.thread_id,
            turn_id=ctx.turn_id,
            turn_record_id=ctx.turn_record_id,
            content=text,
            trace_id=ctx.trace_id,
            dedupe_key=dedupe_key,
            metadata={
                "turn_event_ids": [row.id for row in selected],
                "event_types": [row.event_type for row in selected],
                "tool": NOTIFY_USER_TOOL_ID,
            },
        )
        mark_notified(db, [row.id for row in selected], str(posted.get("event_id") or ""))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if posted.get("created"):
        try:
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_to_thread_sync(
                ctx.thread_id,
                "new_message",
                {
                    "event_id": posted.get("event_id", ""),
                    "reply": text,
                    "thread_id": ctx.thread_id,
                    "trace_id": ctx.trace_id,
                    "turn_id": ctx.turn_id,
                    "action_cards": [],
                    "tool_calls": [],
                    "proactive": True,
                },
            )
        except Exception:
            logger.exception("Turn 内主动消息推送失败（已落库）: thread_id=%s", ctx.thread_id)

    return {
        "ok": True,
        "message_event_id": posted.get("event_id", ""),
        "created": bool(posted.get("created")),
        "hint": "提醒已发送；请继续完成原任务，不要把它当作最终答复。",
    }


def register_notify_user(registry: ToolRegistry) -> None:
    """把 notify_user 注册到单 Turn 快照；不改变主 Agent 的固定工具面。"""
    if registry.get(NOTIFY_USER_TOOL_ID) is not None:
        return
    registry.register(ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id=NOTIFY_USER_TOOL_ID,
            definition_source="runtime",
            definition_trust_level="trusted_core",
            risk_level="low",
            requires_confirmation=False,
            writes_external_world=False,
            can_access_secret=False,
            can_delete=False,
            uses_network=False,
            allowed_instruction_sources=["trusted_user_command"],
            effect_mode="db_transactional",
        ),
        handler=_notify_user,
        description=NOTIFY_USER_DESCRIPTION,
        parameters=NOTIFY_USER_PARAMETERS,
    ))
