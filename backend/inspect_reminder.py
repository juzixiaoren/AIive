"""只读：查看提醒任务当前状态（不改任何数据）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from aiive.config import settings
from aiive.db.models import Task

print("database_url:", settings.database_url)
engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine)
db = SessionLocal()

# 目标 task_id（用户之前给出的）
target = "b1f8db32-2e3e-4551-b096-d76028b270b8"

print("\n--- 指定 task_id ---")
t = db.query(Task).filter(Task.id == target).first()
if t:
    for k in ("id", "task_type", "status", "title", "next_check_at", "last_checked_at", "created_at"):
        print(f"  {k}: {getattr(t, k)}")
else:
    print("  未找到该 task_id")

print("\n--- 全部 reminder 类型任务 ---")
rows = db.query(Task).filter(Task.task_type == "reminder").order_by(Task.created_at.desc()).all()
for t in rows:
    print(f"  id={t.id} status={t.status} next_check_at={t.next_check_at} title={t.title!r}")

db.close()
