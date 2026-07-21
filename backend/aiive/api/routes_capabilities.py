"""MCP 能力自举 API：从目标生成安装计划、激活能力。"""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.core.llm_client import default_llm_client
from aiive.db.base import get_db
from aiive.db.models import CapabilityPlan
from aiive.mcp.capability_planner import CapabilityPlanner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/capabilities")


class PlanRequest(BaseModel):
    goal: str = Field(..., min_length=1)


class PlanResponse(BaseModel):
    ok: bool
    plan_id: str = ""
    goal_summary: str = ""
    missing_type: str = ""
    candidates_found: int = 0
    selected: str = ""
    risk_summary: str = ""
    status: str = ""


class ActivateResponse(BaseModel):
    ok: bool
    capability_id: str = ""
    smoke_result: dict[str, object] | None = None
    error: str = ""


@router.post("/plan-from-goal")
def plan_from_goal(request: PlanRequest, db: Session = Depends(get_db)):
    """从用户目标生成 MCP 安装计划。"""
    try:
        llm = default_llm_client()
        planner = CapabilityPlanner(llm)
        candidates_raw = planner.search_candidates([request.goal])
        evaluations = planner.evaluate_candidates(candidates_raw)
        plan = planner.generate_plan(request.goal, candidates_raw, evaluations)

        db_plan = CapabilityPlan(
            goal=plan.goal,
            goal_summary=plan.goal_summary,
            missing_capability_type=plan.missing_capability_type,
            candidates=[{
                "name": e.candidate_name, "source": e.source, "version": e.version,
                "risk_verdict": e.risk_score.verdict,
                "recommendation": e.recommendation,
            } for e in evaluations],
            risk_scores=[{
                "candidate": e.candidate_name, "verdict": e.risk_score.verdict,
                "overall": e.risk_score.overall,
            } for e in evaluations],
            selected_candidate=plan.selected,
            status=plan.status,
        )
        db.add(db_plan)
        db.commit()

        return PlanResponse(
            ok=True,
            plan_id=db_plan.id,
            goal_summary=plan.goal_summary,
            missing_type=plan.missing_capability_type,
            candidates_found=len(candidates_raw),
            selected=plan.selected,
            risk_summary=plan.risk_summary,
            status=plan.status,
        )
    except Exception:
        logger.exception("capability plan failed")
        return PlanResponse(ok=False, status="failed")


@router.post("/{plan_id}/activate")
def activate_capability(plan_id: str, db: Session = Depends(get_db)):
    """激活已通过评估的能力计划。"""
    plan = db.get(CapabilityPlan, plan_id)
    if plan is None:
        return ActivateResponse(ok=False, error="Plan not found")
    if plan.status != "evaluated":
        return ActivateResponse(ok=False, error=f"Cannot activate: status={plan.status}")
    if not plan.selected_candidate:
        return ActivateResponse(ok=False, error="No candidate selected")

    plan.status = "needs_user_review"
    plan.smoke_result = {
        "ok": False,
        "code": "real_mcp_runtime_unavailable",
        "message": "真实 MCP 安装、启动、tools/list、tools/call 和 ToolRegistry 注册链路尚未完成",
    }
    db.commit()
    return ActivateResponse(
        ok=False,
        capability_id=plan.capability_id or "",
        smoke_result=plan.smoke_result,
        error="Real MCP runtime and smoke verification are required before activation",
    )
