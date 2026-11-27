"""内置 Skill catalog 的只读 API。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Any

from aiive.db.base import get_db
from aiive.skills.catalog import SkillDefinition, SkillService
from aiive.tools.registry import get_tool_registry

router = APIRouter(prefix="/api/skills")


def _payload(
    skill: SkillDefinition,
    *,
    include_instructions: bool = False,
) -> dict[str, Any]:
    payload = skill.as_dict(include_instructions=include_instructions)
    registry = get_tool_registry()
    payload["available_capabilities"] = [
        capability for capability in skill.capabilities if registry.get(capability) is not None
    ]
    payload["missing_capabilities"] = [
        capability for capability in skill.capabilities if registry.get(capability) is None
    ]
    payload["status"] = "ready" if not payload["missing_capabilities"] else "degraded"
    return payload


@router.get("")
def skill_catalog(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_payload(skill) for skill in SkillService(db).list()]


@router.get("/{skill_id}")
def skill_detail(skill_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    skill = SkillService(db).get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    return _payload(skill, include_instructions=True)
