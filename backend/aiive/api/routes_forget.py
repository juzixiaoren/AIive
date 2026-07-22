"""Phase 6A forget API 路由。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from aiive.forget.selector_normalizer import MAX_INLINE_SHIELD_TARGETS
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
        """校验选择器组合与时间范围。"""
        if bool(self.scope_type) != bool(self.scope_id):
            raise ValueError("scope_type 与 scope_id 必须同时提供")
        if self.time_from and self.time_to and self.time_from > self.time_to:
            raise ValueError("time_from 不能晚于 time_to")
        has_selector = bool(
            self.memory_ids
            or self.turn_ids
            or self.event_ids
            or self.thread_id
            or self.canonical_key
            or (self.scope_type and self.scope_id)
            or self.all_user_data
            or self.time_from
            or self.time_to
        )
        if not has_selector:
            raise ValueError("至少需要提供一种遗忘选择器")
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
