"""V61.2 Chunker 边界正确性回归测试。

覆盖三个已发现的边界缺陷：

1. _split_code_by_lines 对 base 偏移重复累加，导致后续函数中的超长单行定位漂移；
2. _enforce_budget 把最终正文当原文重新切分，丢失来源映射并清零 context_tokens；
3. _cell_spans 过滤空单元格，超长表格行拆分后列结构改变。
"""
from __future__ import annotations

from aiive.knowledge.blocks import (
    BLOCK_CODE,
    BLOCK_PARAGRAPH,
    BLOCK_TABLE,
    parse_document_blocks,
)
from aiive.knowledge.chunker import (
    ChunkConfig,
    ChunkDraft,
    ChunkPart,
    _cell_spans,
    _enforce_budget,
    chunk_blocks,
)
from aiive.knowledge.tokenization import count_tokens

FENCE = chr(96) * 3
MAX_TOKENS = 700


def _letters(index: int) -> str:
    """把序号转成纯字母串（避免在测试文本里引入数字干扰计数）。"""
    chars: list[str] = []
    for _ in range(3):
        chars.append(chr(ord("a") + index % 26))
        index //= 26
    return "".join(reversed(chars))


def _blocks(text: str, name: str = "doc.md"):
    return parse_document_blocks(text.encode("utf-8"), name)


def _chunks(text: str, name: str = "doc.md", config: ChunkConfig | None = None):
    return chunk_blocks(_blocks(text, name), config)


def _assert_budget(chunks, limit: int = MAX_TOKENS) -> None:
    for chunk in chunks:
        assert count_tokens(chunk.content) <= limit, (
            f"chunk {chunk.chunk_index} 超限：{count_tokens(chunk.content)} > {limit}"
        )


class TestCodeSourceOffsets:
    LONG_LINE = "    payload = " + "0123456789" * 320
    # 让第二个函数在代码块中的起始偏移足够大：base 一旦被重复累加，
    # 偏移就会跨过超长行落到后面的行上，测试才能捕获这类回归。
    PADDING = "\n".join(
        f"# padding {_letters(index)} " + "x" * 40 for index in range(60)
    )

    CODE = (
        "def first(value):\n"
        "    return value + one\n"
        + PADDING
        + "\n"
        "\n"
        "def second(value):\n"
        + LONG_LINE
        + "\n"
        "    return value\n"
        "\n"
        "def third(value):\n"
        "    return value * two\n"
    )

    def _setup(self):
        text = "# 代码\n\n" + FENCE + "python\n" + self.CODE + "\n" + FENCE + "\n"
        blocks = _blocks(text, "code.md")
        code_block = [block for block in blocks if block.block_type == BLOCK_CODE][0]
        lines = code_block.content.split("\n")
        return text, code_block, lines, _chunks(text, "code.md")

    @staticmethod
    def _source_line(lines: list[str], first_content_line: str) -> int:
        """返回 Chunk 首行内容在代码块中对应的行下标。"""
        first = first_content_line.strip()
        assert first
        matches = [index for index, line in enumerate(lines) if first in line]
        assert matches, first[:60]
        return matches[0]

    def test_long_line_in_second_function_maps_to_real_line(self):
        _, code_block, lines, chunks = self._setup()
        long_index = lines.index(self.LONG_LINE)
        expected = code_block.line_start + long_index
        payload_chunks = [chunk for chunk in chunks if "payload" in chunk.content]
        long_chunks = [
            chunk for chunk in chunks
            if self._source_line(lines, chunk.content.split("\n")[0]) == long_index
        ]

        assert len(payload_chunks) == 1
        assert payload_chunks[0].line_start == expected
        assert payload_chunks[0].line_end == expected
        assert len(long_chunks) >= 2  # 单行超预算，被字符窗口继续拆开
        for chunk in long_chunks:
            assert chunk.line_start == expected
        assert sum(
            sum(char.isdigit() for char in chunk.content) for chunk in chunks
        ) == 3200

    def test_every_chunk_starts_on_its_source_line(self):
        _, code_block, lines, chunks = self._setup()

        assert len(chunks) > 1
        for chunk in chunks:
            first = chunk.content.split("\n")[0]
            index = self._source_line(lines, first)
            assert chunk.line_start == code_block.line_start + index
            assert chunk.line_end <= code_block.line_end
            assert chunk.line_end >= chunk.line_start

    def test_line_ranges_after_long_line_are_not_shifted(self):
        _, code_block, lines, chunks = self._setup()
        expected = code_block.line_start + lines.index("    return value * two")
        tail = [chunk for chunk in chunks if "return value * two" in chunk.content]

        assert tail
        assert tail[0].line_end == expected
        assert tail[0].line_start <= expected

    def test_single_function_long_line_keeps_local_offsets(self):
        code = "def only(value):\n" + self.LONG_LINE + "\n    return value\n"
        text = "# 代码\n\n" + FENCE + "python\n" + code + "\n" + FENCE + "\n"
        code_block = [
            block for block in _blocks(text, "code.md") if block.block_type == BLOCK_CODE
        ][0]
        lines = code_block.content.split("\n")
        expected = code_block.line_start + lines.index(self.LONG_LINE)
        chunks = _chunks(text, "code.md")

        assert len(chunks) >= 2
        _assert_budget(chunks)
        long_chunks = [
            chunk for chunk in chunks
            if self._source_line(lines, chunk.content.split("\n")[0]) == lines.index(self.LONG_LINE)
        ]
        assert len(long_chunks) >= 2
        for chunk in long_chunks:
            assert chunk.line_start == expected
            assert chunk.line_end >= expected


class TestEnforceBudgetProvenance:
    UNIT = "正文内容说明。"

    def _draft(self, content, line_start, line_end, parts, context_tokens=0):
        return ChunkDraft(
            content=content,
            heading_path=("标题",),
            block_type=BLOCK_PARAGRAPH,
            section_id="s-test",
            line_start=line_start,
            line_end=line_end,
            page_start=None,
            page_end=None,
            chunk_index=0,
            token_count=count_tokens(content),
            context_tokens=context_tokens,
            parts=parts,
        )

    def test_overlap_context_is_not_mapped_to_source(self):
        overlap = "上一块尾部句子。"
        body = "\n".join(
            f"第{index}行：" + self.UNIT * 20 for index in range(12)
        )
        draft = self._draft(
            overlap + "\n\n" + body,
            line_start=20,
            line_end=31,
            parts=(
                ChunkPart(overlap, 10, 10, context=True),
                ChunkPart(body, 20, 31),
            ),
            context_tokens=count_tokens(overlap),
        )
        parts = _enforce_budget([draft], ChunkConfig())

        assert len(parts) > 1
        _assert_budget(parts)
        for part in parts:
            assert 20 <= part.line_start <= part.line_end <= 31
        assert parts[0].context_tokens == count_tokens(overlap)
        assert all(part.context_tokens == 0 for part in parts[1:])
        assert sum(part.content.count(self.UNIT) for part in parts) == 240

    def test_table_header_stays_context_in_fallback(self):
        header = "| 名称 | 值 |\n|---|---|"
        rows = "\n".join(f"| 行{index} | " + "值" * 120 + " |" for index in range(10))
        draft = self._draft(
            header + "\n" + rows,
            line_start=5,
            line_end=16,
            parts=(
                ChunkPart(header, 5, 6, context=True),
                ChunkPart(rows, 7, 16),
            ),
            context_tokens=count_tokens(header),
        )
        parts = _enforce_budget([draft], ChunkConfig())

        assert len(parts) > 1
        _assert_budget(parts)
        assert parts[0].context_tokens == count_tokens(header)
        for part in parts:
            assert part.line_start >= 7
            assert part.line_end <= 16
        assert sum(part.content.count("行") for part in parts) == 10

    def test_fallback_without_parts_keeps_legacy_behaviour(self):
        body = self.UNIT * 400
        draft = self._draft(body, line_start=3, line_end=9, parts=())
        parts = _enforce_budget([draft], ChunkConfig())

        assert len(parts) > 1
        _assert_budget(parts)
        assert all(part.context_tokens == 0 for part in parts)
        for part in parts:
            assert 3 <= part.line_start <= part.line_end <= 9
        assert sum(part.content.count(self.UNIT) for part in parts) == 400

    def test_budget_pass_through_keeps_draft_untouched(self):
        draft = self._draft(self.UNIT * 10, 1, 1, ())
        assert _enforce_budget([draft], ChunkConfig()) == [draft]


class TestEmptyTableCells:
    def test_cell_spans_cover_line_without_gaps(self):
        for line in (
            "| a |  | b |",
            "a | b",
            "| a | b |",
            "||",
            "| |",
            "| a |  |  | b |",
            "|  |",
        ):
            spans = _cell_spans(line)
            assert spans, line
            assert "".join(line[start:end] for start, end in spans) == line

    def test_oversized_row_keeps_consecutive_empty_cells(self):
        huge = "值" * 900
        text = (
            "| 名称 | 空1 | 空2 | 值 |\n"
            "|---|---|---|---|\n"
            f"| 张三 |  |  | {huge} |\n"
        )
        chunks = _chunks(text)

        assert len(chunks) > 1
        _assert_budget(chunks)
        joined = "\n".join(chunk.content for chunk in chunks)
        assert "| 张三 |  |  |" in joined
        assert joined.count("值") >= 900

    def test_oversized_row_keeps_leading_and_trailing_empty_cells(self):
        huge = "值" * 900
        text = (
            "| 空1 | 值 | 空2 |\n"
            "|---|---|---|\n"
            f"|  | {huge} |  |\n"
        )
        chunks = _chunks(text)

        _assert_budget(chunks)
        joined = "\n".join(chunk.content for chunk in chunks)
        assert "|  |" in joined
        assert chunks[-1].content.rstrip().endswith("|")

    def test_oversized_cell_split_keeps_other_columns(self):
        huge = "超长单元格内容。" * 200
        text = (
            "| A | B | C |\n"
            "|---|---|---|\n"
            f"| 首 | {huge} | 尾 |\n"
        )
        chunks = _chunks(text)

        _assert_budget(chunks)
        joined = "\n".join(chunk.content for chunk in chunks)
        assert "| 首 |" in joined
        assert "| 尾 |" in joined
        assert joined.count("超长单元格内容。") == 200

    def test_mixed_document_respects_budget(self):
        huge = "值" * 900
        text = (
            "# 文档\n\n"
            + "段落内容说明。" * 200
            + "\n\n| A | B |\n|---|---|\n"
            f"|  | {huge} |\n\n"
            + FENCE
            + "python\n"
            + "def f(x):\n    return x\n"
            + FENCE
            + "\n"
        )
        blocks = _blocks(text)
        assert any(block.block_type == BLOCK_TABLE for block in blocks)
        _assert_budget(chunk_blocks(blocks))
