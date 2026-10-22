"""Supervisor 守护进程：A/B 槽位晋升的真实进程编排。

职责边界
--------
- 本模块运行在**独立进程**中，不导入 FastAPI 应用，也不依赖被托管进程存活；
- 应用进程（被托管者）只通过 runtime/supervisor 下的文件提交请求、读取状态；
- 所有进程操作都经 ProcessController（受控参数 + 记录表所有权），不使用 Agent 的 run_shell。

一次晋升的顺序（任一环节失败都必须让"实际运行版本 / active_slot / 持久化状态"三者一致）
------------------------------------------------------------------------------
1. 候选槽位静态健康检查（manifest、app 目录、可导入性）；
2. 在暂存端口启动候选进程并等待 HTTP 健康（证明候选真的能跑起来）；
3. 停止旧进程 → 在主端口启动候选进程 → 再次健康检查；
4. 只有第 3 步通过后才写 active_slot 与 release_state（观察窗开始）；
5. 第 2/3 步失败 → 停止候选、重启旧槽位并验证健康，active_slot 保持旧值。

退出与恢复
----------
- 应用进程异常退出：观察窗内 → 自动回滚并验证；观察窗外 → 在重启预算内重启，
  预算耗尽 → 回滚到上一槽位；回滚也失败 → 进入 degraded 并停止重试（不静默循环）。
- Supervisor 自身重启：读取 state.json，接管仍存活的进程（adopt），清理僵尸记录；
  若上次停在 switching 阶段，则判定为中断切换，回滚到 previous 槽位并验证。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiive.supervisor.control import (
    ControlPaths,
    FileLock,
    append_event,
    find_result_by_key,
    finish_request,
    iso_now,
    pending_requests,
    pid_alive,
    read_events,
    read_json,
    utc_now,
    write_json_atomic,
)
from aiive.supervisor.health_http import probe_http
from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.process_control import ProcessController, ProcessRecord
from aiive.supervisor.slot_manager import SlotManager

_SLOTS = ("A", "B")


@dataclass
class SupervisorConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    app_module: str = "aiive.main:app"
    app_command: str = ""
    health_path: str = "/health"
    start_timeout: float = 90.0
    stop_timeout: float = 15.0
    poll_interval: float = 2.0
    max_restarts: int = 3
    restart_window: float = 300.0
    monitor_seconds: int = 600
    python_executable: str = ""
    state_dir: str = ""

    @classmethod
    def from_settings(cls) -> "SupervisorConfig":
        from aiive.config import settings

        return cls(
            host=str(settings.supervisor_host),
            port=int(settings.supervisor_port),
            app_module=str(settings.supervisor_app_module),
            app_command=str(settings.supervisor_app_command or ""),
            health_path=str(settings.supervisor_health_path),
            start_timeout=float(settings.supervisor_start_timeout_seconds),
            stop_timeout=float(settings.supervisor_stop_timeout_seconds),
            poll_interval=float(settings.supervisor_poll_seconds),
            max_restarts=int(settings.supervisor_max_restarts),
            restart_window=float(settings.supervisor_restart_window_seconds),
            monitor_seconds=int(settings.supervisor_monitor_seconds),
            python_executable="",
            state_dir=str(settings.supervisor_state_dir or ""),
        )


class Supervisor:
    """单实例守护进程：拥有应用进程，负责切换、健康检查与自动恢复。"""

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        paths: ControlPaths | None = None,
        controller: ProcessController | None = None,
        slot_manager: SlotManager | None = None,
        static_probe: HealthProbe | None = None,
        probe: Any = None,
    ) -> None:
        self.config: SupervisorConfig = config or SupervisorConfig()
        self.paths: ControlPaths = (paths or ControlPaths.resolve(self.config.state_dir)).ensure()
        self.slots: SlotManager = slot_manager or SlotManager()
        self.static_probe: HealthProbe = static_probe or HealthProbe()
        self.controller: ProcessController = controller or ProcessController(
            self.paths,
            host=self.config.host,
            app_module=self.config.app_module,
            app_command_template=self.config.app_command,
            python_executable=self.config.python_executable or None,
            stop_timeout=self.config.stop_timeout,
        )
        self._probe: Any = probe or probe_http
        self._lock: FileLock = FileLock(self.paths.supervisor_lock)
        self._restart_history: list[float] = []
        self._started_at: str = iso_now()

    # ── 状态与事件 ────────────────────────────────────────────
    def state(self) -> dict[str, Any]:
        return read_json(self.paths.state_file)

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        return read_events(self.paths, limit)

    def _write_state(self, **updates: Any) -> dict[str, Any]:
        state = self.state()
        state.update(updates)
        state["supervisor_pid"] = os.getpid()
        state["supervisor_started_at"] = self._started_at
        state["updated_at"] = iso_now()
        write_json_atomic(self.paths.state_file, state)
        return state

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        return append_event(self.paths, kind, supervisor_pid=os.getpid(), **fields)

    def _slot_root(self, slot: str) -> Path:
        for item in self.slots.list_slots():
            if item.name == slot:
                return item.root
        return self.paths.base / "slots" / slot

    def _static_health(self, slot: str) -> dict[str, Any]:
        result = self.static_probe.check(slot, str(self._slot_root(slot)))
        return {"healthy": result.healthy, "message": result.message, "checks": result.checks}

    def _record_from_state(self, state: dict[str, Any], key: str = "process") -> ProcessRecord | None:
        raw = state.get(key)
        return ProcessRecord.from_dict(raw) if isinstance(raw, dict) else None

    # ── 生命周期 ──────────────────────────────────────────────
    def run_forever(self) -> None:
        self.paths.ensure()
        if not self._lock.acquire():
            raise RuntimeError("supervisor_already_running")
        self._event("supervisor_started", port=self.config.port, pid=os.getpid())
        try:
            self.recover()
            while True:
                self.tick()
                time.sleep(max(0.05, self.config.poll_interval))
        finally:
            self._write_state(phase="stopped", stopped_at=iso_now())
            self._event("supervisor_stopped", pid=os.getpid())
            self._lock.release()

    def recover(self) -> dict[str, Any]:
        """Supervisor 启动时的状态收敛：接管、清理、必要时回滚。"""
        self.paths.ensure()
        # 上一实例若在切换中途崩溃，switch.lock 会残留；此时我们是唯一实例，可安全清理。
        self.paths.switch_lock.unlink(missing_ok=True)
        state = self.state()
        active = self.slots.get_active_slot()
        phase = str(state.get("phase") or "")
        pruned = self.controller.prune()

        if phase == "switching":
            previous = str(state.get("previous_slot") or ("B" if active == "A" else "A"))
            candidate = self._record_from_state(state, "candidate")
            if candidate is not None:
                self.controller.stop(candidate)
            current = self._record_from_state(state)
            if current is not None:
                self.controller.stop(current)
            restored = self._restore_previous(previous, reason="interrupted_switch")
            result = {
                "ok": bool(restored.get("ok")),
                "action": "recovered_interrupted_switch",
                "previous_slot": previous,
                "restore": restored,
                "pruned_pids": pruned,
            }
            self._event("recovered_interrupted_switch", ok=result["ok"], detail=restored)
            return result

        record = self._record_from_state(state)
        if record is not None and self.controller.alive(record):
            health = self._probe(self.config.host, record.port, self.config.health_path, timeout=2.0)
            healthy = bool(getattr(health, "ok", False))
            if healthy:
                updates: dict[str, Any] = {"phase": "running", "process": record.as_dict()}
                if record.slot != active:
                    # 实际运行版本与活动槽位不一致：以真实进程为准收敛指针。
                    self.slots.set_active_slot(record.slot)
                    updates["active_slot"] = record.slot
                    self._event(
                        "reconciled_active_slot", slot=record.slot, previous=active, pid=record.pid,
                    )
                else:
                    updates["active_slot"] = record.slot
                self._write_state(**updates)
                self._event("adopted_running_process", slot=record.slot, pid=record.pid)
                return {"ok": True, "action": "adopted", "slot": record.slot, "pid": record.pid}
            self.controller.stop(record)
            self._event("stopped_unhealthy_process", slot=record.slot, pid=record.pid)
        elif record is not None:
            self._event("pruned_dead_process", slot=record.slot, pid=record.pid)

        record_new, error = self._ensure_running(active)
        self._event(
            "recovered_start", slot=active, ok=bool(record_new), error=error,
            pid=record_new.pid if record_new else None,
        )
        return {
            "ok": bool(record_new),
            "action": "started",
            "slot": active,
            "error": error,
            "pruned_pids": pruned,
        }

    def tick(self) -> dict[str, Any]:
        """一次守护循环：回收僵尸 → 处理一个请求 → 监测进程。"""
        self.paths.ensure()
        for item in self.controller.reap():
            self._event("process_exited", pid=item["pid"], exit_code=item["exit_code"])
        for request in pending_requests(self.paths):
            return self._dispatch(request)
        return self._watch()

    # ── 请求分发 ──────────────────────────────────────────────
    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = str(request.get("kind") or "")
        key = str(request.get("idempotency_key") or "")
        if key:
            cached = find_result_by_key(self.paths, key)
            if cached:
                result = {**cached, "deduplicated": True}
                finish_request(self.paths, request, result)
                self._event("request_deduplicated", request_kind=kind, idempotency_key=key)
                return result

        lock = FileLock(self.paths.switch_lock, stale_seconds=max(30, int(self.config.start_timeout * 2)))
        if not lock.acquire():
            result = {"ok": False, "status": "switch_in_progress", "error": "switch_in_progress"}
            finish_request(self.paths, request, result)
            self._event("request_rejected", request_kind=kind, reason="switch_in_progress")
            return result
        try:
            handlers = {
                "promote": self._handle_promote,
                "rollback": self._handle_rollback,
                "restart": self._handle_restart,
                "status": self._handle_status,
            }
            handler = handlers.get(kind)
            if handler is None:
                result = {"ok": False, "status": "rejected", "error": "unknown_request_kind"}
            else:
                result = handler(request)
        except Exception as exc:  # 守护进程不能因单次请求崩溃
            self._event("request_failed", request_kind=kind, error=f"{exc.__class__.__name__}: {exc}")
            result = {"ok": False, "status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        finally:
            lock.release()
        finish_request(self.paths, request, result)
        return result

    # ── promote ──────────────────────────────────────────────
    def _handle_promote(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("payload") or {}
        active = self.slots.get_active_slot()
        target = str(payload.get("target") or ("B" if active == "A" else "A")).upper()
        if target not in _SLOTS:
            return {"ok": False, "status": "rejected", "error": "invalid_target_slot"}
        if payload.get("gate_ok") is not True:
            # 进程切换不重跑 Eval/契约（那需要应用侧上下文），但必须由通过闸门的
            # 调用方显式声明；否则文件协议就成了绕过安全门的捷径。
            self._event("switch_rejected", target=target, reason="gate_not_passed")
            return {"ok": False, "status": "rejected", "error": "gate_not_passed"}
        if target == active:
            record, error = self._ensure_running(active)
            if record is None:
                return {"ok": False, "status": "noop_unhealthy", "error": error, "active_slot": active}
            return {
                "ok": True, "status": "already_active", "active_slot": active, "pid": record.pid,
            }

        static = self._static_health(target)
        if not static["healthy"]:
            self._event("switch_failed", target=target, reason="candidate_static_health_failed")
            return {
                "ok": False, "status": "switch_failed",
                "error": "candidate_static_health_failed", "health": static, "active_slot": active,
            }

        self._write_state(
            phase="switching", previous_slot=active, candidate_slot=target,
            release_id=str(payload.get("release_id") or ""), last_error=None,
        )
        self._event("switch_started", target=target, previous=active)

        staging_port = int(self.config.port) + 1
        candidate, error = self.controller.start(
            slot=target, slot_root=self._slot_root(target), port=staging_port, role="candidate",
        )
        if candidate is None:
            self._fail_switch(active, "candidate_start_failed", error=error)
            return {
                "ok": False, "status": "switch_failed", "error": "candidate_start_failed",
                "detail": error, "active_slot": active,
            }
        self._write_state(candidate=candidate.as_dict())
        self._event("candidate_started", slot=target, pid=candidate.pid, port=staging_port)

        staging_wait = self._wait_process_healthy(candidate, staging_port)
        if not staging_wait["ok"]:
            self.controller.stop(candidate)
            reason = (
                "candidate_start_failed"
                if staging_wait["reason"] == "process_exited"
                else "candidate_health_failed"
            )
            self._fail_switch(active, reason, health=staging_wait.get("health"))
            return {
                "ok": False, "status": "switch_failed", "error": reason,
                "health": staging_wait.get("health"), "active_slot": active,
            }
        self._event("candidate_healthy", slot=target, pid=candidate.pid, port=staging_port)
        staging_stop = self.controller.stop(candidate)
        if not staging_stop.get("ok"):
            # 候选进程没停下：主端口切换仍可进行（端口不同），但必须留下可追踪记录，
            # 避免出现"登记表里还有进程但没人解释"的中间状态。
            self._event(
                "candidate_stop_failed", slot=target, pid=candidate.pid, detail=staging_stop,
            )
        self._write_state(candidate=None)

        current = self._record_from_state(self.state())
        if current is not None:
            self.controller.stop(current)
        self._event("previous_process_stopped", slot=active, pid=current.pid if current else None)

        new_record, error = self.controller.start(
            slot=target, slot_root=self._slot_root(target), port=self.config.port, role="active",
        )
        if new_record is None:
            self._event(
                "switch_failed", target=target, reason="candidate_start_failed_on_primary",
                detail=error,
            )
            restore = self._restore_previous(active, reason="candidate_start_failed_on_primary")
            return {
                "ok": False, "status": "switch_failed", "error": "candidate_start_failed_on_primary",
                "detail": error, "rollback": restore, "active_slot": self.slots.get_active_slot(),
            }
        health = self._wait_process_healthy(new_record, self.config.port)
        if not health["ok"]:
            self.controller.stop(new_record)
            reason = (
                "post_switch_start_failed"
                if health["reason"] == "process_exited"
                else "post_switch_health_failed"
            )
            self._event("switch_failed", target=target, reason=reason, health=health.get("health"))
            restore = self._restore_previous(active, reason=reason, health=health.get("health"))
            return {
                "ok": False, "status": "switch_failed", "error": reason,
                "health": health.get("health"), "rollback": restore,
                "active_slot": self.slots.get_active_slot(),
            }

        self.slots.set_active_slot(target)
        monitor_until = (
            datetime.now(timezone.utc).timestamp() + max(0, self.config.monitor_seconds)
        )
        self._write_state(
            phase="running", active_slot=target, previous_slot=active,
            process=new_record.as_dict(), candidate=None, candidate_slot=None,
            monitor_until=datetime.fromtimestamp(monitor_until, timezone.utc).isoformat(),
            successful_checks=1, last_error=None,
        )
        self._write_release_state(
            status="monitoring", previous_active=active, active_slot=target,
            monitor_until=datetime.fromtimestamp(monitor_until, timezone.utc).isoformat(),
        )
        self._event("switch_completed", slot=target, previous=active, pid=new_record.pid, health=health)
        return {
            "ok": True, "status": "switched", "active_slot": target, "previous_slot": active,
            "pid": new_record.pid, "health": health, "monitor_until": monitor_until,
        }

    def _fail_switch(self, active: str, reason: str, **detail: Any) -> None:
        self._write_state(phase="running", active_slot=active, candidate=None, last_error=reason)
        self._write_release_state(
            status="switch_failed", previous_active=None, active_slot=active, reason=reason,
        )
        self._event("switch_failed", reason=reason, active=active, **detail)

    # ── rollback / restart / status ───────────────────────────
    def _handle_rollback(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("payload") or {}
        state = self.state()
        active = self.slots.get_active_slot()
        target = str(payload.get("target") or state.get("previous_slot") or ("B" if active == "A" else "A")).upper()
        if target not in _SLOTS:
            return {"ok": False, "status": "rejected", "error": "invalid_target_slot"}
        if target == active:
            return {"ok": True, "status": "already_active", "active_slot": active}
        self._write_state(phase="switching", previous_slot=active, candidate_slot=target)
        self._event("rollback_started", target=target, previous=active, reason=payload.get("reason") or "manual")
        restore = self._restore_previous(target, reason=str(payload.get("reason") or "manual"))
        if not restore.get("ok"):
            self._event("rollback_failed", target=target, detail=restore)
            return {
                "ok": False, "status": "rollback_failed", "error": restore.get("error", "restore_failed"),
                "rollback": restore, "active_slot": self.slots.get_active_slot(),
            }
        self._event("rollback_completed", slot=target, pid=restore.get("pid"))
        return {
            "ok": True, "status": "rolled_back", "active_slot": target, "previous_slot": active,
            "pid": restore.get("pid"), "health": restore.get("health"),
        }

    def _handle_restart(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("payload") or {}
        slot = str(payload.get("slot") or self.slots.get_active_slot()).upper()
        if slot not in _SLOTS:
            return {"ok": False, "status": "rejected", "error": "invalid_slot"}
        self._event("restart_requested", slot=slot)
        current = self._record_from_state(self.state())
        if current is not None:
            self.controller.stop(current)
        record, error = self.controller.start(
            slot=slot, slot_root=self._slot_root(slot), port=self.config.port, role="active",
        )
        if record is None:
            self._write_state(phase="degraded", last_error=f"restart_failed:{error}")
            self._event("restart_failed", slot=slot, error=error)
            return {"ok": False, "status": "restart_failed", "error": error, "active_slot": slot}
        health = self._wait_process_healthy(record, self.config.port)
        if not health["ok"]:
            self.controller.stop(record)
            error_code = (
                "restart_process_exited"
                if health["reason"] == "process_exited"
                else "restart_health_failed"
            )
            self._write_state(phase="degraded", last_error=error_code, process=None)
            self._event("restart_failed", slot=slot, error=error_code, health=health.get("health"))
            return {
                "ok": False, "status": "restart_failed", "error": error_code,
                "health": health.get("health"), "active_slot": slot,
            }
        self._write_state(phase="running", active_slot=slot, process=record.as_dict(), last_error=None)
        self._event("restart_completed", slot=slot, pid=record.pid)
        return {"ok": True, "status": "restarted", "active_slot": slot, "pid": record.pid}

    def _handle_status(self, _request: dict[str, Any]) -> dict[str, Any]:
        state = self.state()
        record = self._record_from_state(state)
        health = self._probe(self.config.host, self.config.port, self.config.health_path, timeout=2.0)
        return {
            "ok": True,
            "status": "status",
            "active_slot": self.slots.get_active_slot(),
            "phase": state.get("phase", ""),
            "pid": record.pid if record else None,
            "process_alive": self.controller.alive(record),
            "healthy": bool(getattr(health, "ok", False)),
            "monitor_until": state.get("monitor_until"),
        }

    # ── 运行期监测 ────────────────────────────────────────────
    def _watch(self) -> dict[str, Any]:
        state = self.state()
        if str(state.get("phase") or "") == "switching":
            return {"ok": True, "action": "switching"}
        active = self.slots.get_active_slot()
        record = self._record_from_state(state)
        if record is None:
            if str(state.get("phase") or "") == "degraded":
                # degraded 表示自动恢复已失败：停止无休止重试，等待人工 restart 请求。
                return {"ok": False, "action": "degraded", "last_error": state.get("last_error")}
            started, error = self._ensure_running(active)
            return {
                "ok": started is not None, "action": "started", "slot": active, "error": error,
            }
        monitor_until = _parse_deadline(state.get("monitor_until"))
        in_monitor = monitor_until is not None and utc_now() < monitor_until
        previous = str(state.get("previous_slot") or "")

        if not self.controller.alive(record):
            self._event("app_exited_unexpectedly", slot=record.slot, pid=record.pid, in_monitor=in_monitor)
            self._write_state(process=None)
            if in_monitor and previous in _SLOTS and previous != record.slot:
                restore = self._restore_previous(previous, reason="app_exited_during_monitor")
                self._event("auto_rollback", reason="app_exited", ok=restore.get("ok"), detail=restore)
                return {"ok": bool(restore.get("ok")), "action": "auto_rollback", "rollback": restore}
            return self._restart_after_exit(record.slot, previous)

        if in_monitor:
            health = self._probe(self.config.host, self.config.port, self.config.health_path, timeout=2.0)
            if not getattr(health, "ok", False):
                self._event("monitor_health_failed", slot=record.slot, detail=health.as_dict()
                            if hasattr(health, "as_dict") else str(health))
                restore = self._restore_previous(previous or ("B" if record.slot == "A" else "A"),
                                                 reason="monitor_health_failed")
                self._event("auto_rollback", reason="monitor_health_failed", ok=restore.get("ok"))
                return {"ok": bool(restore.get("ok")), "action": "auto_rollback", "rollback": restore}
            self._write_state(successful_checks=int(state.get("successful_checks") or 0) + 1)
            return {"ok": True, "action": "monitoring_healthy"}
        if monitor_until is not None and state.get("previous_slot"):
            self._write_state(previous_slot=None, monitor_until=None)
            self._write_release_state(
                status="stable", previous_active=state.get("previous_slot"),
                active_slot=active,
            )
            self._event("monitoring_completed", slot=active)
            return {"ok": True, "action": "monitoring_completed"}
        return {"ok": True, "action": "healthy"}

    def _restart_after_exit(self, slot: str, previous: str) -> dict[str, Any]:
        now = time.monotonic()
        self._restart_history = [item for item in self._restart_history if now - item < self.config.restart_window]
        if len(self._restart_history) >= max(1, self.config.max_restarts):
            if previous in _SLOTS and previous != slot:
                restore = self._restore_previous(previous, reason="restart_budget_exhausted")
                self._event("auto_rollback", reason="restart_budget_exhausted", ok=restore.get("ok"))
                return {"ok": bool(restore.get("ok")), "action": "auto_rollback", "rollback": restore}
            self._write_state(phase="degraded", last_error="restart_budget_exhausted")
            self._event("degraded", reason="restart_budget_exhausted", slot=slot)
            return {"ok": False, "action": "degraded", "error": "restart_budget_exhausted"}
        self._restart_history.append(now)
        record, error = self._ensure_running(slot)
        self._event(
            "restart_after_exit", slot=slot, ok=record is not None, error=error,
            pid=record.pid if record else None, attempt=len(self._restart_history),
        )
        if record is None:
            self._write_state(phase="degraded", last_error=f"restart_failed:{error}")
            return {"ok": False, "action": "restart_failed", "error": error}
        return {"ok": True, "action": "restarted", "pid": record.pid}

    # ── 启动与恢复原版本 ──────────────────────────────────────
    def _ensure_running(self, slot: str) -> tuple[ProcessRecord | None, str]:
        """确保某槽位在主端口上运行且健康；必要时重启。"""
        record = self._record_from_state(self.state())
        if record is not None and record.slot == slot and self.controller.alive(record):
            health = self._probe(self.config.host, self.config.port, self.config.health_path, timeout=2.0)
            if getattr(health, "ok", False):
                return record, ""
            self.controller.stop(record)
            self._write_state(process=None)
        elif record is not None and self.controller.alive(record):
            self.controller.stop(record)
            self._write_state(process=None)
        started, error = self.controller.start(
            slot=slot, slot_root=self._slot_root(slot), port=self.config.port, role="active",
        )
        if started is None:
            return None, error
        health = self._wait_process_healthy(started, self.config.port)
        if not health["ok"]:
            self.controller.stop(started)
            self._write_state(process=None)
            return None, "health_check_failed"
        self._write_state(phase="running", active_slot=slot, process=started.as_dict(), last_error=None)
        return started, ""

    def _wait_process_healthy(
        self, record: ProcessRecord, port: int, *, timeout: float | None = None,
    ) -> dict[str, Any]:
        """等待进程健康；进程提前退出立即返回，不必等满超时。"""
        deadline = time.monotonic() + (self.config.start_timeout if timeout is None else timeout)
        health: dict[str, Any] = {}
        while True:
            if not self.controller.alive(record):
                return {"ok": False, "reason": "process_exited", "health": health}
            probe = self._probe(self.config.host, port, self.config.health_path, timeout=2.0)
            health = probe.as_dict() if hasattr(probe, "as_dict") else {"ok": bool(getattr(probe, "ok", False))}
            if health.get("ok"):
                return {"ok": True, "reason": "healthy", "health": health}
            if time.monotonic() >= deadline:
                return {"ok": False, "reason": "timeout", "health": health}
            time.sleep(0.2)

    def _restore_previous(self, slot: str, *, reason: str, **detail: Any) -> dict[str, Any]:
        """回滚：启动目标槽位并**验证**健康后才写 active_slot。"""
        record = self._record_from_state(self.state())
        if record is not None:
            self.controller.stop(record)
        started, error = self.controller.start(
            slot=slot, slot_root=self._slot_root(slot), port=self.config.port, role="active",
        )
        if started is None:
            self._write_state(phase="degraded", last_error=f"restore_failed:{error}", process=None)
            self._event("restore_failed", slot=slot, reason=reason, error=error, **detail)
            return {"ok": False, "error": error, "verified": False, "slot": slot}
        health = self._wait_process_healthy(started, self.config.port)
        if not health["ok"]:
            self.controller.stop(started)
            self._write_state(phase="degraded", last_error="restore_health_failed", process=None)
            self._event("restore_failed", slot=slot, reason=reason, health=health.get("health"), **detail)
            return {
                "ok": False, "error": "restore_health_failed",
                "verified": False, "health": health.get("health"),
            }
        self.slots.set_active_slot(slot)
        self._write_state(
            phase="running", active_slot=slot, process=started.as_dict(),
            previous_slot=None, monitor_until=None, candidate=None, last_error=None,
        )
        self._write_release_state(
            status="rolled_back", previous_active=None, active_slot=slot, reason=reason,
        )
        self._event("restored_previous", slot=slot, pid=started.pid, reason=reason, **detail)
        return {"ok": True, "verified": True, "slot": slot, "pid": started.pid, "health": health}

    # ── 发布状态（与 active_slot 保持一致） ───────────────────
    def _write_release_state(self, **updates: Any) -> None:
        path = self.paths.base.parent / "release_state.json"
        state = read_json(path)
        state.update(updates)
        state["updated_at"] = iso_now()
        state["source"] = "supervisor"
        write_json_atomic(path, state)

    def stop_managed_process(self) -> dict[str, Any]:
        """显式停止受管进程（运维/测试用），不改变 active_slot。"""
        record = self._record_from_state(self.state())
        result = self.controller.stop(record)
        self._write_state(process=None)
        self._event("managed_process_stopped", result=result)
        return result


def _parse_deadline(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _supervisor_status(paths: ControlPaths) -> dict[str, Any]:
    state = read_json(paths.state_file)
    pid = state.get("supervisor_pid")
    return {
        "state": state,
        "supervisor_alive": pid_alive(pid if isinstance(pid, int) else None),
        "events": read_events(paths, limit=20),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIive Supervisor：A/B 槽位进程托管")
    parser.add_argument("--state-dir", default="", help="Supervisor 运行目录（默认 runtime/supervisor）")
    parser.add_argument("--port", type=int, default=None, help="被托管应用监听端口")
    parser.add_argument("--once", action="store_true", help="恢复并执行一次循环后退出（运维/测试）")
    parser.add_argument("--status", action="store_true", help="打印当前状态与最近事件")
    args = parser.parse_args(argv)

    config = SupervisorConfig.from_settings()
    if args.state_dir:
        config.state_dir = args.state_dir
    if args.port is not None:
        config.port = args.port
    paths = ControlPaths.resolve(config.state_dir).ensure()

    if args.status:
        print(json.dumps(_supervisor_status(paths), ensure_ascii=False, indent=2))
        return 0

    supervisor = Supervisor(config, paths=paths)
    if args.once:
        result = supervisor.recover()
        tick = supervisor.tick()
        print(json.dumps({"recover": result, "tick": tick}, ensure_ascii=False, indent=2))
        return 0

    def _handle_signal(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    for name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, name, None)
        if candidate is not None:
            try:
                signal.signal(candidate, _handle_signal)
            except (ValueError, OSError):  # pragma: no cover - 非主线程
                pass
    supervisor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
