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


def save_tool_output(content: str, tool_name: str) -> Path:
    """把完整工具输出写入专用目录并返回路径。

    失败时抛异常，由调用方降级为有界预览；绝不写半截文件。
    """
    root = ensure_tool_output_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    slug = _SLUG_RE.sub("-", (tool_name or "tool").lower()).strip("-")[:32] or "tool"
    target = root / f"{stamp}-{uuid.uuid4().hex[:12]}-{slug}{LOG_SUFFIX}"
    tmp_path = root / f"{TMP_PREFIX}{uuid.uuid4().hex}"
    data = content.encode("utf-8", errors="replace")
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理未完成的临时文件失败: %s", tmp_path)
        raise
    return target


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
