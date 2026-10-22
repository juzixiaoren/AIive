"""关键流程测试公共配置。

与单元测试相同：流程用例按需把 SessionLocal 重定向到隔离的 SQLite 会话。
流程测试几乎都需要数据库，通常显式请求 ``db_session``；未请求的用例不建库，
SessionLocal 回退到根 conftest 统一指定的 SQLite 测试库。
"""
from __future__ import annotations

import pytest

from tests.support import set_current_test_session


@pytest.fixture(autouse=True)
def _patch_sessionlocal_to_sqlite(request):
    """按需重定向：仅当用例显式请求 db_session 时才注册隔离会话。"""
    session = request.getfixturevalue("db_session") if "db_session" in request.fixturenames else None
    set_current_test_session(session)
    try:
        yield
    finally:
        set_current_test_session(None)
