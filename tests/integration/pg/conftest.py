"""PostgreSQL 专项契约测试夹具。

默认快速测试不启动 PostgreSQL：未设置 ``AIIVE_PG_TESTS=1`` 时，本目录用例在
收集阶段被标记 skip（零成本，不启动容器）。启用后：

  - 若提供 ``AIIVE_PG_TEST_URL``，直接使用该库（调用方负责隔离）；
  - 否则用 Docker 启动一次性 ``pgvector/pgvector:pg16`` 容器（随机本机端口、
    ``--rm``），会话结束自动移除。

隔离原则：不连接生产库、不调用真实外部 API；每个用例前 TRUNCATE 全部表。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid

import pytest
from aiive.db import forget_models, retention_models  # noqa: F401  register all tables
from aiive.db.models import Base
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests import support

PG_IMAGE = "pgvector/pgvector:pg16"
PG_USER = "aiive"
PG_PASSWORD = "aiive_test"
PG_DB = "aiive_test"
_PORT_CANDIDATES = (55432, 55433, 55434, 55435)


def pytest_collection_modifyitems(config, items) -> None:
    del config
    if os.environ.get("AIIVE_PG_TESTS") == "1":
        return
    skip = pytest.mark.skip(
        reason="PG 专项测试未启用（设置 AIIVE_PG_TESTS=1 并提供 AIIVE_PG_TEST_URL 或 Docker）"
    )
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/integration/pg/" in path:
            item.add_marker(skip)


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=20, check=False,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败即视为不可用
        return False


def _wait_port(host: str, port: int, timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _start_container() -> tuple[str, int]:
    name = "aiive-pg-test-" + uuid.uuid4().hex[:8]
    last_err = ""
    for port in _PORT_CANDIDATES:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
        r = subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "-e", f"POSTGRES_USER={PG_USER}",
                "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
                "-e", f"POSTGRES_DB={PG_DB}",
                "-p", f"127.0.0.1:{port}:5432",
                PG_IMAGE,
            ],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if r.returncode == 0:
            return name, port
        last_err = (r.stderr or r.stdout or "")[-400:]
    pytest.skip("启动 PG 测试容器失败: " + last_err)


@pytest.fixture(scope="session")
def pg_url():
    env_url = os.environ.get("AIIVE_PG_TEST_URL")
    if env_url:
        yield env_url
        return
    if not _docker_available():
        pytest.skip("Docker 不可用且未提供 AIIVE_PG_TEST_URL，无法运行 PG 专项测试")
    name, port = _start_container()
    url = f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{port}/{PG_DB}"
    try:
        if not _wait_port("127.0.0.1", port, 120):
            pytest.skip("PG 测试容器端口未就绪")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)


@pytest.fixture(scope="session")
def pg_engine(pg_url):
    eng = create_engine(pg_url, pool_pre_ping=True)
    last_err = None
    for _ in range(60):
        try:
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001 - 等待 PG 真正接受连接
            last_err = exc
            time.sleep(1)
    if last_err is not None:
        eng.dispose()
        pytest.skip("PG 连接不可用: " + str(last_err))
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def pg_sessionmaker(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _route_sessionlocal(monkeypatch, pg_sessionmaker):
    """把生产模块的 SessionLocal() 重定向到 PG 测试库。

    tests/support.py 的分发器在没有 current_test_session 时调用 _real_session_local；
    这里替换该真实工厂，使 Handler/Worker 内部新开的会话也落到 PG。
    """
    monkeypatch.setattr(support, "_real_session_local", pg_sessionmaker)
    monkeypatch.setattr(support, "_current_test_session", None)
    yield


@pytest.fixture(autouse=True)
def _clean_pg(pg_engine):
    """每个用例前清空全部表。

    并发用例需要多个真实连接并发提交，不能用外层事务回滚做隔离，故采用 TRUNCATE。
    """
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def pg_session(pg_sessionmaker):
    session = pg_sessionmaker()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
