"""
API路由模块：自进化开发（Self-Dev）
- 提供代码槽位（Slots）状态查询和健康检查
- 提供自进化计划（Plan）的创建和查询
- 提供补丁应用、升级和回滚操作的接口
"""
from fastapi import APIRouter, Depends, HTTPException
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


class PlanRequest(BaseModel):
    """自进化计划请求体"""
    goal: str = Field(..., min_length=1)


# ===================== V11: 槽位管理 =====================

@router.get("/slots")
def list_slots():
    """获取当前代码槽位状态

    Returns:
        各槽位的激活状态和相关信息
    """
    launcher = Launcher()
    return launcher.get_status()


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
    client = LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )
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
def apply_inactive(request_id: str, req: ApplyRequest, db: Session = Depends(get_db)):
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

    executor = PatchExecutor()
    result = executor.apply_to_inactive(req.operations)

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
            test_report = TargetedTestRunner().run_for_changed_files(changed_files)
        except Exception as e:
            test_report = {"ok": False, "error": f"targeted test runner crashed: {e}"}
        sreq.plan = {**(sreq.plan or {}), "targeted_test_report": test_report}

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
def promote(request_id: str, req: PromoteRequest, db: Session = Depends(get_db)):
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
    if (
        not req.force
        and isinstance(test_report, dict)
        and test_report.get("ok") is not True
    ):
        return {
            "ok": False,
            "error": "targeted tests did not pass; promotion refused (use force=true to override)",
            "targeted_test_report": test_report,
        }

    from aiive.selfdev.promote_rollback import PromoteRollback

    pr = PromoteRollback()
    result = pr.promote(run_health_check=req.run_health_check)

    sreq.status = "promoted" if result["ok"] else "promote_failed"
    db.commit()

    return result


@router.post("/{request_id}/rollback")
def rollback(request_id: str, db: Session = Depends(get_db)):
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

    from aiive.selfdev.promote_rollback import PromoteRollback

    active = PromoteRollback().get_active_slot()
    previous = "B" if active == "A" else "A"

    pr = PromoteRollback()
    result = pr.rollback(previous)

    sreq.status = "rolled_back" if result["ok"] else "rollback_failed"
    db.commit()

    return result
