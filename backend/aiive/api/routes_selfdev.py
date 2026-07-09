from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.core.llm_client import LLMClient
from aiive.db.base import get_db
from aiive.db.models import SelfDevRequest, PatchOperation
from aiive.runtime.trace import Trace
from aiive.selfdev.planner import SelfDevPlanner
from aiive.supervisor.launcher import Launcher

router = APIRouter(prefix="/api/selfdev")


class PlanRequest(BaseModel):
    goal: str = Field(..., min_length=1)


# ===================== V11: Slots =====================

@router.get("/slots")
def list_slots():
    launcher = Launcher()
    return launcher.get_status()


@router.post("/slots/health-check")
def health_check():
    launcher = Launcher()
    return launcher.health_check()


# ===================== V12: Self-Dev Plan =====================

@router.post("/plan")
def create_plan(request: PlanRequest, db: Session = Depends(get_db)):
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
            not_allowed_yet=op.get("not_allowed_yet", True),
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
    req = db.get(SelfDevRequest, request_id)
    if not req:
        return {"error": "not found"}

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


# ===================== V13: Apply / Promote / Rollback =====================

class ApplyRequest(BaseModel):
    operations: list[dict] = Field(default_factory=list)


@router.post("/{request_id}/apply-inactive")
def apply_inactive(request_id: str, req: ApplyRequest, db: Session = Depends(get_db)):
    sreq = db.get(SelfDevRequest, request_id)
    if not sreq:
        return {"ok": False, "error": "Request not found"}

    from aiive.selfdev.patch_executor import PatchExecutor

    executor = PatchExecutor()
    result = executor.apply_to_inactive(req.operations)

    sreq.status = "applied_inactive" if result["ok"] else "apply_failed"
    db.commit()

    return result


class PromoteRequest(BaseModel):
    run_health_check: bool = True


@router.post("/{request_id}/promote")
def promote(request_id: str, req: PromoteRequest, db: Session = Depends(get_db)):
    sreq = db.get(SelfDevRequest, request_id)
    if not sreq:
        return {"ok": False, "error": "Request not found"}

    from aiive.selfdev.promote_rollback import PromoteRollback

    pr = PromoteRollback()
    result = pr.promote()

    sreq.status = "promoted" if result["ok"] else "promote_failed"
    db.commit()

    return result


@router.post("/{request_id}/rollback")
def rollback(request_id: str, db: Session = Depends(get_db)):
    sreq = db.get(SelfDevRequest, request_id)
    if not sreq:
        return {"ok": False, "error": "Request not found"}

    from aiive.selfdev.promote_rollback import PromoteRollback

    active = PromoteRollback()._manager.get_active_slot()
    previous = "B" if active == "A" else "A"

    pr = PromoteRollback()
    result = pr.rollback(previous)

    sreq.status = "rolled_back" if result["ok"] else "rollback_failed"
    db.commit()

    return result
