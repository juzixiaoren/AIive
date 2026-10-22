"""本机 Bash 执行器：真实 bash 子进程 + 超时终止 + 输出上限 + 环境隔离。

约束：
- 只用真实 bash（PATH / 显式配置 / 常见 Git Bash 安装路径），找不到时返回
  shell_unavailable，绝不静默回退到 cmd / powershell。
- 每次执行使用最小环境变量白名单，不把密钥类变量传给子进程。
- stdout / stderr 直接落临时文件并限量读取，避免管道死锁与内存膨胀。
- 超时后终止整个进程树（POSIX 进程组 / Windows taskkill /T），避免遗留子进程。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from aiive.config import settings

_WINDOWS_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files\Git\mingw64\bin\bash.exe",
)

# 只透传运行 bash 与常见只读命令所需的最小环境；其余变量（含密钥）一律不带入。
_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "TERM",
)

_VERSION_CACHE: dict[str, str] = {}


def resolve_bash() -> str | None:
    """按 显式配置 → PATH → 常见 Git Bash 路径 的顺序解析真实 bash。"""
    candidates: list[str] = []
    configured = (settings.shell_bash_path or "").strip()
    if configured:
        candidates.append(configured)
    discovered = shutil.which("bash")
    if discovered:
        candidates.append(discovered)
    if sys.platform.startswith("win"):
        candidates.extend(_WINDOWS_BASH_CANDIDATES)
    seen: set[str] = set()
    for candidate in candidates:
        path = Path(candidate).expanduser()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return str(path)
    return None


def bash_version(path: str) -> str:
    """返回 bash 自报版本首行；失败返回空串（不缓存失败，便于修复后重试）。"""
    cached = _VERSION_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False,
        )
        lines = (proc.stdout or proc.stderr or "").splitlines()
        version = lines[0].strip() if lines else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    if version:
        _VERSION_CACHE[path] = version
    return version


def isolated_env() -> dict[str, str]:
    """构造子进程环境：只保留运行必需的白名单变量。"""
    env: dict[str, str] = {}
    for name in _ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value:
            env[name] = value
    for name, value in os.environ.items():
        if name.startswith("LC_") and value:
            env[name] = value
    if "HOME" not in env and env.get("USERPROFILE"):
        env["HOME"] = env["USERPROFILE"]
    return env


def run_bash(command: str, *, cwd: str, timeout_seconds: float | None = None) -> dict[str, Any]:
    """在真实 bash 中执行单条命令并返回结构化结果。"""
    started = time.monotonic()
    bash = resolve_bash()
    if bash is None:
        return _failure(
            "shell_unavailable",
            "未找到 bash；请安装 Git Bash 或设置 AIIVE_SHELL_BASH_PATH。",
            started,
        )
    workdir = Path(cwd).expanduser()
    if not workdir.is_dir():
        return _failure("cwd_not_found", f"工作目录不存在或不是目录: {cwd}", started)

    timeout = float(timeout_seconds or settings.shell_default_timeout_seconds)
    timeout = max(1.0, min(timeout, float(settings.shell_max_timeout_seconds)))
    limit = int(settings.shell_max_output_bytes)
    argv = [bash, "-c", command]
    windows = sys.platform.startswith("win")
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            env=isolated_env(),
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
            start_new_session=not windows,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        out_file.seek(0)
        raw_stdout = out_file.read(limit + 1)
        err_file.seek(0)
        raw_stderr = err_file.read(limit + 1)

    exit_code = None if timed_out else proc.returncode
    result: dict[str, Any] = {
        "ok": (not timed_out) and exit_code == 0,
        "exit_code": exit_code,
        "stdout": raw_stdout[:limit].decode("utf-8", errors="replace"),
        "stderr": raw_stderr[:limit].decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "truncated": {
            "stdout": len(raw_stdout) > limit,
            "stderr": len(raw_stderr) > limit,
        },
        "cwd": str(workdir),
        "shell": {"kind": "bash", "path": bash, "version": bash_version(bash)},
    }
    if timed_out:
        result["error_type"] = "timeout"
        result["error"] = f"命令在 {timeout:.0f}s 内未结束，已终止进程树。"
    return result


def _failure(error_type: str, message: str, started: float) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": error_type,
        "error": message,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "truncated": {"stdout": False, "stderr": False},
    }


def _terminate_tree(proc: subprocess.Popen[bytes]) -> None:
    """终止整个进程树；先按平台批量终止，再兜底 kill 主进程。"""
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass

