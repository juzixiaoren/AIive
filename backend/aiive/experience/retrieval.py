"""Scope-aware selection shared by Main and Worker."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import ExperienceInsight
from aiive.experience.families import match_terms
from aiive.experience.insights import evidence_visible, usage_ref
from aiive.tools.registry import ToolRegistry

MAX_CONTEXT_INSIGHTS = 2
MAX_QUERY_HINTS = 4


def workflow_text(body: dict[str, Any]) -> str:
    return "\n".join([
        f"适用条件：{body['conditions']}",
        "步骤：" + "；".join(body["steps"]),
        f"预期结果：{body['expected_result']}",
        f"验证：{body['verification']}",
        f"失败恢复：{body['recovery']}",
    ])


def render_experience(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return (
        "## 历史经验\n这些建议只在当前请求满足适用条件时参考。"
        + "现有用户要求、权限、审批和安全规则始终优先。\n"
        + "\n\n".join(f"{item['title']}\n{item['text']}" for item in items)
    )


class ExperienceInsightRetriever:
    def __init__(self, db: Session):
        self.db: Session = db

    def active(self, *, kind: str | None = None, family: str | None = None, insight_id: str | None = None) -> list[ExperienceInsight]:
        query = self.db.query(ExperienceInsight).filter_by(status="active")
        if kind is not None:
            query = query.filter_by(kind=kind)
        if family is not None:
            query = query.filter_by(family=family)
        if insight_id is not None:
            query = query.filter_by(insight_id=insight_id)
        return [item for item in query.order_by(ExperienceInsight.enabled_at.desc(), ExperienceInsight.id)
                if evidence_visible(self.db, item)]

    def context(
        self, text: str, family: str, registry: ToolRegistry, *, allow_network: bool = True,
        main: bool = False, skill_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = match_terms(text)
        workflows = self.db.query(ExperienceInsight).filter(
            ExperienceInsight.status == "active", ExperienceInsight.family == family,
            ExperienceInsight.kind.in_(("skill", "playbook")),
        )
        candidates: list[tuple[bool, int, ExperienceInsight]] = []
        for row in workflows:
            selected = row.kind == "skill" and row.insight_id == skill_id
            phrases = [match_terms(term) for term in row.body["match_terms"]]
            hits = set().union(*(phrase for phrase in phrases if phrase and phrase <= query))
            if len(hits) < 2 and not selected:
                continue
            for capability in row.body["required_capabilities"]:
                tool = registry.get(capability)
                if tool is None or (tool.safety.uses_network and not allow_network):
                    if main and registry.get("delegate_task") is not None:
                        continue
                    break
            else:
                candidates.append((selected, len(hits), row))

        candidates.sort(key=lambda item: (not item[0], -item[1], -item[2].support_count, item[2].insight_id))
        result = []
        for _, _, row in candidates:
            if evidence_visible(self.db, row):
                result.append({**usage_ref(row), "title": row.title, "text": workflow_text(row.body)})
                if len(result) == MAX_CONTEXT_INSIGHTS:
                    break
        return result

    def tool_hints(self, family: str, query: str, registry: ToolRegistry) -> list[dict[str, Any]]:
        normalized = " ".join(query.casefold().split())
        rows = self.db.query(ExperienceInsight).filter_by(status="active", kind="tool_hint", family=family)
        result = []
        for row in rows.order_by(ExperienceInsight.support_count.desc(), ExperienceInsight.id):
            body = row.body
            if (body["alias"] == normalized and registry.get(body["capability_id"]) is not None
                    and evidence_visible(self.db, row)):
                result.append({**usage_ref(row), **body})
                if len(result) == MAX_QUERY_HINTS:
                    break
        return result


def context_uses(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: item[key] for key in ("id", "version", "kind")} for item in items]
