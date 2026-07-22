"""测试 Phase 6A Forget HTTP 请求契约。"""

from fastapi.testclient import TestClient

from aiive.main import create_app


def test_forget_accepts_json_body(monkeypatch):
    """遗忘选择器必须通过 JSON Body 传递并完整委托工具层。"""
    captured = {}

    def fake_handle_forget(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "operation_key": "forget-key",
            "status": "shielded",
            "shielded_at": "2026-07-22T00:00:00+00:00",
            "target_count": 1,
            "mode": kwargs["mode"],
            "note": "",
        }

    monkeypatch.setattr("aiive.api.routes_forget.handle_forget", fake_handle_forget)
    client = TestClient(create_app())

    response = client.post("/api/forget", json={
        "mode": "history_only",
        "event_ids": ["event-1"],
        "reason": "用户请求删除",
    })

    assert response.status_code == 200
    assert captured["event_ids"] == ["event-1"]
    assert captured["reason"] == "用户请求删除"
    assert len(captured["requested_by"]) == 36


def test_forget_rejects_query_only_selector():
    """旧 Query 参数不得继续形成隐式遗忘请求。"""
    client = TestClient(create_app())

    response = client.post("/api/forget?mode=everywhere&memory_ids=secret-id")

    assert response.status_code == 422


def test_forget_rejects_invalid_mode():
    """非法 mode 应由请求模型稳定返回 422。"""
    client = TestClient(create_app())

    response = client.post("/api/forget", json={
        "mode": "everywher",
        "memory_ids": ["memory-1"],
    })

    assert response.status_code == 422


def test_forget_rejects_invalid_scope_pair():
    """scope_type/scope_id 必须成对出现。"""
    client = TestClient(create_app())

    response = client.post("/api/forget", json={
        "mode": "everywhere",
        "scope_type": "thread",
    })

    assert response.status_code == 422


def test_forget_accepts_time_range(monkeypatch):
    """时间范围属于合法宽选择器，API 与工具层能力保持一致。"""
    captured = {}

    def fake_handle_forget(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "operation_key": "forget-time",
            "status": "shielded",
            "shielded_at": "2026-07-22T00:00:00+00:00",
            "target_count": 0,
            "mode": kwargs["mode"],
            "note": "",
        }

    monkeypatch.setattr("aiive.api.routes_forget.handle_forget", fake_handle_forget)
    client = TestClient(create_app())

    response = client.post("/api/forget", json={
        "mode": "history_only",
        "time_from": "2026-07-01T00:00:00Z",
        "time_to": "2026-07-02T00:00:00Z",
    })

    assert response.status_code == 200
    assert captured["time_from"] is not None
    assert captured["time_to"] is not None
