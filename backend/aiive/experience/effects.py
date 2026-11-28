"""Observational same-family comparisons of actual runtime usage."""
from __future__ import annotations

from typing import Any

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import batched
from statistics import median

from sqlalchemy.orm import Session

from aiive.db.models import ExperienceEpisode, ExperienceInsight
from aiive.experience.capture import NEGATIVE, POSITIVE, result_for, signals_by_episode, utc, visible_episodes

WINDOW = timedelta(days=14)
MIN_SAMPLES = 5


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator >= MIN_SAMPLES else None,
            "status": "observed" if denominator >= MIN_SAMPLES else "insufficient_data"}


def _median(values: list[int | float]) -> dict[str, Any]:
    return {"samples": len(values), "value": median(values) if len(values) >= MIN_SAMPLES else None,
            "status": "observed" if len(values) >= MIN_SAMPLES else "insufficient_data"}


def _is_used(episode: ExperienceEpisode, insight: ExperienceInsight) -> bool:
    return any(use["id"] == insight.insight_id and use["version"] == insight.version for use in episode.uses)


class ExperienceEffectEvaluator:
    def __init__(self, db: Session):
        self.db: Session = db

    def _visible(self, rows: Iterable[ExperienceEpisode]) -> Iterator[ExperienceEpisode]:
        for group in batched(rows, 128):
            yield from visible_episodes(self.db, group)

    def summarize(self, episodes: list[ExperienceEpisode]) -> dict[str, Any]:
        outcomes: list[str] = []
        signal_kinds: list[set[str]] = []
        for group in batched(episodes, 128):
            signals = signals_by_episode(self.db, [row.id for row in group])
            outcomes.extend(result_for(row, signals[row.id]) for row in group)
            signal_kinds.extend({signal.kind for signal in signals[row.id]} for row in group)
        user_episodes = len(episodes)
        task_outcomes = [(row, row.outcome) for row in episodes if row.kind == "task"]
        skilled = [(row, outcome) for row, outcome in zip(episodes, outcomes)
                   if any(use["kind"] == "skill" for use in row.uses)]
        assessed_skills = [outcome for _, outcome in skilled if outcome in POSITIVE | NEGATIVE]
        observed_feedback = {"corrected", "rework", "explicit_negative", "explicit_positive", "new_request", "continuation"}
        return {
            "samples": user_episodes,
            "follow_up_samples": sum(bool(kinds & observed_feedback) for kinds in signal_kinds),
            "skill_exposures": len(skilled),
            "rates": {
                "correction": _rate(sum(bool(kinds & {"corrected", "explicit_negative"})
                                       for kinds in signal_kinds), user_episodes),
                "rework": _rate(sum("rework" in kinds for kinds in signal_kinds), user_episodes),
                "task_failure": _rate(sum(outcome == "failed" for _, outcome in task_outcomes), len(task_outcomes)),
                "tool_failure": _rate(sum(row.metrics["tool_failures"] for row in episodes),
                                      sum(row.metrics["tool_calls"] for row in episodes)),
                "search_no_results": _rate(sum(row.metrics["search_misses"] for row in episodes),
                                           sum(row.metrics["searches"] for row in episodes)),
                "skill_success": _rate(sum(outcome in POSITIVE for outcome in assessed_skills), len(assessed_skills)),
                "skill_rework": _rate(sum(outcome == "rework" for _, outcome in skilled), len(skilled)),
            },
            "medians": {name: _median([row.metrics[name] for row in episodes if row.metrics.get(name) is not None])
                        for name in ("actions", "tokens", "duration_ms")},
        }

    def usage(self, insight: ExperienceInsight) -> dict[str, Any]:
        query = self.db.query(ExperienceEpisode).filter_by(family=insight.family)
        used = (row for row in query.yield_per(128) if _is_used(row, insight))
        samples = helpful = harmful = 0
        for group in batched(self._visible(used), 128):
            signals = signals_by_episode(self.db, [row.id for row in group])
            for row in group:
                outcome = result_for(row, signals[row.id])
                samples += 1
                helpful += outcome in POSITIVE
                harmful += outcome in NEGATIVE
        return {"samples": samples, "helpful": helpful, "harmful": harmful}

    def compare(self, insight: ExperienceInsight, *, now: datetime | None = None) -> dict[str, Any]:
        if insight.enabled_at is None:
            return {"status": "insufficient_data", "minimum_samples": MIN_SAMPLES, "cohorts": {}}
        boundary = utc(insight.enabled_at)
        now = utc(now or datetime.now(UTC))
        end = min(now, boundary + WINDOW, utc(insight.disabled_at) if insight.disabled_at else now)
        span = max(timedelta(0), end - boundary)
        start = boundary - span
        rows = self.db.query(ExperienceEpisode).filter(
            ExperienceEpisode.family == insight.family, ExperienceEpisode.completed_at >= start,
            ExperienceEpisode.completed_at < end,
        ).order_by(ExperienceEpisode.completed_at, ExperienceEpisode.id)
        cohorts = {"turn": {"before": [], "after": [], "exposed": []},
                   "task": {"before": [], "after": [], "exposed": []}}
        for row in self._visible(rows.yield_per(128)):
            group = cohorts[row.kind]
            if utc(row.completed_at) < boundary:
                group["before"].append(row)
            else:
                group["after"].append(row)
                if _is_used(row, insight):
                    group["exposed"].append(row)
        observed = any(min(len(group["before"]), len(group["exposed"])) >= MIN_SAMPLES for group in cohorts.values())
        return {
            "status": "observed" if observed else "insufficient_data", "minimum_samples": MIN_SAMPLES,
            "method": "observational_same_family",
            "windows": {"before": {"start": start, "end": boundary},
                        "after": {"start": boundary, "end": end}},
            "cohorts": {kind: {key: self.summarize(items) for key, items in groups.items()}
                        for kind, groups in cohorts.items()},
        }
