"""tests/ 目录公共配置：按用例隔离的 SQLite 测试会话夹具。

环境注入（SQLite 地址、向量开关、.env 加载）与统一结果输出插件由仓库根
conftest.py 承担，此处不再重复。本文件提供单元/流程测试使用的隔离会话：
独立临时库 + 外层事务，teardown 整体回滚；并安装 SessionLocal 分发器
（见 tests/support.py），保证测试期间所有 SessionLocal() 都落到测试库。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiive.db import forget_models, models, retention_models  # noqa: F401  ensure all models are registered
from aiive.db.models import Base

from tests.support import install_session_dispatcher, set_current_test_session

# 先于任何 aiive 业务模块 import 安装分发器
install_session_dispatcher()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """按目录自动标注测试层级标记（unit / integration / flow）。

    集中标注避免每个测试文件手写 marker；层级与目录结构一一对应：
    tests/unit -> unit，tests/integration -> integration，tests/flow -> flow。

    注意：pytest 核心插件在同一钩子内执行 -m/-k 过滤，且其 hook 优先于
    conftest，因此这里添加的 marker 只用于报告与 --markers 查询，
    不能作为 -m <layer> 的选择依据。层级筛选请使用目录路径入口：
    pytest tests/unit / pytest tests/integration / pytest tests/flow。
    """
    del config  # 仅使用 items
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if "/tests/unit/" in nodeid:
            item.add_marker(pytest.mark.unit)
        elif "/tests/integration/" in nodeid:
            item.add_marker(pytest.mark.integration)
        elif "/tests/flow/" in nodeid:
            item.add_marker(pytest.mark.flow)


@pytest.fixture(scope="session")
def _db_template(tmp_path_factory) -> Path:
    """会话级 SQLite 模板库：整个 pytest 进程只建一次表，用例侧只做文件拷贝。

    实测 Base.metadata.create_all 在 Windows 上约 1.2s/次，而 unit 层约 250 个
    用例请求 db_session，逐用例建表会把 unit 层拖到 5 分钟以上。改为模板库后
    建表开销摊到会话级一次，用例只剩 shutil.copyfile（约 8ms），隔离语义不变：
    每个用例仍是独立物理库文件 + 外层事务回滚。

    PRAGMA journal_mode=DELETE 保证模板文件自包含（WAL 模式下数据可能留在
    -wal 旁路文件里，只拷 .db 会丢表）。
    """
    path = tmp_path_factory.mktemp("db_template") / "template.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=DELETE")
        conn.commit()
    engine.dispose()
    return path


@pytest.fixture
def db_session(_db_template: Path):
    """Create temporary SQLite database session fixture.

    每个用例独立临时库 + 外层事务，teardown 回滚，杜绝跨用例污染。
    库文件由会话级模板库拷贝而来，避免逐用例重复建表。
    """
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"
    shutil.copyfile(_db_template, db_path)

    engine = create_engine(f"sqlite:///{db_path}")

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    # 注册给 SessionLocal 分发器：生产代码内部自开的 SessionLocal() 也落到本库，
    # 否则会回落到进程级空库并报 no such table（见 tests/support.py 的分层约定）。
    set_current_test_session(session)
    try:
        yield session
    finally:
        set_current_test_session(None)
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()

        shutil.rmtree(tmpdir, ignore_errors=True)
