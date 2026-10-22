"""测试基础设施：SQLite 测试会话分发器（供 conftest 与测试模块共用）。

背景：生产模块大多以 ``from aiive.db.base import SessionLocal`` 做模块级绑定，
monkeypatch 无法撤销这种 from-import 绑定。因此在 conftest 加载时（先于任何
aiive 业务模块 import）安装一个稳定的分发函数：运行期查当前测试会话，无测试
会话时回退真实 SessionLocal。

分层使用约定：
- 单元/流程测试：经 autouse fixture 把按用例隔离的 SQLite 会话注册进来
  （见 tests/unit/conftest.py）。该注册是零成本的（不建库、不建表），
  纯逻辑测试不会因 autouse 而付出数据库初始化开销；
  真正需要数据库的用例显式请求 ``db_session`` 才会建库。
- 本地集成测试：不注册，天然使用进程级共享库（Handler 内部新开会话也能读到
  同一物理库，这是集成 fixture 的设计前提）。
"""
from __future__ import annotations

import sys

import aiive.db.base as _db_base
from sqlalchemy.orm import Session

_real_session_local = _db_base.SessionLocal
_current_test_session: Session | None = None


def _dispatching_session_local() -> Session:
    if _current_test_session is not None:
        return _current_test_session
    return _real_session_local()


def install_session_dispatcher() -> None:
    """将 SessionLocal 指向分发函数，并修正已导入 aiive 模块的旧绑定。"""
    _db_base.SessionLocal = _dispatching_session_local
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith("aiive.") and mod is not None:
            if getattr(mod, "SessionLocal", None) is _real_session_local:
                mod.SessionLocal = _dispatching_session_local


def set_current_test_session(session: Session | None) -> None:
    """注册/注销当前测试会话（仅单元与流程测试使用）。"""
    global _current_test_session
    _current_test_session = session
