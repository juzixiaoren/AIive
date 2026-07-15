"""pytest 公共 fixtures - 提供测试用的数据库会话等共享资源。"""
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiive.db.models import Base
from aiive.db.base import SessionLocal as _SessionLocal
from aiive.db import models  # noqa: F401  ensure all Phase 1 models are imported


@pytest.fixture(autouse=True)
def _patch_sessionlocal_to_sqlite(monkeypatch, db_session):
    """Redirect ALL SessionLocal() calls to the test SQLite session.

    This prevents TaskWorker / Heartbeat / ContextAssembler from connecting
    to PostgreSQL when running tests.
    """
    def _test_session():
        return db_session

    monkeypatch.setattr("aiive.db.base.SessionLocal", _test_session)
    # Also patch in turn_execution module (direct import)
    import aiive.runtime.turn_execution as te
    monkeypatch.setattr(te, "SessionLocal", _test_session)
    import aiive.runtime.tool_normalizer as tn
    monkeypatch.setattr(tn, "SessionLocal", _test_session)


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
