"""pytest 公共 fixtures - 提供测试用的数据库会话等共享资源。"""
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiive.db.models import Base


# ---------- 从项目 .env 加载 LLM 配置 ----------
def _load_dotenv():
    env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and value and key not in __import__("os").environ:
                __import__("os").environ[key] = value


_load_dotenv()
# -----------------------------------------------


@pytest.fixture
def db_session():
    """创建临时 SQLite 数据库会话 fixture，测试结束后自动清理。"""
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
