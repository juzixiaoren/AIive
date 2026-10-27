"""本机 Bash 执行器：真实 bash 子进程 + 超时终止 + 流式日志 + 环境隔离。

约束：
- 只用真实 bash（PATH / 显式配置 / 常见 Git Bash 安装路径），找不到时返回
  shell_unavailable，绝不静默回退到 cmd / powershell。
- 每次执行使用最小环境变量白名单，不把密钥类变量传给子进程。
- stdout / stderr 由后台线程边读边写入受控日志（tool_output_store 的同一目录），
  内存只保留有界预览；不在内存里先截断再把截断结果当成完整日志。
- 日志容量受 shell_max_log_bytes 限制，超出后停止写盘并标记 capture_truncated，
  返回结果里明确给出总字节数、预览读取位置与日志路径。
- 超时后终止整个进程树（POSIX 进程组 / Windows taskkill /T），避免遗留子进程。
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from aiive.config import settings
from aiive.runtime.tool_output_store import StreamingLog

logger = logging.getLogger(__name__)

# 管道分片大小：一次最多读入这么多字节，读完立刻写日志，内存占用与输出总量无关。
_PIPE_CHUNK = 64 * 1024
# 等待读取线程收尾的上限（进程被终止后管道通常立即 EOF）。
_JOIN_TIMEOUT = 10.0

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
    output_limit = max(1, int(settings.shell_max_output_bytes))
    memory_limit = max(1024, output_limit)
    # 预览同时受 shell_output_preview_chars 与 shell_max_output_bytes 约束：
    # 后者是内存回填的硬上限（日志不可用时的兜底），前者是默认预览长度。
    preview_chars = max(1, min(int(settings.shell_output_preview_chars), output_limit))
    max_log_bytes = max(4096, int(settings.shell_max_log_bytes))
    argv = [bash, "-c", command]
    windows = sys.platform.startswith("win")
    out_capture = _Capture(
        "run_shell_stdout", max_bytes=max_log_bytes, memory_limit=memory_limit,
        keep_bytes=preview_chars,
    )
    err_capture = _Capture(
        "run_shell_stderr", max_bytes=max_log_bytes, memory_limit=memory_limit,
        keep_bytes=preview_chars,
    )
    timed_out = False
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            env=isolated_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not windows,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        )
        out_stream = proc.stdout
        err_stream = proc.stderr
        if out_stream is None or err_stream is None:  # pragma: no cover - PIPE 必不为 None
            raise RuntimeError("bash_pipe_unavailable")
        pumps = [
            threading.Thread(target=_pump, args=(out_stream, out_capture), daemon=True),
            threading.Thread(target=_pump, args=(err_stream, err_capture), daemon=True),
        ]
        for pump in pumps:
            pump.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for pump in pumps:
            pump.join(timeout=_JOIN_TIMEOUT)
    finally:
        out_capture.close()
        err_capture.close()

    out_text, out_next, out_next_char = out_capture.preview(preview_chars)
    err_text, err_next, err_next_char = err_capture.preview(preview_chars)
    exit_code = None if timed_out else proc.returncode
    result: dict[str, Any] = {
        "ok": (not timed_out) and exit_code == 0,
        "exit_code": exit_code,
        "stdout": out_text,
        "stderr": err_text,
        "stdout_bytes": out_capture.total_bytes,
        "stderr_bytes": err_capture.total_bytes,
        "stdout_log": out_capture.path,
        "stderr_log": err_capture.path,
        "stdout_next_offset": out_next,
        "stdout_next_char_offset": out_next_char,
        "stderr_next_offset": err_next,
        "stderr_next_char_offset": err_next_char,
        "preview_chars": preview_chars,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "duration_ms": int((time.monotonic() - started) * 1000),
        # truncated: 预览是否被截（完整内容在日志里）；capture_truncated: 日志本身是否达到容量上限。
        "truncated": {
            "stdout": len(out_text.encode("utf-8")) < out_capture.total_bytes,
            "stderr": len(err_text.encode("utf-8")) < err_capture.total_bytes,
        },
        "capture_truncated": {
            "stdout": out_capture.capture_truncated,
            "stderr": err_capture.capture_truncated,
        },
        "cwd": str(workdir),
        "shell": {"kind": "bash", "path": bash, "version": bash_version(bash)},
    }
    if out_capture.log_error or err_capture.log_error:
        result["log_error"] = out_capture.log_error or err_capture.log_error
    if timed_out:
        result["error_type"] = "timeout"
        result["error"] = f"命令在 {timeout:.0f}s 内未结束，已终止进程树。"
    return result


def _next_position(text: str) -> tuple[int, int]:
    """把前缀文本换算成（下一行行号，行内字符偏移），供 read_text_file 续读。"""
    if not text:
        return 0, 0
    if text.endswith("\n"):
        return text.count("\n"), 0
    line = text.count("\n")
    return line, len(text.rsplit("\n", 1)[-1])


def _pump(stream: Any, capture: "_Capture") -> None:
    """后台读取一路管道并写入捕获器，避免管道写满阻塞子进程。"""
    try:
        while True:
            chunk = stream.read(_PIPE_CHUNK)
            if not chunk:
                break
            capture.write(chunk)
    except (OSError, ValueError):
        logger.warning("读取 bash 输出流失败", exc_info=True)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


class _Capture:
    """单路输出捕获：优先流式落盘，日志不可用时退化为有界内存。

    - 落盘：内容边读边写进 tool_output_store 的日志文件，内存不保留完整副本；
    - 日志不可用（磁盘/权限）：退回有界内存，最多保留 memory_limit 字节；
    - 两种情况都精确统计 total_bytes，并用 capture_truncated 标明是否丢内容。
    """

    def __init__(
        self, tool_name: str, *, max_bytes: int, memory_limit: int, keep_bytes: int = 0,
    ) -> None:
        self._memory_limit: int = memory_limit
        self._keep_bytes: int = max(0, keep_bytes)
        self._memory: bytearray = bytearray()
        self._log: StreamingLog | None = None
        self.log_error: str = ""
        try:
            self._log = StreamingLog(tool_name, max_bytes=max_bytes)
        except Exception:
            logger.warning("工具输出日志创建失败，退化为有界内存捕获: %s", tool_name, exc_info=True)
            self.log_error = "log_write_failed"
        self.total_bytes: int = 0
        self.capture_truncated: bool = False
        self.path: str = ""

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        log = self._log
        if log is not None:
            log.write(chunk)
            self.capture_truncated = log.capture_truncated
            return
        remaining = self._memory_limit - len(self._memory)
        if remaining <= 0:
            self.capture_truncated = True
            return
        if len(chunk) > remaining:
            self._memory.extend(chunk[:remaining])
            self.capture_truncated = True
            return
        self._memory.extend(chunk)

    def close(self) -> None:
        """收尾日志；失败时把已写入的临时文件读回内存，保证预览不丢。"""
        log = self._log
        if log is None:
            return
        if self.total_bytes <= self._keep_bytes and not log.capture_truncated:
            # 全部内容都在预览范围内：不留日志文件，避免每条短命令产生两个小文件。
            log.flush()
            try:
                with open(log.tmp_path, "rb") as handle:
                    self._memory.extend(handle.read(self._memory_limit))
            except OSError:
                pass
            log.abort()
            return
        try:
            self.path = str(log.close())
        except Exception:
            logger.warning("工具输出日志收尾失败: %s", log.tmp_path, exc_info=True)
            self.log_error = "log_write_failed"
            try:
                with open(log.tmp_path, "rb") as handle:
                    self._memory.extend(handle.read(self._memory_limit))
            except OSError:
                pass
            log.abort()
        self.capture_truncated = self.capture_truncated or log.capture_truncated

    def preview(self, chars: int) -> tuple[str, int, int]:
        """返回（预览文本，下一页起始行，该行内的字符偏移）。

        位置与 read_text_file 的 offset / char_offset 同一语义：预览是日志的前缀，
        因此从预览末尾继续读就等于从「行号 + 行内偏移」继续读。
        """
        text = ""
        if self.path:
            try:
                with open(self.path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read(chars)
            except OSError:
                logger.warning("读取工具输出日志失败: %s", self.path, exc_info=True)
        if not text and not self.path:
            text = bytes(self._memory).decode("utf-8", errors="replace")[:chars]
        return (text, *_next_position(text))


def _failure(error_type: str, message: str, started: float) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": error_type,
        "error": message,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_log": "",
        "stderr_log": "",
        "stdout_next_offset": 0,
        "stdout_next_char_offset": 0,
        "stderr_next_offset": 0,
        "stderr_next_char_offset": 0,
        "timed_out": False,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "truncated": {"stdout": False, "stderr": False},
        "capture_truncated": {"stdout": False, "stderr": False},
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

