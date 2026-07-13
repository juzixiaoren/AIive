"""Configured budgets and weights for the V2 recall architecture.

No magic numbers scattered in business code — all knobs live here and are
overridable per-process.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RecallConfig:
    """All recall-side budgets, weights, and limits."""

    # --- Core Memory Blocks ---
    core_memory_total_token_budget: int = 600
    core_memory_max_blocks: int = 3

    # --- Automatic Recall ---
    automatic_recall_top_k: int = 8
    automatic_recall_token_budget: int = 1200
    recall_relevance_threshold: float = 0.08  # lowered for CJK bigram noise

    # --- Agent-Initiated Recall limits (Kernel enforcement) ---
    max_memory_tool_calls_per_turn: int = 6
    max_total_recall_token_budget: int = 2400
    memory_tool_top_k: int = 8
    memory_tool_token_budget: int = 1000

    # --- Route / fusion ---
    route_timeout_ms: int = 500
    rrf_k: int = 60
    route_weights: dict[str, float] = field(default_factory=lambda: {
        "rrf": 0.40,
        "relevance": 0.30,
        "scope": 0.15,
        "recency": 0.05,
        "importance": 0.05,
        "trust": 0.05,
    })

    # --- Adapters (Qdrant / temporal KG not yet available) ---
    vector_adapter_enabled: bool = False
    temporal_graph_adapter_enabled: bool = False

    # --- Core projection refresh debounce ---
    core_projection_refresh_debounce_s: int = 5

    # Default token estimator (chars / 4)
    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)
