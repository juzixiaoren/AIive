"""Supervisor 控制面：请求/结果文件、事件流、状态快照与文件锁。

被托管的应用进程与 Supervisor 是两个独立进程，二者之间不使用网络或数据库，
只通过 runtime/supervisor 下的文件协作：

- state.json      Supervisor 当前认知（阶段、活动槽位、进程记录、观察窗）
- events.jsonl    只追加的事件流，用于解释每次切换为什么成功或失败
- requests/       应用侧提交的切换请求（原子创建，文件名有序）
- results/        Supervisor 对每个请求的结果（请求 ID 一一对应）
- processes.json  受管进程清单，即"进程所有权"的唯一凭据
- supervisor.lock 守护进程单实例锁
- switch.lock     切换临界区锁（避免并发切换）

所有写入都采用"临时文件 + os.replace"的原子替换，进程被强杀也不会留下半截 JSON。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_DIR = _REPO_ROOT / "runtime" / "supervisor"

# 超过该时长未更新且持有者已死亡的锁视为陈旧。
_LOCK_STALE_SECONDS = 300


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def pid_alive(pid: int | None) -> bool:
    """判断 pid 是否仍存活。

    Windows 上不能使用 os.kill(pid, 0)（会被解释为 TerminateProcess），
    改用 OpenProcess + GetExitCodeProcess 查询，避免把"探活"变成"杀进程"。
    """
    if not pid or pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class ControlPaths:
    """Supervisor 运行目录布局。"""

    base: Path

    @property
    def state_file(self) -> Path:
        return self.base / "state.json"

    @property
    def events_file(self) -> Path:
        return self.base / "events.jsonl"

    @property
    def process_file(self) -> Path:
        return self.base / "processes.json"

    @property
    def supervisor_lock(self) -> Path:
        return self.base / "supervisor.lock"

    @property
    def switch_lock(self) -> Path:
        return self.base / "switch.lock"

    @property
    def requests_dir(self) -> Path:
        return self.base / "requests"

    @property
    def results_dir(self) -> Path:
        return self.base / "results"

    @property
    def logs_dir(self) -> Path:
        return self.base / "logs"

    def ensure(self) -> "ControlPaths":
        for directory in (self.base, self.requests_dir, self.results_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def resolve(cls, base: str | Path | None = None) -> "ControlPaths":
        if base:
            return cls(Path(base))
        try:
            from aiive.config import settings

            configured = str(getattr(settings, "supervisor_state_dir", "") or "").strip()
        except Exception:  # pragma: no cover - 配置不可用时退回仓库默认目录
            configured = ""
        return cls(Path(configured) if configured else DEFAULT_RUNTIME_DIR)


def read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON：写临时文件 → fsync → os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def append_event(paths: ControlPaths, kind: str, **fields: Any) -> dict[str, Any]:
    """追加一条事件。事件流是解释切换结果的唯一时间线。"""
    event = {"ts": iso_now(), "kind": kind, **fields}
    paths.ensure()
    with paths.events_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return event


def read_events(paths: ControlPaths, limit: int = 50) -> list[dict[str, Any]]:
    try:
        lines = paths.events_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


class FileLock:
    """基于 O_EXCL 的跨进程互斥锁，带 pid 与陈旧检测。"""

    def __init__(self, path: Path, *, stale_seconds: int = _LOCK_STALE_SECONDS) -> None:
        self._path: Path = path
        self._stale_seconds: int = stale_seconds
        self._held: bool = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "ts": time.time()}).encode()
        for _attempt in range(2):
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._is_stale():
                    return False
                self._path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._held = True
            return True
        return False

    def release(self) -> None:
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False

    def _is_stale(self) -> bool:
        holder = read_json(self._path)
        pid = holder.get("pid")
        if isinstance(pid, int) and pid_alive(pid):
            return False
        try:
            age = time.time() - float(holder.get("ts") or 0)
        except (TypeError, ValueError):
            return True
        return age > self._stale_seconds or not isinstance(pid, int)

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def submit_request(
    paths: ControlPaths,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """应用侧提交一个控制请求（写入 requests/ 即返回，不等待执行）。"""
    paths.ensure()
    request_id = uuid4().hex
    record = {
        "id": request_id,
        "kind": kind,
        "payload": payload or {},
        "idempotency_key": idempotency_key,
        "submitted_at": iso_now(),
        "source_pid": os.getpid(),
    }
    filename = f"{time.time_ns()}-{request_id}.json"
    write_json_atomic(paths.requests_dir / filename, record)
    return record


def pending_requests(paths: ControlPaths) -> list[dict[str, Any]]:
    paths.ensure()
    items: list[dict[str, Any]] = []
    for path in sorted(paths.requests_dir.glob("*.json")):
        record = read_json(path)
        if record.get("id"):
            record["_path"] = str(path)
            items.append(record)
    return items


def finish_request(paths: ControlPaths, request: dict[str, Any], result: dict[str, Any]) -> None:
    """写入结果并移除请求文件（先写结果，保证不会丢结果）。"""
    request_id = str(request.get("id") or "")
    if not request_id:
        return
    payload = {
        "request_id": request_id,
        "kind": request.get("kind", ""),
        "idempotency_key": request.get("idempotency_key", ""),
        **result,
    }
    write_json_atomic(paths.results_dir / f"{request_id}.json", payload)
    raw_path = request.get("_path")
    if raw_path:
        Path(str(raw_path)).unlink(missing_ok=True)


def read_result(paths: ControlPaths, request_id: str) -> dict[str, Any]:
    return read_json(paths.results_dir / f"{request_id}.json")


def wait_result(
    paths: ControlPaths, request_id: str, timeout: float, *, interval: float = 0.1,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        result = read_result(paths, request_id)
        if result:
            return result
        time.sleep(interval)
    return {}


def find_result_by_key(paths: ControlPaths, idempotency_key: str) -> dict[str, Any]:
    """按幂等键查找历史结果，用于重复晋升请求的去重。"""
    if not idempotency_key:
        return {}
    paths.ensure()
    for path in sorted(paths.results_dir.glob("*.json"), reverse=True)[:200]:
        result = read_json(path)
        if str(result.get("idempotency_key") or "") == idempotency_key:
            return result
    return {}
