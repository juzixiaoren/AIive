"""MCP 能力自举 API：从目标生成安装计划、端到端激活能力。

激活链路（真实执行）：
计划(evaluated) → npm 沙箱安装 → stdio 子进程启动 + tools/list 冒烟 →
ToolRegistry 注册（definition_source="remote_mcp"）→ plan.status=activated。

失败时 plan.status 保持/回到 evaluated（可重试），不再进入不可恢复状态。
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.core.llm_client import default_llm_client
from aiive.db.base import get_db
from aiive.db.models import Capability, CapabilityPlan, MCPInstallRecord
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
    registered_tools: list[str] = []
    error: str = ""


@router.post("/plan-from-goal")
def plan_from_goal(request: PlanRequest, db: Session = Depends(get_db)):
    """从用户目标生成 MCP 安装计划。

    使用 planner.plan_from_goal：先 LLM 分析目标提取 search_keywords，
    再用关键词驱动候选搜索（修复关键词从未用于搜索的缺陷）。
    """
    try:
        llm = default_llm_client()
        planner = CapabilityPlanner(llm)
        plan = planner.plan_from_goal(request.goal)
        evaluations = plan.candidates

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
            candidates_found=len(evaluations),
            selected=plan.selected,
            risk_summary=plan.risk_summary,
            status=plan.status,
        )
    except Exception:
        logger.exception("capability plan failed")
        return PlanResponse(ok=False, status="failed")


@router.post("/{plan_id}/activate")
def activate_capability(plan_id: str, db: Session = Depends(get_db)):
    """端到端激活已评估的能力计划。

    流程：真实 npm 沙箱安装（如未装）→ 真实 stdio 冒烟（tools/list）→
    把真实工具注册进 ToolRegistry → plan.status=activated。

    任一环节失败：如实返回 ok=False，plan.status 回到 evaluated 可重试
    （修复此前置 needs_user_review 后无法再次激活的单向死路）。
    """
    plan = db.get(CapabilityPlan, plan_id)
    if plan is None:
        return ActivateResponse(ok=False, error="Plan not found")
    # 兼容历史死路数据：needs_user_review 的旧计划也允许重新激活
    if plan.status not in ("evaluated", "needs_user_review"):
        return ActivateResponse(ok=False, error=f"Cannot activate: status={plan.status}")
    if not plan.selected_candidate:
        return ActivateResponse(ok=False, error="No candidate selected")

    def _fail(error: str, smoke: dict[str, Any] | None = None) -> ActivateResponse:
        plan.status = "evaluated"  # 保持可重试，不进入不可恢复状态
        if smoke is not None:
            plan.smoke_result = smoke
        db.commit()
        return ActivateResponse(
            ok=False,
            capability_id=plan.capability_id or "",
            smoke_result=smoke,
            error=error,
        )

    from aiive.mcp.discovery import get_candidate_by_name
    candidate = get_candidate_by_name(plan.selected_candidate)
    if candidate is None:
        return _fail(f"Selected candidate not in catalog: {plan.selected_candidate}")

    full_id = f"mcp:{candidate.name}"
    cap = (
        db.query(Capability)
        .filter(Capability.capability_id == full_id)
        .first()
    )

    # 1) 真实安装（尚未安装 / 缺启动信息时）
    if cap is None or not isinstance((cap.definition or {}).get("launch"), dict):
        from aiive.mcp.installer import install_sandbox

        install_result = install_sandbox(
            db=db,
            candidate_name=candidate.name,
            package_ref=candidate.package_ref,
            version=candidate.version,
            transport=candidate.transport,
            declared_tools=candidate.declared_tools,
            definition={
                "name": candidate.name,
                "source": candidate.source,
                "description": candidate.description,
                "risk_notes": candidate.risk_notes,
                "trust_level": candidate.definition_trust_level,
            },
            env_keys=candidate.required_env,
        )
        if not install_result.get("ok"):
            return _fail(f"install failed: {install_result.get('error')}")
        db.commit()
        cap = (
            db.query(Capability)
            .filter(Capability.capability_id == full_id)
            .first()
        )
        if cap is None:
            return _fail("install succeeded but capability record missing")

    # 计划中的风险评估落到能力定义，供注册与重启恢复使用
    risk_verdict = _risk_verdict_for(plan, candidate.name)
    definition = dict(cap.definition or {})
    definition["risk_verdict"] = risk_verdict
    definition.setdefault("trust_level", candidate.definition_trust_level)
    cap.definition = definition

    # 2) 真实冒烟（尚未 active 时）：启动 stdio 子进程 → tools/list
    if cap.state != "active":
        smoke = _real_smoke(db, cap)
        plan.smoke_result = {k: v for k, v in smoke.items() if k != "tools"}
        if not smoke.get("ok"):
            return _fail(
                f"smoke failed: {smoke.get('error')}",
                smoke=plan.smoke_result,
            )
        db.commit()

    # 3) 注册进 ToolRegistry（Agent 即刻可调用）
    from aiive.mcp.bootstrap import activate_capability_tools
    from aiive.tools.registry import get_tool_registry

    activation = activate_capability_tools(get_tool_registry(), cap)
    if not activation.get("ok"):
        return _fail(f"tool registration failed: {activation.get('error')}")

    plan.status = "activated"
    plan.capability_id = cap.capability_id
    db.commit()
    return ActivateResponse(
        ok=True,
        capability_id=cap.capability_id,
        smoke_result=plan.smoke_result,
        registered_tools=list(activation.get("registered", [])),
    )


def _risk_verdict_for(plan: CapabilityPlan, candidate_name: str) -> str:
    """从计划的风险评分中取选中候选的 verdict（缺省 medium）。"""
    for row in plan.risk_scores or []:
        if isinstance(row, dict) and row.get("candidate") == candidate_name:
            verdict = str(row.get("verdict", "medium"))
            if verdict in ("low", "medium", "high", "critical"):
                return verdict
    return "medium"


def _real_smoke(db: Session, cap: Capability) -> dict[str, Any]:
    """对能力做真实 tools/list 冒烟，并通过 installer.run_smoke 推进状态机。

    返回冒烟结果字典（含 ok / real_tools / error；tools 键携带完整工具
    定义用于缓存，不落入 smoke 记录）。
    """
    from aiive.mcp.installer import run_smoke
    from aiive.mcp.runtime_client import build_launch_spec, get_runtime_client

    launch = (cap.definition or {}).get("launch")
    if not isinstance(launch, dict):
        return {"ok": False, "mode": "tools_list", "error": "missing launch config"}

    install = (
        db.query(MCPInstallRecord)
        .filter(MCPInstallRecord.capability_id == cap.id)
        .order_by(MCPInstallRecord.created_at.desc())
        .first()
    )
    declared = set(install.declared_tools) if install else set()

    client = get_runtime_client()
    try:
        client.configure(cap.capability_id, build_launch_spec(launch))
        tools = client.list_tools(cap.capability_id)
    except Exception as e:
        logger.warning("MCP 激活冒烟失败: %s error=%s", cap.capability_id, e)
        smoke: dict[str, Any] = {"ok": False, "mode": "tools_list", "error": str(e)}
        run_smoke(db, cap.capability_id, smoke)
        return smoke

    real_names = [t["name"] for t in tools]
    smoke = {
        "ok": len(real_names) > 0,
        "mode": "tools_list",
        "real_tools": real_names,
        "declared_missing": sorted(declared - set(real_names)),
        "undeclared_extra": sorted(set(real_names) - declared),
        "error": None if real_names else "server started but exposes no tools",
    }
    # 缓存真实工具定义，注册与重启恢复时复用
    definition = dict(cap.definition or {})
    definition["tools"] = tools
    cap.definition = definition

    run_smoke(db, cap.capability_id, smoke)
    smoke["tools"] = tools
    return smoke
