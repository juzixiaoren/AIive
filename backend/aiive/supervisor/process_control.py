"""受控进程控制：启动、停止、存活判定与所有权登记。

设计约束（对应"不得误杀无关进程"）：
- 只管理 processes.json 中登记过的 pid；任何不在登记表里的进程一律拒绝操作；
- 从不按进程名、端口或命令行扫描杀进程；
- 停止时先请求优雅退出，超时后结束整棵进程树（POSIX 进程组 / Windows taskkill /T）；
- 进程记录同时写入 pid、端口、槽位、启动时间与命令，便于事后解释"为什么被杀"。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiive.supervisor.control import ControlPaths, iso_now, pid_alive, read_json, write_json_atomic

_DEFAULT_STOP_TIMEOUT = 15.0


@dataclass
class ProcessRecord:
    """一个由 Supervisor 拥有的应用进程。"""

    slot: str
    pid: int
    port: int
    started_at: str
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    log_file: str = ""
    role: str = "active"

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "command": list(self.command),
            "cwd": self.cwd,
            "log_file": self.log_file,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProcessRecord | None":
        try:
            return cls(
                slot=str(raw.get("slot") or ""),
                pid=int(raw.get("pid") or 0),
                port=int(raw.get("port") or 0),
                started_at=str(raw.get("started_at") or ""),
                command=[str(item) for item in raw.get("command") or []],
                cwd=str(raw.get("cwd") or ""),
                log_file=str(raw.get("log_file") or ""),
                role=str(raw.get("role") or "active"),
            )
        except (TypeError, ValueError):
            return None


class ProcessController:
    """应用进程的唯一控制入口；记录表即所有权凭据。"""

    def __init__(
        self,
        paths: ControlPaths,
        *,
        host: str = "127.0.0.1",
        app_module: str = "aiive.main:app",
        app_command_template: str = "",
        python_executable: str | None = None,
        stop_timeout: float = _DEFAULT_STOP_TIMEOUT,
    ) -> None:
        self._paths: ControlPaths = paths
        self._host: str = host
        self._app_module: str = app_module
        self._template: str = app_command_template.strip()
        self._python: str = python_executable or sys.executable
        self._stop_timeout: float = stop_timeout
        self._children: dict[int, subprocess.Popen[Any]] = {}

    # ── 记录表 ────────────────────────────────────────────────
    def records(self) -> list[ProcessRecord]:
        raw = read_json(self._paths.process_file)
        items = raw.get("processes")
        if not isinstance(items, list):
            return []
        records: list[ProcessRecord] = []
        for item in items:
            if isinstance(item, dict):
                record = ProcessRecord.from_dict(item)
                if record is not None:
                    records.append(record)
        return records

    def _write_records(self, records: list[ProcessRecord]) -> None:
        write_json_atomic(
            self._paths.process_file,
            {"processes": [record.as_dict() for record in records], "updated_at": iso_now()},
        )

    def owned(self, pid: int) -> bool:
        return any(record.pid == pid for record in self.records())

    # ── 启动 ──────────────────────────────────────────────────
    def command_for(self, *, slot: str, slot_root: Path, port: int) -> list[str]:
        if self._template:
            return [
                token.format(
                    python=self._python,
                    host=self._host,
                    port=port,
                    slot=slot,
                    slot_root=str(slot_root),
                    app_module=self._app_module,
                )
                for token in _split_template(self._template)
            ]
        return [
            self._python, "-m", "uvicorn", self._app_module,
            "--host", self._host, "--port", str(port),
        ]

    def start(
        self,
        *,
        slot: str,
        slot_root: Path,
        port: int,
        role: str = "active",
        extra_env: dict[str, str] | None = None,
    ) -> tuple[ProcessRecord | None, str]:
        """启动槽位进程；返回 (记录, 错误码)。"""
        backend_dir = slot_root / "app" / "backend"
        if not (backend_dir / "aiive" / "main.py").is_file():
            return None, "slot_payload_missing"
        command = self.command_for(slot=slot, slot_root=slot_root, port=port)
        self._paths.ensure()
        log_path = self._paths.logs_dir / f"{slot}-{port}.log"
        env = dict(os.environ)
        env.update({
            "HOST": self._host,
            "PORT": str(port),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(backend_dir),
            "AIIVE_MANAGED_BY_SUPERVISOR": "1",
            "AIIVE_PROCESS_ROLE": role,
            "AIIVE_SLOT": slot,
        })
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        popen_kwargs: dict[str, Any] = {}
        if sys.platform.startswith("win"):
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        try:
            with log_path.open("ab") as log_stream:
                log_stream.write(
                    f"\n===== supervisor start slot={slot} port={port} at {iso_now()} =====\n".encode()
                )
                log_stream.flush()
                proc = subprocess.Popen(  # noqa: S603 - 受控参数，命令来自配置模板
                    command,
                    cwd=str(backend_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    **popen_kwargs,
                )
        except OSError as exc:
            return None, f"spawn_failed:{exc.__class__.__name__}"
        record = ProcessRecord(
            slot=slot,
            pid=proc.pid,
            port=port,
            started_at=iso_now(),
            command=list(command),
            cwd=str(backend_dir),
            log_file=str(log_path),
            role=role,
        )
        self._children[proc.pid] = proc
        self._write_records([*self.records(), record])
        return record, ""

    # ── 存活与回收 ────────────────────────────────────────────
    def alive(self, record: ProcessRecord | None) -> bool:
        if record is None:
            return False
        child = self._children.get(record.pid)
        if child is not None:
            return child.poll() is None
        return pid_alive(record.pid)

    def reap(self) -> list[dict[str, Any]]:
        """回收已退出的子进程，返回退出事件（避免僵尸）。"""
        exited: list[dict[str, Any]] = []
        for pid, child in list(self._children.items()):
            code = child.poll()
            if code is None:
                continue
            exited.append({"pid": pid, "exit_code": code})
            self._children.pop(pid, None)
        return exited

    # ── 停止 ──────────────────────────────────────────────────
    def stop(self, record: ProcessRecord | None, *, timeout: float | None = None) -> dict[str, Any]:
        if record is None:
            return {"ok": True, "stopped": False, "reason": "no_record"}
        if not self.owned(record.pid):
            return {"ok": False, "error": "process_not_owned", "pid": record.pid}
        wait_seconds = self._stop_timeout if timeout is None else timeout
        if not self.alive(record):
            self._drop(record.pid)
            return {"ok": True, "stopped": False, "pid": record.pid, "reason": "already_exited"}
        child = self._children.get(record.pid)
        if child is not None and sys.platform.startswith("win"):
            # Windows 的 taskkill 单次调用在本机约 1.7s，正常路径直接结束自己持有的
            # 子进程句柄（TerminateProcess），只有接管来的进程才走 taskkill /T。
            _terminate_child(child, wait_seconds)
        else:
            _terminate_tree(record.pid, graceful=True)
        deadline = time.monotonic() + max(0.1, wait_seconds)
        while time.monotonic() < deadline:
            if not self.alive(record):
                self._drop(record.pid)
                return {"ok": True, "stopped": True, "pid": record.pid}
            time.sleep(0.05)
        _terminate_tree(record.pid, graceful=False)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not self.alive(record):
                self._drop(record.pid)
                return {"ok": True, "stopped": True, "pid": record.pid, "forced": True}
            time.sleep(0.05)
        return {"ok": False, "error": "stop_timeout", "pid": record.pid}

    def _drop(self, pid: int) -> None:
        child = self._children.pop(pid, None)
        if child is not None and child.poll() is None:  # pragma: no cover - 兜底
            try:
                child.kill()
            except OSError:
                pass
        self._write_records([record for record in self.records() if record.pid != pid])

    def prune(self) -> list[int]:
        """丢弃已死亡进程的记录（Supervisor 重启后的状态收敛）。"""
        dead = [record.pid for record in self.records() if not pid_alive(record.pid)]
        if dead:
            self._write_records([record for record in self.records() if record.pid not in dead])
        return dead


def _split_template(template: str) -> list[str]:
    """按空白切分启动命令模板，支持用引号包裹含空格的路径。

    不使用 shlex：posix 模式会把 Windows 路径里的反斜杠当转义符，
    非 posix 模式又会保留引号。这里只处理引号与空白，不做转义。
    """
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    for char in template:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
        elif char in {'"', "'"}:
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _terminate_child(child: subprocess.Popen[Any], wait_seconds: float) -> None:
    """结束 Supervisor 自己启动的子进程（Windows 快速路径）。"""
    try:
        child.terminate()
    except OSError:
        return
    try:
        child.wait(timeout=max(0.2, min(2.0, wait_seconds)))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        child.kill()
        child.wait(timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_tree(pid: int, *, graceful: bool) -> None:
    if sys.platform.startswith("win"):
        # Windows 控制台进程没有可用的 SIGTERM 语义：taskkill 不带 /F 只是发
        # WM_CLOSE，python 进程不会响应，只会白白多等一个 stop_timeout。
        command = ["taskkill", "/PID", str(pid), "/T", "/F"]
        try:
            subprocess.run(command, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        return
    import signal

    try:
        if graceful:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM if graceful else signal.SIGKILL)
        except OSError:
            pass
