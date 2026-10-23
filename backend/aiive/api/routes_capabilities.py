"""MCP 能力自举与生命周期 API。

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

from aiive.capability.lifecycle import CapabilityLifecycleService
from aiive.core.llm_client import default_llm_client
from aiive.db.base import get_db
from aiive.db.models import Capability, CapabilityPlan
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
    #    验证只推进到 verified，不把"验证通过"当成"已启用"。
    lifecycle = CapabilityLifecycleService(db)
    if cap.state != "active":
        smoke = lifecycle.probe(cap)
        plan.smoke_result = {k: v for k, v in smoke.items() if k != "tools"}
        if not smoke.get("ok"):
            return _fail(
                f"smoke failed: {smoke.get('error')}",
                smoke=plan.smoke_result,
            )
        db.commit()

    # 3) 注册进 ToolRegistry 并正式启用（Agent 即刻可调用）
    enabled = lifecycle.enable(cap.capability_id)
    if not enabled.get("ok"):
        return _fail(f"tool registration failed: {enabled.get('error')}")
    registered = [str(item) for item in enabled["details"].get("registered", [])]

    plan.status = "activated"
    plan.capability_id = cap.capability_id
    db.commit()
    return ActivateResponse(
        ok=True,
        capability_id=cap.capability_id,
        smoke_result=plan.smoke_result,
        registered_tools=registered,
    )


def _risk_verdict_for(plan: CapabilityPlan, candidate_name: str) -> str:
    """从计划的风险评分中取选中候选的 verdict（缺省 medium）。"""
    for row in plan.risk_scores or []:
        # JSON 历史数据可能绕过 ORM 类型约束；这里有意保留运行时防御。
        if isinstance(row, dict) and row.get("candidate") == candidate_name:  # pyright: ignore[reportUnnecessaryIsInstance]
            verdict = str(row.get("verdict", "medium"))
            if verdict in ("low", "medium", "high", "critical"):
                return verdict
    return "medium"


class LifecycleRequest(BaseModel):
    """生命周期操作的请求体（升级可覆盖版本与启动参数，回滚可指定目标版本）。"""

    target_version: str = ""
    launch_args: list[str] | None = None
    env_keys: list[str] | None = None
    force: bool = False


class LifecycleResponse(BaseModel):
    """生命周期操作结果：ok 与 error_type 区分"做成了"和"为什么没做成"。"""

    ok: bool
    capability_id: str = ""
    action: str = ""
    state: str = ""
    error: str = ""
    error_type: str = ""
    changed: bool = False
    details: dict[str, object] = {}


def _lifecycle_response(result: dict[str, Any]) -> LifecycleResponse:
    """把服务层结果转成响应模型（字段一一对应，不做语义改写）。"""
    return LifecycleResponse(
        ok=bool(result.get("ok")),
        capability_id=str(result.get("capability_id", "")),
        action=str(result.get("action", "")),
        state=str(result.get("state", "")),
        error=str(result.get("error", "")),
        error_type=str(result.get("error_type", "")),
        changed=bool(result.get("changed")),
        details=dict(result.get("details") or {}),
    )


@router.get("/lifecycle/audit")
def audit_capabilities(db: Session = Depends(get_db)):
    """核对能力在数据库、注册表与文件系统之间的一致性。"""
    return CapabilityLifecycleService(db).audit()


@router.post("/lifecycle/audit/reconcile")
def reconcile_capabilities(db: Session = Depends(get_db)):
    """按核查结果修复注册索引漂移（不动文件、不删记录）。"""
    return CapabilityLifecycleService(db).reconcile()


@router.get("/lifecycle/{capability_id:path}")
def capability_lifecycle_status(
    capability_id: str, db: Session = Depends(get_db),
):
    """查询单个能力的生命周期状态（安装/验证/注册/启用四个事实分列）。"""
    return _lifecycle_response(CapabilityLifecycleService(db).status(capability_id))


@router.post("/lifecycle/{capability_id:path}/verify")
def verify_capability(capability_id: str, db: Session = Depends(get_db)):
    """重新验证能力：真实 tools/list 通过后进入 verified（未启用）。"""
    return _lifecycle_response(CapabilityLifecycleService(db).verify(capability_id))


@router.post("/lifecycle/{capability_id:path}/enable")
def enable_capability(capability_id: str, db: Session = Depends(get_db)):
    """启用能力：注册工具并进入 active。"""
    return _lifecycle_response(CapabilityLifecycleService(db).enable(capability_id))


@router.post("/lifecycle/{capability_id:path}/disable")
def disable_capability(capability_id: str, db: Session = Depends(get_db)):
    """停用能力：从发现索引移除，保留安装产物与用户配置。"""
    return _lifecycle_response(CapabilityLifecycleService(db).disable(capability_id))


@router.post("/lifecycle/{capability_id:path}/upgrade")
def upgrade_capability(
    capability_id: str,
    request: LifecycleRequest | None = None,
    db: Session = Depends(get_db),
):
    """升级能力到指定版本；验证或注册失败时旧版本定义与注册保持不变。"""
    body = request or LifecycleRequest()
    return _lifecycle_response(CapabilityLifecycleService(db).upgrade(
        capability_id,
        body.target_version,
        launch_args=body.launch_args,
        env_keys=body.env_keys,
    ))


@router.post("/lifecycle/{capability_id:path}/rollback")
def rollback_capability(
    capability_id: str,
    request: LifecycleRequest | None = None,
    db: Session = Depends(get_db),
):
    """回滚到上一个（或指定）版本：定义、注册与状态一起回退。"""
    body = request or LifecycleRequest()
    return _lifecycle_response(CapabilityLifecycleService(db).rollback(
        capability_id, body.target_version,
    ))


@router.delete("/lifecycle/{capability_id:path}")
def uninstall_capability(
    capability_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """卸载能力：检查运行中引用后清理注册、记录与专属安装产物。"""
    return _lifecycle_response(CapabilityLifecycleService(db).uninstall(
        capability_id, force=force,
    ))
