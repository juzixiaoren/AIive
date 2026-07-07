import pytest
from fastapi.testclient import TestClient

from aiive.main import create_app


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["ok"] is True
    assert data["service"] == "AIive"
    assert data["version"] == "0.1.0"


def test_health_has_required_fields(client):
    response = client.get("/health")
    data = response.json()

    assert "ok" in data
    assert "service" in data
    assert "version" in data
