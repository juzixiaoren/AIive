"""历史异常修复命令：清理旧 recompute 路径产生的 AttentionState 噪声记录。

背景：旧 `AttentionManager.recompute()` 每次调用无条件新增一条 AttentionState，
且 `query_attention` 工具曾把字面量 "query" 写入 focus_topic，污染注意力焦点。
主流程 `resolve_for_turn()` 的持久化不变量是"仅状态转换时写入"。

清理规则（不删除任何 Event / 对话数据）：
- 删除 focus_topic == "query" 的记录（工具字面量污染）；
- 按线程按时间序收敛：连续多条 (decision, suggestion, focus_topic) 完全相同的
  记录只保留最早一条，删除后续重复（recompute 无条件写入产生的噪声）。

用法：
    python -m scripts.repair_attention_noise [--dry-run]
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import AttentionState

logger = logging.getLogger(__name__)

# 旧 query_attention 工具写入的字面量焦点
POLLUTED_FOCUS_TOPIC = "query"


def repair(db: Session, dry_run: bool = False) -> dict[str, int]:
    """执行清理。返回统计。"""
    stats = {"polluted_deleted": 0, "duplicates_deleted": 0, "scanned": 0}

    # 第一步：删除焦点被字面量 "query" 污染的记录
    polluted = (
        db.query(AttentionState)
        .filter(AttentionState.focus_topic == POLLUTED_FOCUS_TOPIC)
        .all()
    )
    for state in polluted:
        db.delete(state)
        stats["polluted_deleted"] += 1
        logger.info(
            "删除污染记录: id=%s thread_id=%s created_at=%s",
            state.id, state.thread_id, state.created_at,
        )
    if polluted:
        db.flush()

    # 第二步：按线程收敛连续重复记录，仅保留每段状态的最早一条
    all_states = (
        db.query(AttentionState)
        .order_by(
            AttentionState.thread_id,
            AttentionState.created_at,
            AttentionState.id,
        )
        .all()
    )
    stats["scanned"] = len(all_states)
    previous_key: tuple[str, str, str | None, str | None] | None = None
    for state in all_states:
        key = (state.thread_id, state.decision, state.suggestion, state.focus_topic)
        if key == previous_key:
            db.delete(state)
            stats["duplicates_deleted"] += 1
            logger.info(
                "删除连续重复记录: id=%s thread_id=%s decision=%s created_at=%s",
                state.id, state.thread_id, state.decision, state.created_at,
            )
        else:
            previous_key = key

    if not dry_run:
        db.commit()
        logger.info("清理已提交: %s", stats)
    else:
        db.rollback()
        logger.info("DRY-RUN 完成，未提交: %s", stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="清理 AttentionState 噪声与污染记录")
    parser.add_argument("--dry-run", action="store_true", help="只统计不提交")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    db = SessionLocal()
    try:
        stats = repair(db, dry_run=args.dry_run)
        print(stats)
    finally:
        db.close()


if __name__ == "__main__":
    main()
