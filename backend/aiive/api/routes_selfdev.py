"""
API路由模块：自进化开发（Self-Dev）
- 提供代码槽位（Slots）状态查询和健康检查
- 提供自进化计划（Plan）的创建和查询
- 提供自主修复闭环（读码 → 规划 → 补丁 → 定向测试 → 有限修复 → 安全门）
- 提供补丁应用、升级和回滚操作的接口
"""
import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Any

from aiive.config import settings
from aiive.core.llm_client import LLMClient
from aiive.db.base import get_db
from aiive.db.models import SelfDevRequest, PatchOperation
from aiive.runtime.trace import Trace
from aiive.selfdev.planner import SelfDevPlanner
from aiive.supervisor.launcher import Launcher

router = APIRouter(prefix="/api/selfdev")


def require_trusted_core_admin(
    x_aiive_trusted_core_token: str = Header(default=""),
) -> None:
    """旧直接变更 API 的显式管理边界；普通 Agent 无法构造此 Header。"""
    if not hmac.compare_digest(
        x_aiive_trusted_core_token,
        settings.aiive_trusted_core_admin_token,
    ):
        raise HTTPException(status_code=403, detail="trusted_core_admin_required")


class PlanRequest(BaseModel):
    """自进化计划请求体"""
    goal: str = Field(..., min_length=1)


class RepairRequest(BaseModel):
    """自主修复闭环请求体。"""

    goal: str = Field(..., min_length=1)
    max_rounds: int = Field(3, ge=0, le=5, description="测试失败后的最大修复轮次")
    promote: bool = Field(False, description="安全门通过后是否提交晋升（仍走 ReleaseManager）")
    run_gate: bool = Field(True, description="是否在晋升前运行 Replay Eval + 内核契约")
    files: list[str] = Field(default_factory=list, description="可选的仓库相对路径提示")


def _build_llm_client() -> LLMClient:
    """按当前配置构造 LLM 客户端（计划与修复共用）。"""
    return LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )


# ===================== V11: 槽位管理 =====================

@router.get("/slots")
def list_slots():
    """获取当前代码槽位状态

    Returns:
        各槽位的激活状态和相关信息
    """
    launcher = Launcher()
    return launcher.get_status()


@router.get("/supervisor")
def supervisor_status():
    """Supervisor 托管状态：守护进程是否存活、当前阶段、受管进程与最近事件。

    只读接口；进程控制只能由 Supervisor 守护进程执行，API 不提供直接启动/杀进程能力。
    """
    from aiive.supervisor.client import SupervisorClient

    return SupervisorClient().status(events=20)


@router.post("/slots/health-check", operation_id="check_selfdev_slot_health")
def check_slot_health():
    """对当前槽位进行健康检查

    Returns:
        健康检查结果
    """
    launcher = Launcher()
    return launcher.health_check()


# ===================== V12: 自进化计划 =====================

@router.post("/plan")
def create_plan(request: PlanRequest, db: Session = Depends(get_db)):
    """创建自进化开发计划

    根据目标描述，通过 LLM 生成开发计划并存入数据库。

    Args:
        request: 包含 goal 的计划请求
        db: 数据库会话

    Returns:
        创建的计划信息，包含 request_id、trace_id、status 和 plan 详情
    """
    client = _build_llm_client()
    trace = Trace.new()
    planner = SelfDevPlanner(client)
    plan = planner.plan(request.goal, trace_id=trace.trace_id)

    req = SelfDevRequest(
        trace_id=trace.trace_id,
        goal=request.goal,
        status="proposed",
        plan=plan,
    )
    db.add(req)
    db.flush()

    for op in plan.get("operations", []):
        patch = PatchOperation(
            request_id=req.id,
            operation=op.get("operation", "add_file"),
            target_file=op.get("target_file", ""),
            reason=op.get("reason", ""),
            risk_notes=op.get("risk_notes", ""),
            requires_schema_change=op.get("requires_schema_change", False),
            safe_delete_scope=op.get("safe_delete_scope"),
            not_allowed_yet=op.get("not_allowed_yet", False),
        )
        db.add(patch)

    db.commit()

    return {
        "request_id": req.id,
        "trace_id": req.trace_id,
        "status": req.status,
        "plan": plan,
    }


@router.post("/repair")
def run_repair(
    request: RepairRequest,
    db: Session = Depends(get_db),
    _authorized: None = Depends(require_trusted_core_admin),
) -> dict[str, Any]:
    """执行自主修复闭环并落库。

    流程：读取相关代码 → 生成补丁 → 应用到非活跃槽位 → 定向测试 →
    失败时按真实错误有限次修复 → 安全门（内核契约 + Replay Eval）→
    可选受控晋升（仍由 ReleaseManager / Supervisor 执行健康检查与回滚）。

    所有代码修改只进入非活跃槽位；未通过测试或安全门的候选版本不会晋升。

    并发：同一项目同一时间只允许一个修复流程。已有修复在跑时不排队、不覆盖，
    直接返回 ``status="repair_busy"``（ok=False，``lock.holder`` 给出持有者），
    调用方可稍后重试。中断恢复：上一次运行若在任意阶段被强杀，本次调用会先
    按运行日志把候选槽位回滚到基线（或显式隔离），再开始新一轮。
    """
    from aiive.selfdev.repair_loop import SelfDevRepairLoop, persist_repair_run

    trace = Trace.new()
    loop = SelfDevRepairLoop(
        llm_client=_build_llm_client(),
        max_rounds=request.max_rounds,
    )
    record = loop.run(
        request.goal,
        trace_id=trace.trace_id,
        files=request.files or None,
        promote=request.promote,
        run_gate=request.run_gate,
    )
    persist_repair_run(db, record)
    return record


@router.get("/requests/{request_id}")
def get_request(request_id: str, db: Session = Depends(get_db)):
    """获取自进化请求的详细信息

    Args:
        request_id: 请求ID
        db: 数据库会话

    Returns:
        请求详情，包含计划、操作列表等；未找到则返回错误
    """
    req = db.get(SelfDevRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="请求不存在")

    ops = (
        db.query(PatchOperation)
        .filter(PatchOperation.request_id == request_id)
        .all()
    )

    return {
        "request_id": req.id,
        "trace_id": req.trace_id,
        "status": req.status,
        "goal": req.goal,
        "plan": req.plan,
        "operations": [
            {
                "operation": o.operation,
                "target_file": o.target_file,
                "reason": o.reason,
                "risk_notes": o.risk_notes,
                "requires_schema_change": o.requires_schema_change,
                "safe_delete_scope": o.safe_delete_scope,
                "not_allowed_yet": o.not_allowed_yet,
            }
            for o in ops
        ],
    }


# ===================== V13: 应用 / 升级 / 回滚 =====================

class ApplyRequest(BaseModel):
    """补丁应用请求体"""
    operations: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/{request_id}/apply-inactive")
def apply_inactive(
    request_id: str,
    req: ApplyRequest,
    db: Session = Depends(get_db),
    _authorized: None = Depends(require_trusted_core_admin),
):
    """将补丁应用到非激活槽位

    Args:
        request_id: 请求ID
        req: 包含 operations 的应用请求
        db: 数据库会话

    Returns:
        应用结果
    """
    sreq = db.get(SelfDevRequest, request_id)
    if not sreq:
        raise HTTPException(status_code=404, detail="请求不存在")

    from aiive.selfdev.patch_executor import PatchExecutor
    from aiive.selfdev.repair_guard import (
        apply_candidate_patch,
        finalize_candidate_patch,
    )
    from aiive.selfdev.trusted_core import validate_operations

    issues = validate_operations(req.operations)
    if issues:
        raise HTTPException(status_code=403, detail={"error": "trusted_core_policy_denied", "issues": issues})

    executor = PatchExecutor()
    # 与自主修复闭环共用同一把候选代码互斥锁：并发的修复流程不会互相覆盖。
    result = apply_candidate_patch(executor, req.operations)
    if result.get("error") == "concurrent_repair_in_progress":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "concurrent_repair_in_progress",
                "holder": result.get("holder") or {},
            },
        )

    applied = result.get("operations_applied", [])
    failed = result.get("operations_failed", [])
    if result.get("ok"):
        sreq.status = "applied_inactive"
    elif applied and failed:
        # 部分成功：区别于整体失败，便于人工介入判断
        sreq.status = "apply_partial"
    else:
        sreq.status = "apply_failed"

    # 应用成功（或部分成功）后自动运行定向测试，结果持久化到 plan JSON，
    # 供 promote 端点做晋升门禁。
    test_report = None
    if applied:
        from aiive.selfdev.targeted_test_runner import TargetedTestRunner

        changed_files = [
            str(op.get("target_file", ""))
            for op in req.operations
            if op.get("target_file")
        ]
        try:
            candidate_root = Path(str(result.get("candidate_root") or ""))
            test_report = TargetedTestRunner(repo_root=candidate_root).run_for_changed_files(changed_files)
        except Exception as e:
            test_report = {"ok": False, "error": f"targeted test runner crashed: {e}"}
        sreq.plan = {**(sreq.plan or {}), "targeted_test_report": test_report}

    # 候选状态定级：定向测试通过才登记为可晋升（并绑定代码指纹），
    # 否则显式标记不可晋升，避免失败代码被后续 promote 误用。
    from aiive.selfdev.repair_guard import CandidateSlotGuard

    tests_ok = isinstance(test_report, dict) and test_report.get("ok") is True
    result["candidate_state"] = finalize_candidate_patch(
        result,
        tests_ok=tests_ok,
        guard=CandidateSlotGuard(manager=getattr(executor, "_manager", None)),
        reason="targeted_tests_failed" if applied else "apply_failed",
    )

    db.commit()

    if test_report is not None:
        result = {**result, "targeted_test_report": test_report}
    result["request_status"] = sreq.status
    return result


class PromoteRequest(BaseModel):
    """升级请求体"""
    run_health_check: bool = True
    # 定向测试未通过时默认拒绝晋升；force=True 显式覆盖
    force: bool = False


@router.post("/{request_id}/promote")
def promote(
    request_id: str,
    req: PromoteRequest,
    db: Session = Depends(get_db),
    _authorized: None = Depends(require_trusted_core_admin),
):
    """将非激活槽位的变更升级为激活状态

    Args:
        request_id: 请求ID
        req: 升级请求，可指定是否运行健康检查
        db: 数据库会话

    Returns:
        升级结果
    """
    sreq = db.get(SelfDevRequest, request_id)
    if not sreq:
        raise HTTPException(status_code=404, detail="请求不存在")

    # 晋升门禁：apply-inactive 阶段的定向测试未通过时拒绝晋升（force 可覆盖）
    test_report = (sreq.plan or {}).get("targeted_test_report")
    if req.force:
        raise HTTPException(status_code=403, detail="trusted_core_test_override_disabled")
    if not isinstance(test_report, dict) or test_report.get("ok") is not True:
        return {
            "ok": False,
            "error": "targeted tests did not pass; promotion refused",
            "targeted_test_report": test_report,
        }

    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager

    # Trusted Core promotion 永远运行健康检查并进入自动回滚观察窗。
    # 启用 Supervisor 时，切换由守护进程执行（本进程随后会被替换）。
    result = ReleaseManager(client=supervisor_client_from_settings()).promote()

    if result.get("status") == "switch_requested":
        sreq.status = "promote_requested"
    else:
        sreq.status = "promoted" if result["ok"] else "promote_failed"
    db.commit()

    return result


@router.post("/{request_id}/rollback")
def rollback(
    request_id: str,
    db: Session = Depends(get_db),
    _authorized: None = Depends(require_trusted_core_admin),
):
    """回滚当前激活槽位到上一个版本

    Args:
        request_id: 请求ID
        db: 数据库会话

    Returns:
        回滚结果
    """
    sreq = db.get(SelfDevRequest, request_id)
    if not sreq:
        raise HTTPException(status_code=404, detail="请求不存在")

    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager

    result = ReleaseManager(client=supervisor_client_from_settings()).rollback()

    if result.get("status") == "switch_requested":
        sreq.status = "rollback_requested"
    else:
        sreq.status = "rolled_back" if result["ok"] else "rollback_failed"
    db.commit()

    return result
