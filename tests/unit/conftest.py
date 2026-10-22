"""单元测试公共配置。

每个单元用例自动把 SessionLocal 重定向到当前测试会话，防止后台服务与上下文
装配在测试中误连其他数据库。该 autouse fixture 本身零成本（不建库、不建表）：
纯逻辑测试不会因此付出数据库初始化开销；需要数据库的用例显式请求
``db_session``（见 tests/conftest.py），届时才建临时库与表结构。
"""
from __future__ import annotations

import pytest

from tests.support import set_current_test_session

# 已迁至 tests/flow/ 的关键流程测试（权威副本在那里）。
# 同名旧副本仍残留在 tests/unit/backend/ 下时会被重复收集，此处显式忽略，
# 待旧副本删除后本段可一并移除。
collect_ignore = [
    "backend/test_turn_execution.py",
    "backend/test_reminder_end_to_end.py",
]


@pytest.fixture(autouse=True)
def _patch_sessionlocal_to_sqlite(request):
    """按需重定向：仅当用例显式请求 db_session 时才注册隔离会话。

    没有请求 db_session 的用例（纯逻辑测试）不建库、不建表，只保持
    SessionLocal 指向分发函数；若其内部仍尝试建会话，会回退到根 conftest
    统一指定的 SQLite 测试库，而不是本机 PostgreSQL。
    """
    session = request.getfixturevalue("db_session") if "db_session" in request.fixturenames else None
    set_current_test_session(session)
    try:
        yield
    finally:
        set_current_test_session(None)
