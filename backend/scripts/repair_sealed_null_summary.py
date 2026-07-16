"""L.4 历史异常修复命令：处理 sealed 但 summary_id IS NULL 的 Segment。

规则（不调用 LLM、不伪造 Summary、不删除原始数据）：
- 仅当同时满足以下全部条件才恢复为 open：
    * 属于当前 active Epoch
    * 是 Thread 最新 Segment（segment_no / created_at 最大）
    * 不存在更新（更高 epoch_no）的 Epoch
    * 不存在其他 status="open" / "sealing" 的 Segment
    * 不存在比该 Segment 更大 turn_sequence 的 Turn
- 其余异常 Segment 一律转为 status="sealing"，并构建完整 CompactionInput +
  补发 segment_sealing OutboxJob，由正常 Handler 生成 Summary。
- 旧（archived）Epoch 的 Segment 不回退 open，只转 sealing。

用法：
    python -m scripts.repair_sealed_null_summary [--dry-run]
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import Epoch, Segment, TurnRecord, WorkingState
from aiive.runtime.epoch_manager import EpochManager

logger = logging.getLogger(__name__)


def _can_restore_open(db: Session, seg: Segment) -> bool:
    """判断该 sealed+NULL Segment 是否满足恢复为 open 的全部条件。"""
    thread_id = seg.thread_id

    active_epoch = db.query(Epoch).filter(
        Epoch.thread_id == thread_id, Epoch.status == "active",
    ).first()
    if active_epoch is None:
        return False  # 旧 archived Epoch，不回退 open

    # 最新 Segment
    latest = (
        db.query(Segment)
        .filter(Segment.thread_id == thread_id)
        .order_by(Segment.segment_no.desc(), Segment.created_at.desc())
        .first()
    )
    if latest is None or latest.id != seg.id:
        return False

    # 不存在更高 epoch_no 的 Epoch
    seg_epoch = db.query(Epoch).filter(Epoch.id == seg.epoch_id).first()
    seg_epoch_no = seg_epoch.epoch_no if seg_epoch else -1
    newer = db.query(Epoch).filter(
        Epoch.thread_id == thread_id, Epoch.epoch_no > seg_epoch_no,
    ).first()
    if newer is not None:
        return False

    # 不存在其他 open / sealing Segment
    others = db.query(Segment).filter(
        Segment.thread_id == thread_id,
        Segment.status.in_(["open", "sealing"]),
        Segment.id != seg.id,
    ).first()
    if others is not None:
        return False

    # 不存在比该 Segment 更大 turn_sequence 的 Turn
    max_turn = db.query(func.max(TurnRecord.turn_sequence)).filter(
        TurnRecord.thread_id == thread_id,
    ).scalar() or 0
    seg_max = seg.end_turn_sequence or 0
    if max_turn > seg_max:
        return False

    return True


def repair(db: Session, dry_run: bool = False) -> dict[str, int]:
    """执行修复。返回统计。"""
    bad = db.query(Segment).filter(
        Segment.status == "sealed", Segment.summary_id.is_(None),
    ).all()
    if not bad:
        logger.info("无需修复：未发现 sealed+NULL summary 的 Segment")
        return {"open": 0, "sealing": 0, "scanned": 0}

    logger.warning("发现 %d 个异常 sealed+NULL Segment，开始修复", len(bad))
    stats = {"open": 0, "sealing": 0, "scanned": len(bad)}
    mgr = EpochManager()

    for seg in bad:
        if _can_restore_open(db, seg):
            seg.status = "open"
            seg.sealed_at = None
            stats["open"] += 1
            logger.info("恢复为 open: segment_id=%s", seg.id)
        else:
            ws = db.query(WorkingState).filter(
                WorkingState.thread_id == seg.thread_id,
            ).first()
            # 转 sealing 并构建完整 CompactionInput + 补发 Job
            try:
                mgr.freeze_segment_for_sealing(
                    db, seg.thread_id, seg,
                    create_successor=False, boundary_working_state=ws,
                )
                seg.status = "sealing"
                stats["sealing"] += 1
                logger.info("转 sealing 并补建 CompactionInput/Job: segment_id=%s", seg.id)
            except Exception:
                logger.exception("freeze 失败，跳过 segment_id=%s", seg.id)

    if not dry_run:
        db.commit()
        logger.info("修复已提交: %s", stats)
    else:
        db.rollback()
        logger.info("DRY-RUN 完成，未提交: %s", stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="修复 sealed+NULL summary 的历史 Segment")
    parser.add_argument("--dry-run", action="store_true", help="只统计不提交")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stats = repair(db, dry_run=args.dry_run)
        print(stats)
    finally:
        db.close()


if __name__ == "__main__":
    main()
