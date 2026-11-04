"""V61.3 兜底拆分正确性回归测试。

覆盖两个边界缺陷：

1. `_pack_parts` 会把 Overlap 上下文单独装箱成一个只有重复文本、没有真实正文的
   Chunk（例如 60 token 上下文 + 700 token 正文 + 700 预算）；
2. `ChunkPart` 不携带页码，跨页 PDF 进入 `_enforce_budget` 后每个子 Chunk 都继承
   原 Chunk 的整个页码范围，而不是自己实际覆盖的页码。

两条不变量贯穿全部用例：每个输出 Chunk 都包含真实正文，且
`count_tokens(chunk.content) <= max_chunk_tokens`。
"""
from __future__ import annotations

from aiive.knowledge.blocks import BLOCK_PARAGRAPH, parse_document_blocks
from aiive.knowledge.chunker import (
    ChunkConfig,
    ChunkDraft,
    ChunkPart,
    _enforce_budget,
    _pack_parts,
    chunk_blocks,
)
from aiive.knowledge.tokenization import count_tokens

MAX_TOKENS = 700
BODY_MARK = "正"
CONTEXT_MARK = "上"


def _draft(
    content: str,
    parts: tuple[ChunkPart, ...],
    *,
    line_start: int,
    line_end: int,
    page_start: int | None = None,
    page_end: int | None = None,
) -> ChunkDraft:
    return ChunkDraft(
        content=content,
        heading_path=("标题",),
        block_type=BLOCK_PARAGRAPH,
        section_id="s-fallback",
        line_start=line_start,
        line_end=line_end,
        page_start=page_start,
        page_end=page_end,
        chunk_index=0,
        token_count=count_tokens(content),
        context_tokens=sum(
            count_tokens(part.text) for part in parts if part.context
        ),
        parts=parts,
    )


def _body_lines(count: int, width: int = 70) -> list[str]:
    return [BODY_MARK * width for _ in range(count)]


def _assert_invariants(chunks: list[ChunkDraft], limit: int = MAX_TOKENS) -> None:
    """每个 Chunk 都必须有真实正文、非空、且不超预算。"""
    assert chunks
    for chunk in chunks:
        assert chunk.content.strip(), "出现空 Chunk"
        assert BODY_MARK in chunk.content, f"纯上下文 Chunk：{chunk.content[:40]!r}"
        assert count_tokens(chunk.content) <= limit, (
            f"chunk {chunk.chunk_index} 超限：{count_tokens(chunk.content)} > {limit}"
        )


def _merged_draft(blocks) -> ChunkDraft:
    """按 `_chunk_section` 合并多个 Block 时的形态构造 draft（正文 + 来源片段）。"""
    parts = tuple(
        ChunkPart(
            text=block.content,
            line_start=block.line_start,
            line_end=block.line_end,
            page_start=block.page_start,
            page_end=block.page_end,
        )
        for block in blocks
    )
    content = "\n\n".join(part.text for part in parts)
    return _draft(
        content,
        parts,
        line_start=min(part.line_start for part in parts),
        line_end=max(part.line_end for part in parts),
        page_start=blocks[0].page_start,
        page_end=blocks[-1].page_end,
    )


def _pdf_blocks(pages: list[str]):
    from tests.unit.backend.test_structure_chunking import _build_pdf

    return parse_document_blocks(_build_pdf(pages), "sample.pdf")


class TestNoContextOnlyChunk:
    def test_overlap_plus_body_at_budget_drops_context(self):
        """60 token Overlap + 700 token 正文 + 700 预算：只保留正文。"""
        context = CONTEXT_MARK * 60
        body = "\n".join(_body_lines(10))
        draft = _draft(
            context + "\n\n" + body,
            (
                ChunkPart(context, 0, 0, context=True),
                ChunkPart(body, 1, 10),
            ),
            line_start=1,
            line_end=10,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        assert len(chunks) == 1
        assert chunks[0].content == body
        assert chunks[0].context_tokens == 0
        assert (chunks[0].line_start, chunks[0].line_end) == (1, 10)

    def test_overlap_plus_oversized_body_resplits_without_losing_content(self):
        """正文超长需要再次拆分时，上下文只在放得下时保留，正文一字不丢。"""
        context = CONTEXT_MARK * 60
        body = "\n".join(_body_lines(20))
        draft = _draft(
            context + "\n\n" + body,
            (
                ChunkPart(context, 0, 0, context=True),
                ChunkPart(body, 1, 20),
            ),
            line_start=1,
            line_end=20,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        assert len(chunks) > 1
        assert sum(chunk.content.count(BODY_MARK) for chunk in chunks) == 1400
        # Overlap 只能出现在第一个子 Chunk，且总量不超过原上下文
        assert chunks[0].context_tokens == count_tokens(context)
        assert all(chunk.context_tokens == 0 for chunk in chunks[1:])
        assert sum(chunk.content.count(CONTEXT_MARK) for chunk in chunks) == 60

    def test_multiple_context_parts_never_form_a_chunk(self):
        """多个连续 context 片段合起来仍放不下正文时，整体舍弃。"""
        first = CONTEXT_MARK * 40
        second = "下" * 40
        body = "\n".join(_body_lines(10))
        draft = _draft(
            first + "\n\n" + second + "\n\n" + body,
            (
                ChunkPart(first, 0, 0, context=True),
                ChunkPart(second, 0, 0, context=True),
                ChunkPart(body, 1, 10),
            ),
            line_start=1,
            line_end=10,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        assert len(chunks) == 1
        assert chunks[0].context_tokens == 0
        assert CONTEXT_MARK not in chunks[0].content
        assert "下" not in chunks[0].content

    def test_multiple_context_parts_are_kept_when_they_fit(self):
        """放得下时正常保留 Overlap 行为（上下文合并计入 context_tokens）。"""
        first = CONTEXT_MARK * 20
        second = "下" * 20
        body = "\n".join(_body_lines(6))
        draft = _draft(
            first + "\n\n" + second + "\n\n" + body,
            (
                ChunkPart(first, 0, 0, context=True),
                ChunkPart(second, 0, 0, context=True),
                ChunkPart(body, 1, 6),
            ),
            line_start=1,
            line_end=6,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        assert len(chunks) == 1
        assert chunks[0].context_tokens == count_tokens(first) + count_tokens(second)
        assert chunks[0].content.startswith(first)

    def test_dropped_context_keeps_body_source_lines(self):
        """上下文被舍弃后，正文来源行号仍然指向真实正文行。"""
        context = CONTEXT_MARK * 60
        body = "\n".join(_body_lines(20))
        draft = _draft(
            context + "\n\n" + body,
            (
                ChunkPart(context, 0, 0, context=True),
                ChunkPart(body, 5, 24),
            ),
            line_start=5,
            line_end=24,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        for chunk in chunks:
            assert 5 <= chunk.line_start <= chunk.line_end <= 24
        # 行号必须单调且首尾闭合：覆盖全部正文行
        assert chunks[0].line_start == 5
        assert chunks[-1].line_end == 24
        assert [chunk.line_start for chunk in chunks] == sorted(
            chunk.line_start for chunk in chunks
        )

    def test_context_only_parts_produce_no_group(self):
        """只有上下文的输入不产生任何组（不生成纯 Overlap Chunk）。"""
        context = CONTEXT_MARK * 60
        groups = _pack_parts(
            [ChunkPart(context, 0, 0, context=True)], ChunkConfig(),
        )
        assert groups == []

    def test_normal_overlap_survives_end_to_end(self):
        """正常路径的 Overlap 行为不变：仍会生成带 context_tokens 的 Chunk。"""
        paragraphs = [
            f"第{index}段：" + "内容说明。" * 40 for index in range(1, 7)
        ]
        text = "# 标题\n\n" + "\n\n".join(paragraphs) + "\n"
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "doc.md"))

        assert len(chunks) > 1
        assert any(chunk.context_tokens > 0 for chunk in chunks)
        for chunk in chunks:
            assert count_tokens(chunk.content) <= MAX_TOKENS
            assert chunk.content.strip()

    def test_real_pipeline_overlap_draft_forced_into_fallback(self):
        """端到端：真实 chunk_blocks 产出的带 Overlap draft，收紧预算后再入兜底。

        收紧前该 Chunk 是「60 token 上下文 + 400 token 正文」，把 max 降到 400 后
        上下文与正文无法共存；兜底必须舍弃上下文，而不是产出纯上下文 Chunk。
        """
        first = "".join("甲" * 19 + "。" for _ in range(20))
        second = "乙" * 400
        text = "# 标题\n\n" + first + "\n\n" + second + "\n"
        drafts = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "doc.md"))

        overlapped = [draft for draft in drafts if draft.context_tokens > 0]
        assert len(overlapped) == 1
        draft = overlapped[0]
        assert draft.context_tokens == 60
        assert any(part.context for part in draft.parts)

        tight = ChunkConfig(
            target_chunk_tokens=300, max_chunk_tokens=400,
            min_chunk_tokens=100, overlap_tokens=60,
        )
        chunks = _enforce_budget([draft], tight)

        assert chunks
        for chunk in chunks:
            assert chunk.content.strip()
            assert "乙" in chunk.content, "出现纯上下文 Chunk"
            assert count_tokens(chunk.content) <= 400
        assert sum(chunk.content.count("乙") for chunk in chunks) == 400
        assert sum(chunk.content.count("甲") for chunk in chunks) == 0
        assert all(chunk.context_tokens == 0 for chunk in chunks)
        assert all((chunk.line_start, chunk.line_end) == (draft.line_start, draft.line_end)
                   for chunk in chunks)


class TestFallbackPageMapping:
    def test_single_page_pdf_chunks_stay_on_original_page(self):
        blocks = _pdf_blocks(["alpha " * 400])
        chunks = chunk_blocks(blocks)

        assert len(chunks) > 1
        for chunk in chunks:
            assert (chunk.page_start, chunk.page_end) == (1, 1)
            assert count_tokens(chunk.content) <= MAX_TOKENS

    def test_single_page_pdf_fallback_split_keeps_page(self):
        blocks = _pdf_blocks(["alpha " * 400])
        draft = _merged_draft(blocks)
        assert count_tokens(draft.content) > MAX_TOKENS

        chunks = _enforce_budget([draft], ChunkConfig())

        assert len(chunks) > 1
        for chunk in chunks:
            assert (chunk.page_start, chunk.page_end) == (1, 1)
            assert count_tokens(chunk.content) <= MAX_TOKENS

    def test_cross_page_pdf_fallback_marks_actual_pages(self):
        blocks = _pdf_blocks(["alpha " * 200, "bravo " * 200])
        assert {block.page_start for block in blocks} == {1, 2}
        draft = _merged_draft(blocks)
        assert count_tokens(draft.content) > MAX_TOKENS

        chunks = _enforce_budget([draft], ChunkConfig())

        assert len(chunks) == 2
        assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
        assert (chunks[1].page_start, chunks[1].page_end) == (2, 2)
        assert "alpha" in chunks[0].content and "bravo" not in chunks[0].content
        assert "bravo" in chunks[1].content and "alpha" not in chunks[1].content
        for chunk in chunks:
            assert count_tokens(chunk.content) <= MAX_TOKENS

    def test_cross_page_chunk_reports_every_page_it_covers(self):
        blocks = _pdf_blocks(["alpha " * 400, "bravo " * 200])
        draft = _merged_draft(blocks)

        chunks = _enforce_budget([draft], ChunkConfig())

        assert len(chunks) == 2
        assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
        # 第二块由第 1 页剩余正文与第 2 页正文组成，必须标记 1..2
        assert (chunks[1].page_start, chunks[1].page_end) == (1, 2)
        assert "alpha" in chunks[1].content and "bravo" in chunks[1].content
        for chunk in chunks:
            assert count_tokens(chunk.content) <= MAX_TOKENS

    def test_context_from_previous_page_is_not_in_page_range(self):
        context = CONTEXT_MARK * 60
        body_first = "\n".join(_body_lines(4, width=100))
        body_second = "\n".join(_body_lines(3, width=100))
        draft = _draft(
            context + "\n\n" + body_first + "\n\n" + body_second,
            (
                ChunkPart(context, 0, 0, page_start=1, page_end=1, context=True),
                ChunkPart(body_first, 5, 8, page_start=2, page_end=2),
                ChunkPart(body_second, 9, 11, page_start=2, page_end=2),
            ),
            line_start=5,
            line_end=11,
            page_start=1,
            page_end=2,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        assert len(chunks) == 2
        assert chunks[0].context_tokens == count_tokens(context)
        for chunk in chunks:
            assert (chunk.page_start, chunk.page_end) == (2, 2)
        assert (chunks[0].line_start, chunks[0].line_end) == (5, 8)
        assert (chunks[1].line_start, chunks[1].line_end) == (9, 11)

    def test_missing_pages_stay_none(self):
        body = "\n".join(_body_lines(20))
        draft = _draft(
            body,
            (ChunkPart(body, 1, 20),),
            line_start=1,
            line_end=20,
            page_start=1,
            page_end=2,
        )
        chunks = _enforce_budget([draft], ChunkConfig())

        _assert_invariants(chunks)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.page_start is None
            assert chunk.page_end is None

    def test_real_pipeline_cross_page_draft_forced_into_fallback(self):
        """端到端：真实 chunk_blocks 把两页合成一个 1..2 的 Chunk，收紧预算后拆分。

        每个子 Chunk 只能标记自己实际覆盖的页码，而不是继承原 Chunk 的 1..2。
        """
        blocks = _pdf_blocks(["alpha " * 100, "bravo " * 100])
        drafts = chunk_blocks(blocks)

        assert len(drafts) == 1
        draft = drafts[0]
        assert (draft.page_start, draft.page_end) == (1, 2)
        assert {part.page_start for part in draft.parts} == {1, 2}

        tight = ChunkConfig(
            target_chunk_tokens=200, max_chunk_tokens=250,
            min_chunk_tokens=100, overlap_tokens=60,
        )
        chunks = _enforce_budget([draft], tight)

        assert len(chunks) == 2
        assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
        assert (chunks[1].page_start, chunks[1].page_end) == (2, 2)
        assert "alpha" in chunks[0].content and "bravo" not in chunks[0].content
        assert "bravo" in chunks[1].content and "alpha" not in chunks[1].content
        for chunk in chunks:
            assert count_tokens(chunk.content) <= 250
            assert chunk.content.strip()


class TestFallbackInvariants:
    """确定性穷举若干片段形状，锁定「行号 / 页码 / context_tokens 三者同源」。"""

    CONFIG = ChunkConfig(
        target_chunk_tokens=200, max_chunk_tokens=300,
        min_chunk_tokens=50, overlap_tokens=40,
    )

    def test_lines_pages_and_context_tokens_are_consistent(self):
        for context_count in (0, 1, 2, 3):
            for body_count in (1, 3, 5):
                for layout in (0, 1):
                    parts: list[ChunkPart] = []
                    for index in range(context_count):
                        parts.append(ChunkPart(
                            CONTEXT_MARK * (10 + index * 10), 0, 0,
                            page_start=1, page_end=1, context=True,
                        ))
                    for index in range(body_count):
                        page = 1 + (index + layout) % 3
                        lines = 2 + (index + layout) % 3
                        text = "\n".join(
                            BODY_MARK * (30 + (index + layout) % 4 * 20)
                            for _ in range(lines)
                        )
                        start = index * 10 + 1
                        parts.append(ChunkPart(
                            text, start, start + lines - 1,
                            page_start=page, page_end=page,
                        ))
                    content = "\n\n".join(part.text for part in parts)
                    pages = [
                        value for part in parts
                        for value in (part.page_start, part.page_end)
                        if value is not None
                    ]
                    draft = _draft(
                        content,
                        tuple(parts),
                        line_start=min(part.line_start for part in parts),
                        line_end=max(part.line_end for part in parts),
                        page_start=min(pages),
                        page_end=max(pages),
                    )
                    if count_tokens(content) <= self.CONFIG.max_chunk_tokens:
                        continue

                    chunks = _enforce_budget([draft], self.CONFIG)
                    assert chunks
                    kept_body = 0
                    for chunk in chunks:
                        body = [
                            part for part in chunk.parts
                            if not part.context and part.text.strip()
                        ]
                        assert body, "纯上下文 Chunk"
                        assert chunk.content.strip()
                        assert count_tokens(chunk.content) <= self.CONFIG.max_chunk_tokens
                        assert chunk.line_start == min(part.line_start for part in body)
                        assert chunk.line_end == max(part.line_end for part in body)
                        body_pages = [
                            value for part in body
                            for value in (part.page_start, part.page_end)
                            if value is not None
                        ]
                        assert chunk.page_start == (
                            min(body_pages) if body_pages else None
                        )
                        assert chunk.page_end == (
                            max(body_pages) if body_pages else None
                        )
                        assert chunk.context_tokens == sum(
                            count_tokens(part.text) for part in chunk.parts
                            if part.context and part.text.strip()
                        )
                        kept_body += sum(
                            part.text.count(BODY_MARK) for part in body
                        )
                    expected = sum(
                        part.text.count(BODY_MARK) for part in parts
                        if not part.context
                    )
                    assert kept_body == expected, "正文丢失或重复生成"
