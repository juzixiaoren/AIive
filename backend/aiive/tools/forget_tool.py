"""Phase 6A forget 工具处理函数。

提供两个工具:
  - forget:         执行 Phase A 立即屏蔽，创建 ForgetOperation → Shield → Tombstone → Cascade
  - forget_status:  查询 forget Operation 的各阶段状态
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiive.context.run_context import RunContext
from aiive.db.base import SessionLocal
from aiive.db.forget_models import (
    ForgetAction,
    ForgetBatch,
    ForgetOperation,
    ForgetStageRun,
    ForgetTarget,
)
from aiive.forget.phase_a_shield import execute_phase_a_shield
from aiive.forget.selector_normalizer import MAX_INLINE_SHIELD_TARGETS

logger = logging.getLogger(__name__)


def _new_api_id() -> str:
    """无 RunContext 时生成一次性请求标识（仅用于审计溯源）。"""
    import uuid

    return str(uuid.uuid4())

# forget 模式说明（返回给用户）
MODE_EXPLANATIONS = {
    "memory_only": (
        "长期记忆已删除（CoreMemory / 检索索引已清理）。"
        "原始聊天记录仍可通过显式历史审计模式查看，但不会进入自动上下文。"
    ),
    "history_only": (
        "指定历史 Turn/Event 及其派生记忆已删除。"
        "受影响 Summary/Checkpoint 将由 Cascade 阶段重建。"
    ),
    "everywhere": (
        "长期记忆 + 原始聊天 + 派生 Summary/Checkpoint/索引已全部进入 Forget Saga。"
        "Phase A 屏蔽已完成，异步清理在后台进行。"
    ),
}


# ── forget 工具 ──


def handle_forget(
    mode: str = "everywhere",
    memory_ids: list[str] | None = None,
    turn_ids: list[str] | None = None,
    event_ids: list[str] | None = None,
    thread_id: str = "",
    canonical_key: str = "",
    scope_type: str = "",
    scope_id: str = "",
    all_user_data: bool = False,
    reason: str = "",
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    requested_by: str = "",
    ctx: RunContext | None = None,
) -> dict[str, Any]:
    """执行 Phase 6A Forget Saga — Phase A 立即屏蔽。

    mode: memory_only / history_only / everywhere
    memory_ids / turn_ids / event_ids: 精确目标 ID 列表
    canonical_key: 按内容键全删
    thread_id / time_range / scope / all_user_data: 宽选择器

    ctx 与 requested_by 二选一：Chat/Graph 调用注入 RunContext；纯 API
    调用无 RunContext，通过 requested_by 标识来源（如 "api"）。
    """
    # 优先使用 RunContext 的 trace_id；否则回退到 requested_by；再否则生成一次性标识
    effective_requested_by = (ctx.trace_id if ctx else None) or requested_by or f"forget:{_new_api_id()}"

    # 参数校验
    has_explicit = bool(memory_ids or turn_ids or event_ids)
    has_wide = bool(thread_id or canonical_key or (scope_type and scope_id) or all_user_data)

    if not has_explicit and not has_wide:
        return {"ok": False, "error": "至少需要指定目标: memory_ids / turn_ids / event_ids / thread_id / canonical_key / scope / all_user_data"}

    if mode not in ("memory_only", "history_only", "everywhere"):
        return {"ok": False, "error": f"mode 必须是 memory_only / history_only / everywhere，收到: {mode}"}

    # 超限预警
    total_ids = len(memory_ids or []) + len(turn_ids or []) + len(event_ids or [])
    if total_ids > MAX_INLINE_SHIELD_TARGETS:
        logger.warning(
            "forget 超限: total_ids=%d > MAX_INLINE_SHIELD_TARGETS=%d，退化为 selector Shield",
            total_ids, MAX_INLINE_SHIELD_TARGETS,
        )

    db = SessionLocal()
    try:
        result = execute_phase_a_shield(
            session=db,
            mode=mode,
            memory_ids=memory_ids,
            turn_ids=turn_ids,
            event_ids=event_ids,
            thread_id=thread_id if thread_id else None,
            canonical_key=canonical_key if canonical_key else None,
            scope_type=scope_type if scope_type else None,
            scope_id=scope_id if scope_id else None,
            time_from=time_from,
            time_to=time_to,
            all_user_data=all_user_data,
            reason=reason,
            requested_by=effective_requested_by,
        )
        db.commit()

        explanation = MODE_EXPLANATIONS.get(mode, "")
        return {
            "ok": True,
            "operation_key": result["operation_key"],
            "status": result["status"],
            "shielded_at": result["shielded_at"],
            "target_count": result["target_count"],
            "mode": mode,
            "note": explanation,
        }
    except Exception as exc:
        db.rollback()
        logger.exception("forget tool failed")
        return {"ok": False, "error": f"Forget Phase A 失败: {exc}"}
    finally:
        db.close()


# ── forget_status 工具 ──


def handle_forget_status(
    operation_key: str = "",
    _ctx: RunContext | None = None,
) -> dict[str, Any]:
    """查询 forget Operation 的各阶段进度。

    返回 stages: 每个 stage 的 status、batch 进度、action 计数。
    不返回已 scrub 的原文。
    """
    if not operation_key:
        return {"ok": False, "error": "operation_key is required"}

    db = SessionLocal()
    try:
        op = db.query(ForgetOperation).filter_by(operation_key=operation_key).first()
        if op is None:
            return {"ok": False, "error": f"Operation not found: {operation_key}"}

        # 各阶段 stage_run 状态
        stage_runs = (
            db.query(ForgetStageRun)
            .filter_by(forget_operation_id=op.id)
            .order_by(ForgetStageRun.created_at.asc())
            .all()
        )
        stages = [
            {
                "stage": sr.stage,
                "status": sr.status,
                "failure_count": sr.failure_count,
            }
            for sr in stage_runs
        ]

        # batch 进度
        batches = (
            db.query(ForgetBatch)
            .filter_by(forget_operation_id=op.id)
            .order_by(ForgetBatch.batch_no.asc())
            .all()
        )
        batch_info = [
            {
                "stage": b.stage,
                "batch_no": b.batch_no,
                "status": b.status,
                "action_count": b.action_count,
                "completed_count": b.completed_count,
            }
            for b in batches
        ]

        # action 汇总
        action_count = (
            db.query(ForgetAction)
            .filter_by(forget_operation_id=op.id)
            .count()
        )
        action_done = (
            db.query(ForgetAction)
            .filter_by(forget_operation_id=op.id, status="done")
            .count()
        )

        # target 计数
        target_count = (
            db.query(ForgetTarget)
            .filter_by(forget_operation_id=op.id)
            .count()
        )

        return {
            "ok": True,
            "operation_key": op.operation_key,
            "mode": op.mode,
            "status": op.status,
            "shielded_at": op.shielded_at.isoformat() if op.shielded_at else None,
            "verified_at": op.verified_at.isoformat() if op.verified_at else None,
            "purged_at": op.purged_at.isoformat() if op.purged_at else None,
            "target_count": target_count,
            "stages": stages,
            "batches": batch_info,
            "actions_total": action_count,
            "actions_done": action_done,
            "error_message": op.error_message,
        }
    finally:
        db.close()
