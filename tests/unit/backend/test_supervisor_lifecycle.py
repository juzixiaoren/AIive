"""Supervisor 进程托管契约测试（离线；真实本地子进程，不访问网络）。

覆盖：进程启动/停止与所有权、晋升成功切换、候选启动失败、候选健康失败回滚、
运行期异常退出回滚、重复请求去重、并发切换拒绝、Supervisor 重启接管、
中断切换恢复，以及 ReleaseManager 在托管模式下的行为。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from aiive.supervisor.client import SupervisorClient
from aiive.supervisor.control import (
    ControlPaths,
    FileLock,
    read_events,
    submit_request,
    write_json_atomic,
)
from aiive.supervisor.daemon import Supervisor, SupervisorConfig
from aiive.supervisor.health_http import probe_http, wait_healthy
from aiive.supervisor.release_manager import ReleaseManager
from aiive.supervisor.slot_manager import SlotManager

_FAKE_APP = Path(__file__).with_name("_fake_slot_app.py")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: Path, *, port: int, extra: str = "", **over: object) -> SupervisorConfig:
    command = f'"{sys.executable}" -u "{_FAKE_APP}" --port {{port}} --slot {{slot}} {extra}'.strip()
    defaults = {
        "host": "127.0.0.1",
        "port": port,
        "app_command": command,
        "health_path": "/health",
        "start_timeout": 5.0,
        "stop_timeout": 3.0,
        "poll_interval": 0.05,
        "monitor_seconds": 30,
        "max_restarts": 2,
        "restart_window": 60.0,
        "state_dir": str(tmp_path / "sup"),
    }
    defaults.update(over)
    return SupervisorConfig(**defaults)  # type: ignore[arg-type]


def _prepare_slots(tmp_path: Path) -> SlotManager:
    manager = SlotManager(base_dir=tmp_path / "slots")
    manager.init_slots()
    for name in ("A", "B"):
        backend = tmp_path / "slots" / name / "app" / "backend"
        (backend / "aiive").mkdir(parents=True, exist_ok=True)
        (backend / "aiive" / "__init__.py").write_text("", encoding="utf-8")
        (backend / "aiive" / "main.py").write_text(f"VERSION = {name!r}\n", encoding="utf-8")
    return manager


@pytest.fixture
def make_supervisor(tmp_path):
    created: list[Supervisor] = []

    def _make(*, extra: str = "", **over: object) -> Supervisor:
        port = int(over.pop("port", 0) or _free_port())
        config = _config(tmp_path, port=port, extra=extra, **over)
        supervisor = Supervisor(
            config,
            paths=ControlPaths(tmp_path / "sup"),
            slot_manager=_prepare_slots(tmp_path),
        )
        created.append(supervisor)
        return supervisor

    yield _make

    for supervisor in created:
        try:
            supervisor.stop_managed_process()
        except Exception:  # pragma: no cover - 清理失败不影响结论
            pass


def _promote(
    supervisor: Supervisor, target: str | None = None, *, key: str = "", gate_ok: bool = True,
) -> dict:
    payload: dict = {"gate_ok": gate_ok}
    if target:
        payload["target"] = target
    submit_request(supervisor.paths, "promote", payload, idempotency_key=key)
    return supervisor.tick()


def _kill(pid: int) -> None:
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return
    os.kill(pid, signal.SIGKILL)


def test_process_controller_starts_and_stops_real_process(make_supervisor, tmp_path) -> None:
    supervisor = make_supervisor()
    port = _free_port()
    record, error = supervisor.controller.start(
        slot="A", slot_root=tmp_path / "slots" / "A", port=port,
    )

    assert error == ""
    assert record is not None
    health = wait_healthy("127.0.0.1", port, "/health", timeout_seconds=8)
    assert health["ok"] is True
    assert supervisor.controller.alive(record) is True

    stopped = supervisor.controller.stop(record)

    assert stopped["ok"] is True and stopped["stopped"] is True
    assert supervisor.controller.alive(record) is False
    assert supervisor.controller.records() == []


def test_process_controller_refuses_unowned_pid(make_supervisor, tmp_path) -> None:
    supervisor = make_supervisor()
    port = _free_port()
    record, _error = supervisor.controller.start(
        slot="A", slot_root=tmp_path / "slots" / "A", port=port,
    )
    assert record is not None

    foreign = type(record)(slot="A", pid=os.getpid(), port=port, started_at=record.started_at)
    result = supervisor.controller.stop(foreign)

    assert result["ok"] is False
    assert result["error"] == "process_not_owned"
    supervisor.controller.stop(record)


def test_promote_switches_real_process_and_keeps_state_consistent(make_supervisor, tmp_path) -> None:
    supervisor = make_supervisor()
    assert supervisor.recover()["ok"] is True
    old_pid = supervisor.state()["process"]["pid"]

    result = _promote(supervisor)

    assert result["ok"] is True, result
    assert result["status"] == "switched"
    assert supervisor.slots.get_active_slot() == "B"
    state = supervisor.state()
    assert state["phase"] == "running"
    assert state["active_slot"] == "B"
    assert state["process"]["slot"] == "B"
    assert state["process"]["pid"] != old_pid
    assert probe_http("127.0.0.1", supervisor.config.port, "/health").ok is True
    assert not _pid_alive(old_pid)

    kinds = [event["kind"] for event in read_events(supervisor.paths, limit=50)]
    assert "switch_started" in kinds
    assert "candidate_healthy" in kinds
    assert "switch_completed" in kinds

    release_state = json.loads((tmp_path / "release_state.json").read_text(encoding="utf-8"))
    assert release_state["active_slot"] == "B"
    assert release_state["status"] == "monitoring"


def test_promote_candidate_start_failure_keeps_old_version(make_supervisor) -> None:
    supervisor = make_supervisor(extra="--fail-slots B")
    supervisor.recover()
    old_pid = supervisor.state()["process"]["pid"]

    result = _promote(supervisor)

    assert result["ok"] is False
    assert result["error"] == "candidate_start_failed"
    assert supervisor.slots.get_active_slot() == "A"
    assert supervisor.state()["process"]["pid"] == old_pid
    assert _pid_alive(old_pid) is True
    assert probe_http("127.0.0.1", supervisor.config.port, "/health").ok is True
    assert any(event["kind"] == "switch_failed" for event in read_events(supervisor.paths, 50))


def test_promote_candidate_unhealthy_on_staging_keeps_old_version(make_supervisor) -> None:
    supervisor = make_supervisor(extra="--unhealthy-slots B")
    supervisor.recover()
    old_pid = supervisor.state()["process"]["pid"]

    result = _promote(supervisor)

    assert result["ok"] is False
    assert result["error"] == "candidate_health_failed"
    assert supervisor.slots.get_active_slot() == "A"
    assert supervisor.state()["process"]["pid"] == old_pid
    assert probe_http("127.0.0.1", supervisor.config.port, "/health").ok is True


def test_promote_post_switch_health_failure_restores_and_verifies(make_supervisor) -> None:
    port = _free_port()
    supervisor = make_supervisor(
        port=port, extra=f"--unhealthy-port {port} --unhealthy-port-slot B",
    )
    supervisor.recover()
    old_pid = supervisor.state()["process"]["pid"]

    result = _promote(supervisor)

    assert result["ok"] is False
    assert result["error"] == "post_switch_health_failed"
    rollback = result["rollback"]
    assert rollback["ok"] is True
    assert rollback["verified"] is True
    assert rollback["slot"] == "A"
    assert supervisor.slots.get_active_slot() == "A"
    assert supervisor.state()["process"]["pid"] == rollback["pid"]
    assert probe_http("127.0.0.1", port, "/health").ok is True
    assert not _pid_alive(old_pid)
    kinds = [event["kind"] for event in read_events(supervisor.paths, 50)]
    assert "restore_failed" not in kinds
    assert "restored_previous" in kinds
    assert "switch_failed" in kinds


def test_candidate_role_skips_migrations_and_workers(monkeypatch) -> None:
    """候选进程只做启动自检，不与运行中的旧版本并发迁移或重复跑周期任务。"""
    from fastapi.testclient import TestClient

    import aiive.main as main_module

    calls: list[str] = []
    monkeypatch.setenv("AIIVE_PROCESS_ROLE", "candidate")
    monkeypatch.setattr(main_module, "_ensure_schema", lambda: calls.append("schema"))
    monkeypatch.setattr(main_module, "_ensure_system_thread", lambda: calls.append("thread"))
    monkeypatch.setattr(main_module, "_ensure_retrieval_generation", lambda: calls.append("retrieval"))

    app = main_module.create_app()
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True

    assert calls == []


def test_slot_manifest_excludes_persistent_data() -> None:
    """版本切换只动代码副本，绝不覆盖用户数据/数据库/对象存储。"""
    from aiive.supervisor.slot_manager import create_version_manifest

    excludes = set(create_version_manifest("A")["excludes"])

    assert {"data/", "postgres/", "object_store/", "logs/", ".env"} <= excludes


def test_app_exit_during_monitor_rolls_back_to_previous(make_supervisor) -> None:
    supervisor = make_supervisor()
    supervisor.recover()
    assert _promote(supervisor)["ok"] is True
    candidate_pid = supervisor.state()["process"]["pid"]

    _kill(candidate_pid)
    outcome = supervisor.tick()

    assert outcome["action"] == "auto_rollback", outcome
    assert supervisor.slots.get_active_slot() == "A"
    assert supervisor.state()["process"]["slot"] == "A"
    assert probe_http("127.0.0.1", supervisor.config.port, "/health").ok is True
    kinds = [event["kind"] for event in read_events(supervisor.paths, 50)]
    assert "auto_rollback" in kinds
    assert "process_exited" in kinds


def test_promote_without_gate_is_rejected(make_supervisor) -> None:
    supervisor = make_supervisor()
    supervisor.recover()

    result = _promote(supervisor, gate_ok=False)

    assert result["ok"] is False
    assert result["error"] == "gate_not_passed"
    assert supervisor.slots.get_active_slot() == "A"
    assert any(event["kind"] == "switch_rejected" for event in read_events(supervisor.paths, 50))


def test_promote_already_active_is_noop(make_supervisor) -> None:
    supervisor = make_supervisor()
    supervisor.recover()
    pid = supervisor.state()["process"]["pid"]

    result = _promote(supervisor, target="A")

    assert result["ok"] is True
    assert result["status"] == "already_active"
    assert result["pid"] == pid
    assert supervisor.slots.get_active_slot() == "A"


def test_restart_and_status_requests(make_supervisor) -> None:
    supervisor = make_supervisor()
    supervisor.recover()
    old_pid = supervisor.state()["process"]["pid"]

    submit_request(supervisor.paths, "restart", {})
    restarted = supervisor.tick()

    assert restarted["ok"] is True
    assert restarted["status"] == "restarted"
    assert restarted["pid"] != old_pid
    assert probe_http("127.0.0.1", supervisor.config.port, "/health").ok is True
    assert not _pid_alive(old_pid)

    submit_request(supervisor.paths, "status", {})
    status = supervisor.tick()

    assert status["ok"] is True
    assert status["status"] == "status"
    assert status["pid"] == restarted["pid"]
    assert status["process_alive"] is True
    assert status["healthy"] is True
    assert status["active_slot"] == "A"


def test_duplicate_request_is_deduplicated(make_supervisor) -> None:
    supervisor = make_supervisor()
    supervisor.recover()

    first = _promote(supervisor, key="release-42")
    pid_after_first = supervisor.state()["process"]["pid"]
    second = _promote(supervisor, key="release-42")

    assert first["ok"] is True
    assert second.get("deduplicated") is True
    assert supervisor.state()["process"]["pid"] == pid_after_first
    assert supervisor.slots.get_active_slot() == "B"


def test_concurrent_switch_is_rejected(make_supervisor) -> None:
    supervisor = make_supervisor()
    supervisor.recover()
    lock = FileLock(supervisor.paths.switch_lock)
    assert lock.acquire() is True
    try:
        result = _promote(supervisor)
    finally:
        lock.release()

    assert result["ok"] is False
    assert result["status"] == "switch_in_progress"
    assert supervisor.slots.get_active_slot() == "A"


def test_supervisor_restart_adopts_running_process(make_supervisor, tmp_path) -> None:
    first = make_supervisor()
    first.recover()
    pid = first.state()["process"]["pid"]

    second = Supervisor(
        first.config,
        paths=ControlPaths(tmp_path / "sup"),
        slot_manager=SlotManager(base_dir=tmp_path / "slots"),
    )
    result = second.recover()

    assert result["action"] == "adopted"
    assert result["pid"] == pid
    assert second.state()["process"]["pid"] == pid
    assert probe_http("127.0.0.1", second.config.port, "/health").ok is True


def test_recover_after_interrupted_switch_rolls_back(make_supervisor, tmp_path) -> None:
    first = make_supervisor()
    first.recover()
    primary = first.config.port
    record_a = first.state()["process"]

    # 模拟：候选 B 已在主端口启动，active_slot 已写 B，但状态停在 switching（切换中途崩溃）
    from aiive.supervisor.process_control import ProcessRecord

    first.controller.stop(ProcessRecord.from_dict(record_a))
    record_b, error = first.controller.start(
        slot="B", slot_root=tmp_path / "slots" / "B", port=primary,
    )
    assert error == "" and record_b is not None
    first.slots.set_active_slot("B")
    write_json_atomic(
        first.paths.state_file,
        {"phase": "switching", "previous_slot": "A", "process": record_b.as_dict(), "active_slot": "B"},
    )

    second = Supervisor(
        first.config,
        paths=ControlPaths(tmp_path / "sup"),
        slot_manager=SlotManager(base_dir=tmp_path / "slots"),
    )
    result = second.recover()

    assert result["action"] == "recovered_interrupted_switch"
    assert result["ok"] is True
    assert second.slots.get_active_slot() == "A"
    assert second.state()["process"]["slot"] == "A"
    assert probe_http("127.0.0.1", primary, "/health").ok is True
    assert not _pid_alive(record_b.pid)


class _StubClient:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def available(self) -> bool:
        return True

    def submit(self, kind, payload=None, **_kwargs):  # noqa: ANN001, ANN003
        self.submitted.append((kind, payload or {}))
        return {"ok": True, "status": "accepted", "request_id": "req-1"}


class _HealthyProbe:
    def check(self, slot: str, slot_root: str):  # noqa: ANN001
        from aiive.supervisor.health_probe import HealthResult

        return HealthResult(healthy=True, slot=slot, message="ok")


class _PassGate:
    def evaluate(self, candidate_app_dir=None, persist=True):  # noqa: ANN001, ANN003
        from aiive.eval.models import GateResult

        return GateResult(ok=True, error="")


def test_release_manager_defers_switch_to_supervisor(tmp_path) -> None:
    manager = SlotManager(base_dir=tmp_path / "slots")
    manager.init_slots()
    client = _StubClient()
    release = ReleaseManager(
        manager=manager,
        probe=_HealthyProbe(),  # type: ignore[arg-type]
        state_file=tmp_path / "runtime" / "release_state.json",
        gate=_PassGate(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
    )

    promoted = release.promote()

    assert promoted["ok"] is True
    assert promoted["status"] == "switch_requested"
    assert promoted["new_active"] == "B"
    assert manager.get_active_slot() == "A"  # 指针只由守护进程在验证后切换
    assert client.submitted and client.submitted[0][0] == "promote"
    assert release.read_state()["status"] == "switch_requested"

    monitored = release.monitor_once()
    assert monitored["action"] == "supervised"

    rolled_back = release.rollback()
    assert rolled_back["status"] == "switch_requested"
    assert client.submitted[-1][0] == "rollback"


def test_degraded_state_stops_auto_restart(make_supervisor) -> None:
    supervisor = make_supervisor()
    write_json_atomic(
        supervisor.paths.state_file,
        {"phase": "degraded", "last_error": "restart_budget_exhausted", "active_slot": "A"},
    )

    outcome = supervisor.tick()

    assert outcome["action"] == "degraded"
    assert outcome["last_error"] == "restart_budget_exhausted"
    assert supervisor.controller.records() == []


def test_supervisor_client_reports_unavailable_without_daemon(tmp_path) -> None:
    client = SupervisorClient(ControlPaths(tmp_path / "sup"))

    assert client.available() is False
    assert client.submit("promote", {"target": "B"})["error"] == "supervisor_unavailable"


def _pid_alive(pid: int) -> bool:
    from aiive.supervisor.control import pid_alive

    return pid_alive(pid)
