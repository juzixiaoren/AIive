"""Domain tool handlers (implementation) split from builtin_tools."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.context.run_context import RunContext
from aiive.runtime.working_state import WorkingStateService
from aiive.tools.builtin_tools import db_handler, require_ctx


@db_handler
def handle_query_rhythm(db: Session):
    """获取每日节奏摘要。"""
    from aiive.runtime.rhythm_manager import RhythmManager
    return RhythmManager(db).daily_summary()

@db_handler
def handle_query_attention(db: Session, ctx: RunContext | None, thread_id: str = ""):
    """只读查询当前注意力状态与空闲评估，不写入新的 AttentionState。"""
    from aiive.runtime.attention_manager import AttentionManager
    effective_thread_id = thread_id or (ctx.thread_id if ctx else "")
    exclude_turn_id = ctx.turn_id if ctx else ""
    return AttentionManager(db).inspect(effective_thread_id, exclude_turn_id=exclude_turn_id)

@db_handler
def handle_update_working_state(
    db: Session, ctx: RunContext | None,
    field: str, operation: str = "set",
    payload: dict[str, Any] | None = None, idempotency_key: str = "",
):
    """显式维护 WorkingState 的语义字段。

    仅用于 current_objective / open_loops / active_constraints 三种语义字段。
    确定性字段（pending_approvals / artifact_refs / verified_tool_states /
    uncommitted_side_effects / running_tool_state）由 Turn 生命周期自动维护，
    不通过此工具修改。

    idempotency_key 用于防止同一更新被重复应用（崩溃重试场景）。
    """
    ctx = require_ctx(ctx, "update_working_state")
    payload = payload or {}
    ws_service = WorkingStateService()
    ws = ws_service.get_or_create(db, ctx.thread_id)

    # 幂等：同一 idempotency_key 仅应用一次
    applied_keys = list(ws.applied_idempotency_keys or [])
    if idempotency_key and idempotency_key in applied_keys:
        return {"ok": True, "applied": False, "reason": "idempotent_skip", "field": field}

    if field == "current_objective":
        ws.current_objective = payload.get("value", "")
        # 统一版本簿记：current_objective 分支也 bump version/updated_at
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)
    elif field in ("open_loops", "active_constraints"):
        # update_semantic_field 内部已 bump version/updated_at，此处不再重复
        ws_service.update_semantic_field(
            db, ctx.thread_id, field, operation, payload,
            ctx.turn_id or "", idempotency_key or "",
        )
    else:
        return {"ok": False, "error": f"unsupported field: {field}"}

    if idempotency_key:
        applied_keys.append(idempotency_key)
        # 滑动窗口上限：仅保留最近 200 个幂等键，防止无限增长
        if len(applied_keys) > 200:
            applied_keys = applied_keys[-200:]
        ws.applied_idempotency_keys = applied_keys

    return {"ok": True, "applied": True, "field": field}
