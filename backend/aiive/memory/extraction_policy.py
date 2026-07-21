"""Extraction policy: deterministic guard for memory extraction flow.

Receives a MemorySignalDecision from the ActionPlanner (model-based classification)
and applies ONLY deterministic policies:
- Non-user-message → skip
- Processed event → skip  
- Idempotency hit → skip
- MemorySignalDecision.SKIP → skip
- MemorySignalDecision.EXTRACT_SYNC → immediate processing
- MemorySignalDecision.EXTRACT_ASYNC → enqueue

NO keyword tables. NO string matching. NO message length heuristics.
All semantic classification is done by the model via MemorySignalDecision.
"""

from __future__ import annotations

from enum import StrEnum


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
    def should_skip_system_message(user_message: str) -> bool:
        """跳过后端内部系统消息，不进行自然语言语义判断。"""
        if not user_message or not user_message.strip():
            return True
        stripped = user_message.strip()
        # 这些前缀表示内部占位消息，不是用户自然语言。
        system_triggers = ("[runtime event", "[system command", "[系统指令]")
        return any(stripped.startswith(t) for t in system_triggers)

    @staticmethod
    def resolve_action(
        signal_action: str | MemorySignalAction | None,
        user_message: str,
    ) -> MemorySignalAction:
        """将模型动作与确定性结构守卫合并为规范动作。"""
        if MemoryExtractionPolicy.should_skip_system_message(user_message):
            return MemorySignalAction.SKIP
        if isinstance(signal_action, MemorySignalAction):
            return signal_action
        try:
            return MemorySignalAction(str(signal_action).strip().lower())
        except (TypeError, ValueError):
            return MemorySignalAction.EXTRACT_ASYNC
