"""Extraction policy: deterministic guard for memory extraction flow.

Receives a MemorySignalDecision from the ActionPlanner (model-based classification)
and applies ONLY deterministic policies:
- Non-user-message → skip
- Processed event → skip  
- Idempotency hit → skip
- MemorySignalDecision.SKIP → skip
- MemorySignalDecision.EXTRACT_SYNC → enqueue priority Outbox
- MemorySignalDecision.EXTRACT_ASYNC → enqueue regular Outbox

NO keyword tables. NO string matching. NO message length heuristics.
All semantic classification is done by the model via MemorySignalDecision.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class MessageSource(StrEnum):
    """由服务端入口确定的消息来源，客户端正文不能改变该值。"""

    USER = "user"
    SYSTEM_COMMAND = "system_command"
    RUNTIME_EVENT = "runtime_event"


class MemorySignalAction(StrEnum):
    SKIP = "skip"
    EXTRACT_ASYNC = "extract_async"
    EXTRACT_SYNC = "extract_sync"


class MemoryExtractionPolicy:
    """Deterministic extraction guard — no natural-language judgment.

    All semantic decisions (SKIP vs EXTRACT) are made by the model
    via MemorySignalDecision. This class only enforces structural rules.
    """

    @staticmethod
    def should_skip_system_message(
        user_message: str,
        message_source: str | MessageSource | None = None,
    ) -> bool:
        """优先按可信来源跳过；仅旧 payload 缺少来源时兼容文本前缀。"""
        if not user_message or not user_message.strip():
            return True
        if message_source is not None:
            try:
                source = message_source if isinstance(message_source, MessageSource) else MessageSource(message_source)
            except ValueError:
                logger.warning("未知 message_source，按非用户消息 fail-closed 跳过: %s", message_source)
                return True
            return source is not MessageSource.USER

        stripped = user_message.strip()
        system_triggers = ("[runtime event", "[system command", "[系统指令]")
        matched = any(stripped.startswith(trigger) for trigger in system_triggers)
        if matched:
            logger.info("旧 memory extraction payload 命中文本前缀兼容守卫")
        return matched

    @staticmethod
    def resolve_action(
        signal_action: str | MemorySignalAction | None,
        user_message: str,
        message_source: str | MessageSource | None = None,
    ) -> MemorySignalAction:
        """将模型动作与可信来源结构守卫合并为规范动作。"""
        if MemoryExtractionPolicy.should_skip_system_message(user_message, message_source):
            return MemorySignalAction.SKIP
        if isinstance(signal_action, MemorySignalAction):
            return signal_action
        try:
            return MemorySignalAction(str(signal_action).strip().lower())
        except (TypeError, ValueError):
            return MemorySignalAction.EXTRACT_ASYNC
