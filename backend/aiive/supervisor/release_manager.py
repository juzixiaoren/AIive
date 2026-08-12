"""Trusted Core Release Manager：原子槽位切换、观察窗与自动回滚。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiive.supervisor.health_probe import HealthProbe
from aiive.supervisor.slot_manager import ACTIVE_SLOT_FILE, SlotManager


_release_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReleaseManager:
    """Promotion 之后持续保留 previous slot，健康异常时自动切回。"""

    def __init__(
        self,
        manager: SlotManager | None = None,
        probe: HealthProbe | None = None,
        state_file: Path | None = None,
        monitor_seconds: int = 600,
    ) -> None:
        self._manager: SlotManager = manager or SlotManager()
        self._probe: HealthProbe = probe or HealthProbe()
        self._state_file: Path = state_file or (ACTIVE_SLOT_FILE.parent / "release_state.json")
        self._monitor_seconds: int = max(60, monitor_seconds)

    def promote(self) -> dict[str, Any]:
        with _release_lock:
            previous = self._manager.get_active_slot()
            if previous not in {"A", "B"}:
                return {"ok": False, "error": "invalid_active_slot"}
            current = "B" if previous == "A" else "A"
            pre_switch_health = self._check_slot(current)
            if not pre_switch_health["healthy"]:
                return {
                    "ok": False,
                    "error": "Health check failed for inactive slot",
                    "health": pre_switch_health,
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
