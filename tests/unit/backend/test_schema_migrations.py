"""ORM 与 Alembic 最终数据库结构契约测试。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, create_engine, inspect

from aiive.config import settings
from aiive.db.models import Base, MemoryRecord


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _upgrade_empty_database(monkeypatch, tmp_path):
    """把独立空 SQLite 数据库升级到当前 Alembic head。"""
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    return create_engine(database_url)


def test_empty_database_upgrade_head_matches_memory_sensitivity_contract(
    monkeypatch, tmp_path,
):
    """空库升级后 sensitivity 必须与 ORM 的最终约束一致。"""
    engine = _upgrade_empty_database(monkeypatch, tmp_path)
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("memory_records")}
        sensitivity = columns["sensitivity"]
        constraints = {
            constraint["name"]: constraint
            for constraint in inspector.get_check_constraints("memory_records")
        }
        orm_column = MemoryRecord.__table__.c.sensitivity
        orm_constraints = {
            constraint.name
            for constraint in MemoryRecord.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        }

        assert sensitivity["nullable"] is orm_column.nullable is False
        assert str(sensitivity["default"]).strip("'\"") == "normal"
        assert "ck_memory_records_sensitivity" in constraints
        assert "ck_memory_records_sensitivity" in orm_constraints
    finally:
        engine.dispose()


def test_upgrade_head_removes_legacy_forget_requests(monkeypatch, tmp_path):
    """最终 schema 与 ORM metadata 都不得再包含旧遗忘请求表。"""
    engine = _upgrade_empty_database(monkeypatch, tmp_path)
    try:
        assert "forget_requests" not in inspect(engine).get_table_names()
        assert "forget_requests" not in Base.metadata.tables
    finally:
        engine.dispose()


def test_upgrade_head_contains_desktop_node_contract(monkeypatch, tmp_path):
    """空库迁移后应包含 Desktop Node、在线租约和线程绑定结构。"""
    engine = _upgrade_empty_database(monkeypatch, tmp_path)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"desktop_nodes", "thread_desktop_bindings"} <= tables
        node_columns = {column["name"] for column in inspector.get_columns("desktop_nodes")}
        assert {
            "id", "capabilities", "status", "last_seen_at", "lease_expires_at",
        } <= node_columns
    finally:
        engine.dispose()


def test_upgrade_head_contains_persistent_agent_task_contract(monkeypatch, tmp_path):
    """P0-P8 任务运行时表、统一审批身份和对账投影必须由 Alembic 创建。"""
    engine = _upgrade_empty_database(monkeypatch, tmp_path)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "agent_tasks", "agent_runs", "agent_actions", "agent_task_events",
            "agent_task_checkpoints", "task_evidence", "task_artifacts",
            "task_resource_locks", "agent_task_watches", "desktop_action_receipts",
        } <= tables
        approval_columns = {column["name"]: column for column in inspector.get_columns("approval_requests")}
        assert {"task_id", "action_id", "checkpoint_id", "preconditions", "effects", "approval_hash", "expires_at"} <= set(approval_columns)
        assert approval_columns["turn_record_id"]["nullable"] is True
        action_columns = {column["name"] for column in inspector.get_columns("agent_actions")}
        assert {"idempotency_key", "arguments_hash", "preconditions", "effects", "evidence_refs"} <= action_columns
    finally:
        engine.dispose()
