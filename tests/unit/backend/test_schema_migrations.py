"""ORM 与 Alembic 最终数据库结构契约测试。"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from aiive.config import settings
from aiive.db.models import Base, MemoryRecord


_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# V39 知识库维护闭环迁移及其父修订（历史重复分块清理需从父修订造数）
_KNOWLEDGE_REVISION = "c7e3a1f5b9d2"
_KNOWLEDGE_PARENT_REVISION = "b4d7c1e9a2f5"
# 活跃文档 source_path 部分唯一索引迁移（并发导入同路径竞态的 schema 根治）
_ACTIVE_SOURCE_PATH_REVISION = "d1e2f3a4b5c6"


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


def test_knowledge_lifecycle_migration_dedups_and_roundtrips(monkeypatch, tmp_path):
    """V39 迁移：历史重复分块清理 + 生命周期结构 + 降级/再升级往返（SQLite）。"""
    database_url = f"sqlite:///{tmp_path / 'knowledge_migration.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))

    command.upgrade(config, _KNOWLEDGE_PARENT_REVISION)
    engine = create_engine(database_url)
    document_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO documents (id, source_path, content_hash, object_bucket,"
                " object_key, content_size, mime_type, title, doc_type, status, created_at)"
                " VALUES (:i, :p, :h, NULL, NULL, 10, 'text/plain', 't', 'text',"
                " 'indexed', :c)"
            ), {"i": document_id, "p": "/tmp/kb.md",
                "h": "h" + document_id.replace("-", ""), "c": now})
            # 同一 (document_id, chunk_index) 的历史重复行：字典序最小者应被保留
            for chunk_id in (f"aaa-{document_id}"[:36], f"zzz-{document_id}"[:36]):
                conn.execute(text(
                    "INSERT INTO chunks (id, document_id, content, line_start, line_end,"
                    " chunk_index, token_estimate, created_at)"
                    " VALUES (:i, :d, :c, 0, 1, 0, 1, :t)"
                ), {"i": chunk_id, "d": document_id, "c": chunk_id, "t": now})

        command.upgrade(config, _KNOWLEDGE_REVISION)
        with engine.connect() as conn:
            inspector = inspect(conn)
            chunk_ids = conn.execute(
                text("SELECT id FROM chunks ORDER BY id")
            ).scalars().all()
            assert chunk_ids == [f"aaa-{document_id}"[:36]], (
                f"历史重复分块应只保留最早一条，实际 {chunk_ids}"
            )
            columns = {column["name"] for column in inspector.get_columns("documents")}
            assert {"superseded_by", "deleted_at", "updated_at"} <= columns
            updated_at = next(
                column for column in inspector.get_columns("documents")
                if column["name"] == "updated_at"
            )
            assert updated_at["nullable"] is False
            indexes = {index["name"] for index in inspector.get_indexes("documents")}
            assert "ix_documents_source_path" in indexes
            unique_constraints = {
                item["name"] for item in inspector.get_unique_constraints("chunks")
            }
            assert "uq_chunks_document_chunk_index" in unique_constraints
            check_constraints = {
                item["name"]: item["sqltext"]
                for item in inspector.get_check_constraints("documents")
            }
            assert "superseded" in check_constraints.get("ck_documents_status", "")
            assert conn.execute(text(
                "SELECT COUNT(*) FROM documents WHERE updated_at IS NULL"
            )).scalar() == 0

        command.downgrade(config, "-1")
        with engine.connect() as conn:
            inspector = inspect(conn)
            columns = {column["name"] for column in inspector.get_columns("documents")}
            assert "superseded_by" not in columns
            assert "uq_chunks_document_chunk_index" not in {
                item["name"] for item in inspector.get_unique_constraints("chunks")
            }
            statuses = conn.execute(
                text("SELECT DISTINCT status FROM documents")
            ).scalars().all()
            assert set(statuses) <= {"indexed", "index_failed", "source_unavailable"}

        command.upgrade(config, _KNOWLEDGE_REVISION)
        with engine.connect() as conn:
            assert "uq_chunks_document_chunk_index" in {
                item["name"] for item in inspect(conn).get_unique_constraints("chunks")
            }
    finally:
        engine.dispose()


def test_active_source_path_unique_migration_dedups_and_roundtrips(monkeypatch, tmp_path):
    """部分唯一索引迁移：历史重复活跃文档去重 + 索引生效 + 降级移除（SQLite）。"""
    database_url = f"sqlite:///{tmp_path / 'active_source_path.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))

    command.upgrade(config, _KNOWLEDGE_REVISION)  # 新迁移的父修订
    engine = create_engine(database_url)
    now = datetime.now(timezone.utc)
    old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            # V39 之前"同路径改内容不替换"的历史形态：同一 source_path 两条活跃文档
            for doc_id, created, title in (
                (old_id, now - timedelta(days=2), "旧版"),
                (new_id, now, "新版"),
            ):
                conn.execute(text(
                    "INSERT INTO documents (id, source_path, content_hash, object_bucket,"
                    " object_key, content_size, mime_type, title, doc_type, status,"
                    " created_at, updated_at) VALUES (:i, :p, :h, NULL, NULL, 10,"
                    " 'text/plain', :t, 'text', 'indexed', :c, :c)"
                ), {"i": doc_id, "p": "/tmp/dup-active.md",
                    "h": "h" + doc_id.replace("-", ""), "t": title, "c": created})
                conn.execute(text(
                    "INSERT INTO chunks (id, document_id, content, line_start, line_end,"
                    " chunk_index, token_estimate, created_at)"
                    " VALUES (:i, :d, :c, 0, 1, 0, 1, :t)"
                ), {"i": doc_id + "-chunk", "d": doc_id, "c": "内容", "t": created})

        command.upgrade(config, _ACTIVE_SOURCE_PATH_REVISION)
        with engine.begin() as conn:
            statuses = dict(
                conn.execute(text("SELECT id, status FROM documents")).all()
            )
            assert statuses[old_id] == "superseded", statuses
            assert statuses[new_id] == "indexed", statuses
            # 旧版分块被清（失效文档分块必须为空），新版分块保留
            chunk_owners = conn.execute(
                text("SELECT document_id FROM chunks")
            ).scalars().all()
            assert chunk_owners == [new_id], chunk_owners
            index_sql = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type = 'index'"
                " AND name = 'ix_documents_active_source_path'"
            )).scalar()
            assert index_sql and "UNIQUE" in index_sql.upper(), index_sql
            assert "WHERE" in index_sql.upper(), index_sql
            predicate = index_sql.upper().split("WHERE")[-1]
            for active_status in ("INDEXED", "INDEX_FAILED", "SOURCE_UNAVAILABLE"):
                assert active_status in predicate, (active_status, index_sql)
            assert "SUPERSEDED" not in predicate, index_sql
            # 唯一性已生效：再插一条同路径活跃文档必须失败
            with pytest.raises(IntegrityError):
                conn.execute(text(
                    "INSERT INTO documents (id, source_path, content_hash, object_bucket,"
                    " object_key, content_size, mime_type, title, doc_type, status,"
                    " created_at, updated_at) VALUES (:i, :p, :h, NULL, NULL, 10,"
                    " 'text/plain', '冲突', 'text', 'indexed', :c, :c)"
                ), {"i": str(uuid.uuid4()), "p": "/tmp/dup-active.md",
                    "h": "h" + uuid.uuid4().hex, "c": now})

        command.downgrade(config, "-1")
        with engine.connect() as conn:
            index_sql = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type = 'index'"
                " AND name = 'ix_documents_active_source_path'"
            )).scalar()
            assert index_sql is None
    finally:
        engine.dispose()
