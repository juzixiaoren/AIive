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

    def active(self, *, kind: str | None = None, family: str | None = None) -> list[ExperienceInsight]:
        query = self.db.query(ExperienceInsight).filter_by(status="active")
        if kind is not None:
            query = query.filter_by(kind=kind)
        if family is not None:
            query = query.filter_by(family=family)
        return [item for item in query.order_by(ExperienceInsight.enabled_at.desc(), ExperienceInsight.id)
                if evidence_visible(self.db, item)]

    def context(
        self, text: str, family: str, registry: ToolRegistry, *, allow_network: bool = True,
        main: bool = False, skill_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from aiive.skills.catalog import SkillService

        query = match_terms(text)
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        skill_service = SkillService(self.db)
        for row in self.active(family=family):
            if row.kind == "tool_hint":
                continue
            body = row.body
            phrases = [match_terms(term) for term in body["match_terms"]]
            hits = set().union(*(phrase for phrase in phrases if phrase and phrase <= query))
            if len(hits) < 2 and not (row.kind == "skill" and skill_id == row.insight_id):
                continue
            for capability in body["required_capabilities"]:
                tool = registry.get(capability)
                if tool is None or (tool.safety.uses_network and not allow_network):
                    if main and registry.get("delegate_task") is not None:
                        continue
                    break
            else:
                if row.kind == "skill":
                    skill = skill_service.from_insight(row)
                    display = skill.instructions
                else:
                    display = workflow_text(body)
                candidates.append((len(hits), row.support_count, {
                    **usage_ref(row), "title": row.title, "text": display,
                }))
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]["id"]))
        return [item[2] for item in candidates[:MAX_CONTEXT_INSIGHTS]]

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
