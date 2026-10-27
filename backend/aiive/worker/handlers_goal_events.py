"""goal_event_dispatch Handler：把已投递的真实运行事件匹配到活动目标计划。

事务边界：
1. 从 Outbox 载荷还原最小事件视图（不完整/未知类型 → 不可重试）；
2. 确定性匹配活动计划并按目标逐个提交（无匹配时不产生任何模型调用）；
3. 失败交给 Outbox 重试；重复投递由 operation_id 与计划内的 last_trigger_key 去重。
"""
from __future__ import annotations

import logging

from aiive.db.base import SessionLocal
from aiive.task_runtime.goal_events import event_from_job_payload
from aiive.task_runtime.goal_trigger_service import GoalTriggerService
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)


def handle_goal_event_dispatch(claimed: ClaimedJob) -> HandlerResult:
    event = event_from_job_payload(dict(claimed.payload or {}))
    if event is None:
        return HandlerResult(
            HandlerOutcome.NON_RETRYABLE,
            "goal_event_dispatch 载荷不完整或事件类型未知",
            terminal_reason="invalid_goal_event_payload",
        )

    db = SessionLocal()
    try:
        fired = GoalTriggerService(db).dispatch_event(event)
        db.commit()
    except Exception:
        # 未提交的写入由 finally 的 close() 回滚；显式 rollback 会连带回滚
        # 调用方（含测试夹具）尚未提交的事务，这里只负责记录并交给 Outbox 重试。
        logger.exception(
            "目标事件匹配异常: event=%s source=%s", event.event_type, event.source,
        )
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, "goal_event_dispatch_failed")
    finally:
        db.close()

    # fired=0 表示没有订阅计划或条件不匹配：这是正常结果，不调用模型。
    return HandlerResult(HandlerOutcome.COMPLETED, f"goal_event_matched:{fired}")
