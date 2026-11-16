"""历史任务类型退役后的 HTTP 契约：保留数据，明确拒绝执行。"""

from datetime import datetime, timedelta, timezone

from aiive.api.routes_tasks import check_task
from aiive.db.models import OutboxJob, Task


def test_check_now_route_returns_unsupported_for_legacy_task(db_session):
    """POST /tasks/{id}/check-now 对历史旧类型必须明确拒绝，不得伪装成功。"""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    task = Task(task_type="routine", title="每日站会", condition="1440", next_check_at=past)
    db_session.add(task)
    db_session.commit()

    result = check_task(task.id, db_session)
    db_session.expire_all()

    assert result["ok"] is False
    assert result["status"] == "unsupported_task_type"
    assert result["task_type"] == "routine"
    assert db_session.get(Task, task.id).status == "pending"
    assert db_session.query(OutboxJob).count() == 0


def test_check_now_route_reports_missing_task(db_session):
    """不存在的任务仍是 not_found，与不支持类型区分。"""
    result = check_task("missing-task", db_session)

    assert result["ok"] is False
    assert result["status"] == "not_found"
