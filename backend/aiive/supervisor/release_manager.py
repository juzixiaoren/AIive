"""Trusted Core Release Manager：原子槽位切换、观察窗与自动回滚。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiive.eval.gate import SafetyGate, evaluate_promotion_gate
from aiive.supervisor.client import SupervisorClient
from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.slot_manager import (
    ACTIVE_SLOT_FILE,
    SlotManager,
    candidate_promotion_block,
    read_candidate_state,
)


_release_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReleaseManager:
    """Promotion 之后持续保留 previous slot，健康异常时自动切回。

    晋升（promote）在切换活跃指针前依次执行：
      1. 非活跃槽位健康检查（HealthProbe）；
      2. 自进化安全门（SafetyGate：内核契约 + Replay Eval + 候选子进程评测，fail-closed）。
    回滚（rollback）是紧急逃生通道，刻意不设闸门——目标槽位是此前已通过校验的版本。
    """

    def __init__(
        self,
        manager: SlotManager | None = None,
        probe: HealthProbe | None = None,
        state_file: Path | None = None,
        monitor_seconds: int = 600,
        gate: SafetyGate | None = None,
        client: SupervisorClient | None = None,
    ) -> None:
        self._manager: SlotManager = manager or SlotManager()
        self._probe: HealthProbe = probe or HealthProbe()
        self._state_file: Path = state_file or (ACTIVE_SLOT_FILE.parent / "release_state.json")
        self._monitor_seconds: int = max(60, monitor_seconds)
        # 闸门可注入（测试替身）；默认构造真实 SafetyGate。
        self._gate: SafetyGate = gate or SafetyGate()
        # 进程托管客户端：非 None 且守护进程存活时，切换交给 Supervisor 执行；
        # None 表示未启用 Supervisor（例如 docker compose 由容器编排负责重启）。
        self._client: SupervisorClient | None = client

    def promote(self, run_gate: bool = True) -> dict[str, Any]:
        with _release_lock:
            previous = self._manager.get_active_slot()
            if previous not in {"A", "B"}:
                return {"ok": False, "error": "invalid_active_slot"}
            current = "B" if previous == "A" else "A"
            # 候选版本核对（fail-closed）：被修复流程标记为不可晋升的槽位、
            # 或验证之后代码又被改动的槽位，一律不得晋升。
            blocked = self._candidate_block(current)
            if blocked is not None:
                return blocked
            pre_switch_health = self._check_slot(current)
            if not pre_switch_health["healthy"]:
                return {
                    "ok": False,
                    "error": "Health check failed for inactive slot",
                    "health": pre_switch_health,
                }
            # 自进化安全门禁：契约 + Replay Eval + 候选评测（fail-closed）。
            # 这是 promote 的真实生产入口（REST /api/selfdev/{id}/promote 与
            # promote_slot 工具都经由本方法），闸门必须在此生效。
            if run_gate:
                slot = next(
                    (item for item in self._manager.list_slots() if item.name == current), None,
                )
                gate_result = evaluate_promotion_gate(
                    slot.root if slot is not None else None,
                    gate=self._gate,
                )
                if not gate_result.ok:
                    return {
                        "ok": False,
                        "error": "Safety gate failed: promote blocked",
                        "gate": gate_result.to_summary(),
                    }
            # Supervisor 托管：真正启动候选进程并验证，切换由守护进程完成。
            # 本进程随后会被替换，因此只提交请求并返回 accepted，不等待结果。
            if self._client is not None and self._client.available():
                accepted = self._client.submit(
                    "promote",
                    {"target": current, "gate_ok": bool(run_gate)},
                    idempotency_key=str(uuid4()),
                )
                if not accepted.get("ok"):
                    return {
                        "ok": False,
                        "error": accepted.get("error", "supervisor_submit_failed"),
                        "previous_active": previous,
                    }
                self._write_state({
                    "release_id": str(uuid4()),
                    "status": "switch_requested",
                    "request_id": accepted.get("request_id", ""),
                    "previous_active": previous,
                    "active_slot": previous,
                    "target_slot": current,
                    "requested_at": _now().isoformat(),
                })
                return {
                    "ok": True,
                    "status": "switch_requested",
                    "request_id": accepted.get("request_id", ""),
                    "previous_active": previous,
                    "new_active": current,
                }

            self._manager.set_active_slot(current)
            result = {"ok": True, "previous_active": previous, "new_active": current}
            now = _now()
            state: dict[str, Any] = {
                "release_id": str(uuid4()),
                "status": "monitoring",
                "previous_active": previous,
                "active_slot": current,
                "activated_at": now.isoformat(),
                "monitor_until": (now + timedelta(seconds=self._monitor_seconds)).isoformat(),
                "successful_checks": 0,
            }
            self._write_state(state)
            health = self._check_slot(current)
            if not health["healthy"]:
                rollback = self._rollback_to(previous)
                state.update({
                    "status": "rolled_back",
                    "rollback_reason": "post_switch_health_failed",
                    "rollback_result": rollback,
                    "completed_at": _now().isoformat(),
                })
                self._write_state(state)
                return {**result, "ok": False, "auto_rollback": rollback, "health": health}
            state["successful_checks"] = 1
            self._write_state(state)
            return {**result, "release_id": state["release_id"], "release_status": "monitoring"}

    def monitor_once(self) -> dict[str, Any]:
        with _release_lock:
            state = self.read_state()
            if self._client is not None and self._client.available():
                # 守护进程负责进程级健康监测与自动回滚，应用侧不得重复干预。
                return {"ok": True, "action": "supervised", "state": state}
            if state.get("status") != "monitoring":
                return {"ok": True, "action": "noop", "state": state}
            current = str(state.get("active_slot") or "")
            previous = str(state.get("previous_active") or "")
            if current not in {"A", "B"} or previous not in {"A", "B"} or current == previous:
                state.update({"status": "invalid", "completed_at": _now().isoformat()})
                self._write_state(state)
                return {"ok": False, "action": "invalid_state", "state": state}
            if self._manager.get_active_slot() != current:
                state.update({"status": "superseded", "completed_at": _now().isoformat()})
                self._write_state(state)
                return {"ok": True, "action": "superseded", "state": state}
            health = self._check_slot(current)
            if not health["healthy"]:
                rollback = self._rollback_to(previous)
                state.update({
                    "status": "rolled_back" if rollback.get("ok") else "rollback_failed",
                    "rollback_reason": "monitor_health_failed",
                    "rollback_result": rollback,
                    "completed_at": _now().isoformat(),
                })
                self._write_state(state)
                return {"ok": bool(rollback.get("ok")), "action": "auto_rollback", "state": state}
            state["successful_checks"] = int(state.get("successful_checks", 0)) + 1
            try:
                monitor_until = datetime.fromisoformat(str(state["monitor_until"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                rollback = self._rollback_to(previous)
                state.update({
                    "status": "rolled_back" if rollback.get("ok") else "rollback_failed",
                    "rollback_reason": "invalid_monitor_deadline",
                    "rollback_result": rollback,
                    "completed_at": _now().isoformat(),
                })
                self._write_state(state)
                return {"ok": bool(rollback.get("ok")), "action": "auto_rollback", "state": state}
            if monitor_until.tzinfo is None:
                monitor_until = monitor_until.replace(tzinfo=timezone.utc)
            if _now() >= monitor_until:
                state.update({"status": "stable", "completed_at": _now().isoformat()})
            self._write_state(state)
            return {"ok": True, "action": "healthy", "state": state}

    def rollback(self) -> dict[str, Any]:
        with _release_lock:
            state = self.read_state()
            current = self._manager.get_active_slot()
            previous = str(state.get("previous_active") or ("B" if current == "A" else "A"))
            if self._client is not None and self._client.available():
                accepted = self._client.submit(
                    "rollback", {"target": previous}, idempotency_key=str(uuid4()),
                )
                if not accepted.get("ok"):
                    return {"ok": False, "error": accepted.get("error", "supervisor_submit_failed")}
                state.update({
                    "status": "switch_requested",
                    "request_id": accepted.get("request_id", ""),
                    "requested_at": _now().isoformat(),
                })
                self._write_state(state)
                return {
                    "ok": True,
                    "status": "switch_requested",
                    "request_id": accepted.get("request_id", ""),
                    "rolled_back_to": previous,
                }
            result = self._rollback_to(previous)
            state.update({
                "status": "rolled_back" if result.get("ok") else "rollback_failed",
                "rollback_reason": "manual",
                "rollback_result": result,
                "completed_at": _now().isoformat(),
            })
            self._write_state(state)
            return result

    def _rollback_to(self, previous: str) -> dict[str, Any]:
        current = self._manager.get_active_slot()
        if previous not in {"A", "B"}:
            return {"ok": False, "error": "invalid_previous_slot"}
        health = self._check_slot(previous)
        if not health["healthy"]:
            return {"ok": False, "error": "Previous slot is unhealthy", "health": health}
        self._manager.set_active_slot(previous)
        return {"ok": True, "rolled_back_from": current, "rolled_back_to": previous}

    def read_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _candidate_block(self, name: str) -> dict[str, Any] | None:
        """晋升前的候选版本核对：返回阻断结果，或 None 表示放行。

        只对"由修复流程登记过状态"的槽位生效（无状态文件的槽位保持原有行为）：
        - promotable=False：该槽位装着失败/未验证的代码，禁止晋升；
        - 指纹与登记值不一致：验证之后代码被改动，禁止晋升。
        """
        slot = next(
            (item for item in self._manager.list_slots() if item.name == name), None,
        )
        if slot is None:
            return None
        blocked = candidate_promotion_block(slot.root)
        if blocked is None:
            return None
        return {
            "ok": False,
            "error": blocked,
            "candidate_state": read_candidate_state(slot.root),
        }

    def _check_slot(self, name: str) -> dict[str, Any]:
        slot = next((item for item in self._manager.list_slots() if item.name == name), None)
        if slot is None:
            return {"healthy": False, "message": "slot_not_found"}
        result = self._probe.check(name, str(slot.root))
        return {"healthy": result.healthy, "message": result.message, "checks": result.checks}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.parent / f".{self._state_file.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_file)
            try:
                directory_fd = os.open(self._state_file.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
