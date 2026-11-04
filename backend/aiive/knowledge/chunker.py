"""结构感知的递归切分器。

切分优先级（结构边界优先于长度边界）
------------------------------------
1. Section：不同 section_id 的 Block 永不进入同一个 Chunk；
2. Block：段落 / 列表项先按块装箱，块是天然的语义边界；
3. 句子：块内超长时按句末标点（。！？；.!?;）拆分；
4. 标点：仍超长时按逗号 / 顿号 / 冒号拆分；
5. 字符窗口：以上都失效时按 token 预算硬切。

特殊结构
--------
* 代码块：优先保持完整；超长时先按顶层 def/class/function 边界拆，再按行，最后按
  字符窗口。代码不参与普通文本 Overlap。
* 表格：超长时按行分组，每组复制表头（Markdown 的 |---| 分隔行一并复制）；
  单行超预算时按单元格边界继续拆，单个单元格仍超预算时递归拆分；
  表头本身超预算时退化为普通文本拆分（不再复制表头），保证不超上限。
* 列表：同一 Section 内的列表项连续装箱，父项说明与子项尽量留在同一 Chunk。
* 空 Block：直接丢弃（不产生空 Chunk）；短尾块：不足 min_chunk_tokens 时与同
  Section 的上一块合并（不超过 max）。
* Overlap：只在同一 Section 的相邻文本 Chunk 之间进行，且只在句子边界上取尾部；
  代码块与表格前后不做 Overlap。

来源定位
--------
切分过程中的每一段都携带它在 Block 原文中的字符区间（_Segment）与来源页码，
Chunk 的行号由该区间换算得出、页码由各片段自带页码汇总，因此一个 Block 拆成多个
Chunk 后各自拥有准确范围。Overlap 前缀与重复的表格表头属于「上下文」，不计入 Chunk
的行号与页码范围，只记录 context_tokens，不会伪装成当前 Chunk 的原始内容。

兜底拆分（_enforce_budget）沿用同一套来源片段：上下文片段不能独立成 Chunk，当它与
紧随其后的正文放不进同一预算时优先舍弃上下文，正文不丢失、不重复；拆分后的每个
Chunk 只标记自己实际覆盖的行号与页码。

不变量
------
count_tokens(chunk.content) <= max_chunk_tokens 对全部输出成立（普通文本、列表、
引用、代码、表格、Overlap、短尾合并与所有兜底路径），最后还有一层 _enforce_budget
兜底拆分。
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

CHUNKER_VERSION = "structure_recursive_v4"

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
class ChunkPart:
    """Chunk 内的一个来源片段：正文 + 它在原始文档中的行/页范围。

    `context=True` 表示该片段是 Overlap 前缀或重复表头，属于「上下文」而不是本 Chunk
    的原始内容，因此不参与 `line_start / line_end / page_start / page_end` 汇总。

    `page_start / page_end` 逐片段携带来源页码（PDF 按页解析，每个 Block 自带页码）；
    缺失时保持 None，不做推测。兜底拆分后各 Chunk 的页码只由自己保留的正文片段决定，
    不会继承整个原 Chunk 的页码范围。
    """

    text: str
    line_start: int
    line_end: int
    page_start: int | None = None
    page_end: int | None = None
    context: bool = False


@dataclass(frozen=True)
class ChunkDraft:
    """切分结果（尚未落库）：正文 + 结构元数据 + 来源定位。

    line_start / line_end 只覆盖 Chunk 自身的原始内容；context_tokens 记录 Overlap
    前缀或重复表头这类「不属于来源范围」的上下文长度。`parts` 保留组成该 Chunk 的
    来源片段（含上下文标记），供 `_enforce_budget` 兜底拆分时继续使用同一套来源映射，
    避免把重新切分的字符串当成原文重新计算行号。
    """

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
    context_tokens: int = 0
    parts: tuple[ChunkPart, ...] = ()

    @property
    def heading_text(self) -> str:
        """标题路径的展示形式（A > B > C）。"""
        return heading_path_text(self.heading_path)


@dataclass(frozen=True)
class _Segment:
    """一段连续内容及其字符区间。

    坐标语义：`start/end` **相对当前函数入参文本**（通常就是 Block.content），
    而不是相对某个中间切片。任何「把局部偏移换算成更外层偏移」的动作只能在调用方
    做一次，禁止在被调函数内部先加一次、返回时又加一次。
    """

    text: str
    start: int
    end: int
    context_tokens: int = 0


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
    # True 表示这段文字是上下文（Overlap 前缀），不参与来源范围汇总
    context: bool = False
    context_tokens: int = 0
    # 组成该片段的来源片段（_assemble 汇总时填充），供兜底拆分复用
    parts: tuple[ChunkPart, ...] = ()


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
    drafts = _enforce_budget(drafts, settings)
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
            context_tokens=piece.context_tokens,
            parts=piece.parts,
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

    def overlap_piece(piece: _Piece, prefix: str) -> _Piece:
        """Overlap 前缀：只作为上下文，来源范围仍指向本 Chunk 的真实内容。

        前缀文本来自上一个 Chunk，不是本片段的原始内容，因此行号退化为「当前片段
        起点」、页码置 None（不把当前正文的页码伪造成前缀的页码）。
        """
        return replace(
            piece,
            text=prefix,
            line_start=piece.line_start,
            line_end=piece.line_start,
            page_start=None,
            page_end=None,
            context=True,
            context_tokens=count_tokens(prefix),
            parts=(),
        )

    for piece in pieces:
        tokens = count_tokens(piece.text)
        if piece.atomic:
            flush()
            assembled.append(piece)
            previous_text = None
            continue
        if tokens > config.max_chunk_tokens:
            flush()
            previous_part: _Piece | None = None
            for segment in _split_text(piece.text, config):
                part = replace(
                    piece,
                    text=segment.text,
                    line_start=_line_at(piece, segment.start),
                    line_end=_line_at(piece, segment.end),
                    parts=(),
                )
                if previous_part is not None:
                    prefix = _overlap_prefix(previous_part.text, config)
                    if (
                        prefix
                        and count_tokens(prefix) + count_tokens(segment.text)
                        <= config.max_chunk_tokens
                    ):
                        part = _assemble([overlap_piece(part, prefix), part])
                assembled.append(part)
                previous_part = part
            previous_text = previous_part.text if previous_part is not None else None
            continue
        if buffer and buffer_tokens + tokens > config.target_chunk_tokens:
            flush()
            prefix = _overlap_prefix(previous_text, config) if previous_text else ""
            if prefix and count_tokens(prefix) + tokens <= config.max_chunk_tokens:
                buffer.append(overlap_piece(piece, prefix))
                buffer_tokens += count_tokens(prefix)
        buffer.append(piece)
        buffer_tokens += tokens
    flush()
    return assembled


def _line_at(piece: _Piece, offset: int) -> int:
    """把片段内的字符偏移换算成行号（用于拆分后的精确来源定位）。"""
    if offset <= 0:
        return piece.line_start
    lines = piece.text.count("\n", 0, min(offset, len(piece.text)))
    return min(piece.line_start + lines, piece.line_end)


def _assemble(parts: list[_Piece]) -> _Piece:
    """把同一 Chunk 内的片段拼成一段正文，并汇总来源位置。

    来源行号与页码只由正文片段决定：Overlap 前缀、重复表头属于上下文，既不能把
    `line_start` 拉回上一块，也不能把自己的页码混进本 Chunk 的页码范围。
    """
    texts = [part.text for part in parts if part.text.strip()]
    joined = _join_texts(texts)
    body = [part for part in parts if part.text.strip() and not part.context]
    source = body or parts
    pages = [p for part in body for p in (part.page_start, part.page_end) if p is not None]
    context_tokens = sum(part.context_tokens for part in parts)
    if not context_tokens:
        context_tokens = sum(
            count_tokens(part.text) for part in parts if part.context and part.text.strip()
        )
    return _Piece(
        text=joined,
        line_start=min(part.line_start for part in source),
        line_end=max(part.line_end for part in source),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        block_type=_dominant_type(parts),
        context_tokens=context_tokens,
        parts=tuple(
            chunk_part
            for part in parts
            if part.text.strip()
            for chunk_part in _piece_parts(part)
        ),
    )


def _piece_parts(piece: _Piece) -> tuple[ChunkPart, ...]:
    """展开片段的来源片段：已汇总过的直接用其 parts，否则把自身视为一个来源片段。

    页码随片段一起下传，兜底拆分才能按「真实正文片段」重新计算页码范围。
    """
    if piece.parts:
        return piece.parts
    return (ChunkPart(
        text=piece.text,
        line_start=piece.line_start,
        line_end=piece.line_end,
        page_start=piece.page_start,
        page_end=piece.page_end,
        context=piece.context,
    ),)


def _join_texts(texts: list[str]) -> str:
    """列表项之间用单换行，其余用空行分隔，保持可读性。"""
    if not texts:
        return ""
    if all(text.lstrip().startswith(("- ", "* ", "+ ")) for text in texts):
        return "\n".join(texts)
    return "\n\n".join(texts)


def _dominant_type(parts: list[_Piece]) -> str:
    types = {part.block_type for part in parts if part.text.strip() and not part.context}
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
# 统一上限兜底
# ---------------------------------------------------------------------------

def _enforce_budget(drafts: list[ChunkDraft], config: ChunkConfig) -> list[ChunkDraft]:
    """兜底拆分任何超过 max 的 Chunk（覆盖 Overlap、短尾合并与所有特殊路径）。

    拆分基于 Chunk 自带的来源片段（`ChunkPart`），而不是把最终正文当成原文重新推算
    行号：上下文片段（Overlap 前缀、重复表头）保留 `context` 标记，只累计
    `context_tokens`，不冒充本 Chunk 的原始内容；正文片段的行号由「片段起点 + 片段内
    换行数」换算，始终指向真实原文。没有片段信息的历史 draft 退化为「整块即一个来源
    片段」，行为与旧实现一致。
    """
    result: list[ChunkDraft] = []
    for draft in drafts:
        if count_tokens(draft.content) <= config.max_chunk_tokens:
            result.append(draft)
            continue
        result.extend(_split_draft(draft, config))
    return result


def _split_draft(draft: ChunkDraft, config: ChunkConfig) -> list[ChunkDraft]:
    """按来源片段拆分超限 Chunk，保留行号、页码与上下文元数据。

    页码随片段一起传递：一个 Chunk 跨页合并后，拆分出的每个片段仍带着自己那一段
    来源的页码，最终由 `_draft_from_parts` 按「本组真实正文」重新求页码范围，不再
    把原 Chunk 的整段页码继承给每个子 Chunk。
    """
    parts = draft.parts or (
        ChunkPart(
            text=draft.content,
            line_start=draft.line_start,
            line_end=draft.line_end,
            page_start=draft.page_start,
            page_end=draft.page_end,
        ),
    )
    segments: list[ChunkPart] = []
    for part in parts:
        for segment in _split_text(part.text, config):
            segments.append(ChunkPart(
                text=segment.text,
                line_start=_line_in_part(part, segment.start),
                line_end=_line_in_part(part, segment.end),
                page_start=part.page_start,
                page_end=part.page_end,
                context=part.context,
            ))
    return [_draft_from_parts(draft, group) for group in _pack_parts(segments, config)]


def _line_in_part(part: ChunkPart, offset: int) -> int:
    """把片段内的字符偏移换算成原文行号（不越出片段自身的行范围）。"""
    if offset <= 0:
        return part.line_start
    lines = part.text.count("\n", 0, min(offset, len(part.text)))
    return min(part.line_start + lines, part.line_end)


def _pack_parts(parts: list[ChunkPart], config: ChunkConfig) -> list[list[ChunkPart]]:
    """把来源片段重新装箱成不超过 max 的组（保持顺序，不丢正文）。

    关键约束：**上下文片段不能独立成组**。Overlap 前缀与重复表头是上一块内容的复制，
    单独成组会产出一个只有重复文本、没有任何真实正文的 Chunk。因此：

    * 上下文只有在能与紧随其后的真实正文放进同一组时才保留；
    * 放不下时优先舍弃上下文（正文完整性与预算优先级更高）；
    * 一个只含上下文的组永不输出，全上下文的输入返回空列表（不产生空 Chunk）。
    """
    groups: list[list[ChunkPart]] = []
    current: list[ChunkPart] = []
    for part in parts:
        if not part.text.strip():
            continue
        candidate = _join_texts([item.text for item in [*current, part]])
        if current and count_tokens(candidate) > config.max_chunk_tokens:
            if any(not item.context for item in current):
                groups.append(current)
                current = [part]
            elif part.context:
                # 纯上下文组放不下下一段：舍弃上下文，等真实正文重新开组
                current = []
            else:
                # 上下文挤不进正文所在组：舍弃上下文，只保留正文
                current = [part]
            continue
        current.append(part)
    if current and any(not item.context for item in current):
        groups.append(current)
    return groups


def _draft_from_parts(draft: ChunkDraft, parts: list[ChunkPart]) -> ChunkDraft:
    """由一组来源片段生成新 ChunkDraft。

    行号、页码都只由**真实正文片段**汇总：上下文片段的页码不参与 min/max，缺失页码
    时保持 None（不继承原 Chunk 的页码范围，也不做推测）。`context_tokens` 只累计
    实际保留在组里的上下文。
    """
    texts = [part.text for part in parts if part.text.strip()]
    body = [part for part in parts if part.text.strip() and not part.context]
    source = body or parts
    pages = [p for part in body for p in (part.page_start, part.page_end) if p is not None]
    content = _join_texts(texts)
    return replace(
        draft,
        content=content,
        line_start=min(part.line_start for part in source),
        line_end=max(part.line_end for part in source),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        token_count=count_tokens(content),
        context_tokens=sum(
            count_tokens(part.text) for part in parts if part.context and part.text.strip()
        ),
        parts=tuple(parts),
    )


# ---------------------------------------------------------------------------
# 超长文本的递归拆分
# ---------------------------------------------------------------------------

def _split_text(
    text: str, config: ChunkConfig, limit: int | None = None,
) -> list[_Segment]:
    """把超长文本按「段落 → 行 → 句子 → 标点 → 字符」递归拆开，返回带偏移的片段。"""
    max_tokens = limit if limit is not None else config.max_chunk_tokens
    if count_tokens(text) <= max_tokens:
        return [_Segment(text, 0, len(text))]
    for separator in _SEPARATOR_LEVELS:
        spans = _separator_spans(text, separator)
        if len(spans) <= 1:
            continue
        pieces: list[_Segment] = []
        for start, end in spans:
            part = text[start:end]
            if not part.strip():
                continue
            if count_tokens(part) > max_tokens:
                for segment in _split_text(part, config, limit=max_tokens):
                    pieces.append(
                        _Segment(segment.text, start + segment.start, start + segment.end)
                    )
            else:
                pieces.append(_Segment(part, start, end))
        return _pack_segments(pieces, config, max_tokens)
    return _char_windows(text, max_tokens)


def _separator_spans(text: str, pattern: str) -> list[tuple[int, int]]:
    """按分隔符切分文本，返回每段在原串中的 [start, end) 区间（分隔符被丢弃）。"""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in re.finditer(pattern, text):
        start, end = match.span()
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def _pack_segments(
    segments: list[_Segment], config: ChunkConfig, max_tokens: int,
) -> list[_Segment]:
    """把已拆开的片段贪心合并到目标长度（不跨结构边界硬凑）。"""
    target = min(config.target_chunk_tokens, max_tokens)
    packed: list[_Segment] = []
    buffer: list[_Segment] = []
    buffer_tokens = 0
    for segment in segments:
        tokens = count_tokens(segment.text)
        if buffer and buffer_tokens + tokens > target:
            packed.append(_merge_segments(buffer))
            buffer = []
            buffer_tokens = 0
        buffer.append(segment)
        buffer_tokens += tokens
    if buffer:
        packed.append(_merge_segments(buffer))
    return [item for item in packed if item.text.strip()]


def _merge_segments(segments: list[_Segment]) -> _Segment:
    """合并相邻片段：文本按可读性拼接，来源范围取首尾。"""
    joined = _join_texts([segment.text for segment in segments])
    leading = len(joined) - len(joined.lstrip())
    context_tokens = sum(segment.context_tokens for segment in segments)
    return _Segment(
        text=joined.strip(),
        start=segments[0].start + leading,
        end=segments[-1].end,
        context_tokens=context_tokens,
    )


def _char_windows(text: str, max_tokens: int) -> list[_Segment]:
    """按 token 预算硬切（二分找最长可行前缀），保证绝不超上限。"""
    limit = max(1, max_tokens)
    windows: list[_Segment] = []
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
        if text[start:low].strip():
            windows.append(_Segment(text[start:low], start, low))
        start = low
    return windows


# ---------------------------------------------------------------------------
# 特殊结构：代码块 / 表格
# ---------------------------------------------------------------------------

def _atomic_pieces(block: Block, config: ChunkConfig) -> list[_Piece]:
    """代码块与表格的专用拆分（保持结构、必要时复制表头）。"""
    text = block.content
    if count_tokens(text) <= config.max_chunk_tokens:
        return [_piece_of(block, _Segment(text, 0, len(text)))]
    if block.block_type == BLOCK_TABLE:
        segments = _split_table(text, config)
    elif block.block_type == BLOCK_CODE:
        segments = _split_code(text, config)
    else:  # pragma: no cover - ATOMIC_BLOCK_TYPES 之外的块不会走到这里
        segments = _split_text(text, config)
    return [_piece_of(block, segment) for segment in segments]


def _piece_of(block: Block, segment: _Segment) -> _Piece:
    """把 Block 内的片段转成 Piece，并换算准确的行号范围。"""
    content = block.content
    line_start = block.line_start + content.count("\n", 0, min(segment.start, len(content)))
    line_end = block.line_start + content.count(
        "\n", 0, min(max(segment.start, segment.end), len(content))
    )
    return _Piece(
        text=segment.text,
        line_start=min(line_start, block.line_end),
        line_end=min(line_end, block.line_end),
        page_start=block.page_start,
        page_end=block.page_end,
        block_type=block.block_type,
        atomic=True,
        context_tokens=segment.context_tokens,
    )


def _lines_with_offsets(text: str) -> list[tuple[str, int]]:
    """返回 (行文本, 行首在 text 中的偏移)。"""
    result: list[tuple[str, int]] = []
    offset = 0
    for line in text.split("\n"):
        result.append((line, offset))
        offset += len(line) + 1
    return result


def _split_table(text: str, config: ChunkConfig) -> list[_Segment]:
    """按行分组拆表格，每组复制表头；单行/单元格超预算时继续按单元格边界拆。

    兜底策略：
    * 表头自身超预算 → 整表退化为普通文本拆分（不再复制表头），内容不丢失；
    * 单个单元格超预算 → 递归按句子/字符拆，单元格内容完整保留。
    """
    lines = _lines_with_offsets(text)
    non_empty = [(line, offset) for line, offset in lines if line.strip()]
    if len(non_empty) <= 1:
        return _split_text(text, config)

    header_lines = [non_empty[0]]
    body = non_empty[1:]
    if body and _TABLE_SEPARATOR.match(body[0][0]):
        header_lines.append(body[0])
        body = body[1:]
    header_text = "\n".join(line for line, _ in header_lines)
    header_tokens = count_tokens(header_text)
    if not body:
        return [
            _Segment(
                header_text, non_empty[0][1], non_empty[-1][1] + len(non_empty[-1][0]),
            )
        ]
    budget = config.max_chunk_tokens - header_tokens
    if header_tokens >= config.max_chunk_tokens or budget < config.min_chunk_tokens:
        # 表头本身超限（或只剩不足 min 的预算）：复制表头已无意义，
        # 退化为普通文本拆分，保证不超上限且内容不丢失。
        return _split_text(text, config)

    groups: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    current_tokens = header_tokens
    for line, offset in body:
        row_tokens = count_tokens(line)
        if row_tokens > budget:
            if current:
                groups.append(current)
                current, current_tokens = [], header_tokens
            # 单行超预算：按单元格边界拆，每段仍带表头
            for part in _split_row(line, budget, config):
                if count_tokens(part.text) > budget:
                    # 续片段补竖线等操作可能略微超出，按剩余预算再拆一次
                    for sub in _split_text(part.text, config, limit=budget):
                        groups.append([(sub.text, offset + part.start + sub.start)])
                    continue
                groups.append([(part.text, offset + part.start)])
            continue
        if current and current_tokens + row_tokens > config.max_chunk_tokens:
            groups.append(current)
            current, current_tokens = [], header_tokens
        current.append((line, offset))
        current_tokens += row_tokens
    if current:
        groups.append(current)

    segments: list[_Segment] = []
    for group in groups:
        rows = "\n".join(line for line, _ in group)
        start = group[0][1]
        end = group[-1][1] + len(group[-1][0])
        segments.append(_Segment(
            text=header_text + "\n" + rows,
            start=start,
            end=end,
            context_tokens=header_tokens,
        ))
    return segments


def _cell_spans(line: str) -> list[tuple[int, int]]:
    """按竖线切出单元格片段（含结尾竖线，返回行内偏移）。

    **空单元格同样保留**：`| a |  | b |` 得到 3 个单元格（`a`、空、`b`），
    依次拼接正好还原原始行，列数与列序都不会因为拆分而改变。行首竖线是行边框，
    并入第一个单元格，避免拼回时丢字符。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"\|", line):
        position = match.start()
        if position == 0:
            continue
        spans.append((start, position + 1))
        start = position + 1
    if start < len(line):
        spans.append((start, len(line)))
    return spans


def _split_row(line: str, budget: int, config: ChunkConfig) -> list[_Segment]:
    """把单行按单元格边界拆成不超过 budget 的片段；单元格超限时递归拆。"""
    if count_tokens(line) <= budget:
        return [_Segment(line, 0, len(line))]
    spans = _cell_spans(line)
    if len(spans) <= 1:
        return _split_text(line, config, limit=budget)

    parts: list[_Segment] = []
    current: list[tuple[int, int]] = []
    current_tokens = 0
    for start, end in spans:
        cell = line[start:end]
        cell_tokens = count_tokens(cell)
        if cell_tokens > budget:
            if current:
                parts.append(_row_fragment(line, current))
                current, current_tokens = [], 0
            for segment in _split_text(cell, config, limit=budget):
                parts.append(
                    _Segment(segment.text, start + segment.start, start + segment.end)
                )
            continue
        if current and current_tokens + cell_tokens > budget:
            parts.append(_row_fragment(line, current))
            current, current_tokens = [], 0
        current.append((start, end))
        current_tokens += cell_tokens
    if current:
        parts.append(_row_fragment(line, current))
    return [part for part in parts if part.text.strip()]


def _row_fragment(line: str, spans: list[tuple[int, int]]) -> _Segment:
    """把若干相邻单元格拼成一个行片段（片段文本是原始行的连续子串）。

    续片段（不从行首开始）连带前一个单元格的结尾竖线，因此列结构保持完整，
    `start/end` 也仍是准确的行内偏移——不再靠人工补 `| ` 造成偏移漂移。
    """
    start, end = spans[0][0], spans[-1][1]
    if start > 0 and line[start - 1] == "|":
        start -= 1
    return _Segment(line[start:end], start, end)


def _split_code(text: str, config: ChunkConfig) -> list[_Segment]:
    """代码块拆分：顶层定义边界 → 行 → 字符窗口，保留每段的行号。"""
    lines = _lines_with_offsets(text)
    boundaries = [
        index for index, (line, _) in enumerate(lines)
        if line.strip() and line[0] not in {" ", "\t"} and _CODE_BOUNDARY.match(line)
    ]
    if boundaries and boundaries[0] != 0:
        boundaries.insert(0, 0)
    if len(boundaries) > 1:
        chunks: list[_Segment] = []
        for position, start in enumerate(boundaries):
            end = boundaries[position + 1] if position + 1 < len(boundaries) else len(lines)
            segment = _line_range_segment(lines, start, end)
            if segment is None:
                continue
            if count_tokens(segment.text) > config.max_chunk_tokens:
                # 子拆分返回的是相对 segment.text 的局部偏移，这里统一 rebase 一次。
                chunks.extend(
                    _Segment(sub.text, segment.start + sub.start, segment.start + sub.end)
                    for sub in _split_code_by_lines(segment.text, config)
                )
            else:
                chunks.append(segment)
        return _pack_segments(chunks, config, config.max_chunk_tokens)
    return _split_code_by_lines(text, config)


def _line_range_segment(
    lines: list[tuple[str, int]], start: int, end: int,
) -> _Segment | None:
    """把 [start, end) 行区间转成片段（去掉首尾空行，保留内部缩进）。"""
    while start < end and not lines[start][0].strip():
        start += 1
    while end > start and not lines[end - 1][0].strip():
        end -= 1
    if start >= end:
        return None
    text = "\n".join(line for line, _ in lines[start:end])
    begin = lines[start][1]
    return _Segment(text, begin, begin + len(text))


def _split_code_by_lines(text: str, config: ChunkConfig) -> list[_Segment]:
    """按行累积拆分代码；单行仍超限时按字符窗口兜底。

    返回的 `_Segment.start/end` 一律是**相对入参 `text` 的局部偏移**。需要相对
    Block 原文的偏移时由调用方（`_split_code` 的函数边界分支）统一 rebase 一次，
    本函数内部不再做任何 base 换算。
    """
    lines = _lines_with_offsets(text)
    chunks: list[_Segment] = []
    buffer: list[tuple[str, int]] = []
    buffer_tokens = 0
    for line, offset in lines:
        line_tokens = count_tokens(line)
        if line_tokens > config.max_chunk_tokens:
            if buffer:
                chunks.append(_buffer_segment(buffer))
                buffer, buffer_tokens = [], 0
            for segment in _char_windows(line, config.max_chunk_tokens):
                chunks.append(_Segment(
                    segment.text, offset + segment.start, offset + segment.end,
                ))
            continue
        if buffer and buffer_tokens + line_tokens > config.max_chunk_tokens:
            chunks.append(_buffer_segment(buffer))
            buffer, buffer_tokens = [], 0
        buffer.append((line, offset))
        buffer_tokens += line_tokens
    if buffer:
        chunks.append(_buffer_segment(buffer))
    return [chunk for chunk in chunks if chunk.text.strip()]


def _buffer_segment(buffer: list[tuple[str, int]]) -> _Segment:
    text = "\n".join(line for line, _ in buffer)
    start = buffer[0][1]
    return _Segment(text, start, start + len(text))


# ---------------------------------------------------------------------------
# 遗留接口
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[dict[str, Any]]:
    """[遗留] 按字符窗口切分纯文本。

    知识摄取链路已改用 chunk_blocks（结构感知递归切分）。本函数保留给尚未迁移的
    调用方，行为与历史版本一致：按行累积到 chunk_size 字符。
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
