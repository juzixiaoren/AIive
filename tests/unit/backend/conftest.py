"""pytest 公共 fixtures - 提供测试用的数据库会话等共享资源。"""
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import aiive.db.base as _db_base
from aiive.db.models import Base
from aiive.db import models  # noqa: F401  ensure all Phase 1 models are imported

# ── SessionLocal 稳定分发器 ──────────────────────────────────────────────
# 生产模块大多以 `from aiive.db.base import SessionLocal` 做模块级绑定。
# 若在 fixture 内按测试临时 monkeypatch，首次 import 发生在哪个测试内，
# 该模块就永久绑死那个测试的会话闭包（monkeypatch 撤销不了 from-import 绑定），
# 后续测试会拿到已关闭连接的旧会话（ResourceClosedError）。
# 因此在 conftest 模块加载时（先于任何 aiive 业务模块 import）安装一个
# 稳定的分发函数：运行期查当前测试会话，无测试会话时回退真实 SessionLocal。
_real_session_local = _db_base.SessionLocal
_current_test_session: Session | None = None


def _dispatching_session_local() -> Session:
    if _current_test_session is not None:
        return _current_test_session
    return _real_session_local()


_db_base.SessionLocal = _dispatching_session_local

# 对已在本 conftest 加载前 import 过的 aiive 模块，修正其 from-import 旧绑定
for _mod_name, _mod in list(sys.modules.items()):
    if _mod_name.startswith("aiive.") and _mod is not None:
        if getattr(_mod, "SessionLocal", None) is _real_session_local:
            _mod.SessionLocal = _dispatching_session_local


@pytest.fixture(autouse=True)
def _patch_sessionlocal_to_sqlite(db_session):
    """将测试期间所有 SessionLocal() 调用重定向到测试 SQLite 会话。

    防止后台服务和 ContextAssembler 在测试中连接 PostgreSQL。
    """
    global _current_test_session
    _current_test_session = db_session
    try:
        yield
    finally:
        _current_test_session = None


@pytest.fixture
def db_session():
    """Create temporary SQLite database session fixture."""
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()

        import shutil
        shutil.rmtree(tmpdir)
