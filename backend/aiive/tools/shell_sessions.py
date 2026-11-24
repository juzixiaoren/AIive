from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

from aiive.runtime.tool_output_store import tool_output_root
from aiive.storage.data_paths import data_path, data_root
from aiive.tools.local_shell import run_bash

# Files let a different Outbox worker poll or cancel the owning process session.
RETENTION_SECONDS = 7 * 86400


def _directory(task_id: str, session_id: str) -> Path:
    return data_path("shell-sessions", str(UUID(task_id)), str(UUID(session_id)))


def _save(path: Path, data: dict) -> None:
    temp = path.with_name(f".{uuid4().hex}.json")
    try:
        temp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _owner_alive(pid: int) -> bool:
    from aiive.supervisor.process_control import pid_alive

    return pid_alive(pid)


def start_session(task_id: str, command: str, cwd: str, timeout: float, env: dict) -> dict:
    session_id = str(uuid4())
    directory = _directory(task_id, session_id)
    directory.mkdir(parents=True)
    state_path = directory / "state.json"
    _save(state_path, {"status": "starting", "pid": os.getpid()})
    from importlib.util import find_spec

    backend = str(Path(find_spec("aiive").origin).resolve().parent.parent)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "aiive.tools.shell_sessions", task_id, session_id],
            cwd=backend, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AIIVE_DATA_DIR": str(data_root()), "PYTHONPATH": backend},
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        _save(state_path, {"status": "finished", "result": {"ok": False, "error": str(exc), "exit_code": None}})
        return {"ok": False, "error": "shell_session_start_failed", "session_id": session_id}
    try:
        _save(state_path, {"status": "starting", "pid": process.pid})
        process.stdin.write(json.dumps({"command": command, "cwd": cwd, "timeout": timeout, "env": env}).encode())
        process.stdin.close()
    except OSError:
        process.terminate()
        process.wait()
        _save(state_path, {"status": "finished", "result": {
            "ok": False, "error": "shell_session_start_failed", "exit_code": None,
        }})
        return {"ok": False, "error": "shell_session_start_failed", "session_id": session_id}
    Thread(target=process.wait, name=f"shell-session-reaper-{session_id}", daemon=True).start()
    return {"session_id": session_id, "status": "starting", "cwd": cwd}


def _run_session(task_id: str, session_id: str) -> None:
    directory = _directory(task_id, session_id)
    state_path = directory / "state.json"
    try:
        spec = json.load(sys.stdin)
        _save(state_path, {"status": "starting", "pid": os.getpid()})
        result = run_bash(
            spec["command"], cwd=spec["cwd"], timeout_seconds=spec["timeout"],
            env=spec["env"], keep_logs=True,
            cancel_check=lambda: (directory / "cancel").exists(),
            on_start=lambda paths: _save(
                state_path, {"status": "running", "pid": os.getpid(), "streams": paths},
            ),
        )
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "exit_code": None}
    _save(state_path, {"status": "finished", "result": result})


def read_session(
    task_id: str, session_id: str, stdout_offset: int = 0, stderr_offset: int = 0,
    limit: int = 4000, cancel: bool = False,
) -> dict:
    directory = _directory(task_id, session_id)
    try:
        state = _read(directory / "state.json")
    except FileNotFoundError:
        return {"ok": False, "error": "shell_session_not_found"}
    if state["status"] != "finished" and not _owner_alive(state["pid"]):
        state["status"] = "interrupted"
    if cancel and state["status"] in {"starting", "running"}:
        (directory / "cancel").touch()
    result = dict(state.get("result") or {})
    response = {"session_id": session_id, "status": state["status"], "cancel_requested": cancel}
    for stream, offset in (("stdout", stdout_offset), ("stderr", stderr_offset)):
        path = result.get(f"{stream}_log") or (state.get("streams") or {}).get(stream)
        content = ""
        if path:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    handle.read(offset)
                    content = handle.read(limit)
            except FileNotFoundError:
                # Finalization atomically renames the live stream to its artifact path.
                updated = _read(directory / "state.json")
                final = (updated.get("result") or {}).get(f"{stream}_log")
                if final:
                    with open(final, encoding="utf-8", errors="replace") as handle:
                        handle.read(offset)
                        content = handle.read(limit)
        response[stream] = content
        response[f"{stream}_next_offset"] = offset + len(content)
    if result:
        response.update(command_ok=result.get("ok"), exit_code=result.get("exit_code"),
                        timed_out=result.get("timed_out", False), cancelled=result.get("cancelled", False),
                        error=result.get("error"), capture_truncated=result.get("capture_truncated"),
                        stdout_log=result.get("stdout_log"), stderr_log=result.get("stderr_log"))
    return response


def task_has_live_sessions(task_id: str) -> bool:
    return _live_sessions(task_id, cancel=False)


def cancel_task_sessions(task_id: str) -> bool:
    return _live_sessions(task_id, cancel=True)


def _live_sessions(task_id: str, *, cancel: bool) -> bool:
    """Request cancellation and report whether any live process still owns a session."""
    live = False
    for state_file in data_path("shell-sessions", str(UUID(task_id))).glob("*/state.json"):
        try:
            state = _read(state_file)
            if state["status"] != "finished" and _owner_alive(state["pid"]):
                if cancel:
                    (state_file.parent / "cancel").touch()
                live = True
        except FileNotFoundError:
            continue
    return live


def prune_sessions() -> None:
    cutoff = time.time() - RETENTION_SECONDS
    for path in data_path("shell-sessions").glob("*/*/state.json"):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            state = _read(path)
            if state["status"] != "finished" and _owner_alive(state["pid"]):
                continue
            for filename in (state.get("streams") or {}).values():
                stream = Path(filename)
                if stream.parent == tool_output_root():
                    stream.unlink(missing_ok=True)
            shutil.rmtree(path.parent)
            try:
                path.parent.parent.rmdir()
            except OSError:
                pass
        except FileNotFoundError:
            continue


if __name__ == "__main__":
    _run_session(sys.argv[1], sys.argv[2])
