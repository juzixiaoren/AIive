"""工具输出落盘存储：唯一文件名、原子写入、路径校验、过期清理。

只管理 `settings.tool_output_dir`（默认 `.data/tool_outputs`）这一个专用目录：
写入用临时文件 + os.replace 保证原子性，文件名带时间戳与随机后缀避免覆盖；
清理只删除本目录内本模块产生的 `.log` / 残留 `.tmp-*`，绝不触碰其他临时文件或项目文件。
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

from aiive.config import settings

logger = logging.getLogger(__name__)

LOG_SUFFIX = ".log"
TMP_PREFIX = ".tmp-"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def tool_output_root() -> Path:
    """返回本功能专用目录的绝对路径（不创建）。"""
    raw = (settings.tool_output_dir or "").strip() or ".data/tool_outputs"
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def ensure_tool_output_root() -> Path:
    root = tool_output_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_managed_path(path: str | Path) -> bool:
    """判断路径是否位于专用目录内（用于读取授权的安全边界）。"""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    root = tool_output_root()
    if resolved == root:
        return False
    return root in resolved.parents


def _log_paths(tool_name: str) -> tuple[Path, Path]:
    """按 时间戳 + 随机后缀 生成本次日志的（目标路径，临时路径）。"""
    root = ensure_tool_output_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    slug = _SLUG_RE.sub("-", (tool_name or "tool").lower()).strip("-")[:32] or "tool"
    target = root / f"{stamp}-{uuid.uuid4().hex[:12]}-{slug}{LOG_SUFFIX}"
    tmp_path = root / f"{TMP_PREFIX}{uuid.uuid4().hex}"
    return target, tmp_path


class StreamingLog:
    """流式工具输出日志：边产生边写盘，容量受限，收尾原子替换。

    与 save_tool_output 共用同一目录与命名规则，不引入第二套存储：
    - 写入先落 .tmp-*，close() 时 os.replace 原子改名，读者看不到半截文件；
    - max_bytes 为磁盘上限：超过后停止写盘但继续统计 total_bytes，
      并把 capture_truncated 置 True，明确表示"磁盘上不是全部内容"；
    - write() 只写调用方传入的分片，自身不缓存完整内容。
    """

    def __init__(self, tool_name: str, *, max_bytes: int | None = None) -> None:
        self.target: Path
        self.tmp_path: Path
        self.target, self.tmp_path = _log_paths(tool_name)
        self.max_bytes: int | None = max_bytes if max_bytes and max_bytes > 0 else None
        self.total_bytes: int = 0
        self.written_bytes: int = 0
        self.capture_truncated: bool = False
        self.path: Path | None = None
        self._handle: BinaryIO = open(self.tmp_path, "wb")
        self._closed: bool = False

    def write(self, chunk: bytes) -> None:
        """写入一片输出；超过容量上限时丢弃该片剩余部分并标记截断。"""
        if not chunk or self._closed:
            return
        self.total_bytes += len(chunk)
        if self.max_bytes is not None:
            remaining = self.max_bytes - self.written_bytes
            if remaining <= 0:
                self.capture_truncated = True
                return
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
                self.capture_truncated = True
        self._handle.write(chunk)
        self.written_bytes += len(chunk)

    def flush(self) -> None:
        """把缓冲写入磁盘；需要在收尾前读取临时文件时调用。"""
        if not self._closed:
            self._handle.flush()

    def close(self) -> Path:
        """落盘收尾：flush + fsync + 原子改名，返回最终路径。"""
        if self._closed and self.path is not None:
            return self.path
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            os.replace(self.tmp_path, self.target)
        except BaseException:
            self.abort()
            raise
        self._closed = True
        self.path = self.target
        return self.target

    def abort(self) -> None:
        """放弃本次日志：关闭句柄并删除临时文件，绝不留下半截日志。"""
        if not self._closed:
            try:
                self._handle.close()
            except OSError:
                pass
            self._closed = True
        try:
            self.tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理未完成的临时文件失败: %s", self.tmp_path)


def save_tool_output(content: str, tool_name: str) -> Path:
    """把完整工具输出写入专用目录并返回路径。

    失败时抛异常，由调用方降级为有界预览；绝不写半截文件。
    """
    log = StreamingLog(tool_name)
    try:
        log.write(content.encode("utf-8", errors="replace"))
    except BaseException:
        log.abort()
        raise
    return log.close()


def cleanup_expired_tool_outputs(
    max_age_hours: float | None = None,
    now: datetime | None = None,
) -> int:
    """删除专用目录内创建时间超过保留期的日志，返回删除数量。

    只处理目录第一层的 `.log` 与残留 `.tmp-*`；不递归、不跟随符号链接、
    不触碰目录外的任何文件。
    """
    root = tool_output_root()
    if not root.is_dir():
        return 0
    hours = float(settings.tool_output_retention_hours if max_age_hours is None else max_age_hours)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.warning("扫描工具输出目录失败: %s", root)
        return 0
    for entry in entries:
        name = entry.name
        if not (name.endswith(LOG_SUFFIX) or name.startswith(TMP_PREFIX)):
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            logger.warning("删除过期工具输出失败: %s", entry)
    return removed
