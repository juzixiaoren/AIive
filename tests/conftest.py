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
    conftest，因此这里添加的 marker 只用于报告与 `--markers` 查询，
    不能作为 `-m <layer>` 的选择依据。层级筛选请使用目录路径入口：
    ``pytest tests/unit`` / ``pytest tests/integration`` / ``pytest tests/flow``。
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


@pytest.fixture
def db_session():
    """Create temporary SQLite database session fixture.

    每个用例独立临时库 + 外层事务，teardown 回滚，杜绝跨用例污染。
    """
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

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
