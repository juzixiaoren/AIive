"""pytest 公共 fixtures - 提供测试用的数据库会话等共享资源。"""
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiive.db.models import Base


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
