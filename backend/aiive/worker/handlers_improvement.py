"""改进候选发现的 Outbox Handler。

约定与既有 Handler 一致：Handler 本身不持有数据库会话，模型调用发生在事务之外
（ImprovementDiscovery 内部按"短事务读 → 模型 → 短事务写"分段），因此模型响应慢
不会长期占着数据库事务。失败按 Outbox 既有语义重试或进入死信。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult

logger = logging.getLogger(__name__)

JOB_IMPROVEMENT_DISCOVERY = "improvement_discovery"


def handle_improvement_discovery(
    claimed: ClaimedJob, *, discovery: Any = None
) -> HandlerResult:
    """扫描新运行证据，必要时调用一次模型生成改进候选。

    discovery 仅用于测试注入；生产路径由本 Handler 按当前配置构造。
    """
    from aiive.core.llm_client import default_llm_client
    from aiive.selfdev.improvement_discovery import (
        RUN_STATUS_INVALID_OUTPUT,
        RUN_STATUS_LLM_FAILED,
        ImprovementDiscovery,
    )

    if discovery is None:
        discovery = ImprovementDiscovery(default_llm_client())
    try:
        summary = discovery.run(trace_id=claimed.trace_id or None)
    except Exception as exc:
        logger.exception("改进候选发现失败")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(exc))

    detail = json.dumps(
        {
            "status": summary.get("status"),
            "llm_called": summary.get("llm_called"),
            "candidate_ids": summary.get("candidate_ids"),
            "group_count": summary.get("group_count"),
            "evidence_count": summary.get("evidence_count"),
        },
        ensure_ascii=False,
    )
    status = summary.get("status")
    if status == RUN_STATUS_LLM_FAILED:
        # 模型不可用：不推进水位，交由 Worker 重试同一批证据。
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, detail)
    if status == RUN_STATUS_INVALID_OUTPUT:
        # 模型输出不可用：本轮不产生候选、不推进水位；不重试同一份坏输出。
        logger.warning("改进候选模型输出非法，已跳过本轮: %s", summary.get("rejected"))
        return HandlerResult(HandlerOutcome.COMPLETED, detail)
    return HandlerResult(HandlerOutcome.COMPLETED, detail)
