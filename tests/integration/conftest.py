"""本地集成测试公共配置：进程级共享 SQLite + 每用例重建全部表。

多会话（测试会话 + Handler 内部会话）共享同一物理库是这套 fixture 的设计
前提，因此不按用例新建数据库；SQLite 默认不强制外键，按连接开启
PRAGMA foreign_keys=ON，使 FK / UNIQUE 约束测试生效。

数据库地址与向量开关由仓库根 conftest.py 统一注入，此处不重复设置。
"""
from __future__ import annotations

import pytest
from sqlalchemy import event

import aiive.db.base as _db_base
from aiive.db.models import Base


def _enable_sqlite_fk(dbapi_conn, _conn_record) -> None:
    """SQLite 默认不强制外键，按连接开启。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


event.listen(_db_base.engine, "connect", _enable_sqlite_fk)

# 暴露给测试用例
engine = _db_base.engine
SessionLocal = _db_base.SessionLocal


_schema_ready = False


def _ensure_schema() -> None:
    """表结构只建一次：DDL 是每用例最贵的开销，重复 drop/create 会拖慢整套集成测试。"""
    global _schema_ready
    if not _schema_ready:
        Base.metadata.create_all(engine)
        _schema_ready = True


def _reset_data() -> None:
    """清空全部表数据（保留表结构），按子表→父表顺序删除以适配 FK 约束。"""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        try:
            conn.exec_driver_sql("DELETE FROM sqlite_sequence")
        except Exception:  # noqa: BLE001 - 无 AUTOINCREMENT 表时该表不存在
            pass


@pytest.fixture
def db():
    """每个测试使用干净的数据，避免跨测试数据污染（表结构复用，只清数据）。"""
    _ensure_schema()
    _reset_data()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
