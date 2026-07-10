"""StewardSignalEnricher: post-extraction enrichment for steward signals.

DEPRECATED as a standalone extractor. Now operates as an enrichment layer
that runs after the unified extraction, adding signal_type markers
(routine/habit/schedule/preference) and adjusting confidence/importance.

The unified MemoryExtractor already includes steward signal detection in its
prompt. This module provides additional deterministic enrichment if needed.

Backward-compatible: StewardSignalExtractor is aliased to UnifiedMemoryExtractor
to avoid breaking imports in outbox_handlers.
"""

from __future__ import annotations

from aiive.memory.memory_extractor import UnifiedMemoryExtractor

# DEPRECATED: use UnifiedMemoryExtractor instead.
# This alias exists for backward compatibility with outbox_handlers.
StewardSignalExtractor = UnifiedMemoryExtractor
