"""Phase 6A forget API 路由。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from aiive.forget.selector_normalizer import (
    MAX_INLINE_SHIELD_TARGETS,
    validate_selector_combination,
)
from aiive.tools.forget_tool import handle_forget, handle_forget_status

router = APIRouter(prefix="/api/forget", tags=["forget"])


class ForgetRequest(BaseModel):
    """结构化遗忘请求；敏感选择器只允许通过 JSON Body 传递。"""

    mode: Literal["memory_only", "history_only", "everywhere"] = "everywhere"
    memory_ids: list[str] = Field(default_factory=list, max_length=MAX_INLINE_SHIELD_TARGETS)
    turn_ids: list[str] = Field(default_factory=list, max_length=MAX_INLINE_SHIELD_TARGETS)
    event_ids: list[str] = Field(default_factory=list, max_length=MAX_INLINE_SHIELD_TARGETS)
    thread_id: str = Field(default="", max_length=36)
    canonical_key: str = Field(default="", max_length=256)
    scope_type: str = Field(default="", max_length=32)
    scope_id: str = Field(default="", max_length=128)
    all_user_data: bool = False
    time_from: datetime | None = None
    time_to: datetime | None = None
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_selector(self) -> "ForgetRequest":
        """校验 Mode / Selector 兼容矩阵与时间范围（与 Tool / Phase A 共用规则）。"""
        validate_selector_combination(
            self.mode,
            memory_ids=self.memory_ids,
            turn_ids=self.turn_ids,
            event_ids=self.event_ids,
            thread_id=self.thread_id or None,
            canonical_key=self.canonical_key or None,
            scope_type=self.scope_type or None,
            scope_id=self.scope_id or None,
            time_from=self.time_from,
            time_to=self.time_to,
            all_user_data=self.all_user_data,
        )
        return self


@router.post("")
def forget_endpoint(request: ForgetRequest):
    """执行 Phase 6A Forget Saga；Phase A 成功后才返回。"""
    result = handle_forget(
        mode=request.mode,
        memory_ids=request.memory_ids or None,
        turn_ids=request.turn_ids or None,
        event_ids=request.event_ids or None,
        thread_id=request.thread_id,
        canonical_key=request.canonical_key,
        scope_type=request.scope_type,
        scope_id=request.scope_id,
        all_user_data=request.all_user_data,
        time_from=request.time_from,
        time_to=request.time_to,
        reason=request.reason,
        requested_by=str(uuid.uuid4()),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "forget failed"))
    return result


@router.get("/{operation_key}/status")
def forget_status_endpoint(operation_key: str):
    """查询 forget Operation 的各阶段进度。"""
    result = handle_forget_status(operation_key=operation_key)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    return result
