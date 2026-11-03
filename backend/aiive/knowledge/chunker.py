"""结构感知的递归切分器。

切分优先级（结构边界优先于长度边界）
------------------------------------
1. Section：不同标题路径的 Block 永不进入同一个 Chunk；
2. Block：段落 / 列表项先按块装箱，块是天然的语义边界；
3. 句子：块内超长时按句末标点（。！？；.!?;）拆分；
4. 标点：仍超长时按逗号 / 顿号 / 冒号拆分；
5. 字符窗口：以上都失效时按 token 预算硬切（保证绝不超上限）。

特殊结构
--------
* 代码块：优先保持完整；超长时先按顶层 def/class/function 边界拆，再按行，最后按
  字符窗口。代码不参与普通文本 Overlap。
* 表格：超长时按行分组，**每组复制表头**（Markdown 的 |---| 分隔行一并复制）。
* 列表：同一 Section 内的列表项连续装箱，父项说明与子项尽量留在同一 Chunk。
* 空 Block：直接丢弃（不产生空 Chunk）；短尾块：不足 min_chunk_tokens 时与同
  Section 的上一块合并（不超过 max）。
* Overlap：只在同一 Section 的相邻文本 Chunk 之间进行，且只在句子边界上取尾部；
  代码块与表格前后不做 Overlap，避免把无关内容拼进下一个 Chunk。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from aiive.knowledge.blocks import (
    BLOCK_CODE,
    BLOCK_HEADING,
    BLOCK_TABLE,
    Block,
    heading_path_text,
)
from aiive.knowledge.tokenization import count_tokens

CHUNKER_VERSION = "structure_recursive_v1"

# 遗留字符窗口切分的默认宽度（仅 chunk_text 使用）。
CHUNK_SIZE = 500


@dataclass(frozen=True)
class ChunkConfig:
    """切分预算（token 口径，见 knowledge.tokenization）。"""

    target_chunk_tokens: int = 450
    max_chunk_tokens: int = 700
    min_chunk_tokens: int = 100
    overlap_tokens: int = 60

    def __post_init__(self) -> None:
        if self.min_chunk_tokens <= 0:
            raise ValueError("min_chunk_tokens 必须大于 0")
        if self.target_chunk_tokens < self.min_chunk_tokens:
            raise ValueError("target_chunk_tokens 不能小于 min_chunk_tokens")
        if self.max_chunk_tokens < self.target_chunk_tokens:
            raise ValueError("max_chunk_tokens 不能小于 target_chunk_tokens")
        if self.overlap_tokens < 0 or self.overlap_tokens >= self.max_chunk_tokens:
            raise ValueError("overlap_tokens 必须落在 [0, max_chunk_tokens) 内")


@dataclass(frozen=True)
class ChunkDraft:
    """切分结果（尚未落库）：正文 + 结构元数据 + 来源定位。"""

    content: str
    heading_path: tuple[str, ...]
    block_type: str
    section_id: str
    line_start: int
    line_end: int
    page_start: int | None
    page_end: int | None
    chunk_index: int
    token_count: int

    @property
    def heading_text(self) -> str:
        """标题路径的展示形式（A > B > C）。"""
        return heading_path_text(self.heading_path)


@dataclass(frozen=True)
class _Piece:
    """切分过程中的候选片段（Block 或 Block 的一部分）。"""

    text: str
    line_start: int
    line_end: int
    page_start: int | None
    page_end: int | None
    block_type: str
    atomic: bool = False


# 由粗到细的分隔层级：段落 → 行 → 句末标点 → 句中标点。
_SEPARATOR_LEVELS: tuple[str, ...] = (
    r"\n\s*\n",
    r"\n",
    r"(?<=[。！？；!?;])",
    r"(?<=[，,、：:])",
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；.!?;])")

# 顶层代码定义边界：只在行首无缩进时成立。
_CODE_BOUNDARY = re.compile(
    r"^(?:async\s+def|def|class|function|func|fn|impl|interface|enum|struct|"
    + r"public|private|protected|internal|export|module|namespace)\b"
)

_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def chunk_blocks(
    blocks: list[Block], config: ChunkConfig | None = None,
) -> list[ChunkDraft]:
    """把 Block 列表切分为 Chunk 列表（chunk_index 全局连续、从 0 开始）。"""
    settings = config or ChunkConfig()
    if not blocks:
        return []

    sections: list[list[Block]] = []
    for block in blocks:
        # 标题只提供层级上下文（heading_path），不作为正文 Chunk。
        if block.block_type == BLOCK_HEADING or not block.content.strip():
            continue
        if sections and sections[-1][0].section_id == block.section_id:
            sections[-1].append(block)
        else:
            sections.append([block])

    drafts: list[ChunkDraft] = []
    for section in sections:
        drafts.extend(_chunk_section(section, settings))
    return [replace(draft, chunk_index=index) for index, draft in enumerate(drafts)]


# ---------------------------------------------------------------------------
# Section 内切分
# ---------------------------------------------------------------------------

def _chunk_section(section: list[Block], config: ChunkConfig) -> list[ChunkDraft]:
    pieces: list[_Piece] = []
    for block in section:
        if block.is_atomic:
            pieces.extend(_atomic_pieces(block, config))
        else:
            pieces.append(_Piece(
                text=block.content,
                line_start=block.line_start,
                line_end=block.line_end,
                page_start=block.page_start,
                page_end=block.page_end,
                block_type=block.block_type,
            ))
    assembled = _pack(pieces, config)
    assembled = _merge_short_tail(assembled, config)
    return [
        ChunkDraft(
            content=piece.text,
            heading_path=section[0].heading_path,
            block_type=piece.block_type,
            section_id=section[0].section_id,
            line_start=piece.line_start,
            line_end=piece.line_end,
            page_start=piece.page_start,
            page_end=piece.page_end,
            chunk_index=0,
            token_count=count_tokens(piece.text),
        )
        for piece in assembled
        if piece.text.strip()
    ]


def _pack(pieces: list[_Piece], config: ChunkConfig) -> list[_Piece]:
    """把片段装箱成 Chunk：原子块单独成块，文本块按目标长度累积并加 Overlap。"""
    assembled: list[_Piece] = []
    buffer: list[_Piece] = []
    buffer_tokens = 0
    previous_text: str | None = None

    def flush() -> None:
        nonlocal buffer, buffer_tokens, previous_text
        if not buffer:
            return
        assembled.append(_assemble(buffer))
        previous_text = assembled[-1].text
        buffer = []
        buffer_tokens = 0

    for piece in pieces:
        tokens = count_tokens(piece.text)
        if piece.atomic:
            flush()
            assembled.append(piece)
            previous_text = None
            continue
        if tokens > config.max_chunk_tokens:
            flush()
            previous_part: str | None = None
            for part in _split_oversized(piece.text, config):
                text = part
                if previous_part is not None:
                    prefix = _overlap_prefix(previous_part, config)
                    if prefix and count_tokens(prefix) + count_tokens(part) <= config.max_chunk_tokens:
                        text = _join_texts([prefix, part])
                assembled.append(_assemble([replace(piece, text=text)]))
                previous_part = part
            previous_text = previous_part
            continue
        if buffer and buffer_tokens + tokens > config.target_chunk_tokens:
            flush()
            prefix = _overlap_prefix(previous_text, config) if previous_text else ""
            if prefix and count_tokens(prefix) + tokens <= config.max_chunk_tokens:
                buffer.append(replace(piece, text=prefix, line_end=piece.line_start))
                buffer_tokens += count_tokens(prefix)
        buffer.append(piece)
        buffer_tokens += tokens
    flush()
    return assembled


def _assemble(parts: list[_Piece]) -> _Piece:
    """把同一 Chunk 内的片段拼成一段正文，并汇总来源位置。"""
    texts = [part.text for part in parts if part.text.strip()]
    joined = _join_texts(texts)
    pages = [p for part in parts for p in (part.page_start, part.page_end) if p is not None]
    return _Piece(
        text=joined,
        line_start=min(part.line_start for part in parts),
        line_end=max(part.line_end for part in parts),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        block_type=_dominant_type(parts),
    )


def _join_texts(texts: list[str]) -> str:
    """列表项之间用单换行，其余用空行分隔，保持可读性。"""
    if not texts:
        return ""
    if all(text.lstrip().startswith(("- ", "* ", "+ ")) for text in texts):
        return "\n".join(texts)
    return "\n\n".join(texts)


def _dominant_type(parts: list[_Piece]) -> str:
    types = {part.block_type for part in parts if part.text.strip()}
    if len(types) == 1:
        return next(iter(types))
    return "mixed"


def _overlap_prefix(previous: str | None, config: ChunkConfig) -> str:
    """取上一 Chunk 的尾部完整句子作为 Overlap，最多 overlap_tokens。"""
    if not previous or config.overlap_tokens <= 0:
        return ""
    units = [unit for unit in _SENTENCE_BOUNDARY.split(previous) if unit.strip()]
    prefix = ""
    for unit in reversed(units):
        candidate = unit + prefix
        if count_tokens(candidate) > config.overlap_tokens:
            break
        prefix = candidate
    return prefix.strip()


def _merge_short_tail(pieces: list[_Piece], config: ChunkConfig) -> list[_Piece]:
    """短尾块（不足 min）与上一块合并且不超上限；原子块不合并。"""
    if len(pieces) < 2:
        return pieces
    last, previous = pieces[-1], pieces[-2]
    if last.atomic or previous.atomic:
        return pieces
    if count_tokens(last.text) >= config.min_chunk_tokens:
        return pieces
    merged_tokens = count_tokens(previous.text) + count_tokens(last.text)
    if merged_tokens > config.max_chunk_tokens:
        return pieces
    merged = _assemble([
        replace(previous, text=previous.text),
        replace(last, text=last.text),
    ])
    return [*pieces[:-2], merged]


# ---------------------------------------------------------------------------
# 超长文本的递归拆分
# ---------------------------------------------------------------------------

def _split_oversized(text: str, config: ChunkConfig) -> list[str]:
    """把超过 max_chunk_tokens 的文本按「段落 → 行 → 句子 → 标点 → 字符」递归拆开。"""
    if count_tokens(text) <= config.max_chunk_tokens:
        return [text]
    for separator in _SEPARATOR_LEVELS:
        parts = [part for part in re.split(separator, text) if part.strip()]
        if len(parts) <= 1:
            continue
        pieces: list[str] = []
        for part in parts:
            if count_tokens(part) > config.max_chunk_tokens:
                pieces.extend(_split_oversized(part, config))
            else:
                pieces.append(part)
        return _pack_text(pieces, config)
    return _char_windows(text, config.max_chunk_tokens)


def _pack_text(pieces: list[str], config: ChunkConfig) -> list[str]:
    """把已拆开的片段贪心合并到目标长度（不跨结构边界硬凑）。"""
    packed: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0
    for piece in pieces:
        tokens = count_tokens(piece)
        if buffer and buffer_tokens + tokens > config.target_chunk_tokens:
            packed.append(_join_texts(buffer).strip())
            buffer = []
            buffer_tokens = 0
        buffer.append(piece)
        buffer_tokens += tokens
    if buffer:
        packed.append(_join_texts(buffer).strip())
    return [item for item in packed if item]


def _char_windows(text: str, max_tokens: int) -> list[str]:
    """按 token 预算硬切（二分找最长可行前缀），保证绝不超上限。"""
    limit = max(1, max_tokens)
    windows: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        low, high = start + 1, length
        while low < high:
            middle = (low + high + 1) // 2
            if count_tokens(text[start:middle]) <= limit:
                low = middle
            else:
                high = middle - 1
        windows.append(text[start:low])
        start = low
    return [window for window in windows if window.strip()]


# ---------------------------------------------------------------------------
# 特殊结构：代码块 / 表格
# ---------------------------------------------------------------------------

def _atomic_pieces(block: Block, config: ChunkConfig) -> list[_Piece]:
    """代码块与表格的专用拆分（保持结构、必要时复制表头）。"""
    text = block.content
    if count_tokens(text) <= config.max_chunk_tokens:
        return [_piece_of(block, text)]
    if block.block_type == BLOCK_TABLE:
        parts = _split_table(text, config)
    elif block.block_type == BLOCK_CODE:
        parts = _split_code(text, config)
    else:  # pragma: no cover - ATOMIC_BLOCK_TYPES 之外的块不会走到这里
        parts = _split_oversized(text, config)
    return [_piece_of(block, part) for part in parts]


def _piece_of(block: Block, text: str) -> _Piece:
    return _Piece(
        text=text,
        line_start=block.line_start,
        line_end=block.line_end,
        page_start=block.page_start,
        page_end=block.page_end,
        block_type=block.block_type,
        atomic=True,
    )


def _split_table(text: str, config: ChunkConfig) -> list[str]:
    """按行分组拆表格，每组都复制表头（含 Markdown 分隔行）。"""
    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) <= 1:
        return _char_windows(text, config.max_chunk_tokens)
    header = [lines[0]]
    body = lines[1:]
    if body and _TABLE_SEPARATOR.match(body[0]):
        header.append(body[0])
        body = body[1:]
    header_text = "\n".join(header)
    header_tokens = count_tokens(header_text)
    if not body:
        return [header_text]
    if header_tokens >= config.max_chunk_tokens:
        # 表头本身超限：退化为普通文本拆分，避免产生只有表头的空转 Chunk。
        return _split_oversized(text, config)

    groups: list[list[str]] = []
    current: list[str] = []
    current_tokens = header_tokens
    for row in body:
        row_tokens = count_tokens(row)
        if row_tokens + header_tokens > config.max_chunk_tokens:
            if current:
                groups.append(current)
                current, current_tokens = [], header_tokens
            groups.append([row])
            continue
        if current and current_tokens + row_tokens > config.max_chunk_tokens:
            groups.append(current)
            current, current_tokens = [], header_tokens
        current.append(row)
        current_tokens += row_tokens
    if current:
        groups.append(current)
    return [
        "\n".join([*header, *group]) if group else header_text
        for group in groups
    ]


def _split_code(text: str, config: ChunkConfig) -> list[str]:
    """代码块拆分：顶层定义边界 → 行 → 字符窗口。"""
    lines = text.split("\n")
    boundaries = [
        index for index, line in enumerate(lines)
        if line.strip() and line[0] not in {" ", "\t"} and _CODE_BOUNDARY.match(line)
    ]
    if boundaries and boundaries[0] != 0:
        boundaries.insert(0, 0)
    if len(boundaries) > 1:
        chunks: list[str] = []
        for position, start in enumerate(boundaries):
            end = boundaries[position + 1] if position + 1 < len(boundaries) else len(lines)
            segment = "\n".join(lines[start:end]).strip("\n")
            if not segment:
                continue
            if count_tokens(segment) > config.max_chunk_tokens:
                chunks.extend(_split_code_by_lines(segment, config))
            else:
                chunks.append(segment)
        return _pack_text(chunks, config)
    return _split_code_by_lines(text, config)


def _split_code_by_lines(text: str, config: ChunkConfig) -> list[str]:
    """按行累积拆分代码；单行仍超限时按字符窗口兜底。"""
    lines = text.split("\n")
    chunks: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0
    for line in lines:
        line_tokens = count_tokens(line)
        if line_tokens > config.max_chunk_tokens:
            if buffer:
                chunks.append("\n".join(buffer))
                buffer, buffer_tokens = [], 0
            chunks.extend(_char_windows(line, config.max_chunk_tokens))
            continue
        if buffer and buffer_tokens + line_tokens > config.max_chunk_tokens:
            chunks.append("\n".join(buffer))
            buffer, buffer_tokens = [], 0
        buffer.append(line)
        buffer_tokens += line_tokens
    if buffer:
        chunks.append("\n".join(buffer))
    return [chunk for chunk in chunks if chunk.strip()]


# ---------------------------------------------------------------------------
# 遗留接口
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[dict[str, Any]]:
    """[遗留] 按字符窗口切分纯文本。

    知识摄取链路已改用 :func:`chunk_blocks`（结构感知递归切分）。本函数保留给
    尚未迁移的调用方，行为与历史版本一致：按行累积到 chunk_size 字符。
    """
    lines = text.split("\n")
    chunks: list[dict[str, Any]] = []
    current = ""
    start_line = 0
    chunk_idx = 0

    for i, line in enumerate(lines):
        if len(line) > chunk_size:
            if current.strip():
                chunks.append({
                    "content": current.rstrip(),
                    "line_start": start_line,
                    "line_end": i - 1,
                    "chunk_index": chunk_idx,
                })
                chunk_idx += 1
            current = ""
            for pos in range(0, len(line), chunk_size):
                piece = line[pos : pos + chunk_size]
                if not piece.strip():
                    continue
                chunks.append({
                    "content": piece,
                    "line_start": i,
                    "line_end": i,
                    "chunk_index": chunk_idx,
                })
                chunk_idx += 1
            start_line = i + 1
            continue

        if current and len(current) + len(line) + 1 > chunk_size:
            chunks.append({
                "content": current.rstrip(),
                "line_start": start_line,
                "line_end": i - 1,
                "chunk_index": chunk_idx,
            })
            current = line + "\n"
            start_line = i
            chunk_idx += 1
        else:
            current += line + "\n"

    if current.strip():
        chunks.append({
            "content": current.rstrip(),
            "line_start": start_line,
            "line_end": len(lines) - 1,
            "chunk_index": chunk_idx,
        })

    return chunks
