"""Observational same-family comparisons of actual runtime usage."""
from __future__ import annotations

from typing import Any

from datetime import UTC, datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from aiive.db.models import ExperienceEpisode, ExperienceInsight
from aiive.experience.capture import NEGATIVE, POSITIVE, episode_visible, result_for, utc, visible_signals

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

    def summarize(self, episodes: list[ExperienceEpisode]) -> dict[str, Any]:
        observations = [(row, visible_signals(self.db, row.id)) for row in episodes]
        outcomes = [result_for(row, signals) for row, signals in observations]
        signal_kinds = [{signal.kind for signal in signals} for _, signals in observations]
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
        rows = [row for row in query if _is_used(row, insight) and episode_visible(self.db, row)]
        outcomes = [result_for(row, visible_signals(self.db, row.id)) for row in rows]
        return {"samples": len(rows), "helpful": sum(item in POSITIVE for item in outcomes),
                "harmful": sum(item in NEGATIVE for item in outcomes)}

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
        for row in rows:
            if not episode_visible(self.db, row):
                continue
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
