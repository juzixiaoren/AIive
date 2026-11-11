"""V64.2 deterministic candidate value policy; no IO or cross-source score arithmetic."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

POLICY_VERSION = "v64.2.lexicographic.1"


@dataclass
class ContextCandidateDecision:
    candidate_id: str
    source_type: str
    source_id: str
    retention_tier: int
    native_score: float | None = None
    source_rank: int = 1
    rank_percentile: float = 1.0
    estimated_tokens: int | None = None
    selected: bool = True
    exclusion_reason: str | None = None
    source_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rank_source(decisions: list[ContextCandidateDecision]) -> None:
    """Input is best first. Freeze ranks against the original visible source set."""
    n = len(decisions)
    for rank, decision in enumerate(decisions, 1):
        decision.source_rank = rank
        decision.rank_percentile = (n - rank) / (n - 1) if n > 1 else 1.0


def eviction_key(group: list[ContextCandidateDecision]) -> tuple[int, float, int, str, str]:
    """Atomic groups inherit their strongest member; unknown sizes release no known tokens."""
    return (
        max(d.retention_tier for d in group),
        max(d.rank_percentile for d in group),
        -sum(d.estimated_tokens or 0 for d in group),
        min(d.source_type for d in group),
        min(d.candidate_id for d in group),
    )
