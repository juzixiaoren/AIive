"""
数据修复脚本：检测并报告 thread_id 相关的数据完整性问题。

检测项：
1. tasks.thread_id IS NULL
2. events.thread_id 为空或非法
3. memory write 没有对应 event 的记录

用法: python3 -m scripts.repair_thread_data
"""
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiive.db.base import SessionLocal
from aiive.db.models import Task, Event, Memory
from aiive.runtime.thread_bootstrap import ThreadBootstrapService


def check_tasks_null_thread():
    """检测 tasks 表中 thread_id 为 NULL 的记录。"""
    db = SessionLocal()
    try:
        null_tasks = db.query(Task).filter(Task.thread_id.is_(None)).all()
        if null_tasks:
            print(f"[WARN] {len(null_tasks)} 条 task 的 thread_id 为 NULL:")
            for t in null_tasks[:10]:
                print(f"  task_id={t.id}  title={t.title[:50]}  status={t.status}")
            if len(null_tasks) > 10:
                print(f"  ... 还有 {len(null_tasks) - 10} 条")
            print("  建议：运行 repair 模式自动填充 system 线程")
        else:
            print("[OK] tasks.thread_id 无 NULL 记录")
        return null_tasks
    finally:
        db.close()


def check_events_empty_thread():
    """检测 events 表中 thread_id 为空字符串的记录。"""
    db = SessionLocal()
    try:
        empty_events = db.query(Event).filter(Event.thread_id == "").all()
        if empty_events:
            print(f"[WARN] {len(empty_events)} 条 event 的 thread_id 为空字符串:")
            for e in empty_events[:10]:
                print(f"  event_id={e.id}  type={e.event_type}  trace_id={e.trace_id}")
            if len(empty_events) > 10:
                print(f"  ... 还有 {len(empty_events) - 10} 条")
        else:
            print("[OK] events.thread_id 无空字符串记录")
        return empty_events
    finally:
        db.close()


def check_memory_without_event():
    """检测记忆记录是否有对应的事件。"""
    db = SessionLocal()
    try:
        memories = db.query(Memory).filter(Memory.lifecycle_state == "active").all()
        missing = []
        for m in memories:
            event = db.query(Event).filter(
                Event.payload["memory_id"].as_string() == m.id
            ).first()
            if not event:
                missing.append(m)
        if missing:
            print(f"[WARN] {len(missing)} 条活跃记忆缺少 memory.created 事件")
        else:
            print("[OK] 所有活跃记忆都有对应事件")
        return missing
    except Exception as e:
        print(f"[SKIP] 记忆事件检查失败: {e}")
        return []
    finally:
        db.close()


def repair_null_thread_tasks():
    """将 task.thread_id IS NULL 的记录填充为 system 线程（迁移兼容）。"""
    db = SessionLocal()
    try:
        # 确保 system 线程存在
        ThreadBootstrapService.ensure_system_thread()
        null_tasks = db.query(Task).filter(Task.thread_id.is_(None)).all()
        if not null_tasks:
            print("[INFO] 无需修复：tasks.thread_id 无 NULL 记录")
            return
        for t in null_tasks:
            t.thread_id = "system"
            print(f"  修复 task_id={t.id} thread_id -> system")
        db.commit()
        print(f"[DONE] 已修复 {len(null_tasks)} 条记录")
    except Exception as e:
        db.rollback()
        print(f"[ERROR] 修复失败: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Thread 数据完整性检测与修复")
    parser.add_argument("--repair", action="store_true", help="执行修复（填充 task.thread_id NULL）")
    args = parser.parse_args()

    print("=" * 50)
    print(f"数据完整性检查 - {datetime.now(timezone.utc).isoformat()}")
    print("=" * 50)

    check_tasks_null_thread()
    check_events_empty_thread()
    check_memory_without_event()

    if args.repair:
        print("\n--- 执行修复 ---")
        repair_null_thread_tasks()
    else:
        print("\n[INFO] 使用 --repair 参数执行自动修复")
