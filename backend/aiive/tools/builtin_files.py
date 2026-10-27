"""Domain tool handlers (implementation) split from builtin_tools."""

import logging

logger = logging.getLogger(__name__)

import os
from collections.abc import Iterator
from typing import TextIO


def handle_safe_delete(path: str, scope_id: str = "test_artifacts", mode: str = "trash"):
    """安全删除文件，仅允许删除已注册 scope 内的文件。

    参数:
        path: 要删除的文件路径
        scope_id: 范围标识符，默认 "test_artifacts"
        mode: 删除模式（trash / quarantine / hard_delete_for_test_only）

    返回:
        包含 allowed、reason、resolved_path 的字典
    """
    from aiive.tools.safe_delete import safe_delete as do_safe_delete
    decision = do_safe_delete(path, scope_id, mode)
    # 关键：必须带 ok 键。operation_executor 依赖 ok/allowed 判定失败，
    # 否则删除被拒绝时会被记成 committed 成功（审计假成功）。
    return {
        "ok": decision.allowed,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "resolved_path": decision.resolved_path,
    }

# 单页回填的默认字符预算与硬上限；分页必须同时受二者约束。
FILE_PAGE_CHAR_BUDGET = 1000
FILE_PAGE_CHAR_LIMIT = 8000
# 流式读取的块大小与单行硬上限：超长行按上限切成续行，内存有界且内容不丢。
FILE_CHUNK_CHARS = 64 * 1024
FILE_MAX_LINE_CHARS = 1_000_000


def _allowed_roots() -> list[str]:
    """允许读取的根目录：~/Documents 与工具输出落盘目录。"""
    roots = [os.path.realpath(os.path.expanduser("~/Documents"))]
    try:
        from aiive.runtime.tool_output_store import tool_output_root

        roots.append(str(tool_output_root()))
    except Exception:  # 配置异常时不扩大授权
        logger.warning("解析工具输出目录失败，仅允许 ~/Documents")
    return [os.path.normcase(os.path.realpath(item)) for item in roots]


def _within_roots(real_path: str, roots: list[str]) -> bool:
    real_nc = os.path.normcase(real_path)
    for root in roots:
        try:
            if os.path.commonpath([real_nc, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _iter_lines(handle: TextIO) -> Iterator[str]:
    """流式按行读取：内存只保留一个块，超长行按硬上限切分为续行。"""
    buffer = ""
    while True:
        chunk = handle.read(FILE_CHUNK_CHARS)
        if not chunk:
            if buffer:
                yield buffer
            return
        buffer += chunk
        while True:
            index = buffer.find("\n")
            if index < 0:
                break
            yield buffer[:index]
            buffer = buffer[index + 1:]
        if len(buffer) > FILE_MAX_LINE_CHARS:
            yield buffer[:FILE_MAX_LINE_CHARS]
            buffer = buffer[FILE_MAX_LINE_CHARS:]


def handle_read_text_file(
    path: str,
    offset: int = 0,
    limit: int = 50,
    max_chars: int = FILE_PAGE_CHAR_BUDGET,
    char_offset: int = 0,
):
    """分页读取文本文件，单页回填受字符预算约束。

    只允许读取 ~/Documents 与工具输出落盘目录内的文件，拒绝其他路径。
    文件按行流式扫描：内存只保留当前页，不整文件载入，因此超大文件也能
    通过 next_offset 持续翻页直到读完。

    参数:
        path: 文件路径
        offset: 起始行号（从 0 开始）
        limit: 本次最多读取的行数
        max_chars: 单页字符预算，超出后提前结束本页并给出下一页位置
        char_offset: 当前行的字符偏移，用于超长行的行内续读

    返回:
        分页结构：path / offset / total_lines / total_chars / lines_read /
        line_range / content / next_offset / next_char_offset / has_more / hint
    """
    real_path = os.path.realpath(path)
    if not _within_roots(real_path, _allowed_roots()):
        return {"ok": False, "error": "Access denied", "error_type": "access_denied", "path": path}
    if not os.path.isfile(real_path):
        return {"ok": False, "error": "File not found", "error_type": "not_found", "path": path}

    start = max(0, int(offset))
    line_limit = max(1, min(int(limit), 1000))
    budget = max(1, min(int(max_chars), FILE_PAGE_CHAR_LIMIT))
    char_start = max(0, int(char_offset))

    taken: list[str] = []
    used = 0
    total_lines = 0
    total_chars = 0
    next_offset: int | None = None
    next_char_offset: int | None = None
    line_truncated = False
    truncated_line_chars = 0
    page_full = False
    try:
        with open(real_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            for index, raw_line in enumerate(_iter_lines(handle)):
                line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
                total_lines = index + 1
                total_chars += len(line)
                if index < start or page_full:
                    continue
                if index >= start + line_limit:
                    next_offset, next_char_offset = index, 0
                    page_full = True
                    continue
                piece = line[char_start:] if index == start and char_start else line
                if not taken and len(piece) > budget:
                    # 单行超过预算：只回填预算内片段，并给出同一行的续读偏移。
                    taken.append(piece[:budget])
                    next_offset, next_char_offset = index, char_start + budget
                    line_truncated = True
                    truncated_line_chars = len(line)
                    page_full = True
                    used = budget
                    continue
                if taken and used + len(piece) + 1 > budget:
                    next_offset, next_char_offset = index, 0
                    page_full = True
                    continue
                taken.append(piece)
                used += len(piece) + 1
    except OSError as error:
        logger.warning("读取文本文件失败: path=%s, error=%s", real_path, error)
        return {"ok": False, "error": str(error), "error_type": "read_failed", "path": path}

    if next_offset is None and start + len(taken) < total_lines:
        next_offset, next_char_offset = start + len(taken), 0

    content = "\n".join(taken)
    result = {
        "ok": True,
        "path": real_path,
        "offset": start,
        "limit": line_limit,
        "total_lines": total_lines,
        "total_chars": total_chars,
        "lines_read": len(taken),
        "line_range": [start, start + len(taken) - 1] if taken else [],
        "content": content,
        "next_offset": next_offset,
        "next_char_offset": next_char_offset,
        "has_more": next_offset is not None,
        "truncated": line_truncated,
        "line_truncated": line_truncated,
        "paged": True,
    }
    if line_truncated:
        # 行内续读：line_range 指同一行，char_range 才是本次实际读取的字符窗口
        # （0 基、左闭右开），next_char_offset 恒等于 char_range[1]。
        result["char_range"] = [char_start, char_start + len(taken[0])]
    if total_lines and start >= total_lines:
        result["error_type"] = "offset_out_of_range"
    if next_offset is not None:
        # 明确下一页位置：行内续读用 char_offset，否则用 offset 翻页。
        if line_truncated:
            result["hint"] = (
                f"本行共 {truncated_line_chars} 字符，已返回从第 {char_start} 字符起的 {budget} 字符；"
                f"续读调用 read_text_file(path, offset={next_offset}, char_offset={next_char_offset}, max_chars={budget})"
            )
        else:
            result["hint"] = (
                f"共 {total_lines} 行 / {total_chars} 字符，已返回第 {start + 1}-{start + len(taken)} 行；"
                f"下一页调用 read_text_file(path, offset={next_offset}, limit={line_limit})"
            )
    return result
