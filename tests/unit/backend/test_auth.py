"""单用户登录认证（V49）验收测试：全离线，不访问任何外部服务。

覆盖：初始化状态、首次注册唯一性、密码哈希、登录与统一失败响应、限流、
Token 过期/签名错误、重启后 Token 仍有效、全部 HTTP 路由的匿名访问矩阵、
WebSocket 首帧认证、桌面节点 WS 的用户级认证。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aiive.auth import security, service
from aiive.auth.guard import EXEMPT_PATHS
from aiive.db.base import get_db
from aiive.db.models import AuthOwner
from aiive.main import create_app

SECRET = "unit-test-auth-secret-0123456789abcdef"


@pytest.fixture
def auth_on(monkeypatch, tmp_path):
    """开启认证并固定签名密钥，避免污染真实 .env 与 .data。"""
    monkeypatch.setattr("aiive.config.settings.aiive_auth_enabled", True)
    monkeypatch.setattr("aiive.config.settings.aiive_auth_secret", SECRET)
    monkeypatch.setattr(
        "aiive.config.settings.aiive_auth_secret_file", str(tmp_path / "auth_secret")
    )
    service.reset_login_failures()
    security._secret_cache.clear()
    yield
    service.reset_login_failures()
    security._secret_cache.clear()


@pytest.fixture
def client(db_session, auth_on):
    """带测试会话覆盖的 HTTP 客户端；中间件仍然真实生效。"""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _register(client: TestClient, username: str = "owner", password: str = "correct-horse") -> dict[str, Any]:
    response = client.post("/api/auth/register", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_status_reports_initialization_state(client: TestClient) -> None:
    assert client.get("/api/auth/status").json() == {"initialized": False}
    _register(client)
    assert client.get("/api/auth/status").json() == {"initialized": True}


def test_status_never_leaks_account_details(client: TestClient) -> None:
    _register(client)
    body = client.get("/api/auth/status").json()
    assert set(body) == {"initialized"}


def test_register_creates_single_owner_and_second_register_conflicts(
    client: TestClient, db_session
) -> None:
    payload = _register(client)
    assert payload["token_type"] == "bearer"
    assert payload["user"] == {"username": "owner"}
    assert payload["access_token"]

    second = client.post(
        "/api/auth/register", json={"username": "intruder", "password": "another-password"}
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "account_already_initialized"
    assert db_session.query(AuthOwner).count() == 1


def test_register_stores_argon2id_hash_not_plaintext(client: TestClient, db_session) -> None:
    _register(client, password="plaintext-secret")
    owner = db_session.query(AuthOwner).one()
    assert owner.password_hash.startswith("$argon2id$")
    assert "plaintext-secret" not in owner.password_hash
    assert owner.singleton == 1


def test_register_rejects_blank_username_and_short_password(client: TestClient) -> None:
    assert client.post(
        "/api/auth/register", json={"username": "  ", "password": "long-enough"}
    ).status_code == 422
    assert client.post(
        "/api/auth/register", json={"username": "owner", "password": "short"}
    ).status_code == 422
    assert client.get("/api/auth/status").json() == {"initialized": False}


def test_login_returns_token_contract(client: TestClient) -> None:
    _register(client)
    response = client.post(
        "/api/auth/login", json={"username": "owner", "password": "correct-horse"}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_at", "user"}
    assert body["token_type"] == "bearer"
    assert body["user"] == {"username": "owner"}
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"], issuer="aiive")
    assert claims["sub"] == "owner"
    assert claims["exp"] - claims["iat"] == 365 * 24 * 3600


def test_login_failure_is_indistinguishable_for_wrong_password_and_unknown_user(
    client: TestClient,
) -> None:
    _register(client)
    wrong = client.post("/api/auth/login", json={"username": "owner", "password": "nope-nope"})
    unknown = client.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "invalid_credentials"}


def test_login_rate_limited_after_repeated_failures(client: TestClient) -> None:
    _register(client)
    for _ in range(5):
        assert client.post(
            "/api/auth/login", json={"username": "owner", "password": "wrong-pass"}
        ).status_code == 401
    blocked = client.post("/api/auth/login", json={"username": "owner", "password": "correct-horse"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "too_many_attempts"


def test_me_requires_valid_token(client: TestClient) -> None:
    token = _register(client)["access_token"]
    assert client.get("/api/auth/me").status_code == 401
    response = client.get("/api/auth/me", headers=_bearer(token))
    assert response.status_code == 200
    assert response.json() == {"username": "owner"}


def test_expired_token_is_rejected(client: TestClient) -> None:
    _register(client)
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": "owner",
            "iat": int((now - timedelta(days=400)).timestamp()),
            "exp": int((now - timedelta(days=1)).timestamp()),
            "iss": "aiive",
        },
        SECRET,
        algorithm="HS256",
    )
    response = client.get("/api/auth/me", headers=_bearer(expired))
    assert response.status_code == 401
    assert response.json()["detail"] == "token_expired"


def test_token_with_foreign_signature_is_rejected(client: TestClient) -> None:
    _register(client)
    forged = jwt.encode(
        {"sub": "owner", "iat": 1, "exp": 9999999999, "iss": "aiive"},
        "another-secret-0123456789abcdefghij",
        algorithm="HS256",
    )
    response = client.get("/api/auth/me", headers=_bearer(forged))
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"


def test_token_survives_restart_when_secret_is_persisted(monkeypatch, tmp_path) -> None:
    """密钥落盘后进程重启（清空内存缓存）仍能校验旧 Token。"""
    secret_file = tmp_path / "auth_secret"
    monkeypatch.setattr("aiive.config.settings.aiive_auth_secret", "")
    monkeypatch.setattr("aiive.config.settings.aiive_auth_secret_file", str(secret_file))
    security._secret_cache.clear()

    token, _ = security.create_access_token("owner")
    stored = secret_file.read_text(encoding="utf-8")
    assert stored and secret_file.exists()

    # 模拟重启：内存缓存清空，只能从文件恢复密钥。
    security._secret_cache.clear()
    assert security.decode_access_token(token)["sub"] == "owner"
    assert security.signing_secret() == stored


def test_changing_secret_invalidates_existing_tokens(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("aiive.config.settings.aiive_auth_secret", "first-secret-0123456789abcdefghijklmn")
    monkeypatch.setattr(
        "aiive.config.settings.aiive_auth_secret_file", str(tmp_path / "auth_secret")
    )
    security._secret_cache.clear()
    token, _ = security.create_access_token("owner")

    monkeypatch.setattr("aiive.config.settings.aiive_auth_secret", "second-secret-0123456789abcdefghijklm")
    security._secret_cache.clear()
    with pytest.raises(security.AuthError):
        security.decode_access_token(token)


def _all_http_routes() -> list[tuple[str, str]]:
    """递归展开 FastAPI 路由（含 _IncludedRouter 包装），返回 (method, path)。"""
    from fastapi.routing import APIRoute

    from aiive.main import app

    rows: list[tuple[str, str]] = []

    def walk(routes) -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                walk(getattr(original, "routes", []))
                continue
            if isinstance(route, APIRoute):
                for method in sorted(route.methods):
                    rows.append((method, route.path))

    walk(app.routes)
    return rows


def test_every_non_whitelisted_route_requires_authentication(client: TestClient) -> None:
    """匿名访问矩阵：除白名单外的每个真实路由都必须返回 401。"""
    routes = _all_http_routes()
    assert len(routes) > 100, f"路由盘点异常，仅发现 {len(routes)} 条"

    checked = 0
    for method, path in routes:
        if path in EXEMPT_PATHS or method in {"HEAD", "OPTIONS"}:
            continue
        concrete = path
        for marker in ("{task_id}", "{thread_id}", "{memory_id}", "{notification_id}"):
            concrete = concrete.replace(marker, "anon")
        concrete = concrete.replace("{capability_id:path}", "anon")
        concrete = concrete.replace("{candidate_name:path}", "anon")
        concrete = concrete.replace("{document_id}", "anon")
        concrete = concrete.replace("{operation_key}", "anon")
        concrete = concrete.replace("{operation_id}", "anon")
        concrete = concrete.replace("{artifact_id}", "anon")
        concrete = concrete.replace("{evidence_id}", "anon")
        concrete = concrete.replace("{step_id}", "anon")
        concrete = concrete.replace("{request_id}", "anon")
        concrete = concrete.replace("{candidate_id}", "anon")
        concrete = concrete.replace("{plan_id}", "anon")
        concrete = concrete.replace("{run_id}", "anon")
        concrete = concrete.replace("{trace_id}", "anon")
        concrete = concrete.replace("{item_id}", "anon")
        concrete = concrete.replace("{skill_id}", "anon")
        concrete = concrete.replace("{node_id}", "anon")
        response = client.request(method, concrete)
        assert response.status_code == 401, f"{method} {concrete} -> {response.status_code}"
        assert response.json()["detail"] in {
            "not_authenticated",
            "invalid_token",
            "token_expired",
        }
        checked += 1
    assert checked > 100


def test_whitelist_is_exactly_the_documented_set() -> None:
    assert EXEMPT_PATHS == {
        "/health",
        "/api/auth/status",
        "/api/auth/register",
        "/api/auth/login",
    }


def test_cors_preflight_is_not_blocked(client: TestClient) -> None:
    response = client.options(
        "/api/memories",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_websocket_rejects_connection_without_auth_frame(client: TestClient) -> None:
    with client.websocket_connect("/ws/some-thread") as ws:
        ws.send_text("not-an-auth-frame")
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_text()
        assert error.value.code == 1008


def test_websocket_rejects_invalid_token_without_leaking_data(client: TestClient) -> None:
    with client.websocket_connect("/ws/__global__") as ws:
        ws.send_json({"type": "auth", "access_token": "forged"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008


def test_websocket_authenticates_then_allows_traffic(client: TestClient) -> None:
    token = _register(client)["access_token"]
    with client.websocket_connect("/ws/some-thread") as ws:
        ws.send_json({"type": "auth", "access_token": token})
        first = ws.receive_json()
        assert first["type"] == "auth_ok"
        assert first["data"] == {"username": "owner"}


def test_desktop_node_websocket_requires_user_token(client: TestClient) -> None:
    from aiive.config import settings

    node_id = "desktop-node-1"
    hello = {
        "type": "hello",
        "data": {
            "node_id": node_id,
            "name": "test-node",
            "platform": "linux",
            "arch": "x64",
            "protocol_version": 2,
            "auth_token": settings.aiive_desktop_node_auth_token,
            "access_token": "",
            "capabilities": [],
        },
    }
    with client.websocket_connect(f"/ws/desktop/{node_id}") as ws:
        ws.send_json(hello)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_text()
        assert error.value.code == 1008
        assert error.value.reason == "unauthorized"


def test_passwords_and_tokens_are_not_logged(client: TestClient, caplog) -> None:
    caplog.set_level("DEBUG")
    payload = _register(client, password="super-secret-password")
    client.post("/api/auth/login", json={"username": "owner", "password": "super-secret-password"})
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret-password" not in logged
    assert payload["access_token"] not in logged

def test_database_rejects_second_owner_even_without_application_check(
    client: TestClient, db_session
) -> None:
    """并发注册的最终裁决者是数据库唯一约束，而不是应用层的"先查后插"。"""
    from sqlalchemy.exc import IntegrityError

    _register(client)
    assert db_session.query(AuthOwner).count() == 1
    db_session.add(AuthOwner(username="second-owner", password_hash="x"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    # 冲突后回滚：注册事务整体撤销，库里不会出现第二个所有者。
    db_session.rollback()


def test_register_maps_concurrent_insert_to_conflict(client: TestClient, db_session) -> None:
    """模拟竞态：存在性检查看不到未提交的写入，冲突必须由 IntegrityError 兜底成 409。"""
    from aiive.auth.security import hash_password

    with db_session.no_autoflush:
        db_session.add(AuthOwner(username="racer", password_hash=hash_password("x" * 12)))
        with pytest.raises(service.AuthError) as error:
            service.register(db_session, "owner", "long-enough-password")
    assert error.value.code == "account_already_initialized"
    assert error.value.status_code == 409
