"""Phase 3 测试全局配置。

必须在导入任何 aiive 模块前将数据库切换到 SQLite 文件库，以保证：
- 多会话（测试会话 + Handler 内部会话）共享同一物理库；
- SQLite 开启外键强制（PRAGMA foreign_keys=ON），使 FK / UNIQUE 约束测试生效。
"""
import atexit
import os
import tempfile

# 文件名带 PID：同一进程内多会话仍共享同一物理库（本文件的设计目标），
# 但并行的多个 pytest 进程互不互踩（固定文件名会导致并发 drop_all 假失败）
_TEST_DB = os.path.join(tempfile.gettempdir(), f"aiive_phase3_test_{os.getpid()}.db")
try:
    os.remove(_TEST_DB)
except (FileNotFoundError, PermissionError):
    pass


@atexit.register
def _cleanup_test_db() -> None:
    try:
        os.remove(_TEST_DB)
    except OSError:
        pass
# pydantic-settings 对应字段 database_url 的环境变量名为 DATABASE_URL（无前缀）
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

import aiive.db.base as _db_base  # noqa: E402
from aiive.db.models import Base  # noqa: E402


def _enable_sqlite_fk(dbapi_conn, _conn_record) -> None:
    """SQLite 默认不强制外键，按连接开启。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


event.listen(_db_base.engine, "connect", _enable_sqlite_fk)

# 暴露给测试用例
engine = _db_base.engine
SessionLocal = _db_base.SessionLocal


@pytest.fixture
def db():
    """每个测试使用干净的表结构，避免跨测试数据污染。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
