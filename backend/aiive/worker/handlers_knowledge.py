"""知识库维护 OutboxJob Handler。

处理流程：
1. 只读扫描知识库一致性（孤儿分块 / 失效分块 / 缺失分块 / 对象引用 / 孤儿对象）
2. 按批执行幂等清理，每批 commit，失败保留可恢复状态
3. 无可自动修复项时返回 COMPLETED（剩余问题留在报告里），不再空转
"""

from __future__ import annotations

import logging

from aiive.db.base import SessionLocal
from aiive.knowledge.maintenance import (
    DEFAULT_BATCH_LIMIT,
    KnowledgeMaintenanceReport,
    apply_report,
    scan_knowledge_base,
)
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

MAX_MAINTENANCE_BATCHES = 5  # 单次 poll 最多扫描-清理轮次


def handle_knowledge_maintenance(claimed: ClaimedJob) -> HandlerResult:
    """处理 knowledge_maintenance OutboxJob（分批、幂等、可中断续跑）。"""
    operation_id = claimed.payload.get("operation_id", "")
    if not operation_id:
        return HandlerResult(
            outcome=HandlerOutcome.NON_RETRYABLE,
            reason="payload 缺少 operation_id",
        )

    db = SessionLocal()
    totals: dict[str, int] = {}
    try:
        for _ in range(MAX_MAINTENANCE_BATCHES):
            report = scan_knowledge_base(db)
            if report.healthy:
                db.commit()
                return HandlerResult(
                    outcome=HandlerOutcome.COMPLETED,
                    reason=_summary(totals, report),
                )
            actions = apply_report(db, report, limit=DEFAULT_BATCH_LIMIT)
            db.commit()
            for key, value in actions.items():
                if key in {"changed", "report"}:
                    continue
                totals[key] = totals.get(key, 0) + int(value)
            if not actions["changed"]:
                # 没有可自动修复的进展（例如重建持续失败）：保留问题，交回报告
                logger.warning(
                    "知识库维护无进展，保留剩余问题：%s",
                    [issue.to_dict() for issue in report.issues if issue.count],
                )
                return HandlerResult(
                    outcome=HandlerOutcome.COMPLETED,
                    reason=_summary(totals, report),
                    terminal_reason="knowledge_maintenance_partial",
                )
        return HandlerResult(
            outcome=HandlerOutcome.CONTINUE,
            reason=f"单次 poll 达到 {MAX_MAINTENANCE_BATCHES} 批上限",
        )
    except Exception as exc:
        db.rollback()
        logger.exception("knowledge_maintenance 处理异常: %s", exc)
        return HandlerResult(
            outcome=HandlerOutcome.RETRYABLE_ERROR,
            reason=str(exc),
        )
    finally:
        db.close()


def _summary(totals: dict[str, int], report: KnowledgeMaintenanceReport) -> str:
    """拼装可读的维护结果摘要。"""
    changed = [f"{key}={value}" for key, value in sorted(totals.items()) if value]
    if report.healthy:
        head = "知识库一致"
    else:
        head = f"知识库维护部分完成，剩余 {report.repairable} 项"
    if not changed:
        return head
    return head + "；" + "，".join(changed)
