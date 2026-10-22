"""Domain tool handlers (implementation) split from builtin_tools."""

from sqlalchemy.orm import Session

from aiive.tools.builtin_tools import db_handler


@db_handler
def handle_plan_capability(db: Session, goal: str):
    """分析目标，搜索 MCP 候选，评估风险，生成安装计划并持久化。

    流程: 分析目标 → 搜索候选 → 风险评估 → 生成计划 → 持久化到 capability_plans 表
    """
    from aiive.core.llm_client import default_llm_client
    from aiive.db.models import CapabilityPlan
    from aiive.mcp.capability_planner import CapabilityPlanner

    llm = default_llm_client()
    planner = CapabilityPlanner(llm)

    candidates_raw = planner.search_candidates([goal])
    evaluations = planner.evaluate_candidates(candidates_raw)
    plan = planner.generate_plan(goal, candidates_raw, evaluations)

    db_plan = CapabilityPlan(
        goal=plan.goal,
        goal_summary=plan.goal_summary,
        missing_capability_type=plan.missing_capability_type,
        candidates=[{
            "name": e.candidate_name, "source": e.source, "version": e.version,
            "risk_verdict": e.risk_score.verdict,
            "risk_overall": e.risk_score.overall,
            "recommendation": e.recommendation,
        } for e in evaluations],
        risk_scores=[{
            "candidate": e.candidate_name, "verdict": e.risk_score.verdict,
            "overall": e.risk_score.overall,
            "source_trust": e.risk_score.source_trust,
            "permission_risk": e.risk_score.permission_risk,
        } for e in evaluations],
        selected_candidate=plan.selected,
        status=plan.status,
    )
    db.add(db_plan)
    db.flush()

    return {
        "ok": True,
        "plan_id": db_plan.id,
        "goal_summary": plan.goal_summary,
        "missing_type": plan.missing_capability_type,
        "candidates_found": len(candidates_raw),
        "selected": plan.selected,
        "risk_summary": plan.risk_summary,
        "status": plan.status,
    }

def handle_search_mcp(goal: str = ""):
    """根据目标搜索匹配的 MCP 候选服务器。

    参数:
        goal: 用户目标描述文本

    返回:
        MCP 候选服务器信息列表，每项包含 name、source、version、description 等
    """
    from aiive.mcp.discovery import search_mcp_candidates
    candidates = search_mcp_candidates(goal)
    return [{"name": c.name, "source": c.source, "version": c.version, "description": c.description, "risk_notes": c.risk_notes, "declared_tools": c.declared_tools} for c in candidates]

@db_handler
def handle_install_mcp_sandbox(
    db: Session,
    candidate_name: str,
    launch_args: list[str] | None = None,
    env_keys: list[str] | None = None,
):
    """将 MCP 候选服务器安装到沙箱环境。

    流程:
    1. 搜索匹配的候选
    2. 调用 install_sandbox 安装到数据库

    参数:
        candidate_name: 候选服务器名称

    返回:
        安装结果字典
    """
    from aiive.mcp.discovery import get_candidate_by_name, search_mcp_candidates
    from aiive.mcp.installer import install_sandbox
    exact = get_candidate_by_name(candidate_name)
    if exact is not None:
        c = exact
    else:
        candidates = search_mcp_candidates(candidate_name)
        if not candidates:
            return {"ok": False, "error": "No candidate found"}
        c = candidates[0]
    requested_env = set(env_keys or c.required_env)
    undeclared_env = requested_env - set(c.required_env)
    if undeclared_env:
        return {
            "ok": False,
            "error": f"MCP env keys not declared by catalog: {sorted(undeclared_env)}",
        }
    return install_sandbox(
        db, c.name, c.package_ref, c.version, c.transport, c.declared_tools,
        {
            "name": c.name, "source": c.source, "description": c.description,
            "risk_notes": c.risk_notes, "trust_level": c.definition_trust_level,
            "homepage": c.homepage,
        },
        launch_args=launch_args or [],
        env_keys=sorted(requested_env),
    )
