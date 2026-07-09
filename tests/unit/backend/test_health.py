"""测试健康检查 API 端点。"""
import pytest
from fastapi.testclient import TestClient

from aiive.main import create_app


@pytest.fixture
def client():
    """创建测试用的 FastAPI 客户端。"""
    app = create_app()
    return TestClient(app)


def test_health_returns_ok(client):
    """验证 /health 端点返回 200 状态码和正确结构。"""
    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["ok"] is True
    assert data["service"] == "AIive"
    assert data["version"] == "0.1.0"


def test_health_has_required_fields(client):
    """验证 /health 端点返回所有必需字段。"""
    response = client.get("/health")
    data = response.json()

    assert "ok" in data
    assert "service" in data
    assert "version" in data
