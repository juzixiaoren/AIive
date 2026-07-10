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
        """Skip extraction for backend system messages (structural, not semantic)."""
        if not user_message or not user_message.strip():
            return True
        stripped = user_message.strip()
        # These are internal placeholders, not user natural language
        system_triggers = ("[runtime event", "[system command")
        return any(stripped.startswith(t) for t in system_triggers)

    @staticmethod
    def resolve_action(
        signal_action: MemorySignalAction,
        user_message: str,
    ) -> MemorySignalAction:
        """Resolve final action combining model signal + deterministic rules.

        Args:
            signal_action: Model-classified action.
            user_message: Raw user message for structural checks only.
        """
        if MemoryExtractionPolicy.should_skip_system_message(user_message):
            return MemorySignalAction.SKIP
        return signal_action
