"""本地开发者诊断接口访问控制与脱敏回归测试。"""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiive.api import developer_security
from aiive.api.routes_debug import router as debug_router
from aiive.api.routes_epochs import router as epochs_router
from aiive.api.routes_outbox import router as outbox_router
from aiive.db.base import get_db
from aiive.db.models import Event, LLMCall, OutboxJob, Thread


def _app(db) -> FastAPI:
    """构造仅注册诊断路由并复用测试事务的应用。"""
    app = FastAPI()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.include_router(debug_router)
    app.include_router(outbox_router)
    app.include_router(epochs_router)
    return app


def _client(app: FastAPI, host: str = "127.0.0.1") -> TestClient:
    """按指定来源地址构造可测试的 HTTP 客户端。"""
    return TestClient(app, client=(host, 50000), base_url="http://localhost")


def test_guard_defaults_to_closed(db, monkeypatch) -> None:
    """诊断开关默认关闭时 loopback 请求也不可读取。"""
    monkeypatch.setattr(developer_security.settings, "aiive_developer_diagnostics_enabled", False)
    response = _client(_app(db)).get("/api/debug/events")
    assert response.status_code == 404


def test_guard_rejects_non_loopback_client(db, monkeypatch) -> None:
    """启用后仍拒绝非 loopback 来源。"""
    monkeypatch.setattr(developer_security.settings, "aiive_developer_diagnostics_enabled", True)
    response = _client(_app(db), "192.0.2.10").get("/api/outbox/jobs")
    assert response.status_code == 403


def test_guard_rejects_non_loopback_host(db, monkeypatch) -> None:
    """伪造外部 Host 时即使客户端为 loopback 也拒绝。"""
    monkeypatch.setattr(developer_security.settings, "aiive_developer_diagnostics_enabled", True)
    response = _client(_app(db)).get("/api/epochs/thread-1", headers={"host": "example.com"})
    assert response.status_code == 403


def test_loopback_reads_are_allowed_and_epoch_writes_stay_compatible(db, monkeypatch) -> None:
    """启用后允许本机读取，Epoch 写接口不误归入只读守卫。"""
    monkeypatch.setattr(developer_security.settings, "aiive_developer_diagnostics_enabled", True)
    client = _client(_app(db))
    assert client.get("/api/debug/events").status_code == 200
    assert client.get("/api/outbox/jobs").status_code == 200
    assert client.get("/api/epochs/thread-1").status_code == 200

    monkeypatch.setattr(developer_security.settings, "aiive_developer_diagnostics_enabled", False)
    response = client.post("/api/epochs/thread-1/seal-segment")
    assert response.status_code == 200
    assert response.json()["reason"] == "no_open_segment"


def test_diagnostic_responses_are_redacted_on_server(db, monkeypatch) -> None:
    """事件、LLM 预览及错误详情在响应离开服务端前完成脱敏。"""
    monkeypatch.setattr(developer_security.settings, "aiive_developer_diagnostics_enabled", True)
    now = datetime.now(timezone.utc)
    db.add(Thread(id="thread-1"))
    db.flush()
    db.add(Event(
        id="event-1",
        trace_id="trace-1",
        thread_id="thread-1",
        event_type="tool_call",
        payload={
            "content": "用户隐私正文",
            "nested": {"api_key": "secret-key", "safe": "Bearer abc.def"},
        },
        created_at=now,
    ))
    db.add(LLMCall(
        id="call-1",
        trace_id="trace-1",
        thread_id="thread-1",
        model="test-model",
        latency_ms=1,
        input_preview="password=hunter2",
        output_preview="Authorization: Bearer output-token",
        created_at=now,
    ))
    db.add(OutboxJob(
        id="job-1",
        operation_id="operation-1",
        job_type="test",
        status="failed",
        trace_id="trace-1",
        error_message="api_key=outbox-secret",
        created_at=now,
    ))
    db.commit()

    client = _client(_app(db))
    event_payload = client.get("/api/debug/events?trace_id=trace-1").json()[0]["payload"]
    llm_payload = client.get("/api/debug/llm_calls?trace_id=trace-1").json()[0]
    outbox_payload = client.get("/api/outbox/jobs?trace_id=trace-1").json()[0]

    assert event_payload["content"] == "[已脱敏]"
    assert event_payload["nested"]["api_key"] == "[已脱敏]"
    assert event_payload["nested"]["safe"] == "Bearer [已脱敏]"
    assert "hunter2" not in llm_payload["input_preview"]
    assert "output-token" not in llm_payload["output_preview"]
    assert "outbox-secret" not in outbox_payload["error_message"]
