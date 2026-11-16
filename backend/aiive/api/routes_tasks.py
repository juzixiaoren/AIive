"""
API路由模块：任务管理
- 提供提醒（reminder）的创建、查询、取消与立即执行接口
- 历史 condition_watch / routine 行只能查询与取消；check-now 明确返回 unsupported_task_type
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.runtime.task_manager import TaskManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class CreateTaskRequest(BaseModel):
    """创建任务请求体。

    Task 表只承载可自动投递的提醒（reminder）；周期性与条件性需求由
    Persistent Task Runtime 的 Watcher / Goal Trigger 承担。
    """
    task_type: str = Field(..., description="仅支持 reminder")
    title: str = Field(..., min_length=1)
    description: str = ""
    condition: str | None = None


@router.post("/tasks")
def create_task(request: CreateTaskRequest, db: Session = Depends(get_db)):
    """创建新任务

    Args:
        request: 包含 task_type、title 等信息的请求体
        db: 数据库会话

    Returns:
        新创建任务的 id、title、status 和 task_type
    """
    if request.task_type != "reminder":
        raise HTTPException(
            status_code=422,
            detail="仅支持 reminder 任务；周期性或条件性任务请使用持久任务 Watcher 或长期目标",
        )
    try:
        mgr = TaskManager(db)
        from datetime import datetime, timezone

        task = mgr.create(
            task_type=request.task_type,
            title=request.title,
            description=request.description,
            condition=request.condition,
            next_check_at=datetime.now(timezone.utc),
        )
        db.commit()
        return {"id": task.id, "title": task.title, "status": task.status, "task_type": task.task_type}
    except Exception:
        logger.exception("创建任务失败")
        db.rollback()
        raise


@router.get("/tasks")
def list_tasks(status: str | None = Query(None), db: Session = Depends(get_db)):
    """获取任务列表，支持按状态过滤

    Args:
        status: 任务状态（可选）
        db: 数据库会话

    Returns:
        任务列表，包含类型、状态、标题、下次检查时间等
    """
    try:
        mgr = TaskManager(db)
        return [
            {
                "id": t.id,
                "task_type": t.task_type,
                "status": t.status,
                "title": t.title,
                "description": t.description,
                "next_check_at": t.next_check_at.isoformat() if t.next_check_at else None,
                "last_checked_at": t.last_checked_at.isoformat() if t.last_checked_at else None,
            }
            for t in mgr.list_all(status)
        ]
    except Exception:
        logger.exception("获取任务列表失败")
        raise


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    """取消指定任务及其尚未投递的提醒。"""
    try:
        result = TaskManager(db).cancel(task_id)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="任务不存在")
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        logger.exception("取消任务失败: task_id=%s", task_id)
        db.rollback()
        raise


@router.post("/tasks/{task_id}/check-now")
def check_task(task_id: str, db: Session = Depends(get_db)):
    """立即执行指定任务

    Args:
        task_id: 任务ID
        db: 数据库会话

    Returns:
        任务执行结果
    """
    try:
        result = TaskManager(db).check_now(task_id)
        db.commit()
        return result
    except Exception:
        logger.exception("立即执行任务失败: task_id=%s", task_id)
        db.rollback()
        raise
